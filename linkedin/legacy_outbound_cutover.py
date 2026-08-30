"""Reviewed one-time retirement of the pre-drip outbound queue.

The cutover is intentionally narrower than deleting historical outreach data.
It preserves Leads, Deals, Messages, Campaign rows, and their audit trail while
terminally completing only current-outbound Tasks and closing active historical
Campaigns.  Empty sender-owned runtime Campaigns keep the existing LinkedIn
daemon/action-log contract available for drip execution.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import psutil
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from linkedin.conf import PEER_STALE_MINUTES
from linkedin.exceptions import LegacyOutboundCutoverError
from linkedin.operators import CANONICAL_OPERATOR_HANDLES, resolve_operator


PLAN_SCHEMA_VERSION = 2
ACTIVE_DRIP_OPERATORS = ("Arian", "Chuka")
LEGACY_OPERATOR_SCOPE = ("Arian", "Athena", "Chuka", "Leili")
RUNTIME_CAMPAIGN_NAMES = {
    operator: f"Drip Runtime - {operator}"
    for operator in ACTIVE_DRIP_OPERATORS
}
LEGACY_TASK_TYPES = (
    "connect",
    "check_pending",
    "follow_up",
    "gmail_follow_up",
    "enrich_email",
    "enrich_phone",
)
_LOCK_KEY = 0x4F4F445249504355  # "OODRIPCU", signed-bigint safe.


def _profile_operator(profile) -> str:
    user = profile.user
    aliases = (
        profile.linkedin_username,
        user.username,
        user.email,
        user.first_name,
        f"{user.first_name} {user.last_name}".strip(),
    )
    resolved = {
        operator
        for value in aliases
        if (operator := resolve_operator(value)) in CANONICAL_OPERATOR_HANDLES
    }
    if len(resolved) > 1:
        raise LegacyOutboundCutoverError(
            f"LinkedInProfile {profile.pk} has conflicting operator aliases: "
            f"{sorted(resolved)}",
        )
    return next(iter(resolved), "")


def _runtime_profiles(*, lock: bool) -> dict[str, Any]:
    from linkedin.models import LinkedInProfile

    queryset = LinkedInProfile.objects.select_related("user").filter(active=True)
    if lock:
        queryset = queryset.select_for_update()
    matches: dict[str, list[Any]] = {operator: [] for operator in ACTIVE_DRIP_OPERATORS}
    for profile in queryset:
        operator = _profile_operator(profile)
        if operator in matches:
            matches[operator].append(profile)

    resolved: dict[str, Any] = {}
    for operator, profiles in matches.items():
        if len(profiles) != 1:
            ids = [profile.pk for profile in profiles]
            raise LegacyOutboundCutoverError(
                f"Expected exactly one active LinkedInProfile for {operator}; "
                f"found {ids}",
            )
        resolved[operator] = profiles[0]
    return resolved


def local_outbound_processes() -> list[dict[str, Any]]:
    """Return locally visible processes that can claim or send outbound work."""
    matches: list[dict[str, Any]] = []
    current_pid = os.getpid()
    expected_process_errors = (
        psutil.AccessDenied,
        psutil.NoSuchProcess,
        psutil.ZombieProcess,
    )
    for process in psutil.process_iter(["pid", "cmdline"]):
        if process.pid == current_pid:
            continue
        try:
            command = process.info.get("cmdline") or []
        except expected_process_errors:
            continue
        if not command:
            continue

        basenames = [Path(argument).name for argument in command]
        kind = ""
        if "daemon_supervisor.py" in basenames:
            kind = "daemon_supervisor"
        elif "manage.py" in basenames:
            manage_index = basenames.index("manage.py")
            remainder = command[manage_index + 1:]
            subcommand = next(
                (argument for argument in remainder if not argument.startswith("-")),
                "",
            )
            if subcommand == "run_gmail_worker":
                kind = "gmail_worker"
            elif not subcommand:
                kind = "linkedin_daemon"
        if kind:
            matches.append({"pid": process.pid, "kind": kind})
    return sorted(matches, key=lambda row: (row["kind"], row["pid"]))


def _campaign_operator_map(*, lock: bool) -> tuple[dict[int, str], list[Any]]:
    from linkedin.models import Campaign, LinkedInProfile

    profiles = LinkedInProfile.objects.select_related("user").all()
    operator_by_user: dict[int, str] = {}
    for profile in profiles:
        operator = _profile_operator(profile)
        if operator:
            existing = operator_by_user.get(profile.user_id)
            if existing and existing != operator:
                raise LegacyOutboundCutoverError(
                    f"User {profile.user_id} maps to both {existing} and {operator}",
                )
            operator_by_user[profile.user_id] = operator

    campaigns = Campaign.objects.select_related("user").order_by("pk")
    if lock:
        campaigns = campaigns.select_for_update()
    campaign_rows = list(campaigns)
    return (
        {
            campaign.pk: operator_by_user.get(campaign.user_id, "")
            for campaign in campaign_rows
        },
        campaign_rows,
    )


def _task_belongs_to_legacy_scope(
    task,
    *,
    historical_campaign_ids: set[int],
    lead_ids_in_scope: set[int],
) -> bool:
    payload = task.payload if isinstance(task.payload, dict) else {}
    operator = resolve_operator(payload.get("operator"))
    if operator in LEGACY_OPERATOR_SCOPE:
        return True

    campaign_id = payload.get("campaign_id")
    try:
        normalized_campaign_id = int(campaign_id)
    except (TypeError, ValueError):
        normalized_campaign_id = None
    if normalized_campaign_id in historical_campaign_ids:
        return True

    lead_id = payload.get("lead_id")
    try:
        normalized_lead_id = int(lead_id)
    except (TypeError, ValueError):
        normalized_lead_id = None
    return normalized_lead_id in lead_ids_in_scope


def _review_state(plan: dict[str, Any]) -> dict[str, Any]:
    """Return only state that must remain exact between review and apply."""
    return {
        "schema_version": plan["schema_version"],
        "runtime_campaigns": plan["runtime_campaigns"],
        "campaigns_to_finish": plan["campaigns_to_finish"],
        "tasks_to_retire": plan["tasks_to_retire"],
    }


def _state_digest(plan: dict[str, Any]) -> str:
    encoded = json.dumps(
        _review_state(plan),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_legacy_outbound_cutover_plan(*, lock: bool = False) -> dict[str, Any]:
    """Build an exact no-write snapshot of the legacy outbound retirement."""
    from crm.models import Deal
    from linkedin.models import Campaign, DaemonHeartbeat, Task

    runtime_profiles = _runtime_profiles(lock=lock)
    campaign_operator, campaigns = _campaign_operator_map(lock=lock)

    runtime_rows: list[dict[str, Any]] = []
    for operator in ACTIVE_DRIP_OPERATORS:
        profile = runtime_profiles[operator]
        name = RUNTIME_CAMPAIGN_NAMES[operator]
        matches = [campaign for campaign in campaigns if campaign.name == name]
        if len(matches) > 1:
            raise LegacyOutboundCutoverError(
                f"Multiple runtime Campaign rows use name {name!r}",
            )
        if matches:
            raise LegacyOutboundCutoverError(
                f"Runtime Campaign name {name!r} already exists; this one-time "
                "cutover has already run or its reserved names are in use",
            )
        runtime_rows.append(
            {
                "operator": operator,
                "name": name,
                "user_id": profile.user_id,
                "linkedin_profile_id": profile.pk,
                "campaign_id": None,
                "current_status": None,
                "action": "create",
            },
        )

    historical_campaign_ids = {
        campaign.pk
        for campaign in campaigns
        if campaign_operator.get(campaign.pk) in LEGACY_OPERATOR_SCOPE
    }
    campaigns_to_finish = [
        {
            "id": campaign.pk,
            "name": campaign.name,
            "operator": campaign_operator[campaign.pk],
            "user_id": campaign.user_id,
            "previous_status": campaign.status,
        }
        for campaign in campaigns
        if campaign.pk in historical_campaign_ids
        and campaign.status == Campaign.Status.ACTIVE
    ]

    lead_ids_in_scope = set(
        Deal.objects.filter(campaign_id__in=historical_campaign_ids).values_list(
            "lead_id",
            flat=True,
        ),
    )
    task_queryset = Task.objects.filter(
        task_type__in=LEGACY_TASK_TYPES,
        status__in=(Task.Status.PENDING, Task.Status.RUNNING),
    ).order_by("pk")
    if lock:
        task_queryset = task_queryset.select_for_update()
    tasks = [
        task
        for task in task_queryset
        if _task_belongs_to_legacy_scope(
            task,
            historical_campaign_ids=historical_campaign_ids,
            lead_ids_in_scope=lead_ids_in_scope,
        )
    ]
    tasks_to_retire = [
        {
            "id": task.pk,
            "task_type": task.task_type,
            "status": task.status,
            "scheduled_at": task.scheduled_at.isoformat(),
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "previous_completed_at": (
                task.completed_at.isoformat() if task.completed_at else None
            ),
            "payload": task.payload if isinstance(task.payload, dict) else {},
            "previous_error": task.error,
        }
        for task in tasks
    ]

    now = timezone.now()
    fresh_heartbeat_after = now - timedelta(minutes=PEER_STALE_MINUTES)
    live_heartbeats = [
        {
            "sender": row.sender,
            "last_alive": row.last_alive.isoformat(),
        }
        for row in DaemonHeartbeat.objects.filter(
            last_alive__gte=fresh_heartbeat_after,
        ).order_by("sender")
        if resolve_operator(row.sender) in CANONICAL_OPERATOR_HANDLES
    ]
    running_task_ids = [
        task.pk
        for task in tasks
        if task.status == Task.Status.RUNNING
    ]

    plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "active_drip_operators": list(ACTIVE_DRIP_OPERATORS),
        "legacy_operator_scope": list(LEGACY_OPERATOR_SCOPE),
        "runtime_campaigns": runtime_rows,
        "campaigns_to_finish": campaigns_to_finish,
        "tasks_to_retire": tasks_to_retire,
        "live_daemon_heartbeats": live_heartbeats,
        "running_task_ids": running_task_ids,
        "local_outbound_processes": local_outbound_processes(),
        "preserved_tables": ["Campaign", "Deal", "Lead", "Message"],
    }
    plan["state_digest"] = _state_digest(plan)
    return plan


def _write_private_json(payload: dict[str, Any], path: str | Path) -> Path:
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        output,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    output.chmod(0o600)
    return output


def write_legacy_outbound_cutover_plan(plan: dict[str, Any], path: str | Path) -> Path:
    return _write_private_json(plan, path)


def write_legacy_outbound_cutover_receipt(
    receipt: dict[str, Any],
    path: str | Path,
) -> Path:
    return _write_private_json(receipt, path)


def load_legacy_outbound_cutover_plan(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        plan = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LegacyOutboundCutoverError(
            f"Could not read reviewed cutover plan {source}: {exc}",
        ) from exc
    if not isinstance(plan, dict):
        raise LegacyOutboundCutoverError("Reviewed cutover plan must be a JSON object")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise LegacyOutboundCutoverError(
            f"Unsupported cutover plan schema {plan.get('schema_version')!r}",
        )
    expected_digest = plan.get("state_digest")
    if not isinstance(expected_digest, str) or expected_digest != _state_digest(plan):
        raise LegacyOutboundCutoverError("Reviewed cutover plan digest is invalid")
    return plan


def _acquire_cutover_lock() -> None:
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [_LOCK_KEY])
        if not cursor.fetchone()[0]:
            raise LegacyOutboundCutoverError(
                "Another legacy outbound cutover is already running",
            )


def _lock_cutover_tables() -> None:
    """Exclude queue claims and campaign/ownership changes until commit."""
    if connection.vendor != "postgresql":
        return

    from crm.models import Deal
    from django.contrib.auth.models import User
    from linkedin.models import Campaign, LinkedInProfile, Task

    table_names = sorted({
        Campaign._meta.db_table,
        Deal._meta.db_table,
        LinkedInProfile._meta.db_table,
        Task._meta.db_table,
        User._meta.db_table,
    })
    quoted = ", ".join(connection.ops.quote_name(name) for name in table_names)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"LOCK TABLE {quoted} IN ACCESS EXCLUSIVE MODE NOWAIT",
            )
    except DatabaseError as exc:
        raise LegacyOutboundCutoverError(
            "Could not exclusively lock outbound queue and Campaign ownership; "
            "stop sender processes and retry with a newly reviewed plan",
        ) from exc


@transaction.atomic
def apply_legacy_outbound_cutover(
    reviewed_plan: dict[str, Any],
    *,
    reviewed_by: str,
    processes_stopped: bool,
) -> dict[str, Any]:
    """Apply an exact reviewed plan atomically, failing on any state drift."""
    from linkedin.models import Campaign, Task

    reviewer = reviewed_by.strip()
    if not reviewer:
        raise LegacyOutboundCutoverError("reviewed_by must not be empty")
    if not processes_stopped:
        raise LegacyOutboundCutoverError(
            "processes_stopped confirmation is required for this one-time cutover",
        )

    visible_processes = local_outbound_processes()
    if visible_processes:
        descriptions = ", ".join(
            f"{row['kind']} pid={row['pid']}" for row in visible_processes
        )
        raise LegacyOutboundCutoverError(
            f"Local outbound processes are still running: {descriptions}",
        )

    _acquire_cutover_lock()
    _lock_cutover_tables()
    current = build_legacy_outbound_cutover_plan(lock=True)
    if reviewed_plan.get("state_digest") != current["state_digest"]:
        raise LegacyOutboundCutoverError(
            "Database state changed after review; generate and review a new plan",
        )
    if current["live_daemon_heartbeats"]:
        senders = ", ".join(
            row["sender"] for row in current["live_daemon_heartbeats"]
        )
        raise LegacyOutboundCutoverError(
            f"Sender daemon heartbeat is still fresh for: {senders}",
        )
    if current["running_task_ids"]:
        raise LegacyOutboundCutoverError(
            "Legacy outbound Tasks are still running: "
            + ", ".join(map(str, current["running_task_ids"])),
        )
    visible_processes = local_outbound_processes()
    if visible_processes:
        descriptions = ", ".join(
            f"{row['kind']} pid={row['pid']}" for row in visible_processes
        )
        raise LegacyOutboundCutoverError(
            f"Local outbound processes started during cutover: {descriptions}",
        )

    runtime_campaign_ids: list[int] = []
    for row in current["runtime_campaigns"]:
        campaign = Campaign.objects.create(
            name=row["name"],
            user_id=row["user_id"],
            status=Campaign.Status.ACTIVE,
        )
        runtime_campaign_ids.append(campaign.pk)

    campaign_ids = [row["id"] for row in current["campaigns_to_finish"]]
    finished_count = Campaign.objects.filter(
        pk__in=campaign_ids,
        status=Campaign.Status.ACTIVE,
    ).update(status=Campaign.Status.FINISHED)
    if finished_count != len(campaign_ids):
        raise LegacyOutboundCutoverError(
            "Historical Campaign state changed during cutover",
        )

    now = timezone.now()
    task_ids = [row["id"] for row in current["tasks_to_retire"]]
    reason = (
        "Terminally retired by reviewed pre-drip outbound cutover; "
        f"reviewed_by={reviewer}; plan={current['state_digest']}"
    )
    retired_count = Task.objects.filter(
        pk__in=task_ids,
        status__in=(Task.Status.PENDING, Task.Status.RUNNING),
    ).update(
        status=Task.Status.COMPLETED,
        completed_at=now,
        error=reason,
    )
    if retired_count != len(task_ids):
        raise LegacyOutboundCutoverError(
            "Legacy Task state changed during cutover",
        )

    return {
        "applied_at": now.isoformat(),
        "reviewed_by": reviewer,
        "processes_stopped_attested": True,
        "plan_digest": current["state_digest"],
        "retired_task_count": retired_count,
        "finished_campaign_count": finished_count,
        "runtime_campaign_ids": runtime_campaign_ids,
    }
