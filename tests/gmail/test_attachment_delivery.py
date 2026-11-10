"""End-to-end campaign email attachments: right file, right recipient, or held.

Uses the real permit, the real ``GmailClient`` MIME/upload path and a fake
Gmail API that records exactly what would be submitted.
"""
import base64
import hashlib
import json
from datetime import timedelta
from email import message_from_bytes, policy
from unittest.mock import patch

import pytest
from django.utils import timezone

from crm.models import Deal, Lead, Message
from email_assets.review import review_campaign_attachments
from email_assets.services import approve_asset, assign_asset, refresh_asset
from gmail.client import GmailClient
from gmail.delivery import consume_gmail_delivery_permit, issue_gmail_delivery_permit
from gmail.exceptions import GmailDeliveryAuthorizationError
from gmail.handoff import maybe_schedule_gmail_sequence
from gmail.submission import ATTACHMENT_HOLD_REASON_KEY, ATTACHMENT_HOLD_RETRY
from gmail.tasks.follow_up import handle_gmail_follow_up
from linkedin.enums import ProfileState
from linkedin.models import (
    Campaign,
    MessageProgram,
    MessageProgramVersion,
    OutboundDelivery,
    Task,
)
from tests.email_assets.helpers import FakeSource, approved_asset, pdf_bytes, sha
from tests.factories import UserFactory


pytestmark = pytest.mark.django_db

AUDIENCE = "csp-any-size-business-leaders-rev5-authorized-both"


# -- fake Gmail API ------------------------------------------------------


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, **_kwargs):
        return self._fn()


class _Messages:
    def __init__(self, service):
        self._service = service

    def send(self, **kwargs):
        def run():
            service = self._service
            number = len(service.sent) + 1
            media = kwargs.get("media_body")
            if media is not None:
                mime_bytes = media.getbytes(0, media.size())
            else:
                raw = kwargs["body"]["raw"]
                mime_bytes = base64.urlsafe_b64decode(raw)
            parsed = message_from_bytes(mime_bytes, policy=policy.default)
            thread_id = kwargs["body"].get("threadId") or f"thread-{number}"
            message_id = f"gmail-message-{number}"
            provider_rfc = f"<provider-{number}@mail.gmail.com>"
            service.sent.append({
                "kwargs": kwargs,
                "mime": parsed,
                "message_id": message_id,
                "thread_id": thread_id,
            })
            service.rfc[message_id] = (thread_id, provider_rfc)
            return {"id": message_id, "threadId": thread_id}

        return _Request(run)

    def get(self, *, userId, id, format, metadataHeaders):
        def run():
            thread_id, provider_rfc = self._service.rfc[id]
            return {
                "id": id,
                "threadId": thread_id,
                "payload": {"headers": [{"name": "Message-ID", "value": provider_rfc}]},
            }

        return _Request(run)


class _Users:
    def __init__(self, service):
        self._messages = _Messages(service)

    def messages(self):
        return self._messages


class FakeGmailService:
    def __init__(self):
        self.sent = []
        self.rfc = {}

    def users(self):
        return _Users(self)


@pytest.fixture
def gmail_service(monkeypatch):
    service = FakeGmailService()

    def factory(*, operator):
        client = object.__new__(GmailClient)
        client.operator = operator
        client.account_key = "arian_boundera"
        client.send_as = "ariant@getboundera.com"
        client.reply_to = "ariant@boundera.io"
        client._service = service
        client.validate_send_as = lambda: None
        return client

    monkeypatch.setattr("gmail.tasks.follow_up.GmailClient", factory)
    return service


@pytest.fixture
def drive(monkeypatch):
    source = FakeSource()
    monkeypatch.setitem(
        __import__("email_assets.providers", fromlist=["_SOURCE_FACTORIES"])._SOURCE_FACTORIES,
        "google_drive",
        lambda: source,
    )
    return source


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "linkedin.notifications.slack.notify_degraded",
        lambda **kwargs: sent.append(kwargs),
    )
    return sent


@pytest.fixture(autouse=True)
def gmail_enabled(monkeypatch):
    monkeypatch.setattr("gmail.handoff.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("gmail.tasks.follow_up.ENABLE_GMAIL_SEQUENCE", True)
    monkeypatch.setattr("linkedin.suppression.lead_suppression_match", lambda lead: None)


# -- campaign fixtures ----------------------------------------------------


def _hash(payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _gmail_row(step_index, *, subject, body, delay_hours, attachments=None):
    row = {
        "audience_key": AUDIENCE,
        "role_persona": "CSP Any Size | Business leaders",
        "fedramp_segment": "Rev5 Authorized",
        "channel": "gmail",
        "step_key": f"email-{step_index + 1}",
        "step_index": step_index,
        "delay_hours": delay_hours,
        "variant_key": "primary",
        "subject": subject,
        "body": body,
        "media": [],
        "sender_override": "",
        "notes": "",
    }
    if attachments:
        row["attachments"] = attachments
    return row


@pytest.fixture
def campaign():
    program = MessageProgram.objects.create(key="marketplace-six-email", name="Marketplace six email")
    payload = {
        "program_key": program.key,
        "program_name": program.name,
        "messages": [
            _gmail_row(0, subject="{company_name} on the Marketplace", body="Hi {first_name}, quick note.", delay_hours=0),
            _gmail_row(
                1,
                subject="Your Marketplace one-pager",
                body="Hi {first_name}, I attached the {company_name} Marketplace PDF.",
                delay_hours=192,
                attachments=[{"slot": "marketplace_pdf", "required": True}],
            ),
        ],
    }
    version = MessageProgramVersion.objects.create(
        program=program, version=1, schema_version=1, payload=payload,
        content_hash=_hash(payload), published_by="reviewer@example.com",
    )
    return Campaign.objects.create(
        name="Marketplace six email | Arian", user=UserFactory(), active_message_version=version,
    )


def _deal(campaign, first_name, company):
    lead = Lead.objects.create(
        first_name=first_name,
        last_name="Example",
        company_name=company,
        linkedin_url=f"https://www.linkedin.com/in/{first_name.lower()}-attach/",
        public_identifier=f"{first_name.lower()}-attach",
        email=f"{first_name.lower()}@{company.split()[0].lower()}.example",
        icp=AUDIENCE,
    )
    return Deal.objects.create(
        lead=lead, campaign=campaign, state=ProfileState.CONNECTED, connected_at=timezone.now(),
    )


def _run(task):
    task.refresh_from_db()
    due_at = max(timezone.now(), task.scheduled_at)
    with patch("django.utils.timezone.now", return_value=due_at):
        task.status = Task.Status.RUNNING
        task.started_at = timezone.now()
        task.save(update_fields={"status", "started_at"})
        handle_gmail_follow_up(task)
        task.refresh_from_db()
        if task.status == Task.Status.RUNNING:
            task.mark_completed()
    return task


def _email_two_task(deal):
    return Task.objects.get(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        payload__deal_id=deal.pk,
        payload__step_index=1,
    )


def _attachments(sent):
    return [
        (part.get_filename(), part.get_content_type(), sha(part.get_payload(decode=True)))
        for part in sent["mime"].iter_attachments()
    ]


def _send_email_one(deal, gmail_service):
    first = maybe_schedule_gmail_sequence(deal=deal, operator="Arian")
    _run(first)
    return gmail_service.sent[-1]


# -- tests ---------------------------------------------------------------


def test_each_recipient_gets_their_own_approved_file_and_email_one_is_unchanged(
    campaign, gmail_service, drive, alerts,
):
    acme = approved_asset(drive, key="acme-marketplace-pdf", content=pdf_bytes("acme"), filename="Acme Marketplace.pdf")
    beta = approved_asset(drive, key="beta-marketplace-pdf", content=pdf_bytes("beta"), filename="Beta Marketplace.pdf")
    ada = _deal(campaign, "Ada", "Acme Cloud")
    bea = _deal(campaign, "Bea", "Acme Cloud")
    cy = _deal(campaign, "Cy", "Beta Systems")
    assign_asset(key=acme.key, slot="marketplace_pdf", lead_ids=[ada.lead_id, bea.lead_id], assigned_by="Arian", apply=True)
    assign_asset(key=beta.key, slot="marketplace_pdf", lead_ids=[cy.lead_id], assigned_by="Arian", apply=True)

    expected = {ada: acme, bea: acme, cy: beta}
    for deal, asset in expected.items():
        email_one = _send_email_one(deal, gmail_service)
        # Email 1 declares no attachment: same inline request shape as before.
        assert "media_body" not in email_one["kwargs"]
        assert "raw" in email_one["kwargs"]["body"]
        assert email_one["mime"].get_content_type() == "text/plain"
        assert _attachments(email_one) == []

        _run(_email_two_task(deal))
        email_two = gmail_service.sent[-1]
        assert email_two["mime"]["To"] == deal.lead.email
        assert _attachments(email_two) == [(asset.filename, "application/pdf", asset.approved_sha256)]
        assert email_two["mime"].get_body(preferencelist=("plain",)).get_content().startswith(
            f"Hi {deal.lead.first_name}, I attached the {deal.lead.company_name} Marketplace PDF."
        )
        # Threading is preserved: same Gmail thread, reply headers to email 1.
        assert email_two["kwargs"]["body"]["threadId"] == email_one["thread_id"]
        assert "raw" not in email_two["kwargs"]["body"]
        _thread, first_rfc = gmail_service.rfc[email_one["message_id"]]
        assert email_two["mime"]["In-Reply-To"] == first_rfc

        sent_two = Message.objects.get(raw__delivery_id=_email_two_task(deal).payload["delivery_id"])
        [evidence] = sent_two.raw["attachments"]
        assert evidence["asset_key"] == asset.key
        assert evidence["sha256"] == asset.approved_sha256
        assert evidence["requirement"] == {"slot": "marketplace_pdf", "required": True}
        sent_one = Message.objects.get(raw__delivery_id=_email_one_delivery(deal).pk)
        assert "attachments" not in sent_one.raw

    assert len(gmail_service.sent) == 6
    assert alerts == []
    report = review_campaign_attachments(campaign_id=campaign.pk)
    assert {(row.deal_id, row.status, row.sha256) for row in report.rows} == {
        (deal.pk, "sent", asset.approved_sha256) for deal, asset in expected.items()
    }


def _email_one_delivery(deal):
    return OutboundDelivery.objects.get(enrollment__deal=deal, step_index=0)


def test_missing_file_holds_the_email_with_a_reason_then_sends_once_assigned(
    campaign, gmail_service, drive, alerts,
):
    dee = _deal(campaign, "Dee", "Gamma Gov")
    _send_email_one(dee, gmail_service)
    task = _email_two_task(dee)
    delivery = OutboundDelivery.objects.get(pk=task.payload["delivery_id"])

    held_at = max(timezone.now(), task.scheduled_at)
    _run(task)

    assert len(gmail_service.sent) == 1  # email 2 was not sent
    task.refresh_from_db()
    delivery.refresh_from_db()
    assert task.status == Task.Status.PENDING
    assert task.error == "Attachment hold: slot marketplace_pdf: no file is assigned to this lead"
    assert task.scheduled_at >= held_at + ATTACHMENT_HOLD_RETRY
    assert delivery.status == OutboundDelivery.Status.QUEUED
    assert "gmail_submission_attempted_at" not in task.payload
    assert len(alerts) == 1 and "email-2" in alerts[0]["title"]

    # The review shows the hold before anything is sent.
    [row] = review_campaign_attachments(campaign_id=campaign.pk).rows
    assert (row.deal_id, row.step_key, row.status) == (dee.pk, "email-2", "missing_assignment")

    # A second check with the same problem does not alert again.
    _run(task)
    assert len(alerts) == 1 and len(gmail_service.sent) == 1

    gamma = approved_asset(drive, key="gamma-marketplace-pdf", content=pdf_bytes("gamma"))
    assign_asset(key=gamma.key, slot="marketplace_pdf", lead_ids=[dee.lead_id], assigned_by="Arian", apply=True)
    _run(task)

    task.refresh_from_db()
    delivery.refresh_from_db()
    assert task.status == Task.Status.COMPLETED
    assert task.error == ""
    assert ATTACHMENT_HOLD_REASON_KEY not in task.payload
    assert delivery.status == OutboundDelivery.Status.SENT
    assert _attachments(gmail_service.sent[-1]) == [
        (gamma.filename, "application/pdf", gamma.approved_sha256),
    ]


def test_file_changed_since_approval_holds_until_the_new_bytes_are_approved(
    campaign, gmail_service, drive, alerts,
):
    acme = approved_asset(drive, key="acme-marketplace-pdf", content=pdf_bytes("approved v1"))
    ada = _deal(campaign, "Ada", "Acme Cloud")
    assign_asset(key=acme.key, slot="marketplace_pdf", lead_ids=[ada.lead_id], assigned_by="Arian", apply=True)
    _send_email_one(ada, gmail_service)
    drive.change(acme.provider_file_id, pdf_bytes("edited v2"))

    task = _run(_email_two_task(ada))

    assert len(gmail_service.sent) == 1
    assert task.status == Task.Status.PENDING
    assert "file changed since approval" in task.error
    report = review_campaign_attachments(campaign_id=campaign.pk, check_files=True)
    assert [row.status for row in report.rows] == ["changed_since_approval"]

    # Inaccessible is a different, new reason: it alerts once more.
    drive.fail(acme.provider_file_id, "permission", "sharing removed")
    task = _run(task)
    assert "could not be read (permission: sharing removed)" in task.error
    assert len(alerts) == 2
    drive.files[acme.provider_file_id].error = None

    refreshed, changed, _ = refresh_asset(key=acme.key, apply=True)
    assert changed
    approve_asset(key=acme.key, approved_by="Arian", sha256=refreshed.sha256, apply=True)
    task = _run(task)

    assert task.status == Task.Status.COMPLETED
    assert _attachments(gmail_service.sent[-1])[0][2] == sha(pdf_bytes("edited v2"))


def test_permit_rejects_any_attachment_other_than_the_one_it_was_issued_for(campaign, drive):
    from email_assets.files import AttachmentFile

    deal = _deal(campaign, "Ada", "Acme Cloud")
    task = Task.objects.create(
        task_type=Task.TaskType.GMAIL_FOLLOW_UP,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        payload={"lead_id": deal.lead_id, "operator": "Arian", "step_index": 1},
    )
    approved = AttachmentFile(
        filename="acme.pdf", mime_type="application/pdf",
        content=pdf_bytes("acme"), sha256=sha(pdf_bytes("acme")),
    )
    swapped = AttachmentFile(
        filename="acme.pdf", mime_type="application/pdf",
        content=pdf_bytes("beta"), sha256=sha(pdf_bytes("beta")),
    )
    message = {
        "operator": "Arian", "account_key": "arian_boundera",
        "to": "ada@acme.example", "subject": "S", "body": "B",
    }
    permit = issue_gmail_delivery_permit(task=task, **message, attachments=[approved])
    with pytest.raises(GmailDeliveryAuthorizationError, match="does not match"):
        consume_gmail_delivery_permit(permit, **message, attachments=[swapped])
    with pytest.raises(GmailDeliveryAuthorizationError, match="does not match"):
        consume_gmail_delivery_permit(
            issue_gmail_delivery_permit(task=task, **message, attachments=[approved]),
            **message,
        )
    consume_gmail_delivery_permit(
        issue_gmail_delivery_permit(task=task, **message, attachments=[approved]),
        **message,
        attachments=[approved],
    )


def test_attachment_steps_are_rejected_on_linkedin_channels(campaign):
    from linkedin.message_delivery import MessageDeliveryError, index_message_version

    version = campaign.active_message_version
    payload = json.loads(json.dumps(version.payload))
    payload["messages"].append({
        **_gmail_row(0, subject="", body="Hi", delay_hours=0),
        "channel": "linkedin_followup",
        "step_key": "followup-1",
        "attachments": [{"slot": "marketplace_pdf", "required": True}],
    })
    bad = MessageProgramVersion.objects.create(
        program=version.program, version=2, schema_version=1, payload=payload,
        content_hash=_hash(payload), published_by="reviewer@example.com",
    )
    with pytest.raises(MessageDeliveryError, match="only supported on Gmail"):
        index_message_version(bad)


def test_non_canonical_attachment_declarations_are_refused(campaign):
    from linkedin.message_delivery import MessageDeliveryError, index_message_version

    version = campaign.active_message_version
    payload = json.loads(json.dumps(version.payload))
    payload["messages"][1]["attachments"] = [{"slot": "marketplace_pdf"}]
    loose = MessageProgramVersion.objects.create(
        program=version.program, version=3, schema_version=1, payload=payload,
        content_hash=_hash(payload), published_by="reviewer@example.com",
    )
    with pytest.raises(MessageDeliveryError, match="canonical"):
        index_message_version(loose)
