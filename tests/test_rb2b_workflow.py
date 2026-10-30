import json
from datetime import datetime, timedelta, timezone as dtz
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from api.rb2b_webhook import normalize_visitor, merge_receipt
from crm.models import Lead, Deal, Message, Account, Opportunity
from linkedin import rb2b
from linkedin.models import (RB2BVisitor, RB2BNotification, LinkedInProfile, LinkedInDiscoveryLead,
                             Campaign, Task, DaemonHeartbeat, ActionLog, OutreachSuppression)
from tests.factories import UserFactory


@pytest.fixture
def owner(db):
    return LinkedInProfile.objects.create(user=UserFactory(username="ariantajbakh"),
                                         linkedin_username="ariantajbakh", active=True)


@pytest.fixture
def row(owner):
    raw = {"LinkedIn URL": "https://www.linkedin.com/in/new-visitor/", "Seen At": "2026-10-30T12:00:00Z",
           "First Name": "New", "Last Name": "Visitor", "Company Name": "Example"}
    visitor = normalize_visitor(raw)
    data, _ = merge_receipt({}, visitor, received_at=timezone.now().isoformat())
    return RB2BVisitor.objects.create(public_identifier=visitor["public_id"], linkedin_url=visitor["url"],
                                     linkedin_profile=owner, events=data["rb2b_intake"]["events"])


def decision(row):
    return {"decision": "qualify", "event_hash": next(iter(row.events)), "linkedin_url": row.linkedin_url,
            "member_urn": "urn:li:fsd_profile:SYNTHETIC", "identity_verified": True, "fit_verified": True,
            "relationships_checked": True, "invitations_checked": True, "evidence_urls": [row.linkedin_url],
            "fit_reason": "Synthetic sourced review", "checked_at": timezone.now().isoformat(),
            "first_name": "New", "last_name": "Visitor", "company_name": "Example", "title": "Security lead",
            "icp": "Advisors", "note": "Hi New, good to connect."}


@pytest.fixture
def ready(row):
    return rb2b.review(row.pk, decision(row))


@pytest.fixture
def enabled(monkeypatch, owner):
    monkeypatch.setenv("RB2B_IAB_SENDS_ENABLED", "true")
    monkeypatch.setenv("RB2B_SLACK_CHANNEL_ID", "C_SYNTHETIC_RB2B")
    monkeypatch.setattr(rb2b.conf, "SLACK_BOT_TOKEN", "synthetic")
    monkeypatch.setattr(rb2b.conf, "SLACK_REPLIES_WEBHOOK_URL", "synthetic")
    monkeypatch.setattr(rb2b.conf, "ENABLE_CONNECT", True)
    monkeypatch.setattr(rb2b.conf, "ENABLE_REALTIME_LISTENER", True)
    monkeypatch.setattr(rb2b.conf, "ENABLE_LINKEDIN_DAEMON", True)
    monkeypatch.setattr(rb2b.conf, "ENABLE_ACTIVE_HOURS", False)
    monkeypatch.setattr(rb2b.conf, "REST_DAYS", ())
    DaemonHeartbeat.objects.create(sender="Arian", last_alive=timezone.now(),
                                   rb2b_coordination=rb2b.coordination_config())


def preflight(row):
    return {"sender": "Arian", "linkedin_url": row.linkedin_url, "checked_at": timezone.now().isoformat(),
            **{k: True for k in ("sender_verified", "identity_verified", "no_pending_invitation", "not_connected",
                                "no_conversation", "relationships_checked", "no_platform_restriction")}}


def sent(row):
    return {"status": "sent", "sender": "Arian", "linkedin_url": row.linkedin_url, "note": row.note,
            "checked_at": timezone.now().isoformat(), "sent_invitations_verified": True, "screenshot": "private.png"}


def test_review_creates_normal_crm_contact_without_discovery_or_enrollment(row):
    counts = [m.objects.count() for m in (Deal, Campaign, Task, LinkedInDiscoveryLead)]
    result = rb2b.review(row.pk, decision(row))
    assert result.status == "ready" and result.lead.icp == "Advisors"
    assert json.loads(result.lead.description)["source"] == "rb2b"
    assert [m.objects.count() for m in (Deal, Campaign, Task, LinkedInDiscoveryLead)] == counts
    assert rb2b.review(row.pk, decision(row)).lead_id == result.lead_id
    assert Lead.objects.filter(linkedin_url=row.linkedin_url).count() == 1


def test_existing_contact_is_reused_without_overwriting_operator_fields(row):
    lead = Lead.objects.create(linkedin_url=row.linkedin_url, public_identifier=row.public_identifier,
                               first_name="Operator value", icp="Channel", description="Private notes")
    result = rb2b.review(row.pk, decision(row))
    lead.refresh_from_db()
    assert result.lead_id == lead.pk
    assert (lead.first_name, lead.icp, lead.description) == ("Operator value", "Channel", "Private notes")


@pytest.mark.parametrize("guard", ["identity_verified", "fit_verified", "relationships_checked", "invitations_checked"])
def test_uncertain_review_does_not_create_contact(row, guard):
    data = decision(row); data[guard] = False
    with pytest.raises(rb2b.RB2BHeld): rb2b.review(row.pk, data)
    assert not Lead.objects.filter(linkedin_url=row.linkedin_url).exists()


def test_holds_suppression_customer_existing_history_and_duplicate_email(row):
    OutreachSuppression.objects.create(kind="company", value="Example")
    result = rb2b.review(row.pk, decision(row))
    assert result.status == "held" and result.lead_id is None
    assert RB2BNotification.objects.filter(kind="held").count() == 1
    OutreachSuppression.objects.all().delete()
    account = Account.objects.create(name="Example")
    Opportunity.objects.create(account=account, stage="closed_won")
    assert rb2b.review(row.pk, decision(row)).status == "held"


def test_conflicting_email_never_merges_people(row):
    Lead.objects.create(linkedin_url="https://www.linkedin.com/in/other/", email="shared@example.test")
    data = decision(row); data["email"] = "shared@example.test"
    with pytest.raises(rb2b.RB2BHeld, match="conflict"): rb2b.review(row.pk, data)
    assert not Lead.objects.filter(linkedin_url=row.linkedin_url).exists()


def test_historical_sent_people_cannot_be_reinvited(row, monkeypatch):
    import hashlib
    monkeypatch.setattr(rb2b, "NO_RESEND", {hashlib.sha256(row.linkedin_url.encode()).hexdigest()})
    assert rb2b.review(row.pk, decision(row)).status == "held"


def test_begin_counts_shared_budget_and_cannot_repeat(ready, enabled, owner):
    before = owner._daily_count("connect")
    result = rb2b.begin(ready.pk, preflight(ready))
    assert result.status == "attempting" and result.attempt_token
    assert owner._daily_count("connect") == before + 1
    assert owner._total_daily_count() >= 1 and owner._weekly_count("connect") >= 1
    with pytest.raises(rb2b.RB2BHeld): rb2b.begin(ready.pk, preflight(ready))
    with pytest.raises(rb2b.RB2BHeld): rb2b.review(ready.pk, decision(ready))


def test_disabled_or_old_worker_cannot_reserve(ready, enabled, monkeypatch):
    monkeypatch.setenv("RB2B_IAB_SENDS_ENABLED", "false")
    with pytest.raises(rb2b.RB2BHeld, match="not_enabled"): rb2b.begin(ready.pk, preflight(ready))
    monkeypatch.setenv("RB2B_IAB_SENDS_ENABLED", "true")
    DaemonHeartbeat.objects.update(rb2b_coordination={})
    with pytest.raises(rb2b.RB2BHeld, match="updated_arian"): rb2b.begin(ready.pk, preflight(ready))


def test_existing_worker_task_excludes_iab_and_reservation_excludes_task(ready, enabled, owner):
    campaign = Campaign.objects.create(user=owner.user, name="Existing campaign")
    task = Task.objects.create(task_type="connect", payload={"campaign_id": campaign.pk},
                               scheduled_at=timezone.now(), status="running")
    with pytest.raises(rb2b.RB2BHeld, match="worker_busy"): rb2b.begin(ready.pk, preflight(ready))
    task.status = "pending"; task.save()
    rb2b.begin(ready.pk, preflight(ready))
    assert Task.objects.claim_next(operator="Arian", campaign_ids=[campaign.pk]) is None
    task.refresh_from_db(); assert task.status == "pending"
    assert Task.objects.seconds_to_next(operator="Arian", campaign_ids=[campaign.pk]) >= 30


def test_restriction_persists_across_profile_instances(ready, enabled, owner):
    owner.mark_exhausted("connect")
    other = LinkedInProfile.objects.get(pk=owner.pk)
    assert not other.can_execute("connect")
    with pytest.raises(rb2b.RB2BHeld, match="shared_sender_limit"): rb2b.begin(ready.pk, preflight(ready))


def test_fresh_suppression_after_review_blocks_send(ready, enabled):
    OutreachSuppression.objects.create(kind="lead", value="New Visitor", public_identifier=ready.public_identifier)
    with pytest.raises(rb2b.RB2BHeld, match="suppressed"): rb2b.begin(ready.pk, preflight(ready))


def test_sent_receipt_is_exact_and_repeat_safe(ready, enabled, owner):
    row = rb2b.begin(ready.pk, preflight(ready))
    data = sent(row); data["note"] = "wrong"
    with pytest.raises(rb2b.RB2BHeld): rb2b.finish(row.pk, row.attempt_token, data)
    good = sent(row)
    result = rb2b.finish(row.pk, row.attempt_token, good)
    assert result.status == "sent" and result.confirmed_at
    rb2b.finish(row.pk, row.attempt_token, good)
    assert RB2BNotification.objects.filter(kind="sent").count() == 1
    assert owner._daily_count("connect") == 1


def test_uncertain_never_refunds_or_retries(ready, enabled, owner):
    row = rb2b.begin(ready.pk, preflight(ready))
    with pytest.raises(rb2b.RB2BHeld): rb2b.finish(row.pk, row.attempt_token, {"status": "failed"})
    rb2b.finish(row.pk, row.attempt_token, {"status": "uncertain", "reason": "No confirmation"})
    with pytest.raises(rb2b.RB2BHeld): rb2b.begin(row.pk, preflight(row))
    assert owner._daily_count("connect") == 1


def test_outcomes_to_rb2b_replies_to_existing_reply_controls(ready, enabled, monkeypatch):
    from linkedin.notifications import slack
    calls = []
    monkeypatch.setattr(slack, "_slack_api", lambda method, payload, label: calls.append((method, payload)) or True)
    monkeypatch.setattr(slack, "notify_message_received", lambda **kw: calls.append(("reply", kw)) or True)
    rb2b._notify(ready, "sent")
    rb2b._notify(ready, "failed", "synthetic")
    message = Message.objects.create(lead=ready.lead, source="linkedin", external_id="urn:li:msg:one",
                                     direction="inbound", body="Interested", sent_at=timezone.now())
    rb2b.queue_reply_notifications(ready.lead, [message.pk])
    assert rb2b.flush_notifications() == 3
    assert [call[1]["channel"] for call in calls[:2]] == ["C_SYNTHETIC_RB2B"] * 2
    assert calls[2][0] == "reply" and calls[2][1]["operator"] == "Arian"
    assert rb2b.flush_notifications() == 0


def test_slack_failure_retries_notification_only(ready, enabled, monkeypatch):
    from linkedin.notifications import slack
    monkeypatch.setattr(slack, "_slack_api", lambda *a: False)
    rb2b._notify(ready, "failed", "synthetic")
    assert rb2b.flush_notifications() == 0
    row = RB2BNotification.objects.get(); assert row.attempts == 1 and row.sent_at is None
    ready.refresh_from_db(); assert ready.attempted_at is None


def test_first_reply_resolves_without_deal_and_is_notified_once(ready):
    from linkedin.realtime.handler import handle_realtime_event
    from linkedin.realtime.parser import ParsedRealtimeMessage
    msg = ParsedRealtimeMessage(entity_urn="urn:li:msg:first", conversation_urn="urn:li:msg_conversation:new",
        sender_member_urn=ready.member_urn, sender_name="New Visitor", text="Thanks, let's talk",
        timestamp="2026-10-30 12:10")
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=msg), patch("linkedin.rb2b.flush_notifications"):
        handle_realtime_event({}, operator="Arian")
        handle_realtime_event({}, operator="Arian")
    assert Message.objects.filter(lead=ready.lead, direction="inbound").count() == 1
    assert RB2BNotification.objects.filter(kind="reply").count() == 1
    assert not Deal.objects.filter(lead=ready.lead).exists()
    from linkedin.realtime.lead_lookup import resolve_lead_for_realtime
    assert resolve_lead_for_realtime(conversation_urn="", sender_member_urn=ready.member_urn, operator="Chuka") is None


def test_catchup_import_is_idempotent_and_note_echo_is_outbound(ready):
    data = {"sender": "Arian", "linkedin_url": ready.linkedin_url, "checked_at": timezone.now().isoformat(),
            "identity_verified": True, "thread_external_id": "urn:li:msg_conversation:catchup", "messages": [
                {"entity_urn": "urn:li:msg:note", "sender": "New Visitor", "text": ready.note, "timestamp": "2026-10-30 12:00"},
                {"entity_urn": "urn:li:msg:reply", "sender": "New Visitor", "text": "Yes please", "timestamp": "2026-10-30 12:01"}]}
    assert rb2b.ingest_replies(ready.pk, data) == 2
    assert rb2b.ingest_replies(ready.pk, data) == 0
    assert Message.objects.get(external_id="urn:li:msg:note").direction == "outbound"
    assert RB2BNotification.objects.filter(kind="reply").count() == 1


def test_migration_preserves_receipts_and_is_idempotent(row):
    old = LinkedInDiscoveryLead.objects.create(public_identifier=row.public_identifier, linkedin_url=row.linkedin_url,
        profile_data={"source": "rb2b_webhook", "rb2b_intake": {"schema_version": 1, "events": row.events}},
        potential_icp="RB2B: needs review")
    before = json.loads(json.dumps(row.events))
    rb2b.migrate_discovery(); rb2b.migrate_discovery()
    old.refresh_from_db(); row.refresh_from_db()
    key = next(iter(before))
    assert row.events[key]["receipt_count"] == 2
    assert old.profile_data["rb2b_intake"]["events"] == before
    assert old.profile_data["rb2b_migrated_to"] == row.pk
    assert LinkedInDiscoveryLead.objects.filter(pk=old.pk).exists()


def test_command_preview_cannot_register_or_reserve(row, tmp_path):
    path = tmp_path / "decision.json"; path.write_text(json.dumps(decision(row)))
    out = StringIO()
    call_command("rb2b_workflow", "review", id=row.pk, input=path, stdout=out)
    assert json.loads(out.getvalue())["applied"] is False
    row.refresh_from_db(); assert row.lead_id is None


def test_reservation_enforces_combined_daily_and_total_limits(ready, enabled, owner, monkeypatch):
    import linkedin.models as models
    campaign = Campaign.objects.create(user=owner.user, name="Quota history")
    ActionLog.objects.create(linkedin_profile=owner, campaign=campaign, action_type="connect")
    owner.connect_daily_limit = 1; owner.save()
    monkeypatch.setitem(models._LIMIT_OVERRIDES, "connect_daily_limit", None)
    with pytest.raises(rb2b.RB2BHeld, match="shared_sender_limit"):
        rb2b.begin(ready.pk, preflight(ready))
    owner.connect_daily_limit = 20; owner.save()
    monkeypatch.setattr(models, "MAX_TOTAL_DAILY_ACTIONS", 1)
    with pytest.raises(rb2b.RB2BHeld, match="shared_sender_limit"):
        rb2b.begin(ready.pk, preflight(ready))
    ready.refresh_from_db(); assert ready.attempted_at is None


def test_late_webhook_preserves_ready_or_sent_review(row, ready):
    if connection.vendor != "postgresql": pytest.skip("raw SQL requires PostgreSQL")
    from api.rb2b_webhook import store_visitor
    key = next(iter(row.events))
    payload = {**row.events[key]["payload"], "Seen At": "2026-11-01T12:00:00Z"}
    store_visitor(connection.connection, normalize_visitor(payload))
    ready.refresh_from_db()
    assert ready.status == "ready" and ready.lead_id and ready.note
    assert len(ready.events) == 2


def test_discovery_migration_hides_only_original_rb2b_rows(row):
    from django.contrib.admin.sites import AdminSite
    from linkedin.admin import LinkedInDiscoveryLeadAdmin
    old = LinkedInDiscoveryLead.objects.create(public_identifier=row.public_identifier, linkedin_url=row.linkedin_url,
        profile_data={"source": "rb2b_webhook", "rb2b_intake": {"schema_version": 1, "events": row.events}})
    collector = LinkedInDiscoveryLead.objects.create(public_identifier="collector", linkedin_url="https://www.linkedin.com/in/collector/",
                                                     profile_data={"source": "recommendations"})
    rb2b.migrate_discovery()
    shown = LinkedInDiscoveryLeadAdmin(LinkedInDiscoveryLead, AdminSite()).get_queryset(None)
    assert not shown.filter(pk=old.pk).exists()
    assert shown.filter(pk=collector.pk).exists()


def test_stale_unknown_or_unsafe_send_evidence_is_rejected(ready, enabled):
    data = preflight(ready); data["checked_at"] = (timezone.now() - timedelta(minutes=6)).isoformat()
    with pytest.raises(rb2b.RB2BHeld, match="fresh"): rb2b.begin(ready.pk, data)
    data = preflight(ready); data["not_connected"] = False
    with pytest.raises(rb2b.RB2BHeld, match="not_connected"): rb2b.begin(ready.pk, data)


def test_live_and_catchup_share_one_reply_notification(ready):
    from linkedin.realtime.handler import handle_realtime_event
    from linkedin.realtime.parser import ParsedRealtimeMessage
    msg = ParsedRealtimeMessage(entity_urn="urn:li:msg:overlap", conversation_urn="urn:li:msg_conversation:overlap",
        sender_member_urn=ready.member_urn, sender_name="New Visitor", text="Hello", timestamp="2026-10-30 12:10")
    data = {"sender": "Arian", "linkedin_url": ready.linkedin_url, "checked_at": timezone.now().isoformat(),
            "identity_verified": True, "thread_external_id": msg.conversation_urn, "messages": [
                {"entity_urn": msg.entity_urn, "sender": msg.sender_name, "text": msg.text, "timestamp": msg.timestamp}]}
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=msg), patch("linkedin.rb2b.flush_notifications"):
        handle_realtime_event({}, operator="Arian")
        rb2b.ingest_replies(ready.pk, data)
    assert RB2BNotification.objects.filter(kind="reply").count() == 1


@pytest.mark.django_db(transaction=True)
def test_parallel_iab_reservations_cannot_take_same_sender_budget(monkeypatch):
    if connection.vendor != "postgresql": pytest.skip("PostgreSQL row locks required")
    import threading
    from django.db import connections
    profile = LinkedInProfile.objects.create(user=UserFactory(username="ariantajbakh"), linkedin_username="ariantajbakh")
    monkeypatch.setenv("RB2B_IAB_SENDS_ENABLED", "true")
    monkeypatch.setenv("RB2B_SLACK_CHANNEL_ID", "C_SYNTHETIC")
    monkeypatch.setattr(rb2b.conf, "SLACK_BOT_TOKEN", "synthetic")
    monkeypatch.setattr(rb2b.conf, "SLACK_REPLIES_WEBHOOK_URL", "synthetic")
    monkeypatch.setattr(rb2b.conf, "ENABLE_CONNECT", True)
    monkeypatch.setattr(rb2b.conf, "ENABLE_REALTIME_LISTENER", True)
    monkeypatch.setattr(rb2b.conf, "ENABLE_LINKEDIN_DAEMON", True)
    monkeypatch.setattr(rb2b.conf, "ENABLE_ACTIVE_HOURS", False)
    monkeypatch.setattr(rb2b.conf, "REST_DAYS", ())
    monkeypatch.setattr(rb2b, "worker_ready", lambda: None)
    rows = []
    for index in range(2):
        url = f"https://www.linkedin.com/in/concurrent-{index}/"
        lead = Lead.objects.create(linkedin_url=url, public_identifier=f"concurrent-{index}")
        rows.append(RB2BVisitor.objects.create(public_identifier=lead.public_identifier, linkedin_url=url,
                    linkedin_profile=profile, lead=lead, member_urn=f"urn:li:fsd_profile:CONCURRENT{index}", status="ready", note="Hello"))
    barrier = threading.Barrier(2)
    results = []
    def run(row):
        try:
            barrier.wait(timeout=5)
            result = rb2b.begin(row.pk, preflight(row))
            results.append(result.status)
        except rb2b.RB2BHeld as exc:
            results.append(str(exc))
        finally:
            connections.close_all()
    threads = [threading.Thread(target=run, args=(row,)) for row in rows]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=10)
    assert results.count("attempting") == 1
    assert results.count("sender_reserved_or_pacing") == 1
    assert profile._daily_count("connect") == 1


def test_published_audience_requires_an_arian_connect_route(row):
    from tests.test_message_delivery import _version, _row
    version = _version(_row(audience_key="exact-audience", channel="linkedin_connect", step_key="connect",
                            subject="", sender_override="Chuka"), key="rb2b-wrong-sender")
    data = decision(row); data.update(icp="exact-audience", message_version_id=version.pk)
    with pytest.raises(rb2b.RB2BHeld, match="unknown_audience"): rb2b.review(row.pk, data)
    version2 = _version(_row(audience_key="exact-audience", channel="linkedin_connect", step_key="connect",
                             subject="", sender_override="Arian"), key="rb2b-right-sender")
    data["message_version_id"] = version2.pk
    assert rb2b.review(row.pk, data).lead.icp == "exact-audience"


def test_deployment_staging_excludes_private_files(tmp_path):
    from scripts.stage_rb2b_deployment import stage, FILES
    import shutil
    from pathlib import Path
    root = tmp_path / "checkout"; root.mkdir()
    for filename in FILES:
        path = root / filename; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("synthetic public source")
    (root / "private-record.txt").write_text("synthetic private data")
    project = tmp_path / "project.json"; project.write_text(json.dumps({"projectId": "prj_synthetic", "orgId": "team_synthetic"}))
    result = stage(project, root=root)
    directory = Path(result["directory"])
    try:
        assert not (directory / "private-record.txt").exists()
        assert {str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file()} == set(FILES) | {".vercel/project.json", "source-manifest.json"}
    finally:
        shutil.rmtree(directory)


@pytest.mark.django_db(transaction=True)
def test_additive_schema_accepts_older_heartbeat_writer_and_preserves_profiles():
    from django.db.migrations.executor import MigrationExecutor
    from django.contrib.auth.models import User
    executor = MigrationExecutor(connection)
    old_apps = executor.loader.project_state([("linkedin", "0037_linkedinrepost_retry_count")]).apps
    old_heartbeat = old_apps.get_model("linkedin", "DaemonHeartbeat")
    # The database default keeps a rolling deployment compatible with an old
    # monitor creating its row before that worker has loaded the new code.
    old_heartbeat.objects.create(sender="OldWriter", last_alive=timezone.now())
    assert DaemonHeartbeat.objects.get(sender="OldWriter").rb2b_coordination == {}


def test_disabled_listener_blocks_iab_readiness(ready, enabled, monkeypatch):
    monkeypatch.setattr(rb2b.conf, "ENABLE_REALTIME_LISTENER", False)
    with pytest.raises(rb2b.RB2BHeld, match="reply_listener"):
        rb2b.begin(ready.pk, preflight(ready))
