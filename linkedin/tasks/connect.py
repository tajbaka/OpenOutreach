# linkedin/tasks/connect.py
"""Connect task — pulls one candidate, connects, self-reschedules.

Works for both regular and freemium campaigns via ConnectStrategy.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from linkedin.tasks.sweep_connections import enqueue_sweep_connections

from django.utils import timezone
from termcolor import colored

from linkedin.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    ACTIVE_TIMEZONE,
    CAMPAIGN_CONFIG,
    CONNECT_DAILY_LIMIT,
    ENABLE_ACTIVE_HOURS,
    ENABLE_CONNECT,
    ENABLE_PACING_CATCH_UP,
    FOLLOW_UP_DAILY_LIMIT,
    OUR_COMPANY_NAME,
    OUR_WEBSITE_URL,
    REST_DAYS,
)
from linkedin.db.deals import increment_connect_attempts, set_profile_state
from linkedin.db.leads import disqualify_lead
from linkedin.models import ActionLog, ConnectIssueLog, Task, log_connect_issue
from linkedin.enums import ProfileState
from linkedin.exceptions import ReachedConnectionLimit, SkipProfile
from linkedin.name_utils import greeting_first_name
from linkedin.operators import resolve_operator

logger = logging.getLogger(__name__)

MAX_CONNECT_ATTEMPTS = 3
PACING_JITTER_MIN = 0.9
PACING_JITTER_MAX = 1.1
PACING_BEHIND_GRACE_ACTIONS = 1.0
PACING_CATCH_UP_MIN_DELAY_SECONDS = 120.0
PACING_CATCH_UP_MAX_DELAY_SECONDS = 180.0


def build_connection_note(lead_id: int | None, sender: str) -> str:
    """Build a connection note for the lead, ICP-keyed via `icp_messages.json`.

    `sender` is the operator's canonical handle (`linkedin.operators.
    resolve_operator`) — it selects the top-level template block, so
    each operator can ship a different connect note.

    `Lead.icp` resolves to a bucket with a `linkedin_connect_note` channel
    → pick a variant from that bucket. Source of truth: CSV `ICP` column
    stamped at `add_seeds` time, or `resolve_icp()` backfill at first
    scrape. If the lead has no resolvable ICP, the sender is unknown, or
    the template is missing, return "" — a note-less connection request
    is fine.

    Variant selection within a bucket: random (defeats templated-text
    detection across batches).
    """
    from crm.models import Lead
    from linkedin.icp_outbound import load_icp_messages, resolve_icp, safe_company_name

    lead = Lead.objects.filter(pk=lead_id).first() if lead_id else None
    first_name = greeting_first_name(lead.first_name if lead else "")

    # ICP-keyed path. Try only when we have a lead + a resolvable ICP +
    # matching connect-note variants in the JSON. Any missing piece
    # bumps us to the env-var fallback so we never refuse to send.
    #
    # Substitution kwargs match the documented ICP-template tokens
    # (see CLAUDE.md "Rigid ICP outbound templates" bullet). Anything
    # not used by a given template is harmless — `str.format` ignores
    # kwargs that don't appear in the string.
    if lead is not None:
        icp = resolve_icp(lead)
        if icp:
            try:
                bucket = load_icp_messages(sender).get(icp, {})
            except Exception as e:
                # Don't let a malformed JSON or an unknown sender kill the
                # send — fall back to a note-less connection request.
                logger.warning("build_connection_note: load_icp_messages failed → %s", e)
                bucket = {}
            variants = bucket.get("linkedin_connect_note") or []
            if variants:
                template = random.choice(variants)
                try:
                    return template.format(
                        first_name=first_name,
                        last_name=(lead.last_name or "").strip(),
                        company_name=safe_company_name(lead.company_name),
                        our_company_name=OUR_COMPANY_NAME,
                        our_website_url=OUR_WEBSITE_URL,
                    )
                except KeyError as e:
                    # Unknown placeholder in template → log the offender
                    # so the operator can fix the JSON, then send note-less
                    # rather than crash the connect Task.
                    logger.error(
                        "build_connection_note: template for icp=%r has "
                        "unknown placeholder %s — fix linkedin/icp_messages.json. "
                        "Sending note-less for this connect.",
                        icp, e,
                    )

    return ""


@dataclass
class ConnectStrategy:
    find_candidate: Callable
    pre_connect: Callable | None
    delay: float
    action_fraction: float  # 1.0 = always fire at base delay
    qualifier: object

    def compute_delay(self, elapsed: float) -> float:
        """Delay until next connect, scaled by elapsed execution time for freemium campaigns."""
        if self.action_fraction >= 1.0:
            return self.delay
        return max(self.delay, elapsed * (1 - self.action_fraction) / self.action_fraction)


def strategy_for(campaign, qualifiers):
    """Build the right ConnectStrategy based on campaign type."""
    qualifier = qualifiers.get(campaign.pk)

    if campaign.is_freemium:
        from linkedin.db.deals import create_freemium_deal
        from linkedin.pipeline.freemium_pool import find_freemium_candidate

        fraction = campaign.action_fraction
        return ConnectStrategy(
            find_candidate=lambda s: find_freemium_candidate(s, qualifier),
            pre_connect=lambda s, pid: create_freemium_deal(s, pid),
            delay=CAMPAIGN_CONFIG["connect_delay_seconds"],
            action_fraction=fraction,
            qualifier=qualifier,
        )

    from linkedin.pipeline.pools import find_candidate

    return ConnectStrategy(
        find_candidate=lambda s: find_candidate(s, qualifier),
        pre_connect=None,
        delay=CAMPAIGN_CONFIG["connect_delay_seconds"],
        action_fraction=1.0,
        qualifier=qualifier,
    )


def _seconds_until_tomorrow() -> float:
    from django.utils import timezone
    import datetime

    now = timezone.now()
    tomorrow = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return (tomorrow - now).total_seconds()


def _seconds_until_next_active_start() -> float:
    if not ENABLE_ACTIVE_HOURS:
        return _seconds_until_tomorrow()
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)
    candidate = timezone.make_aware(
        now.replace(hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0, tzinfo=None),
        timezone=tz,
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() in REST_DAYS:
        candidate += timedelta(days=1)
    return max((candidate - now).total_seconds(), 0.0)


def _outside_active_window() -> bool:
    if not ENABLE_ACTIVE_HOURS:
        return False
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)
    return now.weekday() in REST_DAYS or not (ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR)


def _active_window_progress_seconds(profile, action_type: str) -> tuple[float, float]:
    """Return (remaining, normal_window) seconds in ACTIVE_TIMEZONE.

    The normal window is ACTIVE_START_HOUR → ACTIVE_END_HOUR. Outside that
    window this returns the normal window length so callers fall back to the
    configured average pace. During after-hours catch-up, remaining is forced
    low so the short catch-up delay path can run.
    """
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)
    normal_window_seconds = max((ACTIVE_END_HOUR - ACTIVE_START_HOUR) * 3600, 3600)

    if ACTIVE_END_HOUR <= now.hour and (
        ENABLE_PACING_CATCH_UP
        and _is_behind_normal_window_pace(profile, action_type)
    ):
        return 1.0, float(normal_window_seconds)

    if not (ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR):
        return float(normal_window_seconds), float(normal_window_seconds)

    end = now.replace(hour=ACTIVE_END_HOUR, minute=0, second=0, microsecond=0)
    remaining_seconds = max((end - now).total_seconds(), 1.0)
    return remaining_seconds, float(normal_window_seconds)


def _actions_sent_today(profile, action_type: str) -> int:
    """Count actions of `action_type` for this profile since local midnight."""
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)
    today_start = timezone.make_aware(
        now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None),
        timezone=tz,
    )
    return ActionLog.objects.filter(
        linkedin_profile=profile,
        action_type=action_type,
        created_at__gte=today_start,
    ).count()


def _daily_limit_for(profile, action_type: str) -> int:
    if action_type == ActionLog.ActionType.CONNECT:
        return max(CONNECT_DAILY_LIMIT or profile.connect_daily_limit or 1, 1)
    return max(FOLLOW_UP_DAILY_LIMIT or profile.follow_up_daily_limit or 1, 1)


def _expected_actions_by_now(profile, action_type: str) -> float:
    daily_limit = _daily_limit_for(profile, action_type)
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)
    normal_window_seconds = max((ACTIVE_END_HOUR - ACTIVE_START_HOUR) * 3600, 3600)
    start = now.replace(hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0)
    end = now.replace(hour=ACTIVE_END_HOUR, minute=0, second=0, microsecond=0)
    if now <= start:
        return 0.0
    if now >= end:
        return float(daily_limit)
    elapsed = max((now - start).total_seconds(), 0.0)
    return daily_limit * (elapsed / normal_window_seconds)


def _is_behind_normal_window_pace(profile, action_type: str) -> bool:
    """Whether the sender is behind the normal ACTIVE_START→ACTIVE_END pace."""
    expected_by_now = _expected_actions_by_now(profile, action_type)
    sent_today = _actions_sent_today(profile, action_type)
    return sent_today + PACING_BEHIND_GRACE_ACTIONS < expected_by_now


def recommended_action_delay(profile, action_type: str) -> float:
    """Spread actions across the remaining active window instead of firing in bursts.

    Uses ACTIVE_END_HOUR - ACTIVE_START_HOUR as the window even when
    ENABLE_ACTIVE_HOURS is False — the flag only controls whether the
    daemon SLEEPS outside hours, not the per-action pacing math.

    Unlike the old static average, this adapts to:
      - the active hours you configured, and
      - how many actions have already been sent today.

    That means a 9am–5pm window paces more aggressively than 9am–9pm, and
    the spacing tightens/loosens throughout the day based on actual progress
    toward the daily cap.
    """
    daily_limit = _daily_limit_for(profile, action_type)
    sent_today = _actions_sent_today(profile, action_type)
    if ENABLE_PACING_CATCH_UP and sent_today + PACING_BEHIND_GRACE_ACTIONS < _expected_actions_by_now(profile, action_type):
        return max(
            CAMPAIGN_CONFIG["min_action_interval"],
            random.uniform(PACING_CATCH_UP_MIN_DELAY_SECONDS, PACING_CATCH_UP_MAX_DELAY_SECONDS),
        )

    remaining_actions = max(daily_limit - sent_today, 1)
    remaining_window_seconds, normal_window_seconds = _active_window_progress_seconds(profile, action_type)

    # Dynamic target based on what is left in today's window. By default we
    # keep a floor at the full-window average so sends do not over-accelerate
    # early in the day. When the sender falls materially behind pace, the
    # catch-up branch above uses a short bounded delay until they recover.
    full_window_average = normal_window_seconds / daily_limit
    dynamic_average = remaining_window_seconds / remaining_actions
    base_delay = max(full_window_average, dynamic_average)
    delay = max(
        CAMPAIGN_CONFIG["min_action_interval"],
        random.uniform(base_delay * PACING_JITTER_MIN, base_delay * PACING_JITTER_MAX),
    )
    if _outside_active_window() and not _is_behind_normal_window_pace(profile, action_type):
        delay = min(delay, _seconds_until_next_active_start())
    return delay


def handle_connect(task, session, qualifiers):
    from linkedin.actions.connect import ExistingPendingInvite, send_connection_request
    from linkedin.actions.status import get_connection_status

    # Read at call-time (via the module attr) so tests can `@patch
    # "linkedin.tasks.connect.ENABLE_CONNECT"` without restarting conf.
    if not ENABLE_CONNECT:
        # Defense in depth: should never fire, since enqueue_connect is
        # also gated. But if a task slipped in before the flag flipped,
        # bail without rescheduling so the queue drains.
        logger.debug("connect disabled — skipping task %s", task.pk)
        return

    cfg = CAMPAIGN_CONFIG
    campaign = session.campaign
    campaign_id = campaign.pk
    strategy = strategy_for(campaign, qualifiers)
    operator = resolve_operator(session.linkedin_profile.linkedin_username)

    def _reschedule():
        elapsed = (timezone.now() - task.started_at).total_seconds() if task.started_at else 0
        enqueue_connect(
            campaign_id,
            delay_seconds=max(
                strategy.compute_delay(elapsed),
                recommended_action_delay(session.linkedin_profile, ActionLog.ActionType.CONNECT),
            ),
        )

    # --- Rate limit check ---
    if not session.linkedin_profile.can_execute(ActionLog.ActionType.CONNECT):
        enqueue_connect(campaign_id, delay_seconds=_seconds_until_next_active_start())
        return

    # --- Get candidate ---
    candidate = strategy.find_candidate(session)
    if candidate is None:
        enqueue_connect(campaign_id, delay_seconds=cfg["connect_no_candidate_delay_seconds"])
        return

    public_id = candidate["public_identifier"]
    profile = candidate.get("profile") or candidate

    # Freemium campaigns need a Deal before set_profile_state
    if strategy.pre_connect:
        strategy.pre_connect(session, public_id)

    from linkedin.db.urls import public_id_to_url
    from crm.models import ClosingReason, Deal

    deal = Deal.objects.filter(
        lead__linkedin_url=public_id_to_url(public_id),
        campaign=session.campaign,
    ).select_related("lead").first()
    if deal:
        from linkedin.suppression import lead_suppression_match

        suppression = lead_suppression_match(deal.lead)
        if suppression:
            reason = f"Suppression: {suppression.value}"
            logger.warning("connect: %s blocked by %s - skipping send", public_id, reason)
            disqualify_lead(public_id)
            set_profile_state(session, public_id, ProfileState.FAILED.value, reason=reason)
            deal.closing_reason = ClosingReason.DISQUALIFIED
            deal.reason = reason
            deal.save(update_fields=["closing_reason", "reason"])
            enqueue_connect(campaign_id, delay_seconds=0)
            return

        # A confirmed withdrawal is sender-specific negative history. Preserve
        # the Lead for other operators/manual work, but never let this sender's
        # automated connect lane re-invite it through another campaign.
        if Deal.objects.filter(
            lead=deal.lead,
            invitation_sender=operator,
            invitation_withdrawn_at__isnull=False,
        ).exists():
            reason = (
                f"Auto-connect blocked: {operator} previously withdrew a "
                "project-sent invitation"
            )
            set_profile_state(session, public_id, ProfileState.FAILED.value, reason=reason)
            enqueue_connect(campaign_id, delay_seconds=0)
            return
    reason = deal.reason if deal else ""
    stats = strategy.qualifier.explain(candidate, session) if strategy.qualifier else ""
    logger.info("[%s] %s", campaign, colored("\u25b6 connect", "cyan", attrs=["bold"]))
    logger.info("[%s] %s (%s) — %s", campaign, public_id, stats, reason or "")

    try:
        status = get_connection_status(session, profile)

        if status == ProfileState.CONNECTED:
            set_profile_state(session, public_id, status.value)
            deal = Deal.objects.filter(
                lead__linkedin_url=public_id_to_url(public_id),
                campaign=session.campaign,
            ).select_related("lead").first()
            icp = None
            if deal is not None:
                from linkedin.icp_outbound import resolve_icp

                icp = resolve_icp(deal.lead)
            enqueue_follow_up(
                campaign_id,
                public_id,
                operator=operator,
                icp=icp,
                delay_seconds=recommended_action_delay(
                    session.linkedin_profile, ActionLog.ActionType.FOLLOW_UP,
                ),
            )
            if deal is not None:
                from gmail.handoff import maybe_schedule_gmail_sequence

                maybe_schedule_gmail_sequence(deal=deal, operator=operator)
            # Already-connected profiles are effectively "no connect work
            # done" from this lane's perspective, so keep moving instead of
            # consuming the normal connect pacing budget.
            enqueue_connect(campaign_id, delay_seconds=0)
            return

        if status == ProfileState.PENDING:
            set_profile_state(session, public_id, status.value)
            enqueue_sweep_connections(
                operator=operator,
            )
            # No action taken — short delay before next candidate
            enqueue_connect(campaign_id, delay_seconds=10)
            return

        note = build_connection_note(
            candidate.get("lead_id"),
            sender=operator,
        )
        new_state = send_connection_request(session=session, profile=profile, note=note)

        if new_state == ProfileState.QUALIFIED:
            # No Connect button found — track attempt, disqualify after MAX_CONNECT_ATTEMPTS
            attempts = increment_connect_attempts(session, public_id)
            if attempts >= MAX_CONNECT_ATTEMPTS:
                reason = f"Unreachable: no Connect button after {attempts} attempts"
                disqualify_lead(public_id)
                set_profile_state(session, public_id, ProfileState.FAILED.value, reason=reason)
                logger.warning("Disqualified %s — %s", public_id, reason)
            else:
                set_profile_state(session, public_id, new_state.value)
                logger.debug("%s: connect attempt %d/%d — no button found", public_id, attempts, MAX_CONNECT_ATTEMPTS)
            enqueue_connect(campaign_id, delay_seconds=0)
            return
        else:
            set_profile_state(session, public_id, new_state.value)
            session.linkedin_profile.record_action(
                ActionLog.ActionType.CONNECT, session.campaign,
            )

            if new_state == ProfileState.PENDING:
                Deal.objects.filter(
                    lead__linkedin_url=public_id_to_url(public_id),
                    campaign=session.campaign,
                ).update(
                    sent_note=note,
                    invitation_sent_at=timezone.now(),
                    invitation_sender=operator,
                    invitation_withdrawn_at=None,
                )
                enqueue_sweep_connections(
                    operator=operator,
                )
            elif new_state == ProfileState.CONNECTED:
                deal = Deal.objects.filter(
                    lead__linkedin_url=public_id_to_url(public_id),
                    campaign=session.campaign,
                ).select_related("lead").first()
                icp = None
                if deal is not None:
                    from linkedin.icp_outbound import resolve_icp

                    icp = resolve_icp(deal.lead)
                enqueue_follow_up(
                    campaign_id,
                    public_id,
                    operator=operator,
                    icp=icp,
                    delay_seconds=recommended_action_delay(
                        session.linkedin_profile, ActionLog.ActionType.FOLLOW_UP,
                    ),
                )
                if deal is not None:
                    from gmail.handoff import maybe_schedule_gmail_sequence

                    maybe_schedule_gmail_sequence(deal=deal, operator=operator)

    except ReachedConnectionLimit as e:
        logger.warning("Rate limited: %s", e)
        session.linkedin_profile.mark_exhausted(ActionLog.ActionType.CONNECT)
        enqueue_connect(campaign_id, delay_seconds=_seconds_until_next_active_start())
        return
    except ExistingPendingInvite:
        logger.info("%s PENDING (existing invite)", public_id)
        set_profile_state(session, public_id, ProfileState.PENDING.value)
        enqueue_sweep_connections(
            operator=operator,
        )
        enqueue_connect(campaign_id, delay_seconds=10)
        return
    except SkipProfile as e:
        logger.warning("Skipping %s: %s", public_id, e)
        log_connect_issue(
            linkedin_profile=session.linkedin_profile,
            campaign=session.campaign,
            public_id=public_id,
            profile_url=f"https://www.linkedin.com/in/{public_id}/",
            issue_type=ConnectIssueLog.IssueType.SKIP_PROFILE,
            reason=str(e),
        )
        set_profile_state(session, public_id, ProfileState.FAILED.value)

    _reschedule()


# ------------------------------------------------------------------
# Enqueue helpers (used by all task types)
# ------------------------------------------------------------------

def _enqueue_task(task_type: "Task.TaskType", payload: dict, delay_seconds: float, dedup_keys: list[str] | None = None):
    """Create a pending task if no duplicate exists.

    Deduplication: matches on task_type + status=PENDING + dedup_keys payload
    fields (defaults to all payload keys).
    """
    from datetime import timedelta

    filter_kwargs = {
        "task_type": task_type,
        "status": Task.Status.PENDING,
    }
    for key in (dedup_keys if dedup_keys is not None else payload):
        filter_kwargs[f"payload__{key}"] = payload[key]

    existing = Task.objects.filter(**filter_kwargs).order_by("scheduled_at").first()
    if existing is None:
        Task.objects.create(
            task_type=task_type,
            scheduled_at=timezone.now() + timedelta(seconds=delay_seconds),
            payload=payload,
        )
        return

    if existing.payload != payload:
        existing.payload = payload
        existing.save(update_fields=["payload"])


def enqueue_connect(campaign_id: int, delay_seconds: float = 10):
    if not ENABLE_CONNECT:
        return
    _enqueue_task(
        task_type=Task.TaskType.CONNECT,
        payload={"campaign_id": campaign_id},
        delay_seconds=delay_seconds,
    )


def enqueue_follow_up(
    campaign_id: int,
    public_id: str,
    *,
    operator: str,
    delay_seconds: float = 10,
    sequence_name: str | None = None,
    channel: str | None = None,
    step_index: int | None = None,
    icp: str | None = None,
):
    """Enqueue a follow_up Task.

    `operator` is the canonical handle (`linkedin.operators.resolve_operator`)
    of the LinkedIn account that owns the thread to this lead. Required:
    pending/running follow_up Tasks are ownership-scoped and invalid
    without it.
    """
    from linkedin.conf import ENABLE_FOLLOW_UP

    if not ENABLE_FOLLOW_UP:
        return
    if not operator:
        raise ValueError("enqueue_follow_up requires a non-empty operator")
    payload = {"campaign_id": campaign_id, "public_id": public_id, "operator": operator}
    if sequence_name is not None:
        payload["sequence_name"] = sequence_name
    if channel is not None:
        payload["channel"] = channel
    if step_index is not None:
        payload["step_index"] = step_index
    if icp:
        payload["icp"] = icp
    _enqueue_task(
        task_type=Task.TaskType.FOLLOW_UP,
        payload=payload,
        delay_seconds=delay_seconds,
        dedup_keys=[key for key in payload if key != "icp"],
    )
