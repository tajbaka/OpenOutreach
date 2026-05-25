# linkedin/tasks/connect.py
"""Connect task — pulls one candidate, connects, self-reschedules.

Works for both regular and freemium campaigns via ConnectStrategy.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
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
    ENABLE_CONNECT,
    ENABLE_PACING_CATCH_UP,
    OUR_COMPANY_NAME,
    PACING_CATCH_UP_MAX_SPEED_MULTIPLIER,
    OUR_WEBSITE_URL,
)
from linkedin.db.deals import increment_connect_attempts, set_profile_state
from linkedin.db.leads import disqualify_lead
from linkedin.models import ActionLog, ConnectIssueLog, Task, log_connect_issue
from linkedin.enums import ProfileState
from linkedin.exceptions import ReachedConnectionLimit, SkipProfile
from linkedin.operators import resolve_operator

logger = logging.getLogger(__name__)

MAX_CONNECT_ATTEMPTS = 3


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
    from linkedin.icp_outbound import load_icp_messages, resolve_icp

    lead = Lead.objects.filter(pk=lead_id).first() if lead_id else None
    first_name = lead.first_name.strip() if lead and lead.first_name else ""

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
                        company_name=(lead.company_name or "").strip(),
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


def _active_window_progress_seconds(profile, action_type: str) -> tuple[float, float, float]:
    """Return (remaining, normal_window, effective_window) seconds in ACTIVE_TIMEZONE.

    The normal window is ACTIVE_START_HOUR → ACTIVE_END_HOUR. When spillover is
    enabled and the sender is behind pace, the effective window may extend past
    ACTIVE_END_HOUR so pacing can use that extra time instead of overcompressing
    inside the main window. Outside the effective window this returns the
    effective window length as the remaining seconds so callers fall back to the
    configured average pace.
    """
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)
    normal_window_seconds = max((ACTIVE_END_HOUR - ACTIVE_START_HOUR) * 3600, 3600)
    use_spillover = (
        ENABLE_PACING_CATCH_UP
        and ENABLE_ACTIVE_HOURS_SPILLOVER
        and ACTIVE_SPILLOVER_END_HOUR > ACTIVE_END_HOUR
        and _is_behind_normal_window_pace(profile, action_type)
    )
    effective_end_hour = ACTIVE_SPILLOVER_END_HOUR if use_spillover else ACTIVE_END_HOUR
    effective_window_seconds = max((effective_end_hour - ACTIVE_START_HOUR) * 3600, 3600)

    if not (ACTIVE_START_HOUR <= now.hour < effective_end_hour):
        return float(effective_window_seconds), float(normal_window_seconds), float(effective_window_seconds)

    end = now.replace(hour=effective_end_hour, minute=0, second=0, microsecond=0)
    remaining_seconds = max((end - now).total_seconds(), 1.0)
    return remaining_seconds, float(normal_window_seconds), float(effective_window_seconds)


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


def _is_behind_normal_window_pace(profile, action_type: str) -> bool:
    """Whether the sender is behind the normal ACTIVE_START→ACTIVE_END pace."""
    if action_type == ActionLog.ActionType.CONNECT:
        daily_limit = max(CONNECT_DAILY_LIMIT or profile.connect_daily_limit or 1, 1)
    else:
        daily_limit = max(FOLLOW_UP_DAILY_LIMIT or profile.follow_up_daily_limit or 1, 1)

    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)
    normal_window_seconds = max((ACTIVE_END_HOUR - ACTIVE_START_HOUR) * 3600, 3600)
    start = now.replace(hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0)
    end = now.replace(hour=ACTIVE_END_HOUR, minute=0, second=0, microsecond=0)
    if now <= start:
        expected_by_now = 0.0
    elif now >= end:
        expected_by_now = float(daily_limit)
    else:
        elapsed = max((now - start).total_seconds(), 0.0)
        expected_by_now = daily_limit * (elapsed / normal_window_seconds)

    sent_today = _actions_sent_today(profile, action_type)
    return sent_today < expected_by_now


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
    from linkedin.conf import CONNECT_DAILY_LIMIT, FOLLOW_UP_DAILY_LIMIT

    if action_type == ActionLog.ActionType.CONNECT:
        daily_limit = max(CONNECT_DAILY_LIMIT or profile.connect_daily_limit or 1, 1)
    else:
        daily_limit = max(FOLLOW_UP_DAILY_LIMIT or profile.follow_up_daily_limit or 1, 1)

    sent_today = _actions_sent_today(profile, action_type)
    remaining_actions = max(daily_limit - sent_today, 1)
    remaining_window_seconds, normal_window_seconds, effective_window_seconds = _active_window_progress_seconds(profile, action_type)

    # Dynamic target based on what is left in today's window. By default we
    # keep a floor at the full-window average so sends do not over-accelerate
    # early in the day. When ENABLE_PACING_CATCH_UP is on, allow catch-up,
    # but cap it so the sender runs only modestly faster than the original
    # window-average pace instead of trying to cram the full quota into a
    # short remainder of the day.
    full_window_average = normal_window_seconds / daily_limit
    effective_window_average = effective_window_seconds / daily_limit
    dynamic_average = remaining_window_seconds / remaining_actions
    if ENABLE_PACING_CATCH_UP:
        max_speed = max(PACING_CATCH_UP_MAX_SPEED_MULTIPLIER, 1.0)
        fastest_allowed_delay = full_window_average / max_speed
        base_delay = max(dynamic_average, fastest_allowed_delay)
    else:
        base_delay = max(effective_window_average, dynamic_average)
    return max(
        CAMPAIGN_CONFIG["min_action_interval"],
        random.uniform(base_delay * 0.7, base_delay * 1.3),
    )


def handle_connect(task, session, qualifiers):
    from linkedin.actions.connect import send_connection_request
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
        enqueue_connect(campaign_id, delay_seconds=_seconds_until_tomorrow())
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
    from crm.models import Deal

    deal = Deal.objects.filter(
        lead__linkedin_url=public_id_to_url(public_id),
        campaign=session.campaign,
    ).first()
    reason = deal.reason if deal else ""
    stats = strategy.qualifier.explain(candidate, session) if strategy.qualifier else ""
    logger.info("[%s] %s", campaign, colored("\u25b6 connect", "cyan", attrs=["bold"]))
    logger.info("[%s] %s (%s) — %s", campaign, public_id, stats, reason or "")

    try:
        status = get_connection_status(session, profile)

        if status == ProfileState.CONNECTED:
            set_profile_state(session, public_id, status.value)
            enqueue_follow_up(
                campaign_id,
                public_id,
                operator=resolve_operator(session.linkedin_profile.linkedin_username),
                delay_seconds=recommended_action_delay(
                    session.linkedin_profile, ActionLog.ActionType.FOLLOW_UP,
                ),
            )
            # Already-connected profiles are effectively "no connect work
            # done" from this lane's perspective, so keep moving instead of
            # consuming the normal connect pacing budget.
            enqueue_connect(campaign_id, delay_seconds=0)
            return

        if status == ProfileState.PENDING:
            set_profile_state(session, public_id, status.value)
            enqueue_sweep_connections()
            # No action taken — short delay before next candidate
            enqueue_connect(campaign_id, delay_seconds=10)
            return

        note = build_connection_note(
            candidate.get("lead_id"),
            sender=resolve_operator(session.linkedin_profile.linkedin_username),
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
        else:
            set_profile_state(session, public_id, new_state.value)
            session.linkedin_profile.record_action(
                ActionLog.ActionType.CONNECT, session.campaign,
            )

            if new_state == ProfileState.PENDING:
                Deal.objects.filter(
                    lead__linkedin_url=public_id_to_url(public_id),
                    campaign=session.campaign,
                ).update(sent_note=note)
                enqueue_sweep_connections()
            elif new_state == ProfileState.CONNECTED:
                enqueue_follow_up(
                    campaign_id,
                    public_id,
                    operator=resolve_operator(session.linkedin_profile.linkedin_username),
                    delay_seconds=recommended_action_delay(
                        session.linkedin_profile, ActionLog.ActionType.FOLLOW_UP,
                    ),
                )

    except ReachedConnectionLimit as e:
        logger.warning("Rate limited: %s", e)
        session.linkedin_profile.mark_exhausted(ActionLog.ActionType.CONNECT)
        enqueue_connect(campaign_id, delay_seconds=_seconds_until_tomorrow())
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

    if not Task.objects.filter(**filter_kwargs).exists():
        Task.objects.create(
            task_type=task_type,
            scheduled_at=timezone.now() + timedelta(seconds=delay_seconds),
            payload=payload,
        )


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
    _enqueue_task(
        task_type=Task.TaskType.FOLLOW_UP,
        payload={"campaign_id": campaign_id, "public_id": public_id, "operator": operator},
        delay_seconds=delay_seconds,
    )
