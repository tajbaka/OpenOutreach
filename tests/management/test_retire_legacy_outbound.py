import json
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from crm.models import Deal
from linkedin.legacy_outbound_cutover import (
    RUNTIME_CAMPAIGN_NAMES,
    apply_legacy_outbound_cutover,
    build_legacy_outbound_cutover_plan,
)
from linkedin.exceptions import LegacyOutboundCutoverError
from linkedin.models import Campaign, DaemonHeartbeat, LinkedInProfile, Task
from tests.factories import LeadFactory


@pytest.fixture(autouse=True)
def _no_local_outbound_processes(monkeypatch):
    monkeypatch.setattr(
        "linkedin.legacy_outbound_cutover.local_outbound_processes",
        lambda: [],
    )


def _sender(operator: str, username: str):
    user = User.objects.create(username=operator, email=username)
    profile = LinkedInProfile.objects.create(
        user=user,
        linkedin_username=username,
        linkedin_password="unused",
        active=True,
    )
    campaign = Campaign.objects.create(
        name=f"Historical {operator}",
        user=user,
        status=Campaign.Status.ACTIVE,
    )
    return user, profile, campaign


def _setup_senders():
    arian = _sender("Arian", "ariantajbakh@gmail.com")
    chuka = _sender("Chuka", "chukyjack@gmail.com")
    return arian, chuka


def _task(task_type, payload, *, status=Task.Status.PENDING, started_at=None):
    return Task.objects.create(
        task_type=task_type,
        status=status,
        scheduled_at=timezone.now() - timedelta(days=1),
        started_at=started_at,
        payload=payload,
    )


@pytest.mark.django_db
def test_plan_targets_only_legacy_outbound_and_preserves_rows():
    (arian_user, _arian_profile, arian_campaign), (
        _chuka_user,
        _chuka_profile,
        chuka_campaign,
    ) = _setup_senders()
    lead = LeadFactory()
    Deal.objects.create(lead=lead, campaign=arian_campaign)

    connect = _task(Task.TaskType.CONNECT, {"campaign_id": arian_campaign.pk})
    follow_up = _task(
        Task.TaskType.FOLLOW_UP,
        {"campaign_id": chuka_campaign.pk, "operator": "Chuka", "public_id": "lead"},
    )
    enrich = _task(Task.TaskType.ENRICH_PHONE, {"lead_id": lead.pk})
    manual = _task(
        Task.TaskType.MANUAL_REPLY,
        {"operator": "Arian", "lead_id": lead.pk, "message": "keep"},
    )
    outsider = User.objects.create(username="outsider")
    outsider_campaign = Campaign.objects.create(name="Outsider", user=outsider)
    unrelated = _task(Task.TaskType.CONNECT, {"campaign_id": outsider_campaign.pk})

    plan = build_legacy_outbound_cutover_plan()

    assert {row["id"] for row in plan["campaigns_to_finish"]} == {
        arian_campaign.pk,
        chuka_campaign.pk,
    }
    assert {row["id"] for row in plan["tasks_to_retire"]} == {
        connect.pk,
        follow_up.pk,
        enrich.pk,
    }
    assert manual.pk not in {row["id"] for row in plan["tasks_to_retire"]}
    assert unrelated.pk not in {row["id"] for row in plan["tasks_to_retire"]}
    assert {row["operator"] for row in plan["runtime_campaigns"]} == {
        "Arian",
        "Chuka",
    }
    assert all(row["action"] == "create" for row in plan["runtime_campaigns"])
    assert plan["preserved_tables"] == ["Campaign", "Deal", "Lead", "Message"]
    assert arian_user.campaigns.filter(pk=arian_campaign.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_apply_is_atomic_and_terminally_retires_reviewed_rows():
    (_arian_user, _arian_profile, arian_campaign), (
        _chuka_user,
        _chuka_profile,
        chuka_campaign,
    ) = _setup_senders()
    lead = LeadFactory()
    deal = Deal.objects.create(lead=lead, campaign=arian_campaign)
    connect = _task(Task.TaskType.CONNECT, {"campaign_id": arian_campaign.pk})
    gmail = _task(
        Task.TaskType.GMAIL_FOLLOW_UP,
        {"operator": "Chuka", "lead_id": lead.pk, "step_index": 1},
    )

    plan = build_legacy_outbound_cutover_plan()
    result = apply_legacy_outbound_cutover(
        plan,
        reviewed_by="Arian",
        processes_stopped=True,
    )

    assert result["retired_task_count"] == 2
    assert result["finished_campaign_count"] == 2
    assert result["processes_stopped_attested"] is True
    arian_campaign.refresh_from_db()
    chuka_campaign.refresh_from_db()
    assert arian_campaign.status == Campaign.Status.FINISHED
    assert chuka_campaign.status == Campaign.Status.FINISHED
    for task in (connect, gmail):
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert task.completed_at is not None
        assert "reviewed_by=Arian" in task.error
    assert Deal.objects.filter(pk=deal.pk, lead=lead).exists()
    assert Campaign.objects.filter(pk=arian_campaign.pk).exists()
    for operator, name in RUNTIME_CAMPAIGN_NAMES.items():
        runtime = Campaign.objects.get(name=name)
        assert runtime.status == Campaign.Status.ACTIVE
        assert runtime.deals.count() == 0
        assert runtime.user.username == operator


@pytest.mark.django_db(transaction=True)
def test_apply_rejects_state_drift_without_partial_writes():
    (_arian_user, _arian_profile, arian_campaign), _chuka = _setup_senders()
    reviewed = build_legacy_outbound_cutover_plan()
    new_task = _task(Task.TaskType.CONNECT, {"campaign_id": arian_campaign.pk})

    with pytest.raises(LegacyOutboundCutoverError, match="state changed after review"):
        apply_legacy_outbound_cutover(
            reviewed,
            reviewed_by="Arian",
            processes_stopped=True,
        )

    arian_campaign.refresh_from_db()
    new_task.refresh_from_db()
    assert arian_campaign.status == Campaign.Status.ACTIVE
    assert new_task.status == Task.Status.PENDING
    assert not Campaign.objects.filter(name__startswith="Drip Runtime -").exists()


@pytest.mark.django_db(transaction=True)
def test_apply_rejects_fresh_daemon_heartbeat_without_partial_writes():
    (_arian_user, _arian_profile, arian_campaign), _chuka = _setup_senders()
    reviewed = build_legacy_outbound_cutover_plan()
    DaemonHeartbeat.objects.create(sender="Arian", last_alive=timezone.now())

    with pytest.raises(LegacyOutboundCutoverError, match="heartbeat is still fresh"):
        apply_legacy_outbound_cutover(
            reviewed,
            reviewed_by="Arian",
            processes_stopped=True,
        )

    arian_campaign.refresh_from_db()
    assert arian_campaign.status == Campaign.Status.ACTIVE
    assert not Campaign.objects.filter(name__startswith="Drip Runtime -").exists()


@pytest.mark.django_db
def test_command_writes_review_artifact_and_requires_exact_plan(tmp_path):
    _setup_senders()
    output = tmp_path / "cutover.json"

    call_command("retire_legacy_outbound", output=str(output))

    payload = json.loads(output.read_text())
    assert payload["schema_version"] == 2
    assert payload["state_digest"]
    assert output.stat().st_mode & 0o777 == 0o600
    assert Campaign.objects.filter(status=Campaign.Status.ACTIVE).count() == 2

    with pytest.raises(CommandError, match="--plan is required"):
        call_command(
            "retire_legacy_outbound",
            apply=True,
            reviewed_by="Arian",
        )
    with pytest.raises(CommandError, match="--confirm-processes-stopped"):
        call_command(
            "retire_legacy_outbound",
            plan=str(output),
            reviewed_by="Arian",
            apply=True,
        )

    receipt = tmp_path / "receipt.json"
    call_command(
        "retire_legacy_outbound",
        plan=str(output),
        output=str(receipt),
        reviewed_by="Arian",
        apply=True,
        confirm_processes_stopped=True,
    )
    applied = json.loads(receipt.read_text())
    assert applied["reviewed_by"] == "Arian"
    assert applied["runtime_campaign_ids"]
    assert applied["processes_stopped_attested"] is True
    assert receipt.stat().st_mode & 0o777 == 0o600


@pytest.mark.django_db(transaction=True)
def test_apply_rejects_even_stale_running_legacy_task():
    (_arian_user, _arian_profile, arian_campaign), _chuka = _setup_senders()
    running = _task(
        Task.TaskType.FOLLOW_UP,
        {
            "campaign_id": arian_campaign.pk,
            "operator": "Arian",
            "public_id": "stale-running-lead",
        },
        status=Task.Status.RUNNING,
        started_at=timezone.now() - timedelta(days=30),
    )
    reviewed = build_legacy_outbound_cutover_plan()

    with pytest.raises(LegacyOutboundCutoverError, match="still running"):
        apply_legacy_outbound_cutover(
            reviewed,
            reviewed_by="Arian",
            processes_stopped=True,
        )

    running.refresh_from_db()
    arian_campaign.refresh_from_db()
    assert running.status == Task.Status.RUNNING
    assert arian_campaign.status == Campaign.Status.ACTIVE


@pytest.mark.django_db(transaction=True)
def test_apply_requires_stopped_process_attestation():
    _setup_senders()
    reviewed = build_legacy_outbound_cutover_plan()

    with pytest.raises(LegacyOutboundCutoverError, match="confirmation is required"):
        apply_legacy_outbound_cutover(
            reviewed,
            reviewed_by="Arian",
            processes_stopped=False,
        )

    assert not Campaign.objects.filter(name__startswith="Drip Runtime -").exists()


@pytest.mark.django_db(transaction=True)
def test_apply_rejects_visible_local_outbound_process(monkeypatch):
    _setup_senders()
    reviewed = build_legacy_outbound_cutover_plan()
    monkeypatch.setattr(
        "linkedin.legacy_outbound_cutover.local_outbound_processes",
        lambda: [{"pid": 4242, "kind": "gmail_worker"}],
    )

    with pytest.raises(LegacyOutboundCutoverError, match="pid=4242"):
        apply_legacy_outbound_cutover(
            reviewed,
            reviewed_by="Arian",
            processes_stopped=True,
        )

    assert not Campaign.objects.filter(name__startswith="Drip Runtime -").exists()


@pytest.mark.django_db
def test_reserved_runtime_name_makes_cutover_fail_closed():
    (arian_user, _arian_profile, _campaign), _chuka = _setup_senders()
    Campaign.objects.create(
        name=RUNTIME_CAMPAIGN_NAMES["Arian"],
        user=arian_user,
        status=Campaign.Status.DISABLED,
    )

    with pytest.raises(LegacyOutboundCutoverError, match="one-time cutover"):
        build_legacy_outbound_cutover_plan()


@pytest.mark.django_db(transaction=True)
def test_cutover_cannot_be_applied_twice():
    _setup_senders()
    reviewed = build_legacy_outbound_cutover_plan()
    apply_legacy_outbound_cutover(
        reviewed,
        reviewed_by="Arian",
        processes_stopped=True,
    )

    with pytest.raises(LegacyOutboundCutoverError, match="one-time cutover"):
        build_legacy_outbound_cutover_plan()


@pytest.mark.django_db
def test_plan_explicitly_retires_all_four_historical_operator_scopes():
    _setup_senders()
    _athena_user, _athena_profile, athena_campaign = _sender(
        "Athena",
        "athenaaghdami@gmail.com",
    )
    _leili_user, _leili_profile, leili_campaign = _sender(
        "Leili",
        "leili.ash2011@yahoo.com",
    )

    plan = build_legacy_outbound_cutover_plan()

    assert plan["legacy_operator_scope"] == ["Arian", "Athena", "Chuka", "Leili"]
    assert {row["id"] for row in plan["campaigns_to_finish"]}.issuperset({
        athena_campaign.pk,
        leili_campaign.pk,
    })
