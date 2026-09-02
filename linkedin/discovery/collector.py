"""Database and task orchestration for bounded profile discovery."""
from __future__ import annotations

import logging
import random
import re
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
from linkedin.discovery.sources.mynetwork_recommendations import (
    collect_mynetwork_recommendations,
)
from linkedin.discovery.sources.profile_recommendations import (
    collect_profile_recommendations,
)
from linkedin.exceptions import (
    AuthenticationError,
    DiscoveryScreeningError,
    DiscoverySurfaceError,
    SkipProfile,
)
from linkedin.icp_outbound import (
    DiscoveryTarget,
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

DISCOVERY_SCREEN_BATCH_SIZE = 5


@dataclass(frozen=True)
class DiscoverySaveResult:
    created: bool
    daily_limit_reached: bool
    reason: str


@dataclass(frozen=True)
class DiscoveryVisitResult:
    save: DiscoverySaveResult
    related_cards: tuple[DiscoveryCard, ...] = ()
    related_scroll_rounds: int = 0
    related_empty_scrolls: int = 0


def _canonical_public_identifier(value: str | None) -> str:
    return re.sub(r"[\s\x00-\x1f\x7f]+", "", value or "").strip().strip("/").lower()


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
        "source": "mynetwork_recommendations",
        "run_date": local.date().isoformat(),
        "run_started_at": "",
        "source_complete": False,
        "section_cursor": 0,
        "section_headings": [],
        "pending_cards": [],
        "seen_public_identifiers": [],
        "cards_scanned": 0,
        "sections_scanned": 0,
        "scroll_rounds": 0,
        "consecutive_scrolls_without_new_cards": 0,
        "profile_visits": 0,
        "recommendation_depth": 0,
        "consecutive_no_matches": 0,
        "saved": 0,
        "stop_after_pending": "",
    }


def validate_discovery_payload(payload: dict) -> None:
    """Fail loudly for malformed or search-era discovery cursor state."""
    if payload.get("source") != "mynetwork_recommendations":
        raise ValueError(
            "Discovery payload source must be mynetwork_recommendations",
        )
    if not isinstance(payload.get("section_cursor"), int) or (
        payload["section_cursor"] < 0
    ):
        raise ValueError("Discovery payload section_cursor must be non-negative")
    for field in (
        "cards_scanned",
        "sections_scanned",
        "scroll_rounds",
        "consecutive_scrolls_without_new_cards",
        "profile_visits",
        "consecutive_no_matches",
        "saved",
    ):
        if not isinstance(payload.get(field), int) or payload[field] < 0:
            raise ValueError(f"Discovery payload {field} must be non-negative")
    for field in (
        "section_headings",
        "pending_cards",
        "seen_public_identifiers",
    ):
        if not isinstance(payload.get(field), list):
            raise ValueError(f"Discovery payload {field} must be a list")
    depth = payload.get("recommendation_depth")
    if depth not in {0, 1}:
        raise ValueError("Discovery payload recommendation_depth must be 0 or 1")


def _mynetwork_card_budget(payload: dict) -> int:
    """Reserve part of the run-wide card cap for one-hop profile suggestions."""
    limits = discovery_limits()
    remaining = limits.max_cards - payload["cards_scanned"]
    if remaining <= 1:
        return max(remaining, 0)
    reserved = min(
        limits.max_profile_recommendations_per_visit,
        max(1, remaining // 4),
    )
    return max(1, remaining - reserved)


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
        search_era_payload = (
            task.payload.get("source") != "mynetwork_recommendations"
            or "query_index" in task.payload
            or "page" in task.payload
        )
        at_daily_limit = remaining_today(operator, now=now) <= 0
        if at_daily_limit:
            scheduled_at = next_discovery_day_start(now)
        elif search_era_payload:
            scheduled_at = now
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
            and (
                task.scheduled_at != scheduled_at
                or at_daily_limit
                or search_era_payload
            )
        ):
            task.scheduled_at = scheduled_at
            task.payload = fresh_discovery_payload(
                operator,
                scheduled_for=scheduled_at,
            )
            task.save(update_fields=["scheduled_at", "payload"])
        else:
            validate_discovery_payload(dict(task.payload or {}))
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
        "Discovery stopped operator=%s reason=%s cards=%d sections=%d "
        "scrolls=%d visits=%d saved=%d",
        payload["operator"],
        reason,
        payload["cards_scanned"],
        payload["sections_scanned"],
        payload["scroll_rounds"],
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
    if payload["sections_scanned"] >= limits.max_sections:
        return "section_limit_reached"
    if payload["scroll_rounds"] >= limits.max_scroll_rounds:
        return "scroll_limit_reached"
    if (
        payload["consecutive_scrolls_without_new_cards"]
        >= limits.max_consecutive_empty_scrolls
    ):
        return "empty_scroll_limit_reached"
    if payload["consecutive_no_matches"] >= limits.max_consecutive_no_matches:
        return "consecutive_no_match_limit_reached"
    return ""


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


def _screen_new_cards(
    *,
    cards: list[DiscoveryCard] | tuple[DiscoveryCard, ...],
    payload: dict,
    targets: tuple[DiscoveryTarget, ...],
) -> list[DiscoveryCard]:
    """Deduplicate, skip, and lightly screen one bounded recommendation batch."""
    limits = discovery_limits()
    seen = {
        _canonical_public_identifier(value)
        for value in payload.get("seen_public_identifiers", [])
        if _canonical_public_identifier(value)
    }
    operator = (payload.get("operator") or "").strip()
    match_capacity = min(
        max(limits.max_profile_visits - payload["profile_visits"], 0),
        remaining_today(operator) if operator else limits.max_profile_visits,
    )
    if match_capacity <= 0:
        payload["seen_public_identifiers"] = sorted(seen)
        payload["stop_after_pending"] = "profile_visit_limit_reached"
        return []

    matches: list[DiscoveryCard] = []
    pending_batch: list[DiscoveryCard] = []

    def flush_batch() -> bool:
        if not pending_batch:
            return False
        try:
            decisions = screen_cards(pending_batch, targets)
        except DiscoveryScreeningError as exc:
            logger.warning(
                "Discovery screening rejected batch of %d card(s); skipping batch: %s",
                len(pending_batch),
                exc,
            )
            payload["consecutive_no_matches"] += len(pending_batch)
            pending_batch.clear()
            if (
                payload["consecutive_no_matches"]
                >= limits.max_consecutive_no_matches
            ):
                payload["stop_after_pending"] = (
                    "consecutive_no_match_limit_reached"
                )
                return True
            return False
        for card in pending_batch:
            public_identifier = _canonical_public_identifier(card.public_identifier)
            decision = decisions[public_identifier]
            if decision.should_visit:
                payload["consecutive_no_matches"] = 0
                matches.append(
                    DiscoveryCard(
                        public_identifier=card.public_identifier,
                        linkedin_url=card.linkedin_url,
                        name=card.name,
                        headline=card.headline,
                        company_name=card.company_name,
                        source_context=card.source_context,
                        potential_icp=decision.potential_icp,
                        source_kind=card.source_kind,
                        source_section=card.source_section,
                        source_profile_public_identifier=(
                            card.source_profile_public_identifier
                        ),
                        recommendation_depth=card.recommendation_depth,
                    ),
                )
                if len(matches) >= match_capacity:
                    return True
            else:
                payload["consecutive_no_matches"] += 1
            if (
                payload["consecutive_no_matches"]
                >= limits.max_consecutive_no_matches
            ):
                payload["stop_after_pending"] = (
                    "consecutive_no_match_limit_reached"
                )
                return True
        pending_batch.clear()
        return False

    for card in cards:
        public_identifier = _canonical_public_identifier(card.public_identifier)
        if not public_identifier or public_identifier in seen:
            continue
        if payload["cards_scanned"] >= limits.max_cards:
            payload["stop_after_pending"] = "card_limit_reached"
            break
        seen.add(public_identifier)
        payload["cards_scanned"] += 1
        reason = known_profile_reason(card)
        if reason:
            payload["consecutive_no_matches"] += 1
            if (
                payload["consecutive_no_matches"]
                >= limits.max_consecutive_no_matches
            ):
                payload["stop_after_pending"] = (
                    "consecutive_no_match_limit_reached"
                )
                break
            continue
        pending_batch.append(card)
        if len(pending_batch) >= DISCOVERY_SCREEN_BATCH_SIZE and flush_batch():
            break

    payload["seen_public_identifiers"] = sorted(seen)
    if len(matches) < match_capacity:
        flush_batch()
    return matches


def _visit_one(
    *,
    session,
    operator: str,
    card: DiscoveryCard,
    payload: dict,
    enabled_icps: set[str],
) -> DiscoveryVisitResult:
    if card.potential_icp not in enabled_icps:
        return DiscoveryVisitResult(
            DiscoverySaveResult(False, False, "invalid_potential_icp"),
        )
    skip_reason = known_profile_reason(card)
    if skip_reason:
        return DiscoveryVisitResult(DiscoverySaveResult(False, False, skip_reason))

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
        return DiscoveryVisitResult(
            DiscoverySaveResult(False, False, "restricted_profile"),
        )
    except IOError:
        logger.warning(
            "Discovery profile fetch exhausted retries for %s",
            card.public_identifier,
            exc_info=True,
        )
        return DiscoveryVisitResult(
            DiscoverySaveResult(False, False, "profile_fetch_failed"),
        )

    if not profile:
        return DiscoveryVisitResult(
            DiscoverySaveResult(False, False, "restricted_profile"),
        )
    save_result = save_discovery_profile(
        linkedin_profile=session.linkedin_profile,
        operator=operator,
        potential_icp=card.potential_icp,
        profile=profile,
    )
    if (
        not save_result.created
        or save_result.daily_limit_reached
        or card.recommendation_depth != 0
    ):
        return DiscoveryVisitResult(save_result)

    limits = discovery_limits()
    remaining_card_capacity = limits.max_cards - payload["cards_scanned"]
    if remaining_card_capacity <= 0:
        return DiscoveryVisitResult(save_result)
    related = collect_profile_recommendations(
        session,
        source_profile_public_identifier=card.public_identifier,
        max_cards=min(
            limits.max_profile_recommendations_per_visit,
            remaining_card_capacity,
        ),
        max_scroll_rounds=max(
            0,
            limits.max_scroll_rounds - payload["scroll_rounds"],
        ),
        max_consecutive_empty_scrolls=limits.max_consecutive_empty_scrolls,
    )
    return DiscoveryVisitResult(
        save=save_result,
        related_cards=related.cards,
        related_scroll_rounds=related.scroll_rounds,
        related_empty_scrolls=related.consecutive_empty_scrolls,
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
    visit = _visit_one(
        session=session,
        operator=operator,
        card=card,
        payload=payload,
        enabled_icps={target.icp for target in targets},
    )
    result = visit.save
    if result.created:
        payload["saved"] += 1
    payload["recommendation_depth"] = max(
        payload["recommendation_depth"],
        card.recommendation_depth,
    )
    if visit.related_cards:
        payload["scroll_rounds"] += visit.related_scroll_rounds
        payload["consecutive_scrolls_without_new_cards"] = (
            visit.related_empty_scrolls
        )
        related_matches = _screen_new_cards(
            cards=visit.related_cards,
            payload=payload,
            targets=targets,
        )
        pending.extend(card.to_payload() for card in related_matches)
        payload["pending_cards"] = pending
        if not payload.get("stop_after_pending"):
            cap_reason = _scan_stop_reason(payload)
            if cap_reason:
                payload["stop_after_pending"] = cap_reason
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
    """Handle one bounded recommendation scan or one queued profile visit."""
    del qualifiers
    payload = dict(task.payload or {})
    validate_discovery_payload(payload)
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

    if payload.get("source_complete"):
        _finish_for_day(
            task,
            payload,
            reason=payload.get("stop_after_pending") or "recommendation_source_exhausted",
            now=now,
        )
        return

    limits = discovery_limits()

    try:
        source_result = collect_mynetwork_recommendations(
            session,
            max_cards=_mynetwork_card_budget(payload),
            max_sections=limits.max_sections - payload["sections_scanned"],
            max_scroll_rounds=limits.max_scroll_rounds - payload["scroll_rounds"],
            max_consecutive_empty_scrolls=(
                limits.max_consecutive_empty_scrolls
            ),
        )
    except AuthenticationError:
        payload["stop_reason"] = "authentication_lost"
        task.payload = payload
        task.save(update_fields=["payload"])
        raise
    except DiscoverySurfaceError as exc:
        payload["surface_error"] = str(exc)
        _record_stop(task, payload, "surface_error")
        raise

    payload["source_complete"] = True
    payload["sections_scanned"] += source_result.sections_scanned
    payload["section_cursor"] = payload["sections_scanned"]
    payload["scroll_rounds"] += source_result.scroll_rounds
    payload["consecutive_scrolls_without_new_cards"] = (
        source_result.consecutive_empty_scrolls
    )
    section_headings = list(payload.get("section_headings") or [])
    for heading in source_result.section_headings:
        if heading not in section_headings:
            section_headings.append(heading)
    payload["section_headings"] = section_headings

    pending_matches = _screen_new_cards(
        cards=source_result.cards,
        payload=payload,
        targets=targets,
    )
    payload["pending_cards"] = [card.to_payload() for card in pending_matches]
    if not payload.get("stop_after_pending"):
        payload["stop_after_pending"] = (
            "recommendation_source_exhausted"
            if source_result.stop_reason == "source_exhausted"
            else source_result.stop_reason
        )

    logger.info(
        "Discovery scan operator=%s sections=%d scrolls=%d cards=%d "
        "matches=%d totals(cards=%d sections=%d no_match=%d) stop=%s",
        operator,
        source_result.sections_scanned,
        source_result.scroll_rounds,
        len(source_result.cards),
        len(pending_matches),
        payload["cards_scanned"],
        payload["sections_scanned"],
        payload["consecutive_no_matches"],
        source_result.stop_reason,
    )

    if pending_matches:
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
