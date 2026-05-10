# linkedin/daemon.py
from __future__ import annotations

import logging
import time
import traceback
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import connections
from django.utils import timezone

from termcolor import colored

from linkedin.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    ACTIVE_TIMEZONE,
    CAMPAIGN_CONFIG,
    ENABLE_AUTO_DISCOVERY,
    ENABLE_FOLLOW_UP,
    ENABLE_FREEMIUM_CAMPAIGN,
    ENABLE_SWEEP_CONNECTIONS,
    ENABLE_ACTIVE_HOURS,
    REST_DAYS,
)
from linkedin.diagnostics import failure_diagnostics
from linkedin.ml.qualifier import BayesianQualifier, KitQualifier
from linkedin.models import ActionLog, Task
from linkedin.tasks.connect import (
    enqueue_connect,
    enqueue_follow_up,
    handle_connect,
    recommended_action_delay,
)
from linkedin.tasks.follow_up import handle_follow_up
from linkedin.tasks.sweep_connections import handle_sweep_connections

logger = logging.getLogger(__name__)

_HANDLERS = {
    Task.TaskType.CONNECT: handle_connect,
    Task.TaskType.FOLLOW_UP: handle_follow_up,
    Task.TaskType.SWEEP_CONNECTIONS: handle_sweep_connections,
}


class _FreemiumRotator:
    """Logs rotating freemium messages every *every* task executions."""

    _MESSAGES = [
        colored("Join the community or give direct feedback on Telegram \u2192 https://t.me/+Y5bh9Vg8UVg5ODU0", "blue",
                attrs=["bold"]),
        "\033[38;5;208;1mLove OpenOutreach? Sponsor the project \u2192 https://github.com/sponsors/eracle\033[0m",
    ]

    def __init__(self, every: int = 10):
        self._every = every
        self._ticks = 0
        self._next = 0

    def maybe_log(self):
        self._ticks += 1
        if self._ticks % self._every == 0:
            logger.info(self._MESSAGES[self._next % len(self._MESSAGES)])
            self._next += 1


def _bring_task_forward(
    task_type: str,
    payload: dict,
    scheduled_at,
    dedup_keys: list[str] | None = None,
) -> tuple[bool, bool]:
    """Ensure one pending task exists and is scheduled no later than *scheduled_at*.

    Returns ``(created, rescheduled)``.
    """
    filters = {
        "task_type": task_type,
        "status": Task.Status.PENDING,
    }
    for key in (dedup_keys if dedup_keys is not None else payload):
        value = payload[key]
        filters[f"payload__{key}"] = value

    existing = Task.objects.filter(**filters).order_by("scheduled_at").first()
    if existing is None:
        Task.objects.create(
            task_type=task_type,
            scheduled_at=scheduled_at,
            payload=payload,
        )
        return True, False

    update_fields: list[str] = []
    if existing.scheduled_at > scheduled_at:
        existing.scheduled_at = scheduled_at
        update_fields.append("scheduled_at")
    if existing.payload != payload:
        existing.payload = payload
        update_fields.append("payload")

    if update_fields:
        existing.save(update_fields=update_fields)
        return False, True

    return False, False


def _build_qualifiers(campaigns, cfg, kit_model=None):
    """Create a qualifier for every campaign, keyed by campaign PK."""
    from crm.models import Lead

    qualifiers: dict[int, BayesianQualifier | KitQualifier] = {}
    n_regular = 0
    for campaign in campaigns:
        if campaign.is_freemium:
            if kit_model is None:
                continue
            qualifiers[campaign.pk] = KitQualifier(kit_model)
        else:
            q = BayesianQualifier(
                seed=42,
                n_mc_samples=cfg["qualification_n_mc_samples"],
                campaign=campaign,
            )
            X, y = Lead.get_labeled_arrays(campaign)
            if len(X) > 0:
                q.warm_start(X, y)
                logger.info(
                    colored("GP qualifier warm-started", "cyan")
                    + " on %d labelled samples (%d positive, %d negative)"
                    + " for campaign %s",
                    len(y), int((y == 1).sum()), int((y == 0).sum()), campaign,
                )
            qualifiers[campaign.pk] = q
            n_regular += 1

    return qualifiers


# ------------------------------------------------------------------
# Active-hours schedule guard
# ------------------------------------------------------------------


def seconds_until_active() -> float:
    """Return seconds to wait before the next active window, or 0 if active now."""
    if not ENABLE_ACTIVE_HOURS:
        return 0.0
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)

    if now.weekday() not in REST_DAYS and ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR:
        return 0.0

    # Find the next active start: try today first, then subsequent days
    candidate = timezone.make_aware(
        now.replace(hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0, tzinfo=None),
        timezone=tz,
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() in REST_DAYS:
        candidate += timedelta(days=1)
    return (candidate - now).total_seconds()


# ------------------------------------------------------------------
# Task queue worker
# ------------------------------------------------------------------


def heal_tasks(session):
    """Reconcile task queue with CRM state on daemon startup.

    1. Reset stale 'running' tasks to 'pending' (crashed worker recovery)
    2. Seed one 'connect' task per campaign if none pending
    3. Create 'check_pending' tasks for PENDING profiles without tasks
    4. Create 'follow_up' tasks for CONNECTED profiles without tasks
    """
    from crm.models import Deal
    from linkedin.db.urls import url_to_public_id
    from linkedin.enums import ProfileState
    from linkedin.models import Campaign

    # 1. Recover stale running tasks
    stale_count = Task.objects.filter(status=Task.Status.RUNNING).update(
        status=Task.Status.PENDING,
    )
    if stale_count:
        logger.info("Recovered %d stale running tasks", stale_count)

    if not ENABLE_FREEMIUM_CAMPAIGN:
        disabled_campaign_ids = list(
            Campaign.objects.filter(users=session.django_user, is_freemium=True)
            .values_list("pk", flat=True),
        )
        if disabled_campaign_ids:
            disabled_tasks = Task.objects.filter(
                payload__campaign_id__in=disabled_campaign_ids,
                status=Task.Status.PENDING,
            ).update(
                status=Task.Status.FAILED,
                error="Freemium campaign disabled",
            )
            if disabled_tasks:
                logger.info("Disabled %d pending freemium tasks", disabled_tasks)

    # When auto-discovery is disabled, the qualifier never promotes leads,
    # so any seed import that landed in QUALIFIED would sit there forever.
    # Force them all up to READY_TO_CONNECT so the connect lane can pick
    # them up directly.
    if not ENABLE_AUTO_DISCOVERY:
        from crm.models import Deal
        promoted = Deal.objects.filter(
            state=ProfileState.QUALIFIED,
            campaign__in=session.campaigns,
        ).update(state=ProfileState.READY_TO_CONNECT)
        if promoted:
            logger.info(
                "ENABLE_AUTO_DISCOVERY=false — bulk-promoted %d QUALIFIED → READY_TO_CONNECT",
                promoted,
            )

    # 2. Seed connect tasks per campaign. Bring any existing pending task
    # forward to now so a stale long-delayed task from a prior run doesn't
    # leave the daemon idle right after startup. Subsequent connects self-pace
    # via recommended_action_delay() in handle_connect's reschedule path.
    for campaign in session.campaigns:
        _bring_task_forward(
            Task.TaskType.CONNECT,
            {"campaign_id": campaign.pk},
            timezone.now(),
        )

    # 3. Cancel any legacy per-profile check_pending tasks — superseded by the
    # bulk sweep_connections sweep.
    legacy = Task.objects.filter(
        task_type=Task.TaskType.CHECK_PENDING,
        status=Task.Status.PENDING,
    ).update(status=Task.Status.COMPLETED)
    if legacy:
        logger.info("Retired %d legacy check_pending tasks", legacy)

    # 4. Sweep tasks (acceptance detection — independent of follow-up DMs).
    if ENABLE_SWEEP_CONNECTIONS:
        # Bring forward one sweep_connections task so it runs first on startup.
        _bring_task_forward(
            Task.TaskType.SWEEP_CONNECTIONS,
            {},
            timezone.now(),
            dedup_keys=[],
        )
    else:
        cancelled_sweep = Task.objects.filter(
            task_type=Task.TaskType.SWEEP_CONNECTIONS,
            status=Task.Status.PENDING,
        ).update(status=Task.Status.COMPLETED)
        if cancelled_sweep:
            logger.info(
                "ENABLE_SWEEP_CONNECTIONS=false — cancelled %d pending sweep tasks",
                cancelled_sweep,
            )

    # 5. Follow-up tasks (post-accept DMs — gated separately).
    if not ENABLE_FOLLOW_UP:
        cancelled_fu = Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.PENDING,
        ).update(status=Task.Status.COMPLETED)
        if cancelled_fu:
            logger.info(
                "ENABLE_FOLLOW_UP=false — cancelled %d pending follow-up tasks",
                cancelled_fu,
            )
        pending_count = Task.objects.pending().count()
        logger.info("Task queue healed: %d pending tasks", pending_count)
        return

    # Follow_up tasks for CONNECTED profiles. If the worker was down when a
    # lead accepted, make sure those follow-ups get a prompt retry on startup.
    for campaign in session.campaigns:
        session.campaign = campaign
        connected_deals = Deal.objects.filter(
            state=ProfileState.CONNECTED,
            campaign=campaign,
        ).select_related("lead").order_by("update_date", "id")

        created = 0
        rescheduled = 0
        base_time = timezone.now()

        for index, deal in enumerate(connected_deals):
            public_id = url_to_public_id(deal.lead.linkedin_url) if deal.lead.linkedin_url else None
            if not public_id:
                continue
            target_time = base_time + timedelta(seconds=index * 30)
            was_created, was_rescheduled = _bring_task_forward(
                Task.TaskType.FOLLOW_UP,
                {
                    "campaign_id": campaign.pk,
                    "public_id": public_id,
                },
                target_time,
            )
            created += int(was_created)
            rescheduled += int(was_rescheduled)
        if created or rescheduled:
            logger.info(
                "[%s] follow-up catch-up queued: %d created, %d rescheduled",
                campaign,
                created,
                rescheduled,
            )

    pending_count = Task.objects.pending().count()
    logger.info("Task queue healed: %d pending tasks", pending_count)


def run_daemon(session):
    from linkedin.ml.hub import fetch_kit
    from linkedin.setup.freemium import import_freemium_campaign
    from linkedin.models import Campaign

    cfg = CAMPAIGN_CONFIG

    # Load kit model for freemium campaigns
    kit = fetch_kit() if ENABLE_FREEMIUM_CAMPAIGN else None
    if kit:
        freemium_campaign = import_freemium_campaign(kit["config"])
        if freemium_campaign:
            prev_campaign = session.campaign
            session.campaign = freemium_campaign
            from linkedin.setup.freemium import seed_profiles
            seed_profiles(session, kit["config"])
            session.campaign = prev_campaign
    elif not ENABLE_FREEMIUM_CAMPAIGN:
        logger.info("Freemium campaign disabled")

    qualifiers = _build_qualifiers(
        session.campaigns, cfg, kit_model=kit["model"] if kit else None,
    )

    # Startup healing
    heal_tasks(session)

    campaigns = list(session.campaigns)
    if not campaigns:
        logger.error("No campaigns found — cannot start daemon")
        return

    logger.info(
        colored("Daemon started", "green", attrs=["bold"])
        + " — %d campaigns, task queue worker",
        len(campaigns),
    )

    freemium = _FreemiumRotator(every=2)

    # Single-threaded: one task at a time, no concurrent enqueuing,
    # so sleeping until the next scheduled_at is safe.
    while True:
        # Close stale DB connections at the top of every loop iteration.
        # Neon's idle timeout can kill the SSL socket during any sleep.
        connections.close_all()

        pause = seconds_until_active()
        if pause > 0:
            h, m = int(pause // 3600), int(pause % 3600 // 60)
            logger.info("Outside active hours — sleeping %dh%02dm", h, m)
            connections.close_all()
            time.sleep(pause)
            continue

        task = Task.objects.claim_next()
        if task is None:
            wait = Task.objects.seconds_to_next()
            if wait is None:
                logger.info("Queue empty — nothing to do")
                return
            if wait > 0:
                h, m = int(wait // 3600), int(wait % 3600 // 60)
                logger.info("Next task in %dh%02dm — sleeping", h, m)
                connections.close_all()
                time.sleep(wait)
            continue

        # Account-wide tasks (e.g. sweep_connections) span all campaigns and
        # don't carry a campaign_id; the handler sets session.campaign as needed.
        if task.task_type == Task.TaskType.SWEEP_CONNECTIONS:
            session.campaign = session.campaigns.first()
        else:
            campaign = Campaign.objects.filter(pk=task.payload.get("campaign_id")).first()
            if not campaign:
                task.mark_failed(f"Campaign {task.payload.get('campaign_id')} not found")
                continue
            session.campaign = campaign

        task.mark_running()

        handler = _HANDLERS.get(task.task_type)
        if handler is None:
            task.mark_failed(f"Unknown task type: {task.task_type}")
            continue

        try:
            with failure_diagnostics(session):
                handler(task, session, qualifiers)
        except Exception:
            task.mark_failed(traceback.format_exc())
            logger.exception("Task %s failed", task)
            # Self-rescheduling tasks (connect) never reach their own
            # reschedule path on crash.  Re-seed so the queue doesn't stall.
            if task.task_type == Task.TaskType.CONNECT:
                from linkedin.tasks.connect import enqueue_connect
                cid = task.payload.get("campaign_id")
                if cid:
                    enqueue_connect(cid, delay_seconds=60)
            continue

        task.mark_completed()
        freemium.maybe_log()
