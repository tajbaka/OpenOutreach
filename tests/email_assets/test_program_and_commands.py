import base64
import json
from email import message_from_bytes, policy
from email.message import EmailMessage

import pytest
from django.core.management import CommandError, call_command

from crm.models import Lead
from email_assets.models import EmailAsset, LeadEmailAsset
from email_assets.one_off import (
    attach_to_message,
    recheck_one_off_attachments,
    resolve_one_off_attachments,
    verify_gmail_attachments,
)
from email_assets.resolution import AttachmentHold
from linkedin.exceptions import GeneralICPMessageError
from linkedin.general_icp_json import load_general_message_programs, render_general_icp_stores
from linkedin.general_icp_messages import general_drafts_to_wide_rows
from tests.email_assets.helpers import FakeSource, approved_asset, pdf_bytes, sha


AUDIENCE = "csp-any-size-business-leaders-rev5-authorized-both"


def _store_message(channel, step_index, *, attachments=None):
    message = {
        "audience_key": AUDIENCE,
        "role_persona": "CSP Any Size | Business leaders",
        "fedramp_segment": "Rev5 Authorized",
        "channel": channel,
        "step_key": f"{'email' if channel == 'gmail' else 'connect'}-{step_index + 1}",
        "step_index": step_index,
        "delay_hours": 0 if step_index == 0 else 192,
        "variant_key": "primary",
        "subject": "Subject" if channel == "gmail" else "",
        "body": "Hi {first_name}",
        "media": [],
        "sender_override": "",
        "notes": "",
    }
    if attachments is not None:
        message["attachments"] = attachments
    return message


def _write_stores(tmp_path, gmail_messages, linkedin_messages=None):
    program = {
        "program_name": "Marketplace six email",
        "icp_labels": {AUDIENCE: "Business leaders | Any Size | Rev5 Authorized | Both"},
    }
    linkedin = {
        "schema_version": 2,
        "shared_programs": {"marketplace-six-email": {
            **program,
            "messages": linkedin_messages or [_store_message("linkedin_connect", 0)],
        }},
        "sender_icps": {},
    }
    gmail = {
        "schema_version": 2,
        "shared_programs": {"marketplace-six-email": {**program, "messages": gmail_messages}},
        "sender_icps": {},
    }
    linkedin_path, gmail_path = tmp_path / "linkedin.json", tmp_path / "gmail.json"
    linkedin_path.write_text(json.dumps(linkedin), encoding="utf-8")
    gmail_path.write_text(json.dumps(gmail), encoding="utf-8")
    return linkedin_path, gmail_path


def test_json_program_email_two_attachment_flows_into_the_published_payload(tmp_path):
    paths = _write_stores(tmp_path, [
        _store_message("gmail", 0),
        _store_message("gmail", 1, attachments=[{"slot": "marketplace_pdf"}]),
    ])
    [draft] = load_general_message_programs(linkedin_path=paths[0], gmail_path=paths[1])
    by_step = {message["step_key"]: message for message in draft.payload()["messages"]}

    assert "attachments" not in by_step["email-1"]
    assert by_step["email-2"]["attachments"] == [{"slot": "marketplace_pdf", "required": True}]

    # The generated store round-trips the canonical declaration.
    _linkedin_store, gmail_store = render_general_icp_stores(
        [draft], linkedin_path=paths[0], gmail_path=paths[1],
    )
    gmail_rows = gmail_store["shared_programs"]["marketplace-six-email"]["messages"]
    assert [row.get("attachments") for row in gmail_rows] == [
        None, [{"slot": "marketplace_pdf", "required": True}],
    ]

    # The eight-column Sheet layout cannot carry attachments, so it refuses.
    with pytest.raises(GeneralICPMessageError, match="hidden routing metadata"):
        general_drafts_to_wide_rows([draft])


def test_json_program_rejects_attachments_on_linkedin_steps(tmp_path):
    paths = _write_stores(
        tmp_path,
        [_store_message("gmail", 0)],
        [_store_message("linkedin_connect", 0, attachments=[{"slot": "marketplace_pdf"}])],
    )
    with pytest.raises(GeneralICPMessageError, match="only supported on Gmail"):
        load_general_message_programs(linkedin_path=paths[0], gmail_path=paths[1])


def test_json_program_rejects_malformed_attachment_declarations(tmp_path):
    paths = _write_stores(tmp_path, [_store_message("gmail", 0, attachments=[{"file": "x.pdf"}])])
    with pytest.raises(GeneralICPMessageError, match="unknown fields"):
        load_general_message_programs(linkedin_path=paths[0], gmail_path=paths[1])


# -- one-off emails ---------------------------------------------------------


class _DraftService:
    def __init__(self, raw_bytes):
        self._raw = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")

    def users(self):
        return self

    def drafts(self):
        return self

    def get(self, **kwargs):
        assert kwargs["format"] == "raw"
        return self

    def execute(self):
        return {"message": {"raw": self._raw}}


@pytest.mark.django_db
def test_one_off_draft_attaches_and_verifies_the_approved_file(monkeypatch):
    source = FakeSource()
    asset = approved_asset(source, key="acme-marketplace-pdf", content=pdf_bytes("acme"), filename="Acme.pdf")
    attachments = resolve_one_off_attachments(asset_keys=[asset.key], source_for=lambda _p: source)

    message = EmailMessage()
    message["To"] = "jane@acme.example.com"
    message.set_content("Hi Jane,\nThe PDF is attached.")
    attach_to_message(message, attachments)

    found = verify_gmail_attachments(_DraftService(message.as_bytes()), draft_id="draft-1", expected=attachments)
    assert found == [{
        "filename": "Acme.pdf", "mime_type": "application/pdf",
        "sha256": sha(pdf_bytes("acme")), "size_bytes": len(pdf_bytes("acme")),
    }]

    tampered = EmailMessage()
    tampered.set_content("Hi")
    tampered.add_attachment(pdf_bytes("other"), maintype="application", subtype="pdf", filename="Acme.pdf")
    with pytest.raises(ValueError, match="do not match the approved files"):
        verify_gmail_attachments(_DraftService(tampered.as_bytes()), draft_id="d", expected=attachments)


@pytest.mark.django_db
def test_one_off_recheck_blocks_a_send_after_retirement_or_reassignment():
    from email_assets.services import assign_asset, retire_asset

    source = FakeSource()
    acme = approved_asset(source, key="acme-pdf", content=pdf_bytes("acme"))
    other = approved_asset(source, key="other-pdf", content=pdf_bytes("other"))
    lead = Lead.objects.create(
        first_name="Jane", linkedin_url="https://www.linkedin.com/in/jane-oneoff/",
        public_identifier="jane-oneoff",
    )
    assign_asset(key=acme.key, slot="marketplace_pdf", lead_ids=[lead.pk], assigned_by="Arian", apply=True)
    by_slot = resolve_one_off_attachments(lead_id=lead.pk, slots=["marketplace_pdf"], source_for=lambda _p: source)
    by_key = resolve_one_off_attachments(asset_keys=[other.key], source_for=lambda _p: source)
    recheck_one_off_attachments(by_slot, lead_id=lead.pk)
    recheck_one_off_attachments(by_key)

    assign_asset(key=other.key, slot="marketplace_pdf", lead_ids=[lead.pk], assigned_by="Arian", apply=True)
    with pytest.raises(AttachmentHold, match="assigned file changed"):
        recheck_one_off_attachments(by_slot, lead_id=lead.pk)
    retire_asset(key=other.key, apply=True)
    with pytest.raises(AttachmentHold, match="is retired"):
        recheck_one_off_attachments(by_key)
    with pytest.raises(ValueError, match="lead_id"):
        recheck_one_off_attachments(by_slot)


@pytest.mark.django_db
def test_one_off_refuses_an_unapproved_file():
    source = FakeSource()
    from email_assets.services import register_asset

    file_id = source.add("fileoneoffpending1", pdf_bytes("draft"))
    register_asset(
        url=f"https://drive.google.com/file/d/{file_id}/view", key="draft-pdf",
        name="Draft", registered_by="Arian", apply=True, source=source,
    )
    with pytest.raises(AttachmentHold, match="awaiting approval"):
        resolve_one_off_attachments(asset_keys=["draft-pdf"], source_for=lambda _p: source)


# -- commands ----------------------------------------------------------------


@pytest.fixture
def drive(monkeypatch):
    import email_assets.providers as providers

    source = FakeSource()
    monkeypatch.setitem(providers._SOURCE_FACTORIES, "google_drive", lambda: source)
    return source


def _call(*args):
    from io import StringIO

    out = StringIO()
    call_command(*args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
def test_commands_register_approve_assign_and_review(drive, tmp_path):
    file_id = drive.add("1CommandFlowFile0001", pdf_bytes("acme"), name="Acme Marketplace.pdf")
    link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"

    preview = _call(
        "register_email_asset", link, "--key", "acme-marketplace-pdf",
        "--name", "Acme Marketplace PDF", "--registered-by", "Arian",
    )
    assert "Preview only" in preview and not EmailAsset.objects.exists()

    saved = _call(
        "register_email_asset", link, "--key", "acme-marketplace-pdf",
        "--name", "Acme Marketplace PDF", "--registered-by", "Arian", "--apply",
    )
    digest = sha(pdf_bytes("acme"))
    assert f"--sha256 {digest}" in saved

    with pytest.raises(CommandError, match="not the registered file hash"):
        _call("approve_email_asset", "acme-marketplace-pdf", "--sha256", "1" * 64, "--approved-by", "Arian", "--apply")
    _call("approve_email_asset", "acme-marketplace-pdf", "--sha256", digest, "--approved-by", "Arian", "--apply")
    assert EmailAsset.objects.get().is_sendable

    lead = Lead.objects.create(
        first_name="Ada", company_name="Acme Cloud",
        linkedin_url="https://www.linkedin.com/in/ada-cmd/", public_identifier="ada-cmd",
    )
    assigned = _call(
        "assign_email_asset", "acme-marketplace-pdf", "--slot", "marketplace_pdf",
        "--lead", str(lead.pk), "--assigned-by", "Arian", "--apply",
    )
    assert "Applied: 1 create." in assigned
    assert LeadEmailAsset.objects.get().lead_id == lead.pk

    shown = _call("show_email_asset", "acme-marketplace-pdf", "--check", "--download", str(tmp_path / "copy.pdf"))
    assert "readable and unchanged" in shown
    assert (tmp_path / "copy.pdf").read_bytes() == pdf_bytes("acme")
    assert "acme-marketplace-pdf" in _call("show_email_asset")


@pytest.mark.django_db
def test_review_command_reports_unknown_campaign():
    with pytest.raises(CommandError, match="does not exist"):
        _call("review_email_attachments", "--campaign", "999999")
