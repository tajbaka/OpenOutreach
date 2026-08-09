"""Database and task orchestration for bounded profile discovery."""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from crm.models import Lead
from linkedin import conf
from linkedin.actions.search import search_profile
from linkedin.api.client import PlaywrightLinkedinAPI
from linkedin.db.urls import public_id_to_url, url_to_public_id
from linkedin.discovery.config import (
    discovery_day_end,
    discovery_day_bounds,
    discovery_gate_open,
    discovery_limits,
    discovery_local_now,
    next_discovery_day_start,
)
from linkedin.discovery.limits import remaining_today, saved_today
from linkedin.discovery.screening import screen_cards
from linkedin.discovery.sources.base import DiscoveryCard
from linkedin.discovery.sources.people_search import collect_people_search_cards
from linkedin.exceptions import (
    AuthenticationError,
    LinkedInDiscoveryLimitError,
    SkipProfile,
)
from linkedin.icp_outbound import (
    DiscoveryTarget,
    discovery_search_queries,
    load_discovery_targets,
)
from linkedin.models import (
    LinkedInDiscoveryLead,
    LinkedInProfile,
    Task,
)
from linkedin.operators import resolve_operator
from linkedin.suppression import lead_suppression_match

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoverySaveResult:
    created: bool
    daily_limit_reached: bool
    reason: str


def _canonical_public_identifier(value: str | None) -> str:
    return (value or "").strip().strip("/").lower()


def _profile_company(profile: dict) -> str:
    for position in profile.get("positions") or []:
        company = (position.get("company_name") or "").strip()
        if company:
            return company
    return ""


def _suppression_candidate(
    *,
    public_identifier: str,
    linkedin_url: str,
    full_name: str = "",
    company_name: str = "",
) -> SimpleNamespace:
    first_name, _, last_name = full_name.strip().partition(" ")
    return SimpleNamespace(
        first_name=first_name,
        last_name=last_name,
        company_name=company_name,
        email="",
        linkedin_url=linkedin_url,
        public_identifier=public_identifier,
    )


def known_profile_reason(card: DiscoveryCard) -> str:
    """Return a deterministic skip reason, or an empty string."""
    public_identifier = _canonical_public_identifier(card.public_identifier)
    if not public_identifier:
        return "invalid_profile_url"
    linkedin_url = public_id_to_url(public_identifier)
    if Lead.objects.filter(
        Q(public_identifier__iexact=public_identifier)
        | Q(linkedin_url__iexact=linkedin_url),
    ).exists():
        return "existing_crm_lead"
    if LinkedInDiscoveryLead.objects.filter(
        Q(public_identifier__iexact=public_identifier)
        | Q(linkedin_url__iexact=linkedin_url),
    ).exists():
        return "existing_discovery_lead"
    candidate = _suppression_candidate(
        public_identifier=public_identifier,
        linkedin_url=linkedin_url,
        full_name=card.name,
        company_name=card.company_name,
    )
    if lead_suppression_match(candidate):
        return "suppressed"
    return ""


def _full_profile_skip_reason(profile: dict) -> str:
    public_identifier = _canonical_public_identifier(
        profile.get("public_identifier")
        or url_to_public_id(profile.get("url") or ""),
    )
    if not public_identifier:
        return "invalid_profile"
    card = DiscoveryCard(
        public_identifier=public_identifier,
        linkedin_url=public_id_to_url(public_identifier),
        name=(profile.get("full_name") or "").strip(),
        headline=(profile.get("headline") or "").strip(),
        company_name=_profile_company(profile),
    )
    return known_profile_reason(card)


def save_discovery_profile(
    *,
    linkedin_profile: LinkedInProfile,
    operator: str,
    potential_icp: str,
    profile: dict,
    now: datetime | None = None,
) -> DiscoverySaveResult:
    """Atomically enforce the sender limit and insert one discovery profile."""
    now = now or timezone.now()
    public_identifier = _canonical_public_identifier(
        profile.get("public_identifier")
        or url_to_public_id(profile.get("url") or ""),
    )
    if not public_identifier:
        return DiscoverySaveResult(False, False, "invalid_profile")
    linkedin_url = public_id_to_url(public_identifier)

    skip_reason = _full_profile_skip_reason(profile)
    if skip_reason:
        return DiscoverySaveResult(False, False, skip_reason)

    first_name = (profile.get("first_name") or "").strip()
    last_name = (profile.get("last_name") or "").strip()
    full_name = (
        (profile.get("full_name") or "").strip()
        or f"{first_name} {last_name}".strip()
    )
    start, end = discovery_day_bounds(now)

    try:
        with transaction.atomic():
            LinkedInProfile.objects.select_for_update().get(
                pk=linkedin_profile.pk,
            )
            count = LinkedInDiscoveryLead.objects.filter(
                stored_by_operator=operator,
                created_at__gte=start,
                created_at__lt=end,
            ).count()
            if count >= conf.DISCOVERY_DAILY_LIMIT:
                return DiscoverySaveResult(False, True, "daily_save_limit_reached")

            LinkedInDiscoveryLead.objects.create(
                public_identifier=public_identifier,
                linkedin_url=linkedin_url,
                member_urn=(profile.get("urn") or "").strip(),
                first_name=first_name,
                last_name=last_name,
                full_name=full_name,
                headline=(profile.get("headline") or "").strip(),
                company_name=_profile_company(profile),
                location=(profile.get("location_name") or "").strip(),
                profile_data=profile,
                stored_by_operator=operator,
                stored_by_account_username=linkedin_profile.linkedin_username,
                potential_icp=potential_icp,
                last_seen_at=now,
                last_profiled_at=now,
            )
            reached = count + 1 >= conf.DISCOVERY_DAILY_LIMIT
    except IntegrityError:
        return DiscoverySaveResult(False, False, "existing_discovery_lead")

    return DiscoverySaveResult(True, reached, "saved")


def fresh_discovery_payload(
    operator: str,
    *,
    scheduled_for: datetime | None = None,
) -> dict:
    local = discovery_local_now(scheduled_for)
    return {
        "operator": operator,
        "source": "people_search",
        "run_date": local.date().isoformat(),
        "run_started_at": "",
        "query_index": 0,
        "page": 1,
        "query_pages": {},
        "exhausted_query_indexes": [],
        "pending_cards": [],
        "cards_scanned": 0,
        "pages_scanned": 0,
        "profile_visits": 0,
        "consecutive_no_matches": 0,
        "saved": 0,
        "stop_after_pending": "",
    }


def _active_discovery_task_exists(operator: str) -> bool:
    return Task.objects.filter(
        task_type=Task.TaskType.DISCOVERY,
        status__in=[Task.Status.PENDING, Task.Status.RUNNING],
        payload__operator=operator,
    ).exists()


def enqueue_discovery(
    linkedin_profile: LinkedInProfile,
    operator: str,
    *,
    scheduled_at: datetime | None = None,
) -> bool:
    """Ensure a sender has one future/current discovery task when configured."""
    if not conf.ENABLE_PROFILE_DISCOVERY:
        return False
    targets = load_discovery_targets(operator)
    if not targets:
        return False
    discovery_limits()  # validate settings before creating queue state
    if _active_discovery_task_exists(operator):
        return False
    if remaining_today(operator) <= 0:
        scheduled_at = next_discovery_day_start()
    else:
        scheduled_at = scheduled_at or timezone.now()
    if scheduled_at is None:
        return False
    Task.objects.create(
        task_type=Task.TaskType.DISCOVERY,
        scheduled_at=scheduled_at,
        payload=fresh_discovery_payload(operator, scheduled_for=scheduled_at),
    )
    return True


def disable_pending_discovery_tasks(operator: str, reason: str) -> int:
    return Task.objects.filter(
        task_type=Task.TaskType.DISCOVERY,
        status=Task.Status.PENDING,
        payload__operator=operator,
    ).update(
        status=Task.Status.COMPLETED,
        completed_at=timezone.now(),
        error=reason,
    )


def discovery_enabled_for_sender(
    linkedin_profile: LinkedInProfile,
    operator: str,
) -> bool:
    return bool(
        conf.ENABLE_PROFILE_DISCOVERY
        and load_discovery_targets(operator)
    )


def discovery_available_now(
    linkedin_profile: LinkedInProfile,
    operator: str,
    *,
    now: datetime | None = None,
) -> bool:
    return bool(
        discovery_enabled_for_sender(linkedin_profile, operator)
        and remaining_today(operator, now=now) > 0
        and discovery_gate_open(linkedin_profile, now=now)
    )


def reconcile_discovery_tasks(
    linkedin_profile: LinkedInProfile,
    operator: str,
) -> bool:
    """Normalize startup queue state and ensure one correctly timed task."""
    if not conf.ENABLE_PROFILE_DISCOVERY:
        disable_pending_discovery_tasks(operator, "Profile discovery disabled")
        return False

    targets = load_discovery_targets(operator)
    if not targets:
        Task.objects.filter(
            task_type=Task.TaskType.DISCOVERY,
            status=Task.Status.PENDING,
            payload__operator=operator,
        ).update(
            status=Task.Status.COMPLETED,
            completed_at=timezone.now(),
            error="Sender discovery disabled or no enabled ICPs",
        )
        return False

    discovery_limits()
    pending = list(
        Task.objects.filter(
            task_type=Task.TaskType.DISCOVERY,
            status=Task.Status.PENDING,
            payload__operator=operator,
        ).order_by("scheduled_at", "pk"),
    )
    if len(pending) > 1:
        duplicate_ids = [item.pk for item in pending[1:]]
        Task.objects.filter(pk__in=duplicate_ids).update(
            status=Task.Status.COMPLETED,
            completed_at=timezone.now(),
            error="Duplicate discovery task retired during startup reconciliation",
        )
        pending = pending[:1]

    now = timezone.now()
    if pending:
        task = pending[0]
        at_daily_limit = remaining_today(operator, now=now) <= 0
        if at_daily_limit:
            scheduled_at = next_discovery_day_start(now)
        elif (
            not task.payload.get("run_started_at")
            and task.scheduled_at > now + timedelta(
                seconds=conf.DISCOVERY_PROFILE_DELAY_MAX_SECONDS,
            )
        ):
            scheduled_at = now
        else:
            scheduled_at = None
        if (
            scheduled_at is not None
            and (task.scheduled_at != scheduled_at or at_daily_limit)
        ):
            task.scheduled_at = scheduled_at
            task.payload = fresh_discovery_payload(
                operator,
                scheduled_for=scheduled_at,
            )
            task.save(update_fields=["scheduled_at", "payload"])
        return True

    return enqueue_discovery(linkedin_profile, operator)


def _create_pending_task(payload: dict, scheduled_at: datetime) -> None:
    Task.objects.create(
        task_type=Task.TaskType.DISCOVERY,
        scheduled_at=scheduled_at,
        payload=payload,
    )


def _schedule_fresh_task(
    *,
    operator: str,
    now: datetime,
    next_day: bool,
) -> None:
    scheduled_at = next_discovery_day_start(now) if next_day else now
    if scheduled_at is None:
        return
    if Task.objects.filter(
        task_type=Task.TaskType.DISCOVERY,
        status=Task.Status.PENDING,
        payload__operator=operator,
    ).exists():
        return
    _create_pending_task(
        fresh_discovery_payload(operator, scheduled_for=scheduled_at),
        scheduled_at,
    )


def _record_stop(task: Task, payload: dict, reason: str) -> None:
    payload["stop_reason"] = reason
    task.payload = payload
    task.save(update_fields=["payload"])
    logger.info(
        "Discovery stopped operator=%s reason=%s cards=%d pages=%d "
        "visits=%d saved=%d",
        payload["operator"],
        reason,
        payload["cards_scanned"],
        payload["pages_scanned"],
        payload["profile_visits"],
        payload["saved"],
    )


def _finish_for_day(
    task: Task,
    payload: dict,
    *,
    reason: str,
    now: datetime,
    schedule_next: bool = True,
) -> None:
    _record_stop(task, payload, reason)
    if schedule_next:
        _schedule_fresh_task(
            operator=payload["operator"],
            now=now,
            next_day=True,
        )


def _run_time_exhausted(payload: dict, now: datetime, max_minutes: int) -> bool:
    raw = payload.get("run_started_at") or ""
    if not raw:
        return False
    started = datetime.fromisoformat(raw)
    if timezone.is_naive(started):
        started = timezone.make_aware(started)
    return now >= started + timedelta(minutes=max_minutes)


def _hard_stop_before_visit(
    *,
    session,
    operator: str,
    payload: dict,
    now: datetime,
) -> str:
    limits = discovery_limits()
    if not discovery_gate_open(session.linkedin_profile, now=now):
        return "weekday_connection_work_incomplete"
    if remaining_today(operator, now=now) <= 0:
        return "daily_save_limit_reached"
    if _run_time_exhausted(payload, now, limits.max_run_minutes):
        return "run_time_limit_reached"
    if payload["profile_visits"] >= limits.max_profile_visits:
        return "profile_visit_limit_reached"
    return ""


def _scan_stop_reason(payload: dict) -> str:
    limits = discovery_limits()
    if payload["cards_scanned"] >= limits.max_cards:
        return "card_limit_reached"
    if payload["pages_scanned"] >= limits.max_pages:
        return "page_limit_reached"
    if payload["consecutive_no_matches"] >= limits.max_consecutive_no_matches:
        return "consecutive_no_match_limit_reached"
    return ""


def _advance_query_cursor(
    payload: dict,
    *,
    query_count: int,
    current_had_cards: bool,
) -> bool:
    """Advance round-robin across queries. Return True when all are exhausted."""
    current = int(payload["query_index"])
    exhausted = {
        int(value)
        for value in payload.get("exhausted_query_indexes", [])
    }
    pages = {
        str(key): int(value)
        for key, value in (payload.get("query_pages") or {}).items()
    }
    if current_had_cards:
        pages[str(current)] = int(payload["page"]) + 1
    else:
        exhausted.add(current)

    if len(exhausted) >= query_count:
        payload["exhausted_query_indexes"] = sorted(exhausted)
        payload["query_pages"] = pages
        return True

    for offset in range(1, query_count + 1):
        candidate = (current + offset) % query_count
        if candidate in exhausted:
            continue
        payload["query_index"] = candidate
        payload["page"] = pages.get(str(candidate), 1)
        payload["exhausted_query_indexes"] = sorted(exhausted)
        payload["query_pages"] = pages
        return False
    return True


def _continuation_delay() -> int:
    limits = discovery_limits()
    return random.randint(limits.delay_min_seconds, limits.delay_max_seconds)


def _queue_continuation_or_next_day(
    task: Task,
    payload: dict,
    *,
    now: datetime,
) -> None:
    scheduled_at = now + timedelta(seconds=_continuation_delay())
    if scheduled_at >= discovery_day_end(now):
        _finish_for_day(
            task,
            payload,
            reason="day_ended",
            now=now,
        )
        return
    task.payload = payload
    task.save(update_fields=["payload"])
    _create_pending_task(payload, scheduled_at)


def _visit_one(
    *,
    session,
    operator: str,
    card: DiscoveryCard,
    payload: dict,
    enabled_icps: set[str],
) -> DiscoverySaveResult:
    if card.potential_icp not in enabled_icps:
        return DiscoverySaveResult(False, False, "invalid_potential_icp")
    skip_reason = known_profile_reason(card)
    if skip_reason:
        return DiscoverySaveResult(False, False, skip_reason)

    payload["profile_visits"] += 1
    try:
        search_profile(
            session,
            {
                "url": card.linkedin_url,
                "public_identifier": card.public_identifier,
            },
        )
        api = PlaywrightLinkedinAPI(session=session)
        profile, _raw = api.get_profile(
            public_identifier=card.public_identifier,
        )
    except SkipProfile:
        return DiscoverySaveResult(False, False, "restricted_profile")
    except IOError:
        logger.warning(
            "Discovery profile fetch exhausted retries for %s",
            card.public_identifier,
            exc_info=True,
        )
        return DiscoverySaveResult(False, False, "profile_fetch_failed")

    if not profile:
        return DiscoverySaveResult(False, False, "restricted_profile")
    return save_discovery_profile(
        linkedin_profile=session.linkedin_profile,
        operator=operator,
        potential_icp=card.potential_icp,
        profile=profile,
    )


def _process_pending_card(
    task: Task,
    session,
    payload: dict,
    targets: tuple[DiscoveryTarget, ...],
    *,
    now: datetime,
) -> None:
    operator = payload["operator"]
    stop_reason = _hard_stop_before_visit(
        session=session,
        operator=operator,
        payload=payload,
        now=now,
    )
    if stop_reason:
        _finish_for_day(task, payload, reason=stop_reason, now=now)
        return

    pending = list(payload.get("pending_cards") or [])
    card = DiscoveryCard.from_payload(pending.pop(0))
    payload["pending_cards"] = pending
    result = _visit_one(
        session=session,
        operator=operator,
        card=card,
        payload=payload,
        enabled_icps={target.icp for target in targets},
    )
    if result.created:
        payload["saved"] += 1
    logger.info(
        "Discovery profile operator=%s profile=%s icp=%s result=%s "
        "saved_today=%d/%d",
        operator,
        card.public_identifier,
        card.potential_icp,
        result.reason,
        saved_today(operator),
        conf.DISCOVERY_DAILY_LIMIT,
    )

    if result.daily_limit_reached:
        _finish_for_day(
            task,
            payload,
            reason="daily_save_limit_reached",
            now=timezone.now(),
        )
        return
    if pending:
        _queue_continuation_or_next_day(
            task,
            payload,
            now=timezone.now(),
        )
        return
    if payload.get("stop_after_pending"):
        _finish_for_day(
            task,
            payload,
            reason=payload["stop_after_pending"],
            now=timezone.now(),
        )
        return
    _queue_continuation_or_next_day(
        task,
        payload,
        now=timezone.now(),
    )


def handle_discovery(task: Task, session, qualifiers=None) -> None:
    """Handle one bounded search page or one queued profile visit."""
    del qualifiers
    payload = dict(task.payload or {})
    operator = (payload.get("operator") or "").strip()
    session_operator = resolve_operator(session.linkedin_profile.linkedin_username)
    if not operator or operator != session_operator:
        raise ValueError(
            f"Discovery task operator {operator!r} does not match "
            f"session operator {session_operator!r}",
        )

    now = timezone.now()
    targets = load_discovery_targets(operator)
    if not conf.ENABLE_PROFILE_DISCOVERY:
        _record_stop(task, payload, "discovery_disabled")
        return
    if not targets:
        _record_stop(task, payload, "no_enabled_icps")
        return
    discovery_limits()
    if not discovery_gate_open(session.linkedin_profile, now=now):
        _record_stop(task, payload, "weekday_connection_work_incomplete")
        _schedule_fresh_task(
            operator=operator,
            now=now,
            next_day=False,
        )
        return

    if not payload.get("run_started_at"):
        payload["run_started_at"] = now.isoformat()

    if payload.get("pending_cards"):
        _process_pending_card(task, session, payload, targets, now=now)
        return

    hard_stop = _hard_stop_before_visit(
        session=session,
        operator=operator,
        payload=payload,
        now=now,
    )
    if hard_stop:
        _finish_for_day(task, payload, reason=hard_stop, now=now)
        return
    scan_stop = _scan_stop_reason(payload)
    if scan_stop:
        _finish_for_day(task, payload, reason=scan_stop, now=now)
        return

    queries = discovery_search_queries(targets)
    query_index = int(payload.get("query_index", 0))
    if query_index < 0 or query_index >= len(queries):
        raise ValueError(f"Invalid discovery query_index: {query_index}")
    page_number = int(payload.get("page", 1))
    query = queries[query_index]

    try:
        cards = collect_people_search_cards(
            session,
            query=query,
            page_number=page_number,
        )
    except LinkedInDiscoveryLimitError:
        _finish_for_day(
            task,
            payload,
            reason="linkedin_limit_detected",
            now=now,
        )
        return
    except AuthenticationError:
        payload["stop_reason"] = "authentication_lost"
        task.payload = payload
        task.save(update_fields=["payload"])
        raise

    limits = discovery_limits()
    remaining_card_capacity = limits.max_cards - payload["cards_scanned"]
    cards = cards[:remaining_card_capacity]
    payload["pages_scanned"] += 1
    payload["cards_scanned"] += len(cards)

    candidates: list[DiscoveryCard] = []
    candidate_ids: set[str] = set()
    skip_reasons: dict[str, str] = {}
    for card in cards:
        reason = known_profile_reason(card)
        if reason:
            skip_reasons[card.public_identifier] = reason
            continue
        candidates.append(card)
        candidate_ids.add(card.public_identifier)

    decisions = screen_cards(candidates, targets) if candidates else {}
    pending_cards: list[dict] = []
    reached_no_match_cap = False
    for card in cards:
        if card.public_identifier in skip_reasons:
            payload["consecutive_no_matches"] += 1
        elif card.public_identifier in candidate_ids:
            decision = decisions[card.public_identifier]
            if decision.should_visit:
                payload["consecutive_no_matches"] = 0
                pending_cards.append(
                    DiscoveryCard(
                        public_identifier=card.public_identifier,
                        linkedin_url=card.linkedin_url,
                        name=card.name,
                        headline=card.headline,
                        company_name=card.company_name,
                        source_context=card.source_context,
                        potential_icp=decision.potential_icp,
                    ).to_payload(),
                )
            else:
                payload["consecutive_no_matches"] += 1
        if (
            payload["consecutive_no_matches"]
            >= limits.max_consecutive_no_matches
        ):
            reached_no_match_cap = True
            break

    queries_exhausted = _advance_query_cursor(
        payload,
        query_count=len(queries),
        current_had_cards=bool(cards),
    )
    payload["pending_cards"] = pending_cards

    if reached_no_match_cap:
        payload["stop_after_pending"] = "consecutive_no_match_limit_reached"
    elif payload["cards_scanned"] >= limits.max_cards:
        payload["stop_after_pending"] = "card_limit_reached"
    elif payload["pages_scanned"] >= limits.max_pages:
        payload["stop_after_pending"] = "page_limit_reached"
    elif queries_exhausted:
        payload["stop_after_pending"] = "queries_exhausted"

    logger.info(
        "Discovery scan operator=%s query=%r page=%d cards=%d candidates=%d "
        "matches=%d totals(cards=%d pages=%d no_match=%d)",
        operator,
        query,
        page_number,
        len(cards),
        len(candidates),
        len(pending_cards),
        payload["cards_scanned"],
        payload["pages_scanned"],
        payload["consecutive_no_matches"],
    )

    if pending_cards:
        _process_pending_card(task, session, payload, targets, now=timezone.now())
        return
    if payload.get("stop_after_pending"):
        _finish_for_day(
            task,
            payload,
            reason=payload["stop_after_pending"],
            now=timezone.now(),
        )
        return
    _queue_continuation_or_next_day(
        task,
        payload,
        now=timezone.now(),
    )
