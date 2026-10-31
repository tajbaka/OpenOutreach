import json
from datetime import datetime, timedelta, timezone as dtz
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from api.rb2b_webhook import normalize_visitor, merge_receipt
from crm.models import Lead, Deal, Message, Account, Opportunity
from linkedin import rb2b
from linkedin.models import (RB2BVisitor, RB2BNotification, LinkedInProfile, LinkedInDiscoveryLead,
                             Campaign, Task, DaemonHeartbeat, ActionLog, OutreachSuppression)
from tests.factories import UserFactory
from linkedin.actions.connect import send_connection_request as REAL_SEND
from linkedin.exceptions import InvitationWithdrawalError


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
            "identity_verified": True, "fit_verified": True, "relationships_checked": True,
            "evidence_urls": [row.linkedin_url], "fit_reason": "Synthetic sourced review",
            "checked_at": timezone.now().isoformat(), "first_name": "New", "last_name": "Visitor",
            "company_name": "Example", "title": "Security lead", "icp": "Advisors",
            "note": "Hi New, good to connect."}


@pytest.fixture
def ready(row):
    return rb2b.review(row.pk, decision(row))


@pytest.fixture
def slack_calls(monkeypatch):
    from linkedin.notifications import slack
    calls = []
    monkeypatch.setattr(slack, "_slack_api", lambda method, payload, label: calls.append((method, payload)) or True)
    monkeypatch.setattr(slack, "notify_message_received", lambda **kw: calls.append(("reply", kw)) or True)
    return calls


@pytest.fixture
def enabled(monkeypatch, owner, slack_calls):
    monkeypatch.setenv("RB2B_SLACK_CHANNEL_ID", "C_SYNTHETIC_RB2B")
    monkeypatch.setattr(rb2b.conf, "ENABLE_RB2B_CONNECT", True)
    monkeypatch.setattr(rb2b.conf, "SLACK_BOT_TOKEN", "synthetic")
    monkeypatch.setattr(rb2b.conf, "SLACK_REPLIES_WEBHOOK_URL", "synthetic")
    monkeypatch.setattr(rb2b.conf, "ENABLE_CONNECT", True)
    monkeypatch.setattr(rb2b.conf, "ENABLE_REALTIME_LISTENER", True)


LIVE = {"public_identifier": "new-visitor", "urn": "urn:li:fsd_profile:SYNTHETIC", "first_name": "New",
        "last_name": "Visitor, CISSP", "connection_degree": 2}


class Session:
    """The daemon's browser session, reduced to what the handler reads."""

    def __init__(self, profile):
        self.linkedin_profile, self.campaign = profile, None

    def ensure_browser(self):
        pass


@pytest.fixture
def worker(monkeypatch, owner):
    """Run one rb2b_connect Task with LinkedIn replaced by synthetic doubles."""
    from linkedin.enums import ProfileState
    from linkedin.actions.invitations import SentInvitationMatch, SentInvitationScan
    # "listed" is the synthetic Sent Invitations page: only an invitation the
    # fake helper actually sent appears there, unless a test overrides it.
    state = {"live": dict(LIVE), "status": ProfileState.QUALIFIED, "send": ProfileState.PENDING,
             "sends": [], "listed": set(), "sent_page": None}

    def send(session, profile, note):
        state["sends"].append((profile["public_identifier"], note))
        if isinstance(state["send"], Exception):
            raise state["send"]
        if state["send"] == ProfileState.PENDING:
            state["listed"].add(profile["public_identifier"])
        return state["send"]

    def sent_page(session, targets, approximate_max_age_days=None):
        if isinstance(state["sent_page"], Exception):
            raise state["sent_page"]
        listed = state["listed"] if state["sent_page"] is None else state["sent_page"]
        return SentInvitationScan(matches=tuple(
            SentInvitationMatch(t.public_identifier, t.expected_name, "Sent 1 minute ago")
            for t in targets if t.public_identifier in listed), cards_seen=1, scroll_rounds=0, reached_end=True)

    def live(session, public_id):
        if isinstance(state["live"], Exception):
            raise state["live"]
        return state["live"]

    monkeypatch.setattr("linkedin.tasks.rb2b_connect._live_profile", live)
    monkeypatch.setattr("linkedin.actions.status.get_connection_status", lambda session, profile: state["status"])
    monkeypatch.setattr("linkedin.actions.connect.send_connection_request", send)
    monkeypatch.setattr("linkedin.actions.invitations.scan_sent_invitations", sent_page)
    monkeypatch.setattr("linkedin.tasks.connect.recommended_action_delay", lambda profile, action: 600.0)

    def run(task=None):
        from linkedin.tasks.rb2b_connect import handle_rb2b_connect
        task = task or Task.objects.filter(task_type="rb2b_connect", status="pending").order_by("pk").first()
        task.status = "running"; task.save()
        try:
            handle_rb2b_connect(task, Session(owner), {})
        finally:
            task.status = "completed"; task.save()
        return state
    run.state = state
    return run


def test_review_creates_normal_crm_contact_and_one_worker_task(row):
    counts = [m.objects.count() for m in (Deal, Campaign, LinkedInDiscoveryLead)]
    result = rb2b.review(row.pk, decision(row))
    assert result.status == "ready" and result.lead.icp == "Advisors" and result.member_urn == ""
    assert json.loads(result.lead.description)["source"] == "rb2b"
    assert [m.objects.count() for m in (Deal, Campaign, LinkedInDiscoveryLead)] == counts
    task = Task.objects.get(task_type="rb2b_connect")
    assert task.status == "pending" and task.payload == {"operator": "Arian", "visitor_id": row.pk}
    assert rb2b.review(row.pk, decision(row)).lead_id == result.lead_id
    assert Lead.objects.filter(linkedin_url=row.linkedin_url).count() == 1
    assert Task.objects.filter(task_type="rb2b_connect").count() == 1


def test_existing_contact_is_reused_without_overwriting_operator_fields(row):
    lead = Lead.objects.create(linkedin_url=row.linkedin_url, public_identifier=row.public_identifier,
                               first_name="Operator value", icp="Channel", description="Private notes")
    result = rb2b.review(row.pk, decision(row))
    lead.refresh_from_db()
    assert result.lead_id == lead.pk
    assert (lead.first_name, lead.icp, lead.description) == ("Operator value", "Channel", "Private notes")


@pytest.mark.parametrize("guard", ["identity_verified", "fit_verified", "relationships_checked", "first_name"])
def test_uncertain_review_does_not_create_contact_or_task(row, guard):
    data = decision(row); data[guard] = "" if guard == "first_name" else False
    with pytest.raises(rb2b.RB2BHeld): rb2b.review(row.pk, data)
    assert not Lead.objects.filter(linkedin_url=row.linkedin_url).exists()
    assert not Task.objects.filter(task_type="rb2b_connect").exists()


def test_holds_suppression_customer_existing_history_and_duplicate_email(row):
    OutreachSuppression.objects.create(kind="company", value="Example")
    result = rb2b.review(row.pk, decision(row))
    assert result.status == "held" and result.lead_id is None
    assert RB2BNotification.objects.filter(kind="held").count() == 1
    assert not Task.objects.filter(task_type="rb2b_connect").exists()
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


def test_outcomes_to_rb2b_replies_to_existing_reply_controls(ready, enabled, slack_calls):
    calls = slack_calls
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
    # The worker stores the verified member URN before it sends.
    ready.member_urn = LIVE["urn"]; ready.save()
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
    assert not Task.objects.filter(task_type="rb2b_connect").exists()


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


def _campaign_connect(owner, *, at=None):
    campaign = Campaign.objects.create(user=owner.user, name="Existing campaign")
    task = Task.objects.create(task_type="connect", payload={"campaign_id": campaign.pk},
                               scheduled_at=at or timezone.now())
    return campaign, task


def test_disabled_rb2b_work_is_never_claimed_and_never_blocks_campaigns(ready, owner):
    campaign, connect = _campaign_connect(owner)
    assert Task.objects.seconds_to_next(operator="Arian", campaign_ids=[campaign.pk]) == 0
    claimed = Task.objects.claim_next(operator="Arian", campaign_ids=[campaign.pk])
    assert claimed.pk == connect.pk
    assert Task.objects.claim_next(operator="Arian", campaign_ids=[campaign.pk]) is None
    assert Task.objects.get(task_type="rb2b_connect").status == "pending"


def test_enabled_rb2b_takes_the_campaign_connect_turn_and_stays_with_arian(ready, enabled, owner):
    campaign, connect = _campaign_connect(owner, at=timezone.now() - timedelta(minutes=5))
    assert Task.objects.claim_next(operator="Chuka", campaign_ids=[]) is None
    claimed = Task.objects.claim_next(operator="Arian", campaign_ids=[campaign.pk])
    assert claimed.task_type == "rb2b_connect"
    connect.refresh_from_db(); assert connect.status == "pending"


def test_due_follow_ups_keep_their_place_ahead_of_rb2b(ready, enabled, owner):
    campaign, connect = _campaign_connect(owner)
    followup = Task.objects.create(task_type="follow_up", scheduled_at=timezone.now() - timedelta(hours=1),
                                   payload={"campaign_id": campaign.pk, "operator": "Arian", "public_id": "other"})
    assert Task.objects.claim_next(operator="Arian", campaign_ids=[campaign.pk]).pk == followup.pk
    assert Task.objects.claim_next(operator="Arian", campaign_ids=[campaign.pk]).task_type == "rb2b_connect"
    assert Task.objects.claim_next(operator="Arian", campaign_ids=[campaign.pk]).pk == connect.pk


def test_worker_sends_reviewed_note_and_shares_budget_and_pacing(ready, enabled, owner, worker, slack_calls):
    campaign, connect = _campaign_connect(owner)
    before = owner._daily_count("connect")
    state = worker()
    ready.refresh_from_db()
    assert state["sends"] == [(ready.public_identifier, "Hi New, good to connect.")]
    assert ready.status == "sent" and ready.confirmed_at and ready.attempted_at
    assert ready.member_urn == LIVE["urn"]
    assert owner._daily_count("connect") == before + 1
    assert owner._total_daily_count() >= 1 and owner._weekly_count("connect") >= 1
    # No synthetic campaign records; the campaign's next connect waits one gap.
    assert not ActionLog.objects.exists() and not Deal.objects.exists()
    connect.refresh_from_db()
    assert connect.scheduled_at == ready.cooldown_until > timezone.now() + timedelta(seconds=590)
    assert ready.outcome["result"]["sent_invitations_label"] == "Sent 1 minute ago"
    assert RB2BNotification.objects.get(kind="sent").sent_at
    text = [payload["text"] for method, payload in slack_calls if method == "chat.postMessage"][0]
    assert "connection request sent" in text and "Hi New, good to connect." in text
    # A sent visitor is never contacted again, whatever later work appears.
    rb2b.enqueue_connect_task(ready)
    worker()
    assert len(state["sends"]) == 1
    with pytest.raises(rb2b.RB2BHeld): rb2b.review(ready.pk, decision(ready))


def test_worker_waits_for_shared_pacing_and_holds_campaign_slot(ready, enabled, owner, worker):
    campaign, connect = _campaign_connect(owner)
    ActionLog.objects.create(linkedin_profile=owner, campaign=campaign, action_type="connect")
    state = worker()
    assert state["sends"] == []
    paced = Task.objects.get(task_type="rb2b_connect", status="pending")
    not_before = datetime.fromisoformat(paced.payload["not_before"])
    assert paced.scheduled_at == not_before
    connect.refresh_from_db(); assert connect.scheduled_at == not_before
    # Once that slot arrives the same pacing decision is honored, not re-rolled.
    Task.objects.filter(pk=paced.pk).update(scheduled_at=timezone.now())
    paced.payload["not_before"] = (timezone.now() - timedelta(seconds=1)).isoformat()
    paced.save()
    worker()
    assert len(state["sends"]) == 1
    ready.refresh_from_db(); assert ready.status == "sent"


def test_worker_respects_shared_daily_limit(ready, enabled, owner, worker, monkeypatch):
    import linkedin.models as models
    campaign, _ = _campaign_connect(owner)
    ActionLog.objects.create(linkedin_profile=owner, campaign=campaign, action_type="connect")
    owner.connect_daily_limit = 1; owner.save()
    monkeypatch.setitem(models._LIMIT_OVERRIDES, "connect_daily_limit", None)
    state = worker()
    assert state["sends"] == []
    ready.refresh_from_db(); assert ready.status == "ready" and ready.attempted_at is None
    assert Task.objects.get(task_type="rb2b_connect", status="pending").scheduled_at > timezone.now()


@pytest.mark.parametrize("change,reason", [
    ({"first_name": "Someone", "last_name": "Else"}, "linkedin_name_does_not_match_review"),
    ({"public_identifier": "renamed-visitor"}, "linkedin_alias_requires_review"),
    ({"connection_degree": 1}, "already_connected"),
])
def test_worker_holds_when_live_identity_is_not_provably_the_reviewed_person(ready, enabled, worker, change, reason):
    worker.state["live"] = {**LIVE, **change}
    worker()
    ready.refresh_from_db()
    assert worker.state["sends"] == [] and ready.status == "held" and ready.attempted_at is None
    assert ready.review["hold_reason"] == reason
    assert RB2BNotification.objects.filter(kind="held", detail=reason).exists()


def test_worker_holds_pending_invitations_duplicates_and_new_suppression(ready, enabled, worker, owner):
    from linkedin.enums import ProfileState
    worker.state["status"] = ProfileState.PENDING
    worker()
    ready.refresh_from_db(); assert ready.status == "held" and ready.review["hold_reason"] == "pending_invitation_exists"
    # The held receipt keeps the verified URN; model an alias that claimed it first.
    RB2BVisitor.objects.filter(pk=ready.pk).update(status="ready", member_urn="")
    other = RB2BVisitor.objects.create(public_identifier="alias", linkedin_url="https://www.linkedin.com/in/alias/",
                                       linkedin_profile=owner, member_urn=LIVE["urn"], status="sent")
    rb2b.enqueue_connect_task(ready)
    worker.state["status"] = ProfileState.QUALIFIED
    worker()
    ready.refresh_from_db(); assert ready.review["hold_reason"] == "duplicate_rb2b_identity"
    other.delete()
    RB2BVisitor.objects.filter(pk=ready.pk).update(status="ready")
    OutreachSuppression.objects.create(kind="lead", value="New Visitor", public_identifier=ready.public_identifier)
    rb2b.enqueue_connect_task(ready)
    worker()
    ready.refresh_from_db(); assert ready.review["hold_reason"] == "suppressed"
    assert worker.state["sends"] == []


def test_failures_before_reservation_retry_then_fail_without_sending(ready, enabled, worker):
    worker.state["live"] = IOError("synthetic Voyager outage")
    for attempt in (1, 2):
        with pytest.raises(IOError): worker()
        ready.refresh_from_db(); assert ready.status == "ready" and ready.attempted_at is None
        assert Task.objects.get(task_type="rb2b_connect", status="pending").payload["precheck_attempts"] == attempt
    with pytest.raises(IOError): worker()
    ready.refresh_from_db()
    assert ready.status == "failed" and ready.attempted_at is None and worker.state["sends"] == []
    assert not Task.objects.filter(task_type="rb2b_connect", status="pending").exists()
    # Nothing was reserved, so the next scheduled review may qualify it again.
    assert rb2b.review(ready.pk, decision(ready)).status == "ready"


@pytest.mark.parametrize("outcome,status", [
    ("exception", "uncertain"),
    ("qualified", "uncertain"),
    ("note_limit", "failed"),
])
def test_after_reservation_nothing_is_retried(ready, enabled, owner, worker, outcome, status):
    from linkedin.enums import ProfileState
    from linkedin.exceptions import ReachedConnectionLimit
    worker.state["send"] = {
        "exception": RuntimeError("browser closed mid-send"),
        "qualified": ProfileState.QUALIFIED,
        "note_limit": ReachedConnectionLimit("out of free custom notes. Invitation not sent; retry later."),
    }[outcome]
    if outcome == "exception":
        with pytest.raises(RuntimeError): worker()
    else:
        worker()
    ready.refresh_from_db()
    assert ready.status == status and ready.attempted_at
    assert owner._daily_count("connect") == 1
    assert not Task.objects.filter(task_type="rb2b_connect", status="pending").exists()
    if outcome == "note_limit":
        assert not LinkedInProfile.objects.get(pk=owner.pk).can_execute("connect")
    with pytest.raises(rb2b.RB2BHeld): rb2b.review(ready.pk, decision(ready))
    assert RB2BNotification.objects.filter(kind=status).count() == 1


def test_interrupted_reservation_becomes_uncertain_on_restart(ready, enabled, worker):
    task = Task.objects.get(task_type="rb2b_connect")
    RB2BVisitor.objects.filter(pk=ready.pk).update(status="attempting", attempted_at=timezone.now())
    worker(task)
    ready.refresh_from_db()
    assert ready.status == "uncertain" and worker.state["sends"] == []


def test_startup_reconcile_requeues_only_unreserved_ready_visitors(ready, enabled, owner):
    Task.objects.filter(task_type="rb2b_connect").update(status="failed")
    assert rb2b.reconcile_connect_tasks(owner) == 1
    assert rb2b.reconcile_connect_tasks(owner) == 0
    Task.objects.filter(task_type="rb2b_connect").delete()
    RB2BVisitor.objects.filter(pk=ready.pk).update(status="sent", attempted_at=timezone.now())
    assert rb2b.reconcile_connect_tasks(owner) == 0


def test_daemon_runs_rb2b_with_connect_hours_and_no_campaign(monkeypatch):
    from linkedin import daemon
    assert daemon._HANDLERS["rb2b_connect"].__name__ == "handle_rb2b_connect"
    monkeypatch.setattr(daemon, "ENABLE_ACTIVE_HOURS", True)
    monkeypatch.setattr(daemon, "ENABLE_PACING_CATCH_UP", True)
    monkeypatch.setattr(daemon, "REST_DAYS", ())
    monkeypatch.setattr(daemon, "ACTIVE_END_HOUR", 0)
    monkeypatch.setattr(daemon, "_is_behind_normal_window_pace", lambda profile, action: action == "connect")
    assert daemon._catch_up_task_types(object()) == {"connect", "rb2b_connect"}


def test_preflight_reports_whether_the_live_worker_will_send(ready, enabled, owner):
    from linkedin.monitoring.node_monitor import write_heartbeat
    out = StringIO()
    call_command("rb2b_workflow", "preflight", stdout=out)
    assert json.loads(out.getvalue())["result"]["worker_connect_ready"] is False
    write_heartbeat("Arian")
    out = StringIO()
    call_command("rb2b_workflow", "preflight", stdout=out)
    result = json.loads(out.getvalue())["result"]
    assert result["worker_alive"] and result["worker_connect_ready"] and result["pending_connect_tasks"] == 1


def test_listener_disabled_is_never_send_ready(enabled, monkeypatch):
    monkeypatch.setattr(rb2b.conf, "ENABLE_REALTIME_LISTENER", False)
    assert not rb2b.connect_ready()


def test_review_changed_during_checks_is_never_sent(ready, enabled, worker, monkeypatch):
    from linkedin.enums import ProfileState

    def re_reviewed(session, profile):
        updated = decision(ready)
        updated.update(first_name="Different", last_name="Person", note="Hi Different, another reviewed note.")
        rb2b.review(ready.pk, updated)
        return ProfileState.QUALIFIED
    monkeypatch.setattr("linkedin.actions.status.get_connection_status", re_reviewed)
    worker()
    ready.refresh_from_db()
    assert worker.state["sends"] == [] and ready.status == "ready" and ready.attempted_at is None
    # The re-review's own Task re-runs every check against the new decision.
    assert Task.objects.filter(task_type="rb2b_connect", status="pending").count() == 1


@pytest.mark.parametrize("sent_page,reason", [
    (set(), "not on Sent Invitations"),
    (InvitationWithdrawalError("name mismatch"), "check failed"),
])
def test_clicked_send_without_sent_invitations_receipt_is_uncertain(ready, enabled, worker, sent_page, reason):
    worker.state["sent_page"] = sent_page
    worker()
    ready.refresh_from_db()
    assert len(worker.state["sends"]) == 1 and ready.status == "uncertain"
    assert reason in ready.outcome["result"]["reason"]


def test_click_only_helper_result_is_not_confirmation(ready, enabled, worker, monkeypatch):
    # The real helper with only its UI steps stubbed: Send is "clicked" and it
    # returns PENDING, but LinkedIn's Sent Invitations page never lists anyone.
    from linkedin.actions import connect
    monkeypatch.setattr(connect, "_connect_direct", lambda *a: True)
    monkeypatch.setattr(connect, "_click_with_note", lambda *a, **kw: True)
    monkeypatch.setattr(connect, "_check_weekly_invitation_limit", lambda *a: None)
    monkeypatch.setattr(connect, "send_connection_request", REAL_SEND)
    worker()
    ready.refresh_from_db()
    assert ready.status == "uncertain" and ready.attempted_at


def test_outcome_write_failure_never_leaves_a_silent_reservation(ready, enabled, worker, owner):
    with patch("linkedin.rb2b.record_outcome", side_effect=RuntimeError("synthetic DB interruption after click")):
        with pytest.raises(RuntimeError): worker()
    Task.objects.filter(task_type="rb2b_connect").update(status="failed")
    rb2b.reconcile_connect_tasks(owner)
    ready.refresh_from_db()
    assert ready.status == "uncertain" and len(worker.state["sends"]) == 1
    assert RB2BNotification.objects.filter(kind="uncertain").count() == 1


def test_dead_db_connection_on_outcome_write_is_retried_once(ready, enabled, worker, monkeypatch):
    from types import SimpleNamespace
    from django.db.utils import OperationalError
    from linkedin.tasks import rb2b_connect
    real = rb2b.record_outcome
    calls, recycled = [], []
    # The daemon recycles with close_all(); inside a test transaction that
    # would drop the fixture data, so only record the recycle here.
    monkeypatch.setattr(rb2b_connect, "connections", SimpleNamespace(close_all=lambda: recycled.append(1)))

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("synthetic dropped connection")
        return real(*args, **kwargs)
    with patch("linkedin.rb2b.record_outcome", side_effect=flaky):
        worker()
    ready.refresh_from_db()
    assert ready.status == "sent" and len(calls) == 2 and recycled == [1]


class _FakeAPI:
    def __init__(self, session):
        pass


SELF_URN = "urn:li:fsd_profile:ARIAN"


def _conversation(urn, thread, *, at=None, others=1):
    participants = [{"hostIdentityUrn": SELF_URN}, {"hostIdentityUrn": urn}]
    participants += [{"hostIdentityUrn": f"urn:li:fsd_profile:EXTRA{i}"} for i in range(others - 1)]
    conversation = {"entityUrn": thread, "conversationParticipants": participants}
    if at is not None:
        conversation["lastActivityAt"] = int(at.timestamp() * 1000)
    return conversation


def _messages(*items):
    return {"data": {"messengerMessagesBySyncToken": {"elements": [
        {"entityUrn": urn, "body": {"text": text}, "deliveredAt": 1793500000000,
         "sender": {"participantType": {"member": {"firstName": {"text": first}, "lastName": {"text": "Visitor"}}}}}
        for urn, text, first in items]}}}


@pytest.fixture
def reply_scan(monkeypatch, ready, owner):
    """A sent RB2B visitor plus a synthetic, paged Voyager inbox."""
    from linkedin.tasks import rb2b_replies
    invited = timezone.now() - timedelta(days=3)
    RB2BVisitor.objects.filter(pk=ready.pk).update(status="sent", attempted_at=invited, member_urn=LIVE["urn"])
    old = _conversation("urn:li:fsd_profile:OLD", "urn:li:msg_conversation:old", at=invited - timedelta(days=30))
    inbox = {"pages": [[_conversation(LIVE["urn"], "urn:li:msg_conversation:rb2b", at=timezone.now()), old]],
             "messages": _messages(("urn:li:msg:note", ready.note, "New"), ("urn:li:msg:reply", "Happy to talk", "New")),
             "reads": 0, "older": []}

    def page(index):
        inbox["reads"] += 1
        pages = inbox["pages"]
        return {"data": {"messengerConversationsBySyncToken": {
            "elements": pages[index] if index < len(pages) else []}}}
    monkeypatch.setattr(rb2b_replies, "PAGE_DELAY_SECONDS", 0)
    monkeypatch.setattr("linkedin.api.client.PlaywrightLinkedinAPI", _FakeAPI)
    monkeypatch.setattr("linkedin.api.messaging.utils.get_self_urn", lambda api: SELF_URN)
    monkeypatch.setattr("linkedin.api.messaging.fetch_conversations", lambda api, **kw: page(0))
    monkeypatch.setattr("linkedin.api.messaging.fetch_conversations_by_category",
                        lambda api, **kw: inbox["older"].append(kw) or page(1))
    monkeypatch.setattr("linkedin.api.messaging.fetch_messages", lambda api, thread: inbox["messages"])

    def run():
        from linkedin.tasks.rb2b_replies import handle_rb2b_replies
        task = Task.objects.filter(task_type="rb2b_replies", status="pending").first()
        if task:
            task.status = "running"; task.save()
        handle_rb2b_replies(task or Task(task_type="rb2b_replies", payload={"operator": "Arian"}), Session(owner), {})
        if task:
            task.status = "completed"; task.save()
    # "conversations" is the first page itself, for probes that page by token.
    inbox["conversations"] = inbox["pages"][0]
    run.inbox, run.invited = inbox, invited
    return run


def _next_scan():
    return Task.objects.get(task_type="rb2b_replies", status="pending")


def test_reply_catchup_posts_a_missed_reply_once_and_reschedules(reply_scan, ready, enabled, slack_calls):
    reply_scan()
    reply_scan()
    inbound = Message.objects.filter(lead=ready.lead, direction="inbound")
    assert list(inbound.values_list("external_id", flat=True)) == ["urn:li:msg:reply"]
    assert Message.objects.get(external_id="urn:li:msg:note").direction == "outbound"
    assert RB2BNotification.objects.filter(kind="reply").count() == 1
    assert [call for call in slack_calls if call[0] == "reply"][0][1]["operator"] == "Arian"
    assert _next_scan().scheduled_at > timezone.now() + timedelta(minutes=55)
    assert not Deal.objects.exists()


def test_reply_pushed_onto_an_older_inbox_page_is_still_found(reply_scan, ready, enabled):
    # Newer chatter fills page one; the RB2B reply sits on page two.
    recent = [_conversation(f"urn:li:fsd_profile:BUSY{i}", f"urn:li:msg_conversation:busy{i}",
                            at=timezone.now() - timedelta(minutes=i)) for i in range(3)]
    rb2b_thread, old = reply_scan.inbox["pages"][0]
    rb2b_thread["lastActivityAt"] = int((timezone.now() - timedelta(hours=5)).timestamp() * 1000)
    reply_scan.inbox["pages"] = [recent, [rb2b_thread, old]]
    reply_scan()
    assert reply_scan.inbox["reads"] == 2
    oldest_recent = min(c["lastActivityAt"] for c in recent)
    assert reply_scan.inbox["older"] == [{"last_updated_before": oldest_recent, "count": 20}]
    assert Message.objects.filter(lead=ready.lead, external_id="urn:li:msg:reply", direction="inbound").exists()
    # A complete pass moves the window to this run's start.
    assert parse_datetime(_next_scan().payload["since"]) > timezone.now() - timedelta(minutes=1)


def test_page_capped_scan_keeps_its_window_for_the_next_run(reply_scan, ready, enabled, monkeypatch):
    from linkedin.tasks import rb2b_replies
    monkeypatch.setattr(rb2b_replies, "MAX_PAGES", 2)
    reply_scan.inbox["pages"] = [
        [_conversation(f"urn:li:fsd_profile:P{page}C{i}", f"urn:li:msg_conversation:p{page}c{i}",
                       at=timezone.now() - timedelta(minutes=page * 10 + i)) for i in range(3)]
        for page in range(2)]
    reply_scan()
    assert reply_scan.inbox["reads"] == 2
    assert parse_datetime(_next_scan().payload["since"]) == reply_scan.invited


def test_reply_catchup_skips_group_threads_and_other_leads_threads(reply_scan, ready, enabled):
    rb2b_thread, old = reply_scan.inbox["pages"][0]
    group = _conversation(LIVE["urn"], "urn:li:msg_conversation:group", at=timezone.now(), others=2)
    reply_scan.inbox["pages"] = [[group, old]]
    reply_scan()
    other = Lead.objects.create(linkedin_url="https://www.linkedin.com/in/someone-else/")
    Message.objects.create(lead=other, source="linkedin", external_id="urn:li:msg:other", direction="inbound",
                           body="Hi", sent_at=timezone.now(), thread_external_id="urn:li:msg_conversation:rb2b")
    reply_scan.inbox["pages"] = [[rb2b_thread, old]]
    reply_scan()
    assert not Message.objects.filter(lead=ready.lead).exists()


def test_reply_catchup_is_inert_until_rb2b_is_ready(reply_scan, ready, owner):
    reply_scan()
    assert not Message.objects.filter(lead=ready.lead).exists()
    Task.objects.create(task_type="rb2b_replies", scheduled_at=timezone.now(), payload={"operator": "Arian"})
    assert Task.objects.claim_next(operator="Arian", campaign_ids=[]) is None


def test_linkedin_message_times_are_utc_on_a_toronto_host(monkeypatch, ready):
    import time
    from linkedin.actions.conversations import parse_messages
    from linkedin.db.messages import persist_thread
    monkeypatch.setenv("TZ", "America/Toronto")
    time.tzset()
    try:
        parsed = parse_messages(_messages(("urn:li:msg:tz", "Hello", "New")))
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    delivered = datetime.fromtimestamp(1793500000, tz=dtz.utc)
    assert parsed[0]["timestamp"] == delivered.strftime("%Y-%m-%d %H:%M")
    persist_thread(lead=ready.lead, parsed=parsed, thread_external_id="urn:li:msg_conversation:tz")
    assert Message.objects.get(external_id="urn:li:msg:tz").sent_at == delivered.replace(second=0)


def test_startup_reconcile_schedules_one_reply_scan(ready, enabled, owner):
    rb2b.reconcile_connect_tasks(owner)
    rb2b.reconcile_connect_tasks(owner)
    assert Task.objects.filter(task_type="rb2b_replies", status="pending").count() == 1


def test_stale_hold_never_overwrites_a_newer_review(ready, enabled, worker, monkeypatch):
    real = rb2b.exclusion

    def re_review_then_check(lead, row):
        if not getattr(re_review_then_check, "done", False):
            re_review_then_check.done = True
            updated = decision(ready); updated["note"] = "Hi New, a newer reviewed note."
            rb2b.review(ready.pk, updated)
            return "suppressed"
        return real(lead, row)
    monkeypatch.setattr(rb2b, "exclusion", re_review_then_check)
    worker()
    ready.refresh_from_db()
    assert ready.status == "ready" and ready.note == "Hi New, a newer reviewed note."
    assert not RB2BNotification.objects.filter(kind="held").exists()


def test_inbox_paging_falls_back_to_the_sync_token(reply_scan, ready, enabled, monkeypatch):
    tokens = []

    def base(api, *, sync_token=None, **kwargs):
        tokens.append(sync_token)
        if sync_token == "older":
            return {"data": {"messengerConversationsBySyncToken": {"elements": reply_scan.inbox["conversations"]}}}
        return {"data": {"messengerConversationsBySyncToken": {
            "elements": [_conversation("urn:li:fsd_profile:OTHER", "urn:li:msg_conversation:recent")],
            "nextSyncToken": "older"}}}
    monkeypatch.setattr("linkedin.api.messaging.fetch_conversations", base)
    reply_scan()
    assert tokens == [None, "older"]
    assert Message.objects.filter(lead=ready.lead, external_id="urn:li:msg:reply").exists()


def _since_kept(reply_scan):
    return parse_datetime(_next_scan().payload["since"]) == reply_scan.invited


def test_graphql_error_body_fails_the_scan_and_keeps_the_checkpoint(reply_scan, ready, enabled, monkeypatch):
    from linkedin.exceptions import LinkedInInboxResponseError
    monkeypatch.setattr("linkedin.api.messaging.fetch_conversations", lambda api, **kw: {
        "data": None, "errors": [{"message": "synthetic GraphQL failure"}]})
    with pytest.raises(LinkedInInboxResponseError): reply_scan()
    assert _since_kept(reply_scan)


def test_thread_read_error_fails_the_scan_and_keeps_the_checkpoint(reply_scan, ready, enabled, monkeypatch):
    from linkedin.exceptions import LinkedInInboxResponseError
    monkeypatch.setattr("linkedin.api.messaging.fetch_messages", lambda api, thread: {"errors": [{}], "data": {}})
    with pytest.raises(LinkedInInboxResponseError): reply_scan()
    assert _since_kept(reply_scan) and not Message.objects.filter(lead=ready.lead).exists()


def test_duplicate_page_follows_its_cursor_to_older_threads(reply_scan, ready, enabled, monkeypatch):
    recent = _conversation("urn:li:fsd_profile:OTHER", "urn:li:msg_conversation:recent", at=timezone.now())
    threads, older_reads = reply_scan.inbox["conversations"], []
    monkeypatch.setattr("linkedin.api.messaging.fetch_conversations", lambda api, **kw: {
        "data": {"messengerConversationsBySyncToken": {"elements": [recent]}}})

    def older(api, **kwargs):
        older_reads.append(kwargs)
        if len(older_reads) == 1:  # overlap: only seen conversations, but a further cursor
            return {"data": {"messengerConversationsByCategoryQuery": {
                "elements": [recent], "metadata": {"nextCursor": "older"}}}}
        return {"data": {"messengerConversationsByCategoryQuery": {"elements": threads}}}
    monkeypatch.setattr("linkedin.api.messaging.fetch_conversations_by_category", older)
    reply_scan()
    assert older_reads[1] == {"next_cursor": "older", "count": 20}
    assert Message.objects.filter(lead=ready.lead, external_id="urn:li:msg:reply").exists()
    assert not _since_kept(reply_scan)


@pytest.mark.parametrize("pages", ["repeat", "empty_first"])
def test_unproven_completion_keeps_the_checkpoint(reply_scan, ready, enabled, monkeypatch, pages):
    recent = _conversation("urn:li:fsd_profile:OTHER", "urn:li:msg_conversation:recent", at=timezone.now())
    first = [] if pages == "empty_first" else [recent]
    monkeypatch.setattr("linkedin.api.messaging.fetch_conversations", lambda api, **kw: {
        "data": {"messengerConversationsBySyncToken": {"elements": first}}})
    # The older query keeps returning the same page: no progress, not the end.
    monkeypatch.setattr("linkedin.api.messaging.fetch_conversations_by_category", lambda api, **kw: {
        "data": {"messengerConversationsByCategoryQuery": {"elements": [recent]}}})
    reply_scan()
    assert _since_kept(reply_scan)
