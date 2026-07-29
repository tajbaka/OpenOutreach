"""Batch-reconcile accepted invitations through the Connections page."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from django.db import connections
from django.utils import timezone
from termcolor import colored

from linkedin.actions.connections import (
    ConnectionScrapeResult,
    scrape_connections_with_stats,
)
from linkedin.actions.conversations import get_conversation
from linkedin.conf import (
    CONNECTION_SWEEP_INCOMPLETE_RETRY_MINUTES,
    CONNECTION_SWEEP_INITIAL_LOOKBACK_DAYS,
    CONNECTION_SWEEP_INTERVAL_HOURS,
    CONNECTION_SWEEP_MAX_ROUNDS,
    CONNECTION_SWEEP_MAX_SECONDS,
    CONNECTION_SWEEP_OVERLAP_HOURS,
    ENABLE_INCREMENTAL_CONNECTION_SWEEP,
    ENABLE_SWEEP_CONNECTIONS,
)
from linkedin.db.deals import set_profile_state
from linkedin.db.urls import url_to_public_id
from linkedin.enums import ProfileState
from linkedin.models import ActionLog, Task, WorkflowRun
from linkedin.notifications.slack import (
    latest_reply_from_lead,
    notify_connection_accepted,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepReconciliationResult:
    pending_count: int
    matched_count: int
    cutoff_date: date
    scrape: ConnectionScrapeResult

    @property
    def complete(self) -> bool:
        return self.scrape.complete


def process_accepted_deal(session, deal, *, entry=None) -> None:
    """Apply the shared post-accept path for one freshly accepted Deal."""
    from crm.models import Deal
    from linkedin.tasks.connect import enqueue_follow_up, recommended_action_delay

    public_id = url_to_public_id(deal.lead.linkedin_url) if deal.lead.linkedin_url else None
    if not public_id:
        raise ValueError(f"Deal {deal.pk} has no LinkedIn public identifier")

    session.campaign = deal.campaign
    set_profile_state(session, public_id, ProfileState.CONNECTED.value)
    deal = Deal.objects.select_related("lead", "campaign").get(pk=deal.pk)

    full_name = (
        f"{deal.lead.first_name or ''} {deal.lead.last_name or ''}".strip()
        or public_id
    )
    try:
        messages = get_conversation(session, public_id)
    except Exception as e:
        logger.warning("Could not fetch conversation for %s: %s", full_name, e)
        messages = None
    reply = latest_reply_from_lead(messages, full_name)
    reply_text = reply.get("text") if reply else None

    if reply:
        ts_str = (reply.get("timestamp") or "").strip()
        if ts_str:
            try:
                naive = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
                deal.last_reply_at = timezone.make_aware(
                    naive, timezone.get_current_timezone(),
                )
                deal.save(update_fields=["last_reply_at"])
            except ValueError:
                pass

    from linkedin.operators import resolve_operator

    operator = resolve_operator(session.linkedin_profile.linkedin_username)
    try:
        notify_connection_accepted(
            full_name=full_name,
            title="",
            company=deal.lead.company_name or "",
            profile_url=deal.lead.linkedin_url
            or f"https://www.linkedin.com/in/{public_id}/",
            campaign_name=deal.campaign.name,
            reply_text=reply_text,
            operator=operator,
        )
    except Exception as e:
        logger.warning("Slack notify failed for %s: %s", full_name, e)

    delay_seconds = recommended_action_delay(
        session.linkedin_profile, ActionLog.ActionType.FOLLOW_UP,
    )
    connected_on = getattr(entry, "connected_on", None)
    if connected_on:
        age_days = (timezone.now().date() - connected_on).days
        if age_days > 0:
            logger.debug(
                "%s accepted %d day(s) ago — follow-up in %.0fs",
                public_id, age_days, delay_seconds,
            )

    from linkedin.icp_outbound import resolve_icp

    enqueue_follow_up(
        deal.campaign.pk,
        public_id,
        operator=operator,
        icp=resolve_icp(deal.lead),
        delay_seconds=delay_seconds,
    )
    from gmail.handoff import maybe_schedule_gmail_sequence

    maybe_schedule_gmail_sequence(deal=deal, operator=operator)


def _latest_successful_sweep_at(operator: str) -> datetime | None:
    latest_run = WorkflowRun.objects.filter(
        name="connection-sweep",
        operator=operator,
    ).first()
    if latest_run is not None:
        return latest_run.completed_at

    # Bootstrap the first instrumented run from the most recent legacy sweep.
    # Once new telemetry exists, incomplete runs preserve their own cutoff and
    # this fallback is no longer consulted.
    if WorkflowRun.objects.filter(
        name="connection-sweep-incomplete",
        operator=operator,
    ).exists():
        return None
    return (
        Task.objects.filter(
            task_type=Task.TaskType.SWEEP_CONNECTIONS,
            status=Task.Status.COMPLETED,
            payload__operator=operator,
            completed_at__isnull=False,
        )
        .order_by("-completed_at")
        .values_list("completed_at", flat=True)
        .first()
    )


def _latest_incomplete_cutoff(
    operator: str,
    *,
    newer_than: datetime | None,
) -> date | None:
    run = WorkflowRun.objects.filter(
        name="connection-sweep-incomplete",
        operator=operator,
    ).first()
    if run is None:
        return None
    if newer_than is not None and run.completed_at <= newer_than:
        return None
    raw = (run.counts or {}).get("cutoff_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        logger.warning("Ignoring malformed incomplete sweep cutoff %r", raw)
        return None


def _incremental_cutoff(operator: str) -> date:
    anchor = _latest_successful_sweep_at(operator)
    previous_incomplete = _latest_incomplete_cutoff(
        operator,
        newer_than=anchor,
    )
    if previous_incomplete is not None:
        return previous_incomplete

    if anchor is None:
        return (
            timezone.now()
            - timedelta(days=CONNECTION_SWEEP_INITIAL_LOOKBACK_DAYS)
        ).date()
    return (anchor - timedelta(hours=CONNECTION_SWEEP_OVERLAP_HOURS)).date()


def _legacy_cutoff(pending_deals) -> date:
    oldest_pending = min(
        deal.invitation_sent_at or deal.update_date
        for deal in pending_deals
    )
    return oldest_pending.date()


def _empty_scrape_result() -> ConnectionScrapeResult:
    return ConnectionScrapeResult(
        entries=[],
        rounds=0,
        cards_inspected=0,
        elapsed_seconds=0.0,
        stop_reason="no_pending",
        oldest_connected_on=None,
    )


def _recycle_database_connection() -> None:
    """Discard the socket that sat idle during Playwright browser work."""
    connections.close_all()
    connections["default"].ensure_connection()


def reconcile_pending_connections(session) -> SweepReconciliationResult:
    """Reconcile accepted invitations across this sender's campaigns."""
    from crm.models import Deal
    from linkedin.operators import resolve_operator

    operator = resolve_operator(session.linkedin_profile.linkedin_username)
    started = time.monotonic()

    pending_deals = list(
        Deal.objects.filter(
            state=ProfileState.PENDING,
            campaign__in=session.campaigns,
        )
        .select_related("lead", "campaign")
        .order_by("id")
    )
    if not pending_deals:
        return SweepReconciliationResult(
            pending_count=0,
            matched_count=0,
            cutoff_date=timezone.now().date(),
            scrape=_empty_scrape_result(),
        )

    cutoff = (
        _incremental_cutoff(operator)
        if ENABLE_INCREMENTAL_CONNECTION_SWEEP
        else _legacy_cutoff(pending_deals)
    )
    scrape = scrape_connections_with_stats(
        session,
        stop_before=cutoff,
        max_seconds=CONNECTION_SWEEP_MAX_SECONDS,
        max_rounds=CONNECTION_SWEEP_MAX_ROUNDS,
    )

    # Browser work can run for the whole bounded budget. Reopen the database
    # before state transitions so an idle Postgres/Neon socket cannot turn a
    # successful LinkedIn read into a failed task.
    _recycle_database_connection()
    accepted_by_pid = {entry.public_id: entry for entry in scrape.entries}

    accepted_deals = []
    for deal in pending_deals:
        public_id = url_to_public_id(deal.lead.linkedin_url) if deal.lead.linkedin_url else None
        entry = accepted_by_pid.get(public_id) if public_id else None
        if entry is not None:
            accepted_deals.append((deal, entry))

    matched = 0
    for index, (deal, entry) in enumerate(accepted_deals):
        # A single acceptance may overrun the budget while LinkedIn Messaging
        # loads, but never start another one after the total sweep budget is
        # exhausted. Remaining matches are rediscovered on the short retry.
        if index > 0 and time.monotonic() - started >= CONNECTION_SWEEP_MAX_SECONDS:
            scrape = replace(
                scrape,
                elapsed_seconds=time.monotonic() - started,
                stop_reason="max_seconds_processing",
            )
            break
        process_accepted_deal(session, deal, entry=entry)
        matched += 1
    else:
        scrape = replace(
            scrape,
            elapsed_seconds=time.monotonic() - started,
        )

    return SweepReconciliationResult(
        pending_count=len(pending_deals),
        matched_count=matched,
        cutoff_date=cutoff,
        scrape=scrape,
    )


def _record_sweep_run(
    *,
    task,
    operator: str,
    result: SweepReconciliationResult,
) -> None:
    run_name = "connection-sweep" if result.complete else "connection-sweep-incomplete"
    counts = {
        "task_id": task.pk,
        "pending": result.pending_count,
        "matched": result.matched_count,
        "entries": len(result.scrape.entries),
        "rounds": result.scrape.rounds,
        "cards_inspected": result.scrape.cards_inspected,
        "elapsed_seconds": round(result.scrape.elapsed_seconds, 3),
        "stop_reason": result.scrape.stop_reason,
        "cutoff_date": result.cutoff_date.isoformat(),
        "oldest_connected_on": (
            result.scrape.oldest_connected_on.isoformat()
            if result.scrape.oldest_connected_on
            else None
        ),
    }
    WorkflowRun.objects.create(
        name=run_name,
        operator=operator,
        summary=(
            f"{result.pending_count} pending, {result.matched_count} matched, "
            f"{len(result.scrape.entries)} entries, "
            f"stop={result.scrape.stop_reason}"
        ),
        counts=counts,
    )


def handle_sweep_connections(task, session, qualifiers):
    if not ENABLE_SWEEP_CONNECTIONS:
        logger.debug("sweep_connections disabled — skipping task %s", task.pk)
        return

    logger.info(
        "%s sweep_connections",
        colored("▶", "magenta", attrs=["bold"]),
    )
    result = reconcile_pending_connections(session)
    logger.info(
        "sweep_connections: %d pending → %d newly connected "
        "(%d entries, stop=%s)",
        result.pending_count,
        result.matched_count,
        len(result.scrape.entries),
        result.scrape.stop_reason,
    )

    from linkedin.operators import resolve_operator

    operator = resolve_operator(session.linkedin_profile.linkedin_username)
    _record_sweep_run(task=task, operator=operator, result=result)
    delay_seconds = (
        CONNECTION_SWEEP_INTERVAL_HOURS * 3600
        if result.complete
        else CONNECTION_SWEEP_INCOMPLETE_RETRY_MINUTES * 60
    )
    enqueue_sweep_connections(
        operator=operator,
        delay_seconds=delay_seconds,
    )


def enqueue_sweep_connections(*, operator: str, delay_seconds: float | None = None):
    """Ensure one pending sweep_connections task exists per operator."""
    if not ENABLE_SWEEP_CONNECTIONS:
        return
    if not operator:
        raise ValueError("enqueue_sweep_connections requires a non-empty operator")
    if delay_seconds is None:
        delay_seconds = CONNECTION_SWEEP_INTERVAL_HOURS * 3600

    if Task.objects.filter(
        task_type=Task.TaskType.SWEEP_CONNECTIONS,
        status=Task.Status.PENDING,
        payload__operator=operator,
    ).exists():
        return

    Task.objects.create(
        task_type=Task.TaskType.SWEEP_CONNECTIONS,
        scheduled_at=timezone.now() + timedelta(seconds=delay_seconds),
        payload={"operator": operator},
    )
