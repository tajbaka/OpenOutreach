"""Peer-node liveness monitoring.

Each daemon is a "node". Its `NodeMonitor` background thread stamps the
node's own `DaemonHeartbeat` row every `MONITOR_INTERVAL_SECONDS`, scans
every other node's row for stale heartbeats, and checks expected senders for
outbound activity progress. A node whose heartbeat is older than
`PEER_STALE_MINUTES`, or whose heartbeat is fresh but outbound lane is stuck,
is reported to the ops Slack channel.

No third-party service: the "always-up watcher" is just the other daemons
plus Neon. Coverage therefore needs >=2 daemons running — a lone daemon has
no peer to watch it (an accepted limitation, see the brainstorm).

`down_alerted_at` and `activity_alerted_at` are atomic claim+cooldown markers:
the peer that wins the UPDATE posts (so N peers don't all alert), and each row
is re-claimable only after `DEGRADED_REALERT_HOURS`. Activity cooldowns are not
cleared by a healthy observation because peer daemons can have different
runtime rate-limit overrides.
"""
from __future__ import annotations

import logging
import threading

from django.db import connection
from django.db.models import Count, Min, Q
from django.utils import timezone
from datetime import timedelta

from linkedin import conf
from linkedin.notifications.slack import notify_degraded
from linkedin.operators import resolve_operator

logger = logging.getLogger(__name__)


def write_heartbeat(sender: str) -> None:
    """Stamp this node's heartbeat. Clears `down_alerted_at` — a node that
    is beating is alive, so any prior down-claim is void and the next real
    outage should alert afresh."""
    from linkedin.models import DaemonHeartbeat

    DaemonHeartbeat.objects.update_or_create(
        sender=sender,
        defaults={"last_alive": timezone.now(), "down_alerted_at": None},
    )


def clear_heartbeat(sender: str) -> None:
    """Mark this node intentionally stopped (clean daemon exit).

    `last_alive = NULL` tells peers "not expected to be running" so they do
    not false-alarm on a daemon that exited on purpose (empty queue)."""
    from linkedin.models import DaemonHeartbeat

    DaemonHeartbeat.objects.filter(sender=sender).update(
        last_alive=None, down_alerted_at=None,
    )


def check_peers(self_sender: str) -> None:
    """Scan peer heartbeats; Slack-alert each peer that has gone stale.

    The claim is atomic: `filter(...).update(down_alerted_at=now)` returns
    the row count, and Postgres row locking serialises racing peers — so
    exactly one peer posts per outage. Re-claimable after the cooldown."""
    from linkedin.models import DaemonHeartbeat

    now = timezone.now()
    stale_before = now - timedelta(minutes=conf.PEER_STALE_MINUTES)
    realert_before = now - timedelta(hours=conf.DEGRADED_REALERT_HOURS)

    stale_peers = DaemonHeartbeat.objects.filter(
        last_alive__isnull=False,
        last_alive__lt=stale_before,
    ).exclude(sender=self_sender)

    for peer in stale_peers:
        claimed = (
            DaemonHeartbeat.objects.filter(pk=peer.pk)
            .filter(
                Q(down_alerted_at__isnull=True)
                | Q(down_alerted_at__lt=realert_before)
            )
            .update(down_alerted_at=now)
        )
        if not claimed:
            continue  # another peer already alerted within the cooldown
        age_min = int((now - peer.last_alive).total_seconds() // 60)
        logger.warning(
            "Peer node %r looks down — %d min since last heartbeat",
            peer.sender, age_min,
        )
        notify_degraded(
            sender=peer.sender,
            title=f"{peer.sender}'s daemon looks down",
            detail=(
                f"No heartbeat for {age_min} min "
                f"(threshold {conf.PEER_STALE_MINUTES} min). "
                f"Last seen {peer.last_alive:%Y-%m-%d %H:%M} UTC."
            ),
        )


def _activity_check_window(now):
    """Return active-day start if activity health should run now."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(conf.ACTIVE_TIMEZONE)
    local_now = timezone.localtime(now, timezone=tz)
    if local_now.weekday() in conf.REST_DAYS:
        return None

    if conf.ENABLE_ACTIVE_HOURS:
        start = timezone.make_aware(
            local_now.replace(
                hour=conf.ACTIVE_START_HOUR,
                minute=0,
                second=0,
                microsecond=0,
                tzinfo=None,
            ),
            timezone=tz,
        )
    else:
        start = timezone.make_aware(
            local_now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None),
            timezone=tz,
        )
    return start


def _claim_activity_alert(sender: str, now) -> bool:
    from linkedin.models import DaemonHeartbeat

    realert_before = now - timedelta(hours=conf.DEGRADED_REALERT_HOURS)
    row, _ = DaemonHeartbeat.objects.get_or_create(sender=sender)
    return bool(
        DaemonHeartbeat.objects.filter(pk=row.pk)
        .filter(
            Q(activity_alerted_at__isnull=True)
            | Q(activity_alerted_at__lt=realert_before)
        )
        .update(activity_alerted_at=now)
    )


def _connectable_count(campaign_ids: list[int]) -> int:
    if not campaign_ids:
        return 0

    from crm.models import Deal
    from linkedin.enums import ProfileState

    return Deal.objects.filter(
        campaign_id__in=campaign_ids,
        lead__disqualified=False,
        state__in=[ProfileState.QUALIFIED, ProfileState.READY_TO_CONNECT],
    ).count()


def _outbound_task_summary(sender: str, campaign_ids: list[int], now) -> dict:
    from linkedin.models import Task

    connect_scope = Q(pk__in=[])
    if campaign_ids:
        connect_scope = Q(
            task_type=Task.TaskType.CONNECT,
            payload__campaign_id__in=campaign_ids,
        )
    follow_scope = Q(task_type=Task.TaskType.FOLLOW_UP, payload__operator=sender)
    pending = Task.objects.filter(status=Task.Status.PENDING).filter(
        connect_scope | follow_scope,
    )
    due = pending.filter(scheduled_at__lte=now)
    return {
        "pending": pending.count(),
        "due": due.count(),
        "due_connect": due.filter(task_type=Task.TaskType.CONNECT).count(),
        "due_follow_up": due.filter(task_type=Task.TaskType.FOLLOW_UP).count(),
        "oldest_due_at": due.aggregate(oldest=Min("scheduled_at"))["oldest"],
    }


def _blocked_due_actions(profile, task_summary: dict) -> list[str]:
    """Return due outbound actions blocked by the profile's rate limits."""
    from linkedin.models import ActionLog

    blocked = []
    if (
        task_summary["due_connect"] > 0
        and not profile.can_execute(ActionLog.ActionType.CONNECT)
    ):
        blocked.append(ActionLog.ActionType.CONNECT)
    if (
        task_summary["due_follow_up"] > 0
        and not profile.can_execute(ActionLog.ActionType.FOLLOW_UP)
    ):
        blocked.append(ActionLog.ActionType.FOLLOW_UP)
    return blocked


def _expected_sender_profiles() -> dict[str, object | None]:
    """Map expected canonical sender handles to LinkedInProfile objects."""
    from linkedin.models import Campaign, LinkedInProfile

    profiles = list(
        LinkedInProfile.objects.filter(active=True)
        .select_related("user")
        .order_by("user__username")
    )
    by_sender = {
        resolve_operator(profile.linkedin_username) or profile.user.username: profile
        for profile in profiles
    }
    if conf.EXPECTED_OUTBOUND_SENDERS:
        expected = [resolve_operator(sender) for sender in conf.EXPECTED_OUTBOUND_SENDERS]
        return {sender: by_sender.get(sender) for sender in expected}

    active_user_ids = set(
        Campaign.objects.filter(status=Campaign.Status.ACTIVE)
        .values_list("user_id", flat=True)
    )
    return {
        sender: profile
        for sender, profile in by_sender.items()
        if profile.user_id in active_user_ids
    }


def check_expected_sender_activity(self_sender: str) -> None:
    """Alert when an expected sender is alive but outbound activity is stuck."""
    from linkedin.models import ActionLog, Campaign, DaemonHeartbeat

    now = timezone.now()
    active_start = _activity_check_window(now)
    if active_start is None:
        return
    if now < active_start + timedelta(minutes=conf.SENDER_ACTIVITY_GRACE_MINUTES):
        return

    heartbeat_stale_before = now - timedelta(minutes=conf.PEER_STALE_MINUTES)
    activity_stale_before = now - timedelta(minutes=conf.SENDER_ACTIVITY_STALE_MINUTES)
    heartbeats = {row.sender: row for row in DaemonHeartbeat.objects.all()}

    for sender, profile in _expected_sender_profiles().items():
        heartbeat = heartbeats.get(sender)
        if heartbeat is None or heartbeat.last_alive is None:
            if _claim_activity_alert(sender, now):
                notify_degraded(
                    sender=sender,
                    title=f"{sender}'s outbound sender is missing",
                    detail=(
                        "This sender is expected to run today, but no daemon "
                        "heartbeat row has been seen. Check whether the daemon "
                        "was started for this account."
                    ),
                )
            continue

        if heartbeat.last_alive < heartbeat_stale_before:
            # Peer liveness owns stale-heartbeat alerts.
            continue

        if profile is None:
            if _claim_activity_alert(sender, now):
                notify_degraded(
                    sender=sender,
                    title=f"{sender}'s outbound sender is not configured",
                    detail=(
                        "EXPECTED_OUTBOUND_SENDERS includes this handle, but "
                        "there is no active LinkedInProfile for it."
                    ),
                )
            continue

        campaign_ids = list(
            Campaign.objects.filter(user=profile.user, status=Campaign.Status.ACTIVE)
            .values_list("id", flat=True)
        )
        task_summary = _outbound_task_summary(sender, campaign_ids, now)
        connectable = _connectable_count(campaign_ids)
        if not campaign_ids or (task_summary["pending"] == 0 and connectable == 0):
            continue

        actions = ActionLog.objects.filter(
            linkedin_profile=profile,
            created_at__gte=active_start,
        )
        counts = {
            row["action_type"]: row["count"]
            for row in actions.values("action_type").annotate(count=Count("id"))
        }
        latest_action = actions.order_by("-created_at").first()
        total_today = sum(counts.values())
        oldest_due_at = task_summary["oldest_due_at"]
        blocked_actions = _blocked_due_actions(profile, task_summary)
        if blocked_actions:
            if _claim_activity_alert(sender, now):
                notify_degraded(
                    sender=sender,
                    title=f"{sender}'s outbound sender hit a rate limit",
                    detail=(
                        f"Heartbeat is fresh (last seen "
                        f"{heartbeat.last_alive:%Y-%m-%d %H:%M} UTC), but "
                        f"due outbound work is blocked by rate limits for: "
                        f"{', '.join(blocked_actions)}. "
                        f"Sent today: {counts.get(ActionLog.ActionType.CONNECT, 0)} "
                        f"invites, {counts.get(ActionLog.ActionType.FOLLOW_UP, 0)} "
                        f"follow-ups. Outbound queue: {task_summary['due']} due, "
                        f"{task_summary['pending']} pending. This is not treated "
                        f"as a stuck outbound lane."
                    ),
                )
            continue

        due_is_stale = (
            oldest_due_at is not None
            and oldest_due_at < activity_stale_before
            and (latest_action is None or latest_action.created_at < activity_stale_before)
        )

        if total_today > 0 and not due_is_stale:
            continue

        if not _claim_activity_alert(sender, now):
            continue

        detail = (
            f"Heartbeat is fresh (last seen {heartbeat.last_alive:%Y-%m-%d %H:%M} UTC). "
            f"Sent today: {counts.get(ActionLog.ActionType.CONNECT, 0)} invites, "
            f"{counts.get(ActionLog.ActionType.FOLLOW_UP, 0)} follow-ups. "
            f"Outbound queue: {task_summary['due']} due, {task_summary['pending']} pending. "
            f"Connectable leads: {connectable}."
        )
        if latest_action is not None:
            detail += f" Latest outbound action: {latest_action.created_at:%Y-%m-%d %H:%M} UTC."
        if oldest_due_at is not None:
            detail += f" Oldest due outbound task: {oldest_due_at:%Y-%m-%d %H:%M} UTC."

        notify_degraded(
            sender=sender,
            title=f"{sender}'s outbound activity looks stuck",
            detail=detail,
        )


class NodeMonitor:
    """Background thread that runs `write_heartbeat` + `check_peers` on a
    fixed cadence.

    A separate thread (same pattern as `EnrichmentWorker`) so monitoring
    keeps beating through the daemon's long off-hours sleeps — the
    heartbeat reflects "process alive", not "actively working".

    DB-only, no browser, no HTTP. Tick exceptions are logged and swallowed:
    monitoring is an enhancement and must never crash the outreach daemon
    (same posture as the realtime listener)."""

    def __init__(self, sender: str):
        self._sender = sender
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Spawn the monitor thread. Idempotent."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="node-monitor", daemon=True,
        )
        self._thread.start()
        logger.info("Node monitor started (sender=%s)", self._sender)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop to exit and join the thread. Idempotent."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
            logger.info("Node monitor stopped")

    def _run(self) -> None:
        # First tick runs immediately — a freshly started daemon is visible
        # to peers without waiting one full interval.
        while not self._stop.is_set():
            try:
                write_heartbeat(self._sender)
                check_peers(self._sender)
                check_expected_sender_activity(self._sender)
            except Exception:
                logger.exception("Node monitor tick failed")
            finally:
                # This thread owns its own thread-local Neon connection;
                # close it each tick so Neon's idle timeout can't hand us a
                # dead socket across the interval sleep.
                connection.close()
            self._stop.wait(conf.MONITOR_INTERVAL_SECONDS)
