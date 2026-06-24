from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Deal, Lead, Message
from linkedin.enums import ProfileState
from linkedin.exceptions import SheetsError
from linkedin.models import Campaign, Task
from gmail.tasks.follow_up import handle_gmail_follow_up
from tests.factories import UserFactory


@pytest.fixture(autouse=True)
def no_suppression(monkeypatch):
    monkeypatch.setattr("linkedin.suppression.lead_suppression_match", lambda lead: None)


class FakeGmailClient:
    send_as = "ariant@getboundera.com"

    def __init__(self, *, operator):
        self.operator = operator
        self.sent = []

    def send_as_aliases(self):
        return {"ariant@getboundera.com": {"isDefault": True}}

    def search_threads_for_email(self, email):
        return []

    def send_message(self, *, to, subject, body):
        self.sent.append((to, subject, body))
        return "gmail-id-1"


def _lead(**overrides):
    data = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "company_name": "Analytical Engines",
        "linkedin_url": "https://www.linkedin.com/in/ada-lovelace/",
        "public_identifier": "ada-lovelace",
        "email": "ada@example.com",
    }
    data.update(overrides)
    return Lead.objects.create(**data)


def _deal(lead):
    user = UserFactory(username="owner")
    campaign = Campaign.objects.create(name="Campaign", user=user)
    return Deal.objects.create(lead=lead, campaign=campaign, state=ProfileState.CONNECTED)


def _task(lead, deal=None, **payload):
    base = {
        "lead_id": lead.id,
        "operator": "Arian",
        "sequence_name": "gmail_fallback",
        "step_index": 0,
    }
    if deal is not None:
        base["deal_id"] = deal.id
    base.update(payload)
    return Task.objects.create(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        status=Task.Status.PENDING,
        scheduled_at=timezone.now() - timedelta(seconds=1),
        payload=base,
    )


@pytest.mark.django_db
def test_gmail_follow_up_sends_and_persists(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FakeGmailClient)

    handle_gmail_follow_up(_task(lead, deal))

    msg = Message.objects.get(source=Message.Source.GMAIL, direction=Message.Direction.OUTBOUND)
    assert msg.external_id.startswith(f"gmail-send:Arian:{lead.id}:gmail_fallback:step-0:")
    assert msg.sender == "ariant@getboundera.com"
    assert "Hi Ada" in msg.body
    assert "Analytical Engines" in msg.body


@pytest.mark.django_db
def test_gmail_follow_up_next_step_delay_starts_after_send(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    deal.connected_at = timezone.now() - timedelta(days=30)
    deal.save(update_fields=["connected_at"])
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FakeGmailClient)

    seen = {}

    def fake_delay(delay_hours, *, reference_time=None):
        seen["delay_hours"] = delay_hours
        seen["reference_time"] = reference_time
        return 123

    monkeypatch.setattr("gmail.tasks.follow_up._delay_seconds_to_active_due", fake_delay)

    handle_gmail_follow_up(_task(lead, deal))

    next_task = Task.objects.get(task_type=Task.TaskType.GMAIL_FOLLOW_UP, payload__step_index=1)
    assert seen["delay_hours"] == 192
    assert seen["reference_time"] is None
    assert next_task.scheduled_at > timezone.now() + timedelta(seconds=100)


@pytest.mark.django_db
def test_gmail_follow_up_uses_persisted_icp(monkeypatch):
    lead = _lead(icp="Channel")
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", FakeGmailClient)

    handle_gmail_follow_up(_task(lead, deal))

    msg = Message.objects.get(source=Message.Source.GMAIL, direction=Message.Direction.OUTBOUND)
    assert "vendors around FedRAMP 20x" in msg.body


@pytest.mark.django_db
def test_gmail_follow_up_stops_after_synced_gmail_reply(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)

    class ReplyingClient(FakeGmailClient):
        def search_threads_for_email(self, email):
            return [{
                "id": "thread-1",
                "messages": [{
                    "id": "reply-1",
                    "headers": {"From": lead.email},
                    "snippet": "reply",
                    "internalDate": str(int(timezone.now().timestamp() * 1000)),
                }],
            }]

        def send_message(self, **kwargs):
            raise AssertionError("should not send after reply sync")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", ReplyingClient)

    handle_gmail_follow_up(_task(lead, deal))

    assert Message.objects.filter(source=Message.Source.GMAIL, direction=Message.Direction.INBOUND).exists()
    assert not Message.objects.filter(source=Message.Source.GMAIL, direction=Message.Direction.OUTBOUND).exists()


@pytest.mark.django_db
def test_gmail_follow_up_dedup_skips_existing_step(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
        external_id=f"gmail-send:Arian:{lead.id}:gmail_fallback:step-0:old",
        sender="ariant@getboundera.com",
        body="already sent",
        sent_at=timezone.now(),
    )

    class NoSendClient(FakeGmailClient):
        def send_message(self, **kwargs):
            raise AssertionError("should not send duplicate step")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoSendClient)

    handle_gmail_follow_up(_task(lead, deal))


@pytest.mark.django_db
def test_gmail_follow_up_disabled_noops(monkeypatch):
    lead = _lead()
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", False)

    class NoSendClient(FakeGmailClient):
        def __init__(self, *, operator):
            raise AssertionError("disabled handler should not build Gmail client")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoSendClient)

    handle_gmail_follow_up(_task(lead, deal))

    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


@pytest.mark.django_db
def test_gmail_follow_up_missing_sender_icp_skips_without_client(monkeypatch):
    lead = _lead(icp="CSPs")
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)

    class NoClient:
        def __init__(self, *, operator):
            raise AssertionError("missing template should skip before Gmail client")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoClient)

    handle_gmail_follow_up(_task(lead, deal, operator="Missing"))

    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()


@pytest.mark.django_db
def test_gmail_follow_up_blank_template_skips_without_client(monkeypatch):
    lead = _lead(icp="CSPs")
    deal = _deal(lead)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr(
        "gmail.tasks.follow_up.render_for_icp",
        lambda **kwargs: (_ for _ in ()).throw(SheetsError("gmail templates: step 0 needs body_variants")),
    )

    class NoClient:
        def __init__(self, *, operator):
            raise AssertionError("blank template should skip before Gmail client")

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", NoClient)

    handle_gmail_follow_up(_task(lead, deal))

    assert not Message.objects.filter(source=Message.Source.GMAIL).exists()
