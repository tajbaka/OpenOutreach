# linkedin/daemon.py
from __future__ import annotations

import logging
import time
import traceback
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import connections
from django.db.utils import InterfaceError, OperationalError
from django.utils import timezone

from termcolor import colored

from linkedin.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    ACTIVE_TIMEZONE,
    CAMPAIGN_CONFIG,
    CONNECT_LOW_POOL_THRESHOLD,
    ENABLE_AUTO_DISCOVERY,
    ENABLE_CONNECT,
    ENABLE_FOLLOW_UP,
    ENABLE_FREEMIUM_CAMPAIGN,
    ENABLE_AUTO_PHONE_ENRICHMENT,
    ENABLE_NODE_MONITOR,
    ENABLE_REALTIME_LISTENER,
    ENABLE_SWEEP_CONNECTIONS,
    ENABLE_ACTIVE_HOURS,
    ENABLE_PACING_CATCH_UP,
    ENABLE_PROFILE_DISCOVERY,
    ENRICHMENT_WAIT_POLL_SECONDS,
    LISTENER_ACTIVE_END_HOUR,
    LISTENER_ACTIVE_START_HOUR,
    LISTENER_REST_DAYS,
    MANUAL_REPLY_POLL_SECONDS,
    REST_DAYS,
    TASK_RUNNING_STALE_MINUTES,
)
from linkedin.diagnostics import failure_diagnostics
from linkedin.ml.qualifier import BayesianQualifier, KitQualifier
from linkedin.models import ActionLog, Task
from linkedin.notifications.slack import notify_degraded, notify_error
from linkedin.tasks.connect import (
    enqueue_connect,
    enqueue_follow_up,
    handle_connect,
    recommended_action_delay,
    _is_behind_normal_window_pace,
)
from linkedin.tasks.follow_up import handle_follow_up
from linkedin.tasks.discovery import handle_discovery
from linkedin.tasks.manual_reply import handle_manual_reply
from linkedin.tasks.status_summary import enqueue_status_summary, handle_status_summary
from linkedin.tasks.sweep_connections import handle_sweep_connections

logger = logging.getLogger(__name__)

_HANDLERS = {
    Task.TaskType.CONNECT: handle_connect,
    Task.TaskType.FOLLOW_UP: handle_follow_up,
    Task.TaskType.MANUAL_REPLY: handle_manual_reply,
    Task.TaskType.SWEEP_CONNECTIONS: handle_sweep_connections,
    Task.TaskType.STATUS_SUMMARY: handle_status_summary,
    Task.TaskType.DISCOVERY: handle_discovery,
}

_LOW_POOL_ALERTED: set[int] = set()


def _active_campaigns(session):
    """Campaigns this daemon should actively work."""
    from linkedin.models import Campaign

    return session.campaigns.filter(status=Campaign.Status.ACTIVE)


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


def seconds_until_active(profile=None) -> float:
    """Return seconds to the next outbound or discovery window."""
    if not ENABLE_ACTIVE_HOURS:
        return 0.0
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)
    is_workday = now.weekday() not in REST_DAYS
    is_normal_active = ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR
    is_catch_up_active = bool(_catch_up_task_types(profile, now=now))
    from linkedin.discovery.config import (
        discovery_window_open,
        next_discovery_window_start,
    )
    is_discovery_active = discovery_window_open(now)

    if (is_workday and (is_normal_active or is_catch_up_active)) or is_discovery_active:
        return 0.0

    waits = [_seconds_until_next_active_start(now)]
    discovery_start = next_discovery_window_start(now)
    if discovery_start is not None:
        waits.append(max((discovery_start - now).total_seconds(), 0))
    return min(waits)


def _seconds_until_next_active_start(now=None) -> float:
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = now or timezone.localtime(timezone=tz)
    candidate = timezone.make_aware(
        now.replace(hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0, tzinfo=None),
        timezone=tz,
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() in REST_DAYS:
        candidate += timedelta(days=1)
    return (candidate - now).total_seconds()


def _catch_up_task_types(profile=None, *, now=None) -> set[str]:
    """Task types allowed during after-hours catch-up.

    Empty set means no catch-up lane is active. During normal active hours
    the daemon can claim all task types; this helper is only for the
    after-hours exception.
    """
    if not ENABLE_ACTIVE_HOURS or not ENABLE_PACING_CATCH_UP or profile is None:
        return set()
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = now or timezone.localtime(timezone=tz)
    if now.weekday() in REST_DAYS:
        return set()
    if now.hour < ACTIVE_END_HOUR:
        return set()

    task_types: set[str] = set()
    if _is_behind_normal_window_pace(profile, ActionLog.ActionType.CONNECT):
        task_types.add(Task.TaskType.CONNECT)
    if _is_behind_normal_window_pace(profile, ActionLog.ActionType.FOLLOW_UP):
        task_types.add(Task.TaskType.FOLLOW_UP)
    return task_types


def _claimable_task_types_now(profile=None):
    """Return None for outbound mode, or the restricted off-hours lanes."""
    if not ENABLE_ACTIVE_HOURS:
        return None
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)
    if now.weekday() not in REST_DAYS and ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR:
        return None
    catch_up = _catch_up_task_types(profile, now=now)
    if catch_up:
        return catch_up
    from linkedin.discovery.config import discovery_window_open
    if discovery_window_open(now):
        return {Task.TaskType.DISCOVERY}
    return set()


def listener_should_run_now(*, now=None) -> bool:
    """Whether the realtime listener is allowed under its own schedule."""
    if not ENABLE_REALTIME_LISTENER:
        return False
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = now or timezone.localtime(timezone=tz)
    if now.weekday() in LISTENER_REST_DAYS:
        return False
    return LISTENER_ACTIVE_START_HOUR <= now.hour < LISTENER_ACTIVE_END_HOUR


def _sync_listener_supervisor(listener_supervisor) -> None:
    if listener_should_run_now():
        listener_supervisor.ensure_running()
    else:
        listener_supervisor.stop()


# ------------------------------------------------------------------
# Task queue worker
# ------------------------------------------------------------------


def heal_tasks(session):
    """Reconcile task queue with CRM state on daemon startup.

    1. Recover only this sender's stale running browser tasks
    2. Seed one 'connect' task per campaign if none pending
    3. Create 'check_pending' tasks for PENDING profiles without tasks
    4. Create 'follow_up' tasks for CONNECTED profiles without tasks
    """
    from crm.models import Deal
    from django.db.models import Q
    from linkedin.db.urls import url_to_public_id
    from linkedin.enums import ProfileState
    from linkedin.models import Campaign
    from linkedin.operators import resolve_operator
    our_operator = resolve_operator(session.linkedin_profile.linkedin_username)
    owned_campaign_ids = list(
        session.campaigns.values_list("pk", flat=True),
    )
    stale_before = timezone.now() - timedelta(minutes=TASK_RUNNING_STALE_MINUTES)

    # 1. Recover only stale browser tasks owned by this sender. The previous
    # global reset let an Arian restart flip a healthy Chuka sweep back to
    # pending. Enrichment and Gmail workers retain their own recovery paths.
    stale_count = (
        Task.objects.filter(
            status=Task.Status.RUNNING,
        )
        .filter(Q(started_at__lt=stale_before) | Q(started_at__isnull=True))
        .owned_linkedin_by(our_operator, owned_campaign_ids)
        .update(status=Task.Status.PENDING, started_at=None)
    )
    if stale_count:
        logger.info(
            "Recovered %d stale running browser task(s) for %s",
            stale_count,
            our_operator,
        )

    if not ENABLE_FREEMIUM_CAMPAIGN:
        disabled_campaign_ids = list(
            Campaign.objects.filter(user=session.django_user, is_freemium=True)
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
            campaign__in=_active_campaigns(session),
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
    for campaign in _active_campaigns(session):
        if _campaign_has_connect_work(campaign):
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
        retired_legacy_sweeps = Task.objects.filter(
            task_type=Task.TaskType.SWEEP_CONNECTIONS,
            status=Task.Status.PENDING,
            payload={},
        ).update(status=Task.Status.COMPLETED, completed_at=timezone.now())
        if retired_legacy_sweeps:
            logger.info(
                "Retired %d legacy unscoped sweep_connections task(s)",
                retired_legacy_sweeps,
            )

        # Make this account's sweep eligible on startup. Queue fairness lets
        # due delivery tasks run first unless maintenance has exceeded its
        # maximum queue delay; the sweep itself is runtime-bounded.
        _bring_task_forward(
            Task.TaskType.SWEEP_CONNECTIONS,
            {"operator": our_operator},
            timezone.now(),
            dedup_keys=["operator"],
        )
    else:
        cancelled_sweep = Task.objects.filter(
            task_type=Task.TaskType.SWEEP_CONNECTIONS,
            status=Task.Status.PENDING,
            payload__operator=our_operator,
        ).update(status=Task.Status.COMPLETED, completed_at=timezone.now())
        if cancelled_sweep:
            logger.info(
                "ENABLE_SWEEP_CONNECTIONS=false — cancelled %d pending sweep tasks",
                cancelled_sweep,
            )

    # 5. Hourly all-sender Slack status summary. Account-agnostic: any daemon
    # may claim it, then it self-reschedules for the next hour.
    enqueue_status_summary(delay_seconds=0, since=timezone.now() - timedelta(hours=1))

    # 6. Standalone profile discovery. Reconcile one future/current task for
    # this sender. It is claimed only in the separate discovery windows.
    from linkedin.discovery.collector import reconcile_discovery_tasks

    if reconcile_discovery_tasks(session.linkedin_profile, our_operator):
        logger.info("Profile discovery task ready for %s", our_operator)

    # 7. Follow-up tasks (post-accept DMs — gated separately).
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
    # Tasks owned by other operators are skipped so the catch-up only enqueues
    # work this daemon's account can actually do.
    from linkedin.db.messages import lead_outbound_operators
    from linkedin.icp_outbound import resolve_icp
    for campaign in _active_campaigns(session):
        session.campaign = campaign
        connected_deals = Deal.objects.filter(
            state=ProfileState.CONNECTED,
            campaign=campaign,
        ).select_related("lead").order_by("update_date", "id")

        created = 0
        rescheduled = 0
        skipped_other_operator = 0
        base_time = timezone.now()

        for index, deal in enumerate(connected_deals):
            public_id = url_to_public_id(deal.lead.linkedin_url) if deal.lead.linkedin_url else None
            if not public_id:
                continue
            # Owner-scoping: skip leads whose outbound thread belongs to a
            # different operator. Empty owner set = freshly swept, no
            # outbound yet — fair game for whichever daemon picks it up.
            owners = lead_outbound_operators(deal.lead)
            if owners and our_operator not in owners:
                skipped_other_operator += 1
                continue
            target_time = base_time + timedelta(seconds=index * 30)
            was_created, was_rescheduled = _bring_task_forward(
                Task.TaskType.FOLLOW_UP,
                {
                    "campaign_id": campaign.pk,
                    "public_id": public_id,
                    "operator": our_operator,
                    "icp": resolve_icp(deal.lead),
                },
                target_time,
                dedup_keys=["campaign_id", "public_id", "operator"],
            )
            created += int(was_created)
            rescheduled += int(was_rescheduled)
        if skipped_other_operator:
            logger.info(
                "[%s] follow-up catch-up: skipped %d lead(s) owned by other operators",
                campaign, skipped_other_operator,
            )
        if created or rescheduled:
            logger.info(
                "[%s] follow-up catch-up queued: %d created, %d rescheduled",
                campaign,
                created,
                rescheduled,
            )

    pending_count = Task.objects.pending().count()
    logger.info("Task queue healed: %d pending tasks", pending_count)


# DB error classes that mean "the SSL socket is dead, you need a fresh
# connection." Neon's idle-timeout disconnect surfaces as both depending on
# where in the request lifecycle the kill lands.
_DB_DEAD_ERRORS = (OperationalError, InterfaceError)


def _safe_mark_failed(task, error_text: str) -> bool:
    """`task.mark_failed` that survives a dead Neon connection.

    Returns True if mark_failed succeeded, False if both attempts failed.
    On False, the task is left in `RUNNING` and gets healed back to
    `PENDING` on next daemon startup by `heal_tasks` — operationally safe,
    just costs one stale-task recovery line on the next launch.
    """
    try:
        task.mark_failed(error_text)
        return True
    except _DB_DEAD_ERRORS as e:
        logger.warning(
            "mark_failed for task %s hit a dead DB conn (%s) — recycling "
            "and retrying once", task.id, e.__class__.__name__,
        )
        try:
            connections.close_all()
            task.refresh_from_db()
            task.mark_failed(error_text)
            return True
        except Exception:
            logger.exception(
                "mark_failed retry also failed for task %s — leaving it "
                "RUNNING for heal_tasks to recover on next start", task.id,
            )
            return False


def _format_task_error(exc: BaseException) -> str:
    """Return traceback text plus any diagnostic artifact path."""
    error_text = traceback.format_exc()
    diagnostics_path = getattr(exc, "diagnostics_path", "")
    if diagnostics_path:
        error_text = f"{error_text}\nDiagnostics: {diagnostics_path}"
    return error_text


def _campaign_has_connect_work(campaign) -> bool:
    """Whether a campaign still has leads the connect lane can process."""
    if ENABLE_AUTO_DISCOVERY:
        # The connect lane can search → qualify → promote, so an empty local
        # ready pool is not proof that the campaign is exhausted.
        return True
    return _campaign_connectable_count(campaign) > 0


def _campaign_connectable_count(campaign) -> int:
    """Count leads the connect lane can process for this campaign."""
    from crm.models import Deal
    from linkedin.enums import ProfileState

    states = [ProfileState.READY_TO_CONNECT]
    # heal_tasks bulk-promotes these on startup, but include QUALIFIED as a
    # belt-and-suspenders guard for live imports between heal cycles.
    states.append(ProfileState.QUALIFIED)

    return Deal.objects.filter(
        campaign=campaign,
        lead__disqualified=False,
        state__in=states,
    ).count()


def _ensure_connect_task_for_campaign(campaign, delay_seconds: float = 0) -> bool:
    """Create a missing pending connect task for an active campaign.

    Normal operation self-reschedules inside ``handle_connect``. This is a
    recovery net for the rare case where that chain breaks while the daemon is
    still alive; otherwise the worker can sleep until an unrelated sweep even
    though the campaign still has ready leads.
    """
    if not ENABLE_CONNECT or not _campaign_has_connect_work(campaign):
        return False

    if Task.objects.filter(
        task_type=Task.TaskType.CONNECT,
        status__in=[Task.Status.PENDING, Task.Status.RUNNING],
        payload__campaign_id=campaign.pk,
    ).exists():
        return False

    enqueue_connect(campaign.pk, delay_seconds=delay_seconds)
    return True


def _notify_connect_queue_recovered(sender: str, campaign) -> None:
    notify_degraded(
        sender=sender,
        title=f"{sender}'s connect queue recovered",
        detail=(
            f"Campaign {campaign.pk} ({campaign.name}) still had connectable "
            "leads but no pending connect task. A recovery connect task was "
            "queued automatically."
        ),
    )


def _maybe_alert_low_connect_pool(sender: str, campaign) -> None:
    if (
        ENABLE_AUTO_DISCOVERY
        or CONNECT_LOW_POOL_THRESHOLD <= 0
        or campaign.pk in _LOW_POOL_ALERTED
    ):
        return

    remaining = _campaign_connectable_count(campaign)
    if remaining > CONNECT_LOW_POOL_THRESHOLD:
        return

    _LOW_POOL_ALERTED.add(campaign.pk)
    notify_degraded(
        sender=sender,
        title=f"{sender}'s connect pool is low",
        detail=(
            f"Campaign {campaign.pk} ({campaign.name}) has {remaining} "
            f"connectable lead(s) remaining "
            f"(threshold {CONNECT_LOW_POOL_THRESHOLD}). Add more leads soon."
        ),
    )


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

    # Realtime listener startup catch-up — surface (and optionally backfill)
    # the window the listener was off (off-hours + any downtime). Runs
    # before the task loop; reads the heartbeat file written by the
    # listener's pump. No-op when the listener is disabled.
    if ENABLE_REALTIME_LISTENER:
        from linkedin.realtime.catchup import run_startup_catchup
        run_startup_catchup(
            username=session.linkedin_profile.linkedin_username,
            account_label="primary",
        )

    # Startup healing
    heal_tasks(session)

    campaigns = list(_active_campaigns(session))
    from linkedin.discovery.collector import discovery_enabled_for_sender
    from linkedin.operators import resolve_operator

    our_operator = resolve_operator(session.linkedin_profile.linkedin_username)
    discovery_enabled = discovery_enabled_for_sender(
        session.linkedin_profile,
        our_operator,
    )
    if not campaigns and not discovery_enabled:
        logger.error("No active campaigns found — cannot start daemon")
        return

    # Operator scoping — derived once at startup. Passed to Task.claim_next
    # and seconds_to_next so this daemon never pops a follow_up Task for
    # another account (Travis incident, 2026-05-12) or a connect Task for
    # a campaign it doesn't own (cross-account connect leak, 2026-05-19).
    # The canonical handle lookup also handles the case where
    # LINKEDIN_USERNAME and the LinkedInProfile row use different surface
    # forms ("ariantajbakh@gmail.com" vs "Arian Taj" etc.).
    our_campaign_ids = [c.pk for c in campaigns]

    logger.info(
        colored("Daemon started", "green", attrs=["bold"])
        + " — %d campaigns, task queue worker, operator=%s (%s)",
        len(campaigns), our_operator, session.linkedin_profile.linkedin_username,
    )

    freemium = _FreemiumRotator(every=2)

    # Realtime listener supervisor — owns the listener child process.
    from linkedin.realtime.supervisor import ListenerSupervisor
    listener_supervisor = ListenerSupervisor()

    # Phone-enrichment worker — a background thread claiming enrich_phone
    # tasks. HTTP-only, so (unlike the listener) it is NOT gated on active
    # hours; it runs whenever the daemon is up.
    # Always spawn the enrichment worker — the Slack select menu is always
    # available, so enrich_phone tasks must always be processable. The worker
    # is a cheap idle DB poll when no tasks exist.
    from linkedin.enrichment.worker import EnrichmentWorker
    enrichment_worker = EnrichmentWorker()
    enrichment_worker.start()

    from gmail.worker import GmailWorker
    gmail_worker = GmailWorker(operator=our_operator)
    gmail_worker.start()

    # Node monitoring — a background thread that beats this daemon's
    # DaemonHeartbeat row and watches peers, plus an in-process
    # consecutive-failure tracker for the dispatch loop. Both alert the ops
    # Slack channel. Gated by ENABLE_NODE_MONITOR.
    from linkedin.monitoring import (
        NodeMonitor,
        TaskFailureTracker,
        clear_heartbeat,
    )
    node_monitor = NodeMonitor(our_operator) if ENABLE_NODE_MONITOR else None
    if node_monitor is not None:
        node_monitor.start()
    failure_tracker = TaskFailureTracker(our_operator)

    # Single-threaded: one task at a time, no concurrent enqueuing,
    # so sleeping until the next scheduled_at is safe.
    while True:
        # Close stale DB connections at the top of every loop iteration.
        # Neon's idle timeout can kill the SSL socket during any sleep.
        connections.close_all()

        pause = seconds_until_active(session.linkedin_profile)
        claimable_task_types = _claimable_task_types_now(session.linkedin_profile)
        outside_hours_bypass = False
        if pause > 0:
            always_on_task_types = {
                Task.TaskType.MANUAL_REPLY,
                Task.TaskType.STATUS_SUMMARY,
            }
            always_on_wait = Task.objects.seconds_to_next(
                operator=our_operator,
                campaign_ids=our_campaign_ids,
                task_types=always_on_task_types,
            )
            if always_on_wait is not None and always_on_wait <= 0:
                claimable_task_types = always_on_task_types
                outside_hours_bypass = True
            else:
                if always_on_wait is not None:
                    pause = min(pause, max(always_on_wait, 1))
                pause = min(pause, MANUAL_REPLY_POLL_SECONDS)
                _sync_listener_supervisor(listener_supervisor)
                h, m = int(pause // 3600), int(pause % 3600 // 60)
                logger.info("Outside active hours — sleeping %dh%02dm", h, m)
                connections.close_all()
                time.sleep(pause)
                continue

        if outside_hours_bypass:
            logger.info("Outside active hours — handling always-on task")

        _sync_listener_supervisor(listener_supervisor)

        if claimable_task_types is not None and not outside_hours_bypass:
            claimable_task_types = set(claimable_task_types) | {
                Task.TaskType.MANUAL_REPLY,
                Task.TaskType.STATUS_SUMMARY,
            }

        task = Task.objects.claim_next(
            operator=our_operator, campaign_ids=our_campaign_ids,
            task_types=claimable_task_types,
        )
        if task is None:
            recovered_missing_connect = False
            if (
                claimable_task_types is None
                or Task.TaskType.CONNECT in claimable_task_types
            ):
                for campaign in campaigns:
                    if _ensure_connect_task_for_campaign(campaign, delay_seconds=0):
                        logger.warning(
                            "[%s] connect queue was empty while work remained — queued recovery task",
                            campaign,
                        )
                        _notify_connect_queue_recovered(our_operator, campaign)
                        recovered_missing_connect = True
            if recovered_missing_connect:
                continue

            wait = Task.objects.seconds_to_next(
                operator=our_operator, campaign_ids=our_campaign_ids,
                task_types=claimable_task_types,
            )
            if wait is None:
                if claimable_task_types is not None:
                    if Task.TaskType.MANUAL_REPLY in claimable_task_types:
                        logger.info(
                            "Catch-up queue empty for %s — polling manual replies in %ds",
                            ", ".join(sorted(claimable_task_types)) or "allowed tasks",
                            MANUAL_REPLY_POLL_SECONDS,
                        )
                        _sync_listener_supervisor(listener_supervisor)
                        connections.close_all()
                        time.sleep(MANUAL_REPLY_POLL_SECONDS)
                        continue
                    wait = _seconds_until_next_active_start()
                    logger.info(
                        "Catch-up queue empty for %s — sleeping until next active window (%.0fs)",
                        ", ".join(sorted(claimable_task_types)) or "allowed tasks",
                        wait,
                    )
                    _sync_listener_supervisor(listener_supervisor)
                    connections.close_all()
                    time.sleep(wait)
                    continue
                if Task.objects.filter(
                    task_type__in=[Task.TaskType.ENRICH_PHONE, Task.TaskType.ENRICH_EMAIL],
                    status__in=[Task.Status.PENDING, Task.Status.RUNNING],
                ).exists():
                    logger.info("Outbound queue empty — waiting on enrichment worker")
                    connections.close_all()
                    time.sleep(ENRICHMENT_WAIT_POLL_SECONDS)
                    continue
                if Task.objects.filter(
                    task_type=Task.TaskType.GMAIL_FOLLOW_UP,
                    status__in=[Task.Status.PENDING, Task.Status.RUNNING],
                ).exists():
                    logger.info("Outbound queue empty — waiting on Gmail worker")
                    connections.close_all()
                    time.sleep(ENRICHMENT_WAIT_POLL_SECONDS)
                    continue
                logger.info("Queue empty — nothing to do")
                listener_supervisor.stop()
                enrichment_worker.stop()
                gmail_worker.stop()
                # Clean exit: clear our heartbeat so peers don't false-alarm
                # on a daemon that stopped on purpose, then stop the monitor.
                if node_monitor is not None:
                    clear_heartbeat(our_operator)
                    node_monitor.stop()
                return
            if wait > 0:
                if (
                    claimable_task_types is None
                    or Task.TaskType.MANUAL_REPLY in claimable_task_types
                ):
                    wait = min(wait, MANUAL_REPLY_POLL_SECONDS)
                h, m = int(wait // 3600), int(wait % 3600 // 60)
                logger.info("Next task in %dh%02dm — sleeping", h, m)
                _sync_listener_supervisor(listener_supervisor)
                connections.close_all()
                time.sleep(wait)
            continue

        # Account-wide tasks (e.g. sweep_connections) span all campaigns and
        # don't carry a campaign_id; the handler sets session.campaign as needed.
        if task.task_type in {
            Task.TaskType.SWEEP_CONNECTIONS,
            Task.TaskType.MANUAL_REPLY,
            Task.TaskType.STATUS_SUMMARY,
            Task.TaskType.DISCOVERY,
        }:
            session.campaign = session.campaigns.first()
        else:
            campaign = Campaign.objects.filter(pk=task.payload.get("campaign_id")).first()
            if not campaign:
                _safe_mark_failed(task, f"Campaign {task.payload.get('campaign_id')} not found")
                continue
            session.campaign = campaign

        # Pre-flight: detect a stale Neon socket BEFORE we start the work,
        # so a dead conn doesn't surface mid-task (e.g. after the DM was
        # already sent on LinkedIn — that path leaves orphaned state).
        # ensure_connection() opens a fresh connection if the current one
        # is closed; close_all() ahead of it guarantees we're not reusing
        # a known-dead socket.
        try:
            connections["default"].ensure_connection()
        except _DB_DEAD_ERRORS:
            logger.warning("Pre-flight ensure_connection saw a dead conn — recycling")
            connections.close_all()
            connections["default"].ensure_connection()

        task.mark_running()

        handler = _HANDLERS.get(task.task_type)
        if handler is None:
            _safe_mark_failed(task, f"Unknown task type: {task.task_type}")
            continue

        try:
            with failure_diagnostics(session):
                handler(task, session, qualifiers)
        except Exception as exc:
            _safe_mark_failed(task, _format_task_error(exc))
            failure_tracker.record_failure()
            logger.exception("Task %s failed", task)
            notify_error(
                f"daemon:{task.task_type}",
                exc,
                context={
                    "task_id": task.id,
                    "operator": our_operator,
                    "payload": task.payload,
                },
            )
            # Self-rescheduling tasks (connect) never reach their own
            # reschedule path on crash.  Re-seed so the queue doesn't stall.
            if task.task_type == Task.TaskType.CONNECT:
                from linkedin.tasks.connect import enqueue_connect
                cid = task.payload.get("campaign_id")
                if cid:
                    enqueue_connect(cid, delay_seconds=60)
            elif task.task_type == Task.TaskType.DISCOVERY and ENABLE_PROFILE_DISCOVERY:
                from linkedin.discovery.collector import enqueue_discovery
                from linkedin.discovery.config import next_discovery_window_start

                retry_at = next_discovery_window_start(
                    timezone.now(),
                    after_current_day=True,
                )
                if retry_at is not None:
                    enqueue_discovery(
                        session.linkedin_profile,
                        our_operator,
                        scheduled_at=retry_at,
                    )
            continue

        task.mark_completed()
        if task.task_type == Task.TaskType.CONNECT:
            cid = task.payload.get("campaign_id")
            if cid:
                campaign = Campaign.objects.filter(pk=cid).first()
                if campaign:
                    _maybe_alert_low_connect_pool(our_operator, campaign)
                    if _ensure_connect_task_for_campaign(
                        campaign,
                        delay_seconds=recommended_action_delay(
                            session.linkedin_profile, ActionLog.ActionType.CONNECT,
                        ),
                    ):
                        logger.warning(
                            "[%s] connect self-reschedule was missing — queued recovery task",
                            campaign,
                        )
                        _notify_connect_queue_recovered(our_operator, campaign)
        failure_tracker.record_success()
        freemium.maybe_log()
