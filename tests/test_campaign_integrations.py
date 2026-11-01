"""Synthetic normal-campaign enrollments. No browser, credentials or sends."""
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from crm.models import Account, Deal, Lead, Message
from linkedin import campaign_callbacks as callbacks, campaign_integrations as integrations, conf
from linkedin import message_delivery_runtime as runtime
from linkedin.enums import ProfileState
from linkedin.models import Campaign, CampaignSubmission, LinkedInProfile, OutboundDelivery, Task
from tests.tasks.test_message_program_runtime import _version, AUDIENCE_KEY

pytestmark = pytest.mark.django_db


@pytest.fixture
def body(monkeypatch):
    user = User.objects.create(username="ariantajbakh")
    LinkedInProfile.objects.create(user=user, linkedin_username="ariantajbakh", linkedin_password="synthetic", active=True)
    campaign = Campaign.objects.create(name="External campaign", user=user, status="disabled", connection_priority=10)
    version = _version(campaign)
    monkeypatch.setattr(conf, "ENABLE_CAMPAIGN_INTEGRATIONS", True)
    monkeypatch.setattr(conf, "OUTREACH_CALLBACK_SECRET", "synthetic" * 8)
    monkeypatch.setattr(conf, "OUTREACH_CALLBACK_ALLOWED_HOSTS", "receiver.example")
    return dict(source="synthetic", reference_id="person:1", campaign_id=campaign.pk,
                message_version_id=version.pk, owner_username=user.username,
                linkedin_url="https://www.linkedin.com/in/synthetic-person/", first_name="Synthetic",
                last_name="Person", company_name="Synthetic Company", icp=AUDIENCE_KEY,
                callback_url="https://receiver.example/completed")


def snapshot():
    return {model.__name__: list(model.objects.order_by("pk").values()) for model in
            (Lead, Deal, Campaign, CampaignSubmission, OutboundDelivery, Task)}


def test_preview_is_read_only_and_apply_freezes_normal_campaign_copy(body):
    before = snapshot()
    assert integrations.submit(body)["status"] == "eligible"
    assert snapshot() == before
    result = integrations.submit(body, apply=True)
    assert result["status"] == "enrolled"
    assert result["note"] == "Hi Synthetic, frozen connect for Synthetic Company."
    row = CampaignSubmission.objects.get()
    assert row.enrollment.operator == "Arian"
    assert row.enrollment.audience_key == AUDIENCE_KEY
    assert row.enrollment.deal.state == ProfileState.READY_TO_CONNECT
    assert row.enrollment.message_version_id == body["message_version_id"]
    assert Task.objects.count() == 0  # disabled campaign remains disabled
    assert OutboundDelivery.objects.count() == 1  # follow-ups created by normal acceptance flow
    after = snapshot()
    assert integrations.submit(body, apply=True) == result
    assert snapshot() == after


@pytest.mark.parametrize("status", ["ready", "attempting", "sent", "uncertain", "failed"])
def test_old_receipt_blocks_a_fresh_reference_even_without_campaign_history(body, status):
    from linkedin.models import RB2BVisitor
    profile = LinkedInProfile.objects.get()
    old = RB2BVisitor.objects.create(public_identifier="synthetic-person", linkedin_url=body["linkedin_url"],
                                    linkedin_profile=profile, status=status)
    with pytest.raises(integrations.EnrollmentHold, match="historical_attempt_no_resend"):
        integrations.submit(body, apply=True)
    assert not CampaignSubmission.objects.exists() and not Deal.objects.exists()
    old.refresh_from_db()
    assert old.status == status


def test_active_enrollment_reuses_pending_campaign_task_without_retiming(body, monkeypatch):
    campaign = Campaign.objects.get(pk=body["campaign_id"])
    campaign.status = "active"
    campaign.save(update_fields=["status"])
    monkeypatch.setattr("linkedin.daemon.ENABLE_CONNECT", True)
    original = Task.objects.create(task_type="connect", scheduled_at=timezone.now() + timedelta(hours=1),
                                   payload={"campaign_id": campaign.pk})
    before = list(Task.objects.values())
    integrations.submit(body, apply=True)
    assert list(Task.objects.values()) == before
    original.refresh_from_db()
    assert original.status == "pending"


def test_active_enrollment_creates_only_normal_connect_task(body, monkeypatch):
    Campaign.objects.filter(pk=body["campaign_id"]).update(status="active")
    monkeypatch.setattr("linkedin.daemon.ENABLE_CONNECT", True)
    integrations.submit(body, apply=True)
    assert list(Task.objects.values_list("task_type", flat=True)) == ["connect"]


@pytest.mark.parametrize("change,reason", [
    ({"owner_username": "chuka"}, "campaign_owner_mismatch"),
    ({"message_version_id": 99999}, "message_version_changed"),
    ({"note": "unapproved copy"}, "unsupported_fields"),
    ({"campaign_id": True}, "positive_integer_required"),
    ({"linkedin_url": "https://www.linkedin.com/sales/lead/x"}, "exact_linkedin"),
    ({"message_version_hash": "0" * 64}, "message_version_hash_mismatch"),
    ({"message_version_hash": "not-a-sha256"}, "message_version_hash_invalid"),
])
def test_invalid_enrollment_is_atomic(body, change, reason):
    before = snapshot()
    with pytest.raises(integrations.EnrollmentHold, match=reason):
        integrations.submit({**body, **change}, apply=True)
    assert snapshot() == before


def test_exact_copy_hash_precondition_is_optional_and_keeps_old_digests(body):
    from linkedin.models import MessageProgramVersion
    assert "message_version_hash" not in integrations.normalized_input(body)
    content_hash = MessageProgramVersion.objects.get(pk=body["message_version_id"]).content_hash
    checked = {**body, "message_version_hash": content_hash}
    assert integrations.submit(checked)["status"] == "eligible"
    assert integrations.submit(checked, apply=True)["status"] == "enrolled"
    assert CampaignSubmission.objects.get().input_hash == integrations._digest(integrations.normalized_input(checked))


def test_disabled_feature_blocks_apply_but_not_preview(body, monkeypatch):
    monkeypatch.setattr(conf, "ENABLE_CAMPAIGN_INTEGRATIONS", False)
    assert integrations.submit(body)["status"] == "eligible"
    with pytest.raises(integrations.EnrollmentHold, match="disabled"):
        integrations.submit(body, apply=True)
    assert CampaignSubmission.objects.count() == 0


def test_person_is_reused_and_second_reference_never_reenrolls(body):
    lead = Lead.objects.create(public_identifier="synthetic-person", linkedin_url=body["linkedin_url"])
    result = integrations.submit(body, apply=True)
    assert Deal.objects.get(pk=result["deal_id"]).lead_id == lead.pk
    before = snapshot()
    with pytest.raises(integrations.EnrollmentHold, match="existing_campaign_history"):
        integrations.submit({**body, "reference_id": "another-ref"}, apply=True)
    with pytest.raises(integrations.EnrollmentHold, match="reference_payload_conflict"):
        integrations.submit({**body, "company_name": "Changed"}, apply=True)
    assert snapshot() == before


@pytest.mark.parametrize("url", ["http://linkedin.com/in/synthetic-person", "https://ca.linkedin.com/in/Synthetic-Person/?trk=x"])
def test_legacy_url_aliases_reuse_the_same_person(body, url):
    lead = Lead.objects.create(linkedin_url=url)
    result = integrations.submit(body, apply=True)
    assert Deal.objects.get(pk=result["deal_id"]).lead_id == lead.pk
    assert Lead.objects.count() == 1


@pytest.mark.parametrize("kind", ["suppressed", "conversation", "customer", "identity", "campaign"])
def test_existing_relationships_hold_without_mutation(body, kind):
    lead = Lead.objects.create(public_identifier="synthetic-person", linkedin_url=body["linkedin_url"],
                               first_name="Synthetic", last_name="Person")
    if kind == "suppressed":
        lead.disqualified = True
        lead.save()
    elif kind == "conversation":
        Message.objects.create(lead=lead, source="linkedin", direction="inbound", body="Synthetic reply",
                               sender="Synthetic", external_id="synthetic-reply", sent_at=timezone.now())
    elif kind == "customer":
        Account.objects.create(name="Synthetic Company")
    elif kind == "identity":
        Lead.objects.create(public_identifier="synthetic-person", linkedin_url="https://www.linkedin.com/in/different/")
    else:
        Deal.objects.create(lead=lead, campaign_id=body["campaign_id"])
    before = snapshot()
    with pytest.raises(integrations.EnrollmentHold):
        integrations.submit(body, apply=True)
    assert snapshot() == before


def completed(body):
    result = integrations.submit(body, apply=True)
    row = CampaignSubmission.objects.get(pk=result["submission_id"])
    Deal.objects.filter(pk=row.enrollment.deal_id).update(invitation_sent_at=timezone.now(), invitation_sender="Arian")
    return row


def test_local_outcomes_freeze_once_and_ack_without_http_or_outreach(body, monkeypatch):
    row = completed({**body, "callback_url": ""})
    frozen = {k: v for k, v in snapshot().items() if k != "CampaignSubmission"}
    post = Mock()
    monkeypatch.setattr(callbacks, "post", post)
    events = callbacks.local_outcomes(operator="Arian", source=body["source"])
    assert len(events) == 1 and events[0]["outcome"] == "sent"
    assert events[0]["event_id"] == str(row.event_id)
    assert events[0]["submission_id"] == row.pk
    assert callbacks.local_outcomes(operator="Arian", source=body["source"]) == events
    assert callbacks.deliver(operator="Arian") == 0
    post.assert_not_called()
    receipts = [{"submission_id": row.pk, "event_id": str(row.event_id)}]
    assert callbacks.acknowledge_local_outcomes(operator="Arian", source=body["source"], receipts=receipts) == 1
    assert callbacks.acknowledge_local_outcomes(operator="Arian", source=body["source"], receipts=receipts) == 0
    assert callbacks.local_outcomes(operator="Arian", source=body["source"]) == []
    assert {k: v for k, v in snapshot().items() if k != "CampaignSubmission"} == frozen


def test_local_outcomes_wait_for_evidence_and_do_not_starve_later_completion(body):
    integrations.submit({**body, "callback_url": ""}, apply=True)
    row = completed({**body, "callback_url": "", "reference_id": "person:2",
                     "linkedin_url": "https://www.linkedin.com/in/synthetic-two/"})
    events = callbacks.local_outcomes(operator="Arian", source=body["source"], limit=1)
    assert len(events) == 1 and events[0]["submission_id"] == row.pk


@pytest.mark.parametrize("change", ["source", "operator", "uuid", "http", "not_ready"])
def test_local_ack_rejects_wrong_scope_or_uncommitted_outcome(body, change):
    row = completed({**body, "callback_url": body["callback_url"] if change == "http" else ""})
    if change != "not_ready":
        callbacks.reconcile(operator="Arian", include_local=True)
    before = snapshot()
    with pytest.raises(ValueError, match="identity_mismatch"):
        callbacks.acknowledge_local_outcomes(
            operator="Chuka" if change == "operator" else "Arian",
            source="other" if change == "source" else body["source"],
            receipts=[{"submission_id": row.pk, "event_id": "wrong" if change == "uuid" else str(row.event_id)}])
    assert snapshot() == before


def test_local_export_and_ack_are_feature_gated_and_source_scoped(body, monkeypatch):
    row = completed({**body, "callback_url": ""})
    assert callbacks.local_outcomes(operator="Chuka", source=body["source"]) == []
    assert callbacks.local_outcomes(operator="Arian", source="other") == []
    monkeypatch.setattr(conf, "ENABLE_CAMPAIGN_INTEGRATIONS", False)
    before = snapshot()
    with pytest.raises(ValueError, match="disabled"):
        callbacks.local_outcomes(operator="Arian", source=body["source"])
    with pytest.raises(ValueError, match="disabled"):
        callbacks.acknowledge_local_outcomes(operator="Arian", source=body["source"], receipts=[])
    assert snapshot() == before


def test_local_handoff_public_cli_exports_and_acknowledges_exact_receipt(body, tmp_path):
    import json
    from io import StringIO
    from django.core.management import call_command
    from django.core.management.base import CommandError
    row = completed({**body, "callback_url": ""})
    with pytest.raises(CommandError):
        call_command("campaign_enrollments", "outcomes", source=body["source"], operator="Arian")
    output = StringIO()
    call_command("campaign_enrollments", "outcomes", source=body["source"], operator="Arian", apply=True, stdout=output)
    event = json.loads(output.getvalue())["result"]["events"][0]
    path = tmp_path / "ack.json"
    path.write_text(json.dumps([{"submission_id": row.pk, "event_id": event["event_id"]}]))
    call_command("campaign_enrollments", "ack-outcomes", source=body["source"], operator="Arian",
                 apply=True, input=path, stdout=StringIO())
    row.refresh_from_db()
    assert row.callback_delivered_at is not None and row.callback_payload == event


def test_callback_retry_is_durable_idempotent_and_never_changes_outreach(body, monkeypatch):
    row = completed(body)
    frozen = {k: v for k, v in snapshot().items() if k != "CampaignSubmission"}
    assert callbacks.reconcile(operator="Chuka") == 0
    assert callbacks.reconcile(operator="Arian") == 1
    row.refresh_from_db()
    event = row.callback_payload
    assert event["event_id"] == str(row.event_id)
    assert event["outcome"] == "sent"
    post = Mock(side_effect=TimeoutError)
    monkeypatch.setattr(callbacks, "post", post)
    assert callbacks.deliver(operator="Arian") == 0
    row.refresh_from_db()
    assert row.callback_attempts == 1 and row.callback_next_at > timezone.now()
    assert callbacks.deliver(operator="Arian") == 0
    assert post.call_count == 1
    CampaignSubmission.objects.filter(pk=row.pk).update(callback_next_at=timezone.now() - timedelta(seconds=1))
    post.side_effect = None
    post.return_value = True
    assert callbacks.deliver(operator="Arian") == 1
    assert callbacks.deliver(operator="Arian") == callbacks.reconcile(operator="Arian") == 0
    row.refresh_from_db()
    assert row.callback_payload == event
    assert {k: v for k, v in snapshot().items() if k != "CampaignSubmission"} == frozen


def test_pending_backlog_does_not_starve_completed_callback(body):
    integrations.submit(body, apply=True)
    second = {**body, "reference_id": "person:2", "linkedin_url": "https://www.linkedin.com/in/synthetic-two/"}
    row = completed(second)
    assert callbacks.reconcile(operator="Arian", limit=1) == 1
    row.refresh_from_db()
    assert row.callback_ready_at


@pytest.mark.parametrize("state,delivery_state,expected", [
    (ProfileState.QUALIFIED, OutboundDelivery.Status.FAILED, "enrolled"),
    (ProfileState.FAILED, OutboundDelivery.Status.FAILED, "failed"),
    (ProfileState.QUALIFIED, OutboundDelivery.Status.UNCLEAR, "uncertain"),
    (ProfileState.PENDING, OutboundDelivery.Status.STOPPED, "held"),
    # Crash after LinkedIn took the invitation, before the Deal ledger.
    (ProfileState.READY_TO_CONNECT, OutboundDelivery.Status.SENT, "sent"),
    (ProfileState.PENDING, OutboundDelivery.Status.SENT, "sent"),
])
def test_only_final_evidence_completes_notifications(body, state, delivery_state, expected):
    result = integrations.submit(body, apply=True)
    Deal.objects.filter(pk=result["deal_id"]).update(state=state)
    OutboundDelivery.objects.filter(pk=result["delivery_id"]).update(status=delivery_state)
    row = CampaignSubmission.objects.get()
    assert integrations.status(row)["status"] == expected
    assert callbacks.reconcile(operator="Arian") == int(expected != "enrolled")
    before = snapshot()
    assert integrations.submit(body, apply=True)["status"] == expected
    assert snapshot() == before


@pytest.mark.parametrize("url", ["http://receiver.example/", "https://evil.example/", "https://receiver.example:444/",
                                "https://u@receiver.example/", "https://receiver.example/?secret=x", "https://receiver.example/#x"])
def test_callback_destinations_fail_closed(body, url):
    with pytest.raises(ValueError):
        callbacks.validate_callback_url(url)


def test_callback_rejects_private_or_mixed_dns_before_connection(body, monkeypatch):
    monkeypatch.setattr(callbacks.socket, "getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))])
    connect = Mock()
    monkeypatch.setattr(callbacks.socket, "create_connection", connect)
    with pytest.raises(ValueError, match="non_public"):
        callbacks.PublicHTTPSConnection("receiver.example").connect()
    connect.assert_not_called()


def test_callback_signature_is_exact_and_redirect_is_not_followed(body, monkeypatch):
    import hashlib
    import hmac
    import json
    connection = Mock()
    connection.getresponse.return_value.status = 302
    factory = Mock(return_value=connection)
    monkeypatch.setattr(callbacks, "PublicHTTPSConnection", factory)
    monkeypatch.setattr(callbacks.time, "time", lambda: 1234567890)
    payload = {"event_id": "synthetic", "outcome": "sent"}
    assert callbacks.post(body["callback_url"], payload) is False
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    expected = hmac.new(conf.OUTREACH_CALLBACK_SECRET.encode(), b"1234567890." + raw, hashlib.sha256).hexdigest()
    connection.request.assert_called_once_with("POST", "/completed", body=raw, headers={
        "Content-Type": "application/json", "X-OpenOutreach-Signature": "t=1234567890,v1=" + expected})
    connection.close.assert_called_once()


def test_callback_exhaustion_retains_receipt_without_changing_outreach(body, monkeypatch):
    row = completed(body)
    callbacks.reconcile(operator="Arian")
    CampaignSubmission.objects.filter(pk=row.pk).update(callback_ready_at=timezone.now() - timedelta(days=3))
    post = Mock()
    monkeypatch.setattr(callbacks, "post", post)
    frozen = {k: v for k, v in snapshot().items() if k != "CampaignSubmission"}
    assert callbacks.deliver(operator="Arian") == 0
    row.refresh_from_db()
    assert row.callback_abandoned_at and row.callback_payload["outcome"] == "sent"
    post.assert_not_called()
    assert {k: v for k, v in snapshot().items() if k != "CampaignSubmission"} == frozen


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("same_reference", [True, False])
def test_concurrent_submissions_never_duplicate_person_or_enrollment(body, monkeypatch, same_reference):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from django.db import close_old_connections, connection
    if connection.vendor != "postgresql":
        pytest.skip("Requires isolated PostgreSQL row locks and shared threaded database")
    barrier = threading.Barrier(2)
    lookup = integrations._campaign

    def synchronized_lookup(value, *, lock=False):
        result = lookup(value, lock=lock)
        if not lock:
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(integrations, "_campaign", synchronized_lookup)

    def run(reference):
        close_old_connections()
        try:
            return integrations.submit({**body, "reference_id": reference}, apply=True)
        except integrations.EnrollmentHold as exc:
            return {"status": "held", "reason": str(exc)}
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [body["reference_id"], body["reference_id"] if same_reference else "person:other"]))
    assert CampaignSubmission.objects.count() == Deal.objects.count() == Lead.objects.count() == 1
    assert OutboundDelivery.objects.count() == 1
    assert sum(result["status"] == "enrolled" for result in results) == (2 if same_reference else 1)


def _enrolled_campaign_person(fake_session, monkeypatch):
    """A local-transport enrollment owned by the fake session's sender."""
    fake_session.django_user.username = "ariantajbakh"
    fake_session.django_user.save(update_fields=["username"])
    fake_session.linkedin_profile.linkedin_username = "ariantajbakh"
    fake_session.linkedin_profile.save(update_fields=["linkedin_username"])
    campaign = fake_session.campaign
    version = _version(campaign)
    monkeypatch.setattr(conf, "ENABLE_CAMPAIGN_INTEGRATIONS", True)
    monkeypatch.setattr(conf, "ENABLE_FOLLOW_UP", True)
    monkeypatch.setattr("linkedin.daemon.ENABLE_CONNECT", True)
    monkeypatch.setattr("linkedin.tasks.connect.ENABLE_CONNECT", True)
    monkeypatch.setattr("gmail.handoff.maybe_schedule_gmail_sequence", lambda **kw: None)
    body = dict(source="synthetic", reference_id="person:observed", campaign_id=campaign.pk,
                message_version_id=version.pk, owner_username="ariantajbakh",
                linkedin_url="https://www.linkedin.com/in/observed-person/", first_name="Observed",
                last_name="Person", company_name="Observed Company", icp=AUDIENCE_KEY, callback_url="")
    result = integrations.submit(body, apply=True)
    assert result["status"] == "enrolled"
    return campaign, body, result


def _connect_once(fake_session, monkeypatch, campaign, result, live_status, invitation):
    """One normal connect pass for the enrolled person; `invitation` stands in for LinkedIn."""
    from linkedin.tasks.connect import ConnectStrategy, handle_connect
    deal = Deal.objects.get(pk=result["deal_id"])
    candidate = {"public_identifier": "observed-person", "url": deal.lead.linkedin_url, "lead_id": deal.lead_id,
                 "profile": {"first_name": "Observed", "last_name": "Person", "headline": "Founder",
                             "positions": [{"company_name": "Observed Company"}]}}
    strategy = ConnectStrategy(find_candidate=lambda session: candidate, pre_connect=None, delay=10,
                               action_fraction=1.0, qualifier=Mock(explain=lambda *a, **kw: ""))
    monkeypatch.setattr("linkedin.tasks.connect.strategy_for", lambda *a: strategy)
    monkeypatch.setattr("linkedin.actions.status.get_connection_status", lambda *a, **kw: live_status)
    monkeypatch.setattr("linkedin.actions.connect.send_connection_request", invitation)
    task = Task.objects.create(task_type="connect", scheduled_at=timezone.now(), status="running",
                               started_at=timezone.now(), payload={"campaign_id": campaign.pk})
    handle_connect(task, fake_session, {campaign.pk: strategy.qualifier})
    return deal


def _run_connect_after_profile_check(fake_session, monkeypatch, campaign, result, live_status):
    invitation = Mock(return_value=ProfileState.PENDING)
    deal = _connect_once(fake_session, monkeypatch, campaign, result, live_status, invitation)
    invitation.assert_not_called()
    return deal


@pytest.mark.parametrize("live_status,reason", [
    (ProfileState.CONNECTED, "already_connected"),
    (ProfileState.PENDING, "pending_invitation_exists"),
])
def test_profile_check_before_send_produces_one_final_outcome(fake_session, monkeypatch, live_status, reason):
    campaign, body, result = _enrolled_campaign_person(fake_session, monkeypatch)
    deal = _run_connect_after_profile_check(fake_session, monkeypatch, campaign, result, live_status)
    delivery = OutboundDelivery.objects.get(pk=result["delivery_id"])
    assert delivery.status == OutboundDelivery.Status.STOPPED and delivery.sent_at is None
    current = integrations.submit(body, apply=True)
    assert (current["status"], current["reason"]) == ("held", reason)
    events = callbacks.local_outcomes(operator="Arian", source="synthetic")
    assert [(e["outcome"], e["reason"]) for e in events] == [("held", reason)]
    deal.refresh_from_db()
    assert deal.invitation_sent_at is None and deal.invitation_sender == ""
    # The ordinary campaign path after the check is unchanged.
    assert Task.objects.filter(task_type="follow_up", payload__public_id="observed-person").exists() == (
        live_status == ProfileState.CONNECTED)


@pytest.mark.parametrize("live_status", [ProfileState.CONNECTED, ProfileState.PENDING])
def test_interrupted_send_found_by_profile_check_is_uncertain_and_never_retried(
        fake_session, monkeypatch, live_status):
    campaign, body, result = _enrolled_campaign_person(fake_session, monkeypatch)
    # A previous attempt crashed after entering `sending`, before any receipt.
    OutboundDelivery.objects.filter(pk=result["delivery_id"]).update(status=OutboundDelivery.Status.SENDING)
    _run_connect_after_profile_check(fake_session, monkeypatch, campaign, result, live_status)
    assert OutboundDelivery.objects.get(pk=result["delivery_id"]).status == OutboundDelivery.Status.UNCLEAR
    current = integrations.submit(body, apply=True)
    assert (current["status"], current["reason"]) == ("uncertain", "delivery_unclear")
    assert [e["outcome"] for e in callbacks.local_outcomes(operator="Arian", source="synthetic")] == ["uncertain"]


def test_receipt_already_closed_is_never_rewritten_by_a_later_profile_check(fake_session, monkeypatch):
    campaign, body, result = _enrolled_campaign_person(fake_session, monkeypatch)
    OutboundDelivery.objects.filter(pk=result["delivery_id"]).update(status=OutboundDelivery.Status.SENT)
    _run_connect_after_profile_check(fake_session, monkeypatch, campaign, result, ProfileState.PENDING)
    assert OutboundDelivery.objects.get(pk=result["delivery_id"]).status == OutboundDelivery.Status.SENT
    assert integrations.submit(body, apply=True)["status"] == "sent"


@pytest.mark.parametrize("live_status", [ProfileState.CONNECTED, ProfileState.PENDING])
def test_profile_check_state_and_receipt_commit_together(fake_session, monkeypatch, live_status):
    campaign, body, result = _enrolled_campaign_person(fake_session, monkeypatch)
    real_close = runtime.close_unsent_connection_delivery
    monkeypatch.setattr(runtime, "close_unsent_connection_delivery", Mock(side_effect=RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        _run_connect_after_profile_check(fake_session, monkeypatch, campaign, result, live_status)
    deal = Deal.objects.get(pk=result["deal_id"])
    # The crash rolled the state back with the receipt, so the lane retries it.
    assert deal.state == ProfileState.READY_TO_CONNECT and deal.connected_at is None
    assert OutboundDelivery.objects.get(pk=result["delivery_id"]).status not in {
        OutboundDelivery.Status.STOPPED, OutboundDelivery.Status.UNCLEAR}
    monkeypatch.setattr(runtime, "close_unsent_connection_delivery", real_close)
    _run_connect_after_profile_check(fake_session, monkeypatch, campaign, result, live_status)
    assert integrations.submit(body, apply=True)["status"] == "held"


@pytest.mark.parametrize("state,open_status,closed,outcome,reason", [
    (ProfileState.PENDING, OutboundDelivery.Status.PLANNED, OutboundDelivery.Status.STOPPED,
     "held", "pending_invitation_exists"),
    (ProfileState.PENDING, OutboundDelivery.Status.QUEUED, OutboundDelivery.Status.STOPPED,
     "held", "pending_invitation_exists"),
    (ProfileState.CONNECTED, OutboundDelivery.Status.PLANNED, OutboundDelivery.Status.STOPPED,
     "held", "already_connected"),
    (ProfileState.PENDING, OutboundDelivery.Status.SENDING, OutboundDelivery.Status.UNCLEAR,
     "uncertain", "delivery_unclear"),
    (ProfileState.CONNECTED, OutboundDelivery.Status.FAILED, OutboundDelivery.Status.UNCLEAR,
     "uncertain", "delivery_unclear"),
])
@pytest.mark.parametrize("local", [False, True])
def test_recovery_closes_a_receipt_stranded_by_an_interrupted_profile_check(
        body, state, open_status, closed, outcome, reason, local):
    """Code before the atomic check could commit the Deal state but not the receipt."""
    if local:
        body = {**body, "callback_url": ""}
    result = integrations.submit(body, apply=True)
    Deal.objects.filter(pk=result["deal_id"]).update(
        state=state, connected_at=timezone.now() if state == ProfileState.CONNECTED else None)
    OutboundDelivery.objects.filter(pk=result["delivery_id"]).update(status=open_status)
    row = CampaignSubmission.objects.get()
    assert integrations.status(row)["status"] == "enrolled"
    before = {k: v for k, v in snapshot().items() if k not in {"CampaignSubmission", "OutboundDelivery"}}
    if local:
        events = callbacks.local_outcomes(operator="Arian", source=body["source"])
        assert [(e["outcome"], e["reason"]) for e in events] == [(outcome, reason)]
    else:
        assert callbacks.reconcile(operator="Arian") == 1
    delivery = OutboundDelivery.objects.get(pk=result["delivery_id"])
    assert delivery.status == closed and delivery.sent_at is None
    row.refresh_from_db()
    assert (row.callback_payload["outcome"], row.callback_payload["reason"]) == (outcome, reason)
    # Receipt only: no Deal, Lead, Campaign or Task changed, nothing was queued.
    assert {k: v for k, v in snapshot().items() if k not in {"CampaignSubmission", "OutboundDelivery"}} == before
    assert callbacks.reconcile(operator="Arian", include_local=True) == 0


def test_recovery_leaves_in_flight_and_unrelated_receipts_alone(body):
    """A note still in flight, or one owned by another sender, is not touched."""
    result = integrations.submit(body, apply=True)
    OutboundDelivery.objects.filter(pk=result["delivery_id"]).update(status=OutboundDelivery.Status.SENDING)
    before = snapshot()
    assert callbacks.reconcile(operator="Arian") == 0
    assert callbacks.reconcile(operator="Chuka") == 0
    assert snapshot() == before


def test_interrupted_click_is_never_repeated_on_a_connectable_looking_profile(fake_session, monkeypatch):
    from linkedin.db.deals import get_ready_to_connect_profiles
    campaign, body, result = _enrolled_campaign_person(fake_session, monkeypatch)
    crashed = Mock(side_effect=RuntimeError("stopped after the click, before any receipt"))
    with pytest.raises(RuntimeError):
        _connect_once(fake_session, monkeypatch, campaign, result, ProfileState.READY_TO_CONNECT, crashed)
    crashed.assert_called_once()  # Mock only.
    assert OutboundDelivery.objects.get(pk=result["delivery_id"]).status == OutboundDelivery.Status.SENDING
    assert Deal.objects.get(pk=result["deal_id"]).state == ProfileState.READY_TO_CONNECT
    # A later normal pass still sees a Connect surface; the receipt decides.
    retry = Mock(return_value=ProfileState.PENDING)
    deal = _connect_once(fake_session, monkeypatch, campaign, result, ProfileState.READY_TO_CONNECT, retry)
    retry.assert_not_called()
    assert OutboundDelivery.objects.get(pk=result["delivery_id"]).status == OutboundDelivery.Status.UNCLEAR
    deal.refresh_from_db()
    assert deal.state == ProfileState.PENDING and deal.invitation_sent_at is None
    assert not get_ready_to_connect_profiles(fake_session)
    assert [e["outcome"] for e in callbacks.local_outcomes(operator="Arian", source="synthetic")] == ["uncertain"]
    # Further passes stay quiet.
    again = Mock(return_value=ProfileState.PENDING)
    _connect_once(fake_session, monkeypatch, campaign, result, ProfileState.READY_TO_CONNECT, again)
    again.assert_not_called()


@pytest.mark.parametrize("delivery_status,outcome", [
    (OutboundDelivery.Status.SENT, "sent"),  # stopped after the receipt, before the Deal state
    (OutboundDelivery.Status.UNCLEAR, "uncertain"),
])
def test_a_final_receipt_on_a_ready_deal_is_never_sent_again(fake_session, monkeypatch, delivery_status, outcome):
    campaign, body, result = _enrolled_campaign_person(fake_session, monkeypatch)
    OutboundDelivery.objects.filter(pk=result["delivery_id"]).update(status=delivery_status)
    invitation = Mock(return_value=ProfileState.PENDING)
    deal = _connect_once(fake_session, monkeypatch, campaign, result, ProfileState.READY_TO_CONNECT, invitation)
    invitation.assert_not_called()
    assert OutboundDelivery.objects.get(pk=result["delivery_id"]).status == delivery_status
    deal.refresh_from_db()
    assert deal.state == ProfileState.PENDING
    assert integrations.submit(body, apply=True)["status"] == outcome


def test_a_definite_pre_send_failure_still_retries_once(fake_session, monkeypatch):
    """No button found is not an attempted send; the campaign's existing retry is unchanged."""
    campaign, body, result = _enrolled_campaign_person(fake_session, monkeypatch)
    OutboundDelivery.objects.filter(pk=result["delivery_id"]).update(status=OutboundDelivery.Status.FAILED)
    invitation = Mock(return_value=ProfileState.PENDING)
    deal = _connect_once(fake_session, monkeypatch, campaign, result, ProfileState.READY_TO_CONNECT, invitation)
    invitation.assert_called_once()
    assert OutboundDelivery.objects.get(pk=result["delivery_id"]).status == OutboundDelivery.Status.SENT
    deal.refresh_from_db()
    assert deal.state == ProfileState.PENDING and deal.invitation_sender == "Arian"
    assert integrations.submit(body, apply=True)["status"] == "sent"


def test_a_submission_from_before_the_hash_check_replays_with_the_correct_hash(body):
    from linkedin.models import MessageProgramVersion
    original = integrations.submit(body, apply=True)  # submitted without the optional hash
    before = snapshot()
    content_hash = MessageProgramVersion.objects.get(pk=body["message_version_id"]).content_hash
    checked = {**body, "message_version_hash": content_hash}
    assert integrations.submit(checked) == original
    assert integrations.submit(checked, apply=True) == original
    assert snapshot() == before  # the original receipt, digest included, is kept
    for change in ({"message_version_hash": "0" * 64}, {"first_name": "Other"}):
        with pytest.raises(integrations.EnrollmentHold, match="reference_payload_conflict"):
            integrations.submit({**checked, **change}, apply=True)
    assert snapshot() == before


def _limit_of_one(fake_session, monkeypatch):
    import linkedin.models as models
    monkeypatch.setattr(models, "_LIMIT_OVERRIDES", {})
    fake_session.linkedin_profile.connect_daily_limit = 1
    fake_session.linkedin_profile.save(update_fields=["connect_daily_limit"])


def _budget(profile):
    return {"daily": profile._daily_count("connect"), "weekly": profile._weekly_count("connect"),
            "total": profile._total_daily_count(), "can_connect": profile.can_execute("connect")}


SPENT_ONE = {"daily": 1, "weekly": 1, "total": 1, "can_connect": False}


def test_an_interrupted_click_counts_once_at_its_attempt_time(fake_session, monkeypatch):
    from linkedin.models import ActionLog
    campaign, body, result = _enrolled_campaign_person(fake_session, monkeypatch)
    _limit_of_one(fake_session, monkeypatch)
    crashed = Mock(side_effect=RuntimeError("stopped after the click"))
    with pytest.raises(RuntimeError):
        _connect_once(fake_session, monkeypatch, campaign, result, ProfileState.READY_TO_CONNECT, crashed)
    attempt = ActionLog.objects.get(action_type="connect")
    assert _budget(fake_session.linkedin_profile) == SPENT_ONE
    # The next pass is over the limit, yet still settles the receipt without sending or charging again.
    retry = Mock(return_value=ProfileState.PENDING)
    _connect_once(fake_session, monkeypatch, campaign, result, ProfileState.READY_TO_CONNECT, retry)
    retry.assert_not_called()
    assert OutboundDelivery.objects.get(pk=result["delivery_id"]).status == OutboundDelivery.Status.UNCLEAR
    assert [e["outcome"] for e in callbacks.local_outcomes(operator="Arian", source="synthetic")] == ["uncertain"]
    assert _budget(fake_session.linkedin_profile) == SPENT_ONE
    assert list(ActionLog.objects.values_list("pk", "created_at")) == [(attempt.pk, attempt.created_at)]


def test_a_confirmed_send_counts_once_even_if_the_process_stops_after_its_receipt(fake_session, monkeypatch):
    from linkedin.tasks import connect
    campaign, body, result = _enrolled_campaign_person(fake_session, monkeypatch)
    _limit_of_one(fake_session, monkeypatch)
    real_state = connect.set_profile_state
    monkeypatch.setattr(connect, "set_profile_state", Mock(side_effect=RuntimeError("stopped after the receipt")))
    invitation = Mock(return_value=ProfileState.PENDING)
    with pytest.raises(RuntimeError):
        _connect_once(fake_session, monkeypatch, campaign, result, ProfileState.READY_TO_CONNECT, invitation)
    invitation.assert_called_once()  # Mock only.
    assert OutboundDelivery.objects.get(pk=result["delivery_id"]).status == OutboundDelivery.Status.SENT
    assert _budget(fake_session.linkedin_profile) == SPENT_ONE
    assert [e["outcome"] for e in callbacks.local_outcomes(operator="Arian", source="synthetic")] == ["sent"]
    monkeypatch.setattr(connect, "set_profile_state", real_state)
    again = Mock(return_value=ProfileState.PENDING)
    _connect_once(fake_session, monkeypatch, campaign, result, ProfileState.READY_TO_CONNECT, again)
    again.assert_not_called()
    assert Deal.objects.get(pk=result["deal_id"]).state == ProfileState.PENDING
    assert _budget(fake_session.linkedin_profile) == SPENT_ONE


def test_a_completed_send_is_counted_exactly_once(fake_session, monkeypatch):
    campaign, body, result = _enrolled_campaign_person(fake_session, monkeypatch)
    _limit_of_one(fake_session, monkeypatch)
    invitation = Mock(return_value=ProfileState.PENDING)
    _connect_once(fake_session, monkeypatch, campaign, result, ProfileState.READY_TO_CONNECT, invitation)
    invitation.assert_called_once()
    assert _budget(fake_session.linkedin_profile) == SPENT_ONE


@pytest.mark.parametrize("outcome", ["no_button", "limit_popup"])
def test_a_definite_no_send_returns_the_reserved_budget(fake_session, monkeypatch, outcome):
    from linkedin.exceptions import ReachedConnectionLimit
    from linkedin.models import ActionLog
    campaign, body, result = _enrolled_campaign_person(fake_session, monkeypatch)
    _limit_of_one(fake_session, monkeypatch)
    invitation = (Mock(return_value=ProfileState.QUALIFIED) if outcome == "no_button"
                  else Mock(side_effect=ReachedConnectionLimit("Weekly connection limit pop up appeared")))
    monkeypatch.setattr(fake_session.linkedin_profile, "mark_exhausted", Mock())
    _connect_once(fake_session, monkeypatch, campaign, result, ProfileState.READY_TO_CONNECT, invitation)
    invitation.assert_called_once()
    assert OutboundDelivery.objects.get(pk=result["delivery_id"]).status == OutboundDelivery.Status.FAILED
    assert not ActionLog.objects.exists()
    assert _budget(fake_session.linkedin_profile) == {"daily": 0, "weekly": 0, "total": 0, "can_connect": True}


def test_a_pending_invite_found_mid_attempt_keeps_its_reservation(fake_session, monkeypatch):
    """A direct Connect click earlier in the attempt may be what LinkedIn now shows as pending."""
    from linkedin.actions.connect import ExistingPendingInvite
    campaign, body, result = _enrolled_campaign_person(fake_session, monkeypatch)
    _limit_of_one(fake_session, monkeypatch)
    invitation = Mock(side_effect=ExistingPendingInvite("observed-person"))
    _connect_once(fake_session, monkeypatch, campaign, result, ProfileState.READY_TO_CONNECT, invitation)
    assert OutboundDelivery.objects.get(pk=result["delivery_id"]).status == OutboundDelivery.Status.STOPPED
    assert _budget(fake_session.linkedin_profile) == SPENT_ONE
    current = integrations.submit(body, apply=True)
    assert (current["status"], current["reason"]) == ("held", "pending_invitation_exists")
