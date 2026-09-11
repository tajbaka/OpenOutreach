"""No-provider regressions for the opt-in invitation-anchored Gmail lane."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from crm.models import Deal, Lead, Message
from gmail.handoff import _maybe_schedule_gmail_sequence, maybe_schedule_gmail_sequence
from gmail.tasks.enrich_email import handle_enrich_email
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enums import ProfileState
from linkedin.models import (
    Campaign,
    CampaignMessageEnrollment,
    LinkedInProfile,
    MessageProgram,
    MessageProgramVersion,
    OutboundDelivery,
    Task,
)
from tests.gmail.test_versioned_delivery import _hash, _message


pytestmark = pytest.mark.django_db
OPERATORS = ("Arian", "Chuka")


@pytest.fixture(autouse=True)
def invitation_environment(monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 9, 10, 14, tzinfo=dt_timezone.utc))
    monkeypatch.setattr(timezone, "now", lambda: clock.now)
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("linkedin.tasks.follow_up.ENABLE_ACTIVE_HOURS", False)
    monkeypatch.setattr("linkedin.suppression.lead_suppression_match", lambda lead: None)
    return clock


def invitation_deal(
    *,
    operator="Arian",
    email="ada@example.invalid",
    mode="invitation_sent",
    receipt=True,
    connected=False,
    role="CFO/Finance",
    with_copy=True,
):
    number = Campaign.objects.count() + 1
    user, _ = User.objects.get_or_create(username=operator)
    LinkedInProfile.objects.get_or_create(
        user=user,
        defaults={
            "linkedin_username": "ariantajbakh" if operator == "Arian" else "chukyjack",
            "linkedin_password": "unused-qa-placeholder",
            "active": True,
        },
    )
    program = MessageProgram.objects.create(key=f"invitation-{number}", name="Invitation QA")
    payload = {
        "program_key": program.key,
        "program_name": program.name,
        "messages": [
            _message(
                step_index=0,
                subject="A note for {role} at {company_name}",
                body="Hi {first_name}, I work with {role}. Best, {my_name}",
                delay_hours=24,
            ),
            _message(
                step_index=1,
                subject="Re: {company_name}",
                body="Hi {first_name}, second note for {role}. Best, {my_name}",
                delay_hours=48,
            ),
        ],
    }
    if not with_copy:
        payload["messages"] = [
            {**payload["messages"][0], "channel": "linkedin_connect", "step_key": "connect",
             "subject": "", "delay_hours": 0},
        ]
    version = MessageProgramVersion.objects.create(
        program=program,
        version=1,
        schema_version=1,
        payload=payload,
        content_hash=_hash(payload),
        published_by="offline-qa",
    )
    campaign = Campaign.objects.create(
        name=f"Invitation QA {number}",
        user=user,
        active_message_version=version,
        gmail_start_mode=mode,
    )
    lead = Lead.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        company_name="Example Cloud",
        linkedin_url=f"https://www.linkedin.com/in/invitation-qa-{number}/",
        public_identifier=f"invitation-qa-{number}",
        email=email,
        icp="s-founder-ceo-rev5-maint",
        role_tag=role,
    )
    deal = Deal.objects.create(
        campaign=campaign,
        lead=lead,
        state=ProfileState.CONNECTED if connected else ProfileState.PENDING,
        invitation_sent_at=timezone.now() if receipt else None,
        invitation_sender=operator if receipt else "",
        connected_at=timezone.now() if connected else None,
    )
    return deal


@pytest.mark.parametrize("operator", OPERATORS)
def test_confirmed_invitation_freezes_one_email_at_exact_receipt_anchor(
    invitation_environment, operator,
):
    deal = invitation_deal(operator=operator)
    receipt = deal.invitation_sent_at
    invitation_environment.now += timedelta(hours=7)

    task = _maybe_schedule_gmail_sequence(deal=deal, operator=operator)

    delivery = OutboundDelivery.objects.get(channel="gmail")
    assert task.task_type == Task.TaskType.GMAIL_FOLLOW_UP
    assert task.payload["operator"] == operator
    assert task.payload["deal_id"] == deal.pk
    assert delivery.enrollment.deal_id == deal.pk
    assert delivery.enrollment.operator == operator
    assert delivery.scheduled_at == receipt + timedelta(hours=24)
    assert task.scheduled_at == delivery.scheduled_at
    assert delivery.frozen_subject == "A note for finance leaders at Example Cloud"
    assert delivery.frozen_body == (
        f"Hi Ada, I work with finance leaders. Best, {'Eddy' if operator == 'Chuka' else 'Arian'}"
    )
    assert deal.state == ProfileState.PENDING and deal.connected_at is None
    for _ in range(3):
        repeated = _maybe_schedule_gmail_sequence(deal=deal, operator=operator)
        assert repeated.pk == task.pk
    assert Task.objects.count() == 1
    assert OutboundDelivery.objects.count() == 1


@pytest.mark.parametrize("operator", OPERATORS)
@pytest.mark.parametrize("scenario", ["no_receipt", "already_connected", "wrong_receipt_sender", "withdrawn"])
def test_no_qualifying_invitation_never_starts_email(operator, scenario):
    deal = invitation_deal(
        operator=operator,
        receipt=scenario not in {"no_receipt", "already_connected"},
        connected=scenario == "already_connected",
    )
    if scenario == "wrong_receipt_sender":
        deal.invitation_sender = "Chuka" if operator == "Arian" else "Arian"
        deal.save(update_fields=["invitation_sender"])
    if scenario == "withdrawn":
        deal.invitation_withdrawn_at = timezone.now()
        deal.save(update_fields=["invitation_withdrawn_at"])

    assert maybe_schedule_gmail_sequence(deal=deal, operator=operator) is None
    assert not Task.objects.exists()
    assert not OutboundDelivery.objects.exists()
    assert not CampaignMessageEnrollment.objects.exists()


@pytest.mark.parametrize("operator", OPERATORS)
def test_post_acceptance_mode_does_not_start_from_receipt_alone(operator):
    deal = invitation_deal(operator=operator, mode="post_acceptance")

    assert _maybe_schedule_gmail_sequence(deal=deal, operator=operator) is None
    assert not Task.objects.exists()

    deal.state = ProfileState.CONNECTED
    deal.connected_at = timezone.now() + timedelta(days=2)
    deal.save(update_fields=["state", "connected_at"])
    task = _maybe_schedule_gmail_sequence(deal=deal, operator=operator)
    assert task.scheduled_at == deal.connected_at + timedelta(hours=24)


@pytest.mark.parametrize("operator", OPERATORS)
def test_acceptance_preserves_frozen_email_and_deferred_task(
    invitation_environment, operator,
):
    deal = invitation_deal(operator=operator)
    task = _maybe_schedule_gmail_sequence(deal=deal, operator=operator)
    delivery = OutboundDelivery.objects.get(channel="gmail")
    original = (delivery.pk, delivery.scheduled_at, delivery.frozen_subject, delivery.frozen_body)
    deferred = task.scheduled_at + timedelta(days=2)
    task.scheduled_at = deferred
    task.save(update_fields=["scheduled_at"])
    invitation_environment.now += timedelta(hours=12)
    deal.state = ProfileState.CONNECTED
    deal.connected_at = timezone.now()
    deal.save(update_fields=["state", "connected_at"])
    deal.lead.role_tag = "Founder/CEO"
    deal.lead.save(update_fields=["role_tag"])

    again = _maybe_schedule_gmail_sequence(deal=deal, operator=operator)

    delivery.refresh_from_db()
    again.refresh_from_db()
    assert again.pk == task.pk and again.scheduled_at == deferred
    assert (delivery.pk, delivery.scheduled_at, delivery.frozen_subject, delivery.frozen_body) == original
    assert CampaignMessageEnrollment.objects.count() == 1
    assert Task.objects.count() == 1


@pytest.mark.parametrize("status", ["sent", "sending", "unclear", "stopped", "failed"])
def test_repeated_first_step_hook_never_revives_terminal_or_uncertain_delivery(status):
    deal = invitation_deal()
    task = _maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    delivery = OutboundDelivery.objects.get()
    delivery.status = status
    delivery.save(update_fields=["status"])
    task.status = Task.Status.COMPLETED if status == "sent" else Task.Status.FAILED
    task.save(update_fields=["status"])

    maybe_schedule_gmail_sequence(deal=deal, operator="Arian")

    delivery.refresh_from_db()
    task.refresh_from_db()
    assert delivery.status == status
    assert task.status == (Task.Status.COMPLETED if status == "sent" else Task.Status.FAILED)
    assert Task.objects.count() == 1


@pytest.mark.parametrize("operator", OPERATORS)
@pytest.mark.parametrize("late", [False, True])
def test_email_lookup_runs_early_but_found_address_preserves_send_clock(
    monkeypatch, invitation_environment, operator, late,
):
    deal = invitation_deal(operator=operator, email="")
    receipt = deal.invitation_sent_at
    enrichment = _maybe_schedule_gmail_sequence(deal=deal, operator=operator)
    delivery = OutboundDelivery.objects.get()
    assert enrichment.task_type == Task.TaskType.ENRICH_EMAIL
    assert enrichment.scheduled_at == receipt
    assert delivery.scheduled_at == receipt + timedelta(hours=24)
    assert _maybe_schedule_gmail_sequence(deal=deal, operator=operator).pk == enrichment.pk

    class FoundEmail:
        name = "bettercontact"

        def enrich(self, lead, task):
            return EnrichmentResult(status=EnrichmentStatus.FOUND, provider=self.name, email="ada@example.invalid")

    monkeypatch.setattr("gmail.tasks.enrich_email.BetterContactEmailProvider", FoundEmail)
    invitation_environment.now += timedelta(hours=30 if late else 2)
    handle_enrich_email(enrichment)

    send = Task.objects.get(task_type=Task.TaskType.GMAIL_FOLLOW_UP)
    delivery.refresh_from_db()
    assert send.payload["delivery_id"] == delivery.pk
    assert delivery.scheduled_at == receipt + timedelta(hours=24)
    assert send.scheduled_at >= delivery.scheduled_at
    if not late:
        assert send.scheduled_at == delivery.scheduled_at
    assert not Message.objects.exists()


@pytest.mark.parametrize("operator", OPERATORS)
def test_exhausted_lookup_holds_email_only_and_does_not_requeue_forever(monkeypatch, operator):
    deal = invitation_deal(operator=operator, email="")
    enrichment = _maybe_schedule_gmail_sequence(deal=deal, operator=operator)

    class NoEmail:
        name = "bettercontact"

        def enrich(self, lead, task):
            return EnrichmentResult(status=EnrichmentStatus.NOT_FOUND, provider=self.name)

    monkeypatch.setattr("gmail.tasks.enrich_email.BetterContactEmailProvider", NoEmail)
    handle_enrich_email(enrichment)
    enrichment.status = Task.Status.COMPLETED
    enrichment.save(update_fields=["status"])
    for _ in range(3):
        maybe_schedule_gmail_sequence(deal=deal, operator=operator)
    deal.refresh_from_db()
    deal.lead.refresh_from_db()
    assert deal.state == ProfileState.PENDING
    assert not deal.lead.email
    assert deal.lead.email_providers_tried == ["bettercontact"]
    enrichment.refresh_from_db()
    assert "no usable address" in enrichment.error
    assert OutboundDelivery.objects.get().status == OutboundDelivery.Status.PLANNED
    assert not Task.objects.filter(task_type=Task.TaskType.GMAIL_FOLLOW_UP).exists()
    assert Task.objects.count() == 1
    assert not Message.objects.exists()


@pytest.mark.parametrize("operator", OPERATORS)
def test_later_reviewed_address_recovers_failed_lookup_without_restarting_clock(
    monkeypatch, invitation_environment, operator,
):
    from gmail.handoff import recover_invitation_gmail_sequences

    deal = invitation_deal(operator=operator, email="")
    enrichment = _maybe_schedule_gmail_sequence(deal=deal, operator=operator)
    delivery = OutboundDelivery.objects.get()
    frozen = (delivery.pk, delivery.scheduled_at, delivery.frozen_subject, delivery.frozen_body)

    class NoEmail:
        name = "bettercontact"

        def enrich(self, lead, task):
            return EnrichmentResult(status=EnrichmentStatus.NOT_FOUND, provider=self.name)

    monkeypatch.setattr("gmail.tasks.enrich_email.BetterContactEmailProvider", NoEmail)
    handle_enrich_email(enrichment)
    enrichment.status = Task.Status.COMPLETED
    enrichment.save(update_fields=["status"])
    invitation_environment.now += timedelta(days=3)
    deal.lead.email = "later-reviewed@example.invalid"
    deal.lead.save(update_fields=["email"])

    recover_invitation_gmail_sequences(operator=operator, campaign_ids=[deal.campaign_id])

    delivery.refresh_from_db()
    task = Task.objects.get(task_type=Task.TaskType.GMAIL_FOLLOW_UP)
    assert task.payload["delivery_id"] == delivery.pk
    assert (delivery.pk, delivery.scheduled_at, delivery.frozen_subject, delivery.frozen_body) == frozen
    assert task.scheduled_at >= delivery.scheduled_at
    assert Task.objects.filter(task_type=Task.TaskType.ENRICH_EMAIL).count() == 1
    assert not Message.objects.exists()


@pytest.mark.parametrize("source", [Message.Source.LINKEDIN, Message.Source.GMAIL])
def test_recorded_reply_before_invitation_scheduler_blocks_email(source):
    deal = invitation_deal()
    Message.objects.create(
        lead=deal.lead, source=source, direction=Message.Direction.INBOUND,
        external_id=f"{source}-reply", body="Please stop", sent_at=timezone.now(),
    )
    assert _maybe_schedule_gmail_sequence(deal=deal, operator="Arian") is None
    assert not Task.objects.exists()
    assert not OutboundDelivery.objects.exists()


@pytest.mark.parametrize("scenario", ["inactive", "disqualified", "disabled", "no_mapping", "no_copy", "drip_owned"])
def test_invitation_scheduler_safe_skip_reasons(monkeypatch, scenario):
    deal = invitation_deal(with_copy=scenario != "no_copy")
    if scenario == "inactive":
        deal.campaign.status = Campaign.Status.DISABLED
        deal.campaign.save(update_fields=["status"])
    elif scenario == "disqualified":
        deal.lead.disqualified = True
        deal.lead.save(update_fields=["disqualified"])
    elif scenario == "disabled":
        monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", False)
    elif scenario == "no_mapping":
        monkeypatch.setattr("gmail.handoff._operator_can_send_gmail", lambda operator: False)
    elif scenario == "drip_owned":
        monkeypatch.setattr("drip.services.ownership.drip_owns_channel", lambda **kwargs: True)
    assert _maybe_schedule_gmail_sequence(deal=deal, operator="Arian") is None
    assert not Task.objects.exists()
    assert not OutboundDelivery.objects.exists()


def test_recovery_is_exact_sender_campaign_scoped_and_bounded():
    from gmail.handoff import recover_invitation_gmail_sequences

    first = invitation_deal()
    second = invitation_deal()
    chuka = invitation_deal(operator="Chuka")
    old_mode = invitation_deal(mode="post_acceptance", connected=True)
    observed = invitation_deal(receipt=False)
    campaigns = [deal.campaign_id for deal in (first, second, chuka, old_mode, observed)]

    recover_invitation_gmail_sequences(operator="Arian", campaign_ids=campaigns, limit=1)
    assert Task.objects.count() == 1
    recover_invitation_gmail_sequences(operator="Arian", campaign_ids=campaigns, limit=100)
    assert set(Task.objects.values_list("payload__deal_id", flat=True)) == {first.pk, second.pk}
    before = list(Task.objects.order_by("pk").values("pk", "scheduled_at", "payload"))
    recover_invitation_gmail_sequences(operator="Arian", campaign_ids=campaigns, limit=100)
    assert list(Task.objects.order_by("pk").values("pk", "scheduled_at", "payload")) == before


def test_recovery_restores_missing_planned_work_without_overwriting_frozen_identity():
    from gmail.handoff import recover_invitation_gmail_sequences
    from linkedin.message_delivery import ensure_message_enrollment, get_or_create_delivery

    deal = invitation_deal()
    enrollment = ensure_message_enrollment(deal=deal, audience_key=deal.lead.icp, operator="Arian")
    delivery, _ = get_or_create_delivery(
        enrollment=enrollment, channel="gmail", step_key="email-1", reference_at=deal.invitation_sent_at,
    )
    frozen = (delivery.pk, delivery.scheduled_at, delivery.frozen_subject, delivery.frozen_body)
    assert delivery.task_id is None and delivery.status == OutboundDelivery.Status.PLANNED

    recover_invitation_gmail_sequences(operator="Arian", campaign_ids=[deal.campaign_id])

    delivery.refresh_from_db()
    assert delivery.task_id is not None
    assert (delivery.pk, delivery.scheduled_at, delivery.frozen_subject, delivery.frozen_body) == frozen
    assert Task.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_simultaneous_hooks_serialize_to_one_delivery_and_task():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import connections

    deal = invitation_deal()
    gate = Barrier(2)

    def schedule():
        try:
            local_deal = Deal.objects.get(pk=deal.pk)
            gate.wait(timeout=10)
            task = _maybe_schedule_gmail_sequence(deal=local_deal, operator="Arian")
            return task.pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(schedule) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]

    assert results[0] == results[1]
    assert Task.objects.count() == 1
    assert CampaignMessageEnrollment.objects.count() == 1
    assert OutboundDelivery.objects.count() == 1


def test_daemon_heal_recovers_invitation_email_even_with_linkedin_followups_disabled(monkeypatch):
    from linkedin.daemon import heal_tasks

    deal = invitation_deal()
    profile = LinkedInProfile.objects.get(user=deal.campaign.user)
    session = SimpleNamespace(
        django_user=deal.campaign.user,
        linkedin_profile=profile,
        campaigns=Campaign.objects.filter(pk=deal.campaign_id),
    )
    monkeypatch.setattr("linkedin.daemon.ENABLE_FOLLOW_UP", False)
    monkeypatch.setattr("linkedin.daemon.ENABLE_SWEEP_CONNECTIONS", False)
    monkeypatch.setattr("linkedin.daemon.enqueue_status_summary", lambda **kwargs: None)
    monkeypatch.setattr("linkedin.discovery.collector.reconcile_discovery_tasks", lambda *args: False)

    heal_tasks(session)

    task = Task.objects.get(task_type=Task.TaskType.GMAIL_FOLLOW_UP)
    assert task.payload["deal_id"] == deal.pk
    assert task.scheduled_at == deal.invitation_sent_at + timedelta(hours=24)
    assert not Task.objects.filter(task_type=Task.TaskType.FOLLOW_UP).exists()
