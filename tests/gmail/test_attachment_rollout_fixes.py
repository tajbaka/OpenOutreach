"""Regressions for the pre-rollout review of email attachments.

1. Approval/assignment changes after the download must hold the send.
2. A refresh must never undo a concurrent retirement (or approval race).
3. Preflight must prove the file can actually be downloaded.
4. Preflight must catch attachment combinations that sending rejects.
"""
import pytest

from email_assets.models import EmailAsset, LeadEmailAsset
from email_assets.providers import AssetSourceError
from email_assets.requirements import AttachmentRequirement
from email_assets.resolution import AttachmentHold, HoldCode, check_asset_source, resolve_attachments
from email_assets.review import review_campaign_attachments
from email_assets.services import (
    EmailAssetError,
    approve_asset,
    assign_asset,
    refresh_asset,
    remove_assignments,
    retire_asset,
)
from linkedin.models import MessageProgramVersion, OutboundDelivery, Task
from tests.email_assets.helpers import approved_asset, pdf_bytes, sha
from tests.gmail.test_attachment_delivery import (  # noqa: F401 - fixtures
    _deal,
    _email_two_task,
    _hash,
    _run,
    _send_email_one,
    alerts,
    campaign,
    drive,
    gmail_enabled,
    gmail_service,
)


pytestmark = pytest.mark.django_db


def _held(task, gmail_service, sent_before):
    task.refresh_from_db()
    delivery = OutboundDelivery.objects.get(pk=task.payload["delivery_id"])
    assert len(gmail_service.sent) == sent_before, "email 2 must not be submitted"
    assert task.status == Task.Status.PENDING
    assert "gmail_submission_attempted_at" not in task.payload
    assert delivery.status == OutboundDelivery.Status.QUEUED
    return task.error


# -- 1. final send check -----------------------------------------------------


@pytest.mark.parametrize("change, expected", [
    ("retire", "is retired"),
    ("unassign", "no file is assigned"),
    ("reassign", "assigned file changed while"),
    ("reapprove", "changed since approval"),
])
def test_change_after_download_holds_instead_of_sending(
    campaign, gmail_service, drive, alerts, change, expected,
):
    acme = approved_asset(drive, key="acme-marketplace-pdf", content=pdf_bytes("acme v1"))
    other = approved_asset(drive, key="other-marketplace-pdf", content=pdf_bytes("other"))
    ada = _deal(campaign, "Ada", "Acme Cloud")
    assign_asset(key=acme.key, slot="marketplace_pdf", lead_ids=[ada.lead_id], assigned_by="Arian", apply=True)
    _send_email_one(ada, gmail_service)

    def mid_download(_file_id):
        if change == "retire":
            retire_asset(key=acme.key, apply=True)
        elif change == "unassign":
            remove_assignments(slot="marketplace_pdf", lead_ids=[ada.lead_id], apply=True)
        elif change == "reassign":
            assign_asset(key=other.key, slot="marketplace_pdf", lead_ids=[ada.lead_id], assigned_by="Arian", apply=True)
        else:
            # The operator approves new bytes after this send downloaded the old ones.
            drive.change(acme.provider_file_id, pdf_bytes("acme v2"))
            refreshed, _changed, _ = refresh_asset(key=acme.key, apply=True, source=drive)
            approve_asset(key=acme.key, approved_by="Arian", sha256=refreshed.sha256, apply=True, source=drive)

    drive.on_download = mid_download
    task = _run(_email_two_task(ada))

    error = _held(task, gmail_service, sent_before=1)
    assert error.startswith("Attachment hold: ") and expected in error


def test_unchanged_state_still_sends_after_the_final_check(campaign, gmail_service, drive, alerts):
    acme = approved_asset(drive, key="acme-marketplace-pdf", content=pdf_bytes("acme"))
    ada = _deal(campaign, "Ada", "Acme Cloud")
    assign_asset(key=acme.key, slot="marketplace_pdf", lead_ids=[ada.lead_id], assigned_by="Arian", apply=True)
    _send_email_one(ada, gmail_service)

    task = _run(_email_two_task(ada))

    assert task.status == Task.Status.COMPLETED
    assert len(gmail_service.sent) == 2


# -- 2. refresh/approve races -------------------------------------------------


def test_refresh_never_undoes_a_concurrent_retirement(drive):
    acme = approved_asset(drive, key="acme", content=pdf_bytes("v1"))
    drive.on_download = lambda _file_id: retire_asset(key="acme", apply=True)

    refresh_asset(key="acme", apply=True, source=drive)

    acme.refresh_from_db()
    assert acme.status == EmailAsset.Status.RETIRED


def test_changed_bytes_refresh_keeps_a_concurrent_retirement(drive):
    acme = approved_asset(drive, key="acme", content=pdf_bytes("v1"))
    drive.change(acme.provider_file_id, pdf_bytes("v2"))
    drive.on_download = lambda _file_id: retire_asset(key="acme", apply=True)

    refreshed, changed, _ = refresh_asset(key="acme", apply=True, source=drive)

    acme.refresh_from_db()
    assert changed
    assert acme.status == EmailAsset.Status.RETIRED
    assert acme.sha256 == sha(pdf_bytes("v2"))
    assert refreshed.status == EmailAsset.Status.RETIRED


def test_approval_refuses_when_the_asset_is_retired_meanwhile(drive):
    from email_assets.services import register_asset

    file_id = drive.add("fileapproverace01", pdf_bytes("v1"))
    asset, _ = register_asset(
        url=f"https://drive.google.com/file/d/{file_id}/view", key="acme",
        name="Acme", registered_by="Arian", apply=True, source=drive,
    )
    drive.on_download = lambda _file_id: retire_asset(key="acme", apply=True)

    with pytest.raises(EmailAssetError, match="changed while it was being approved"):
        approve_asset(key="acme", approved_by="Arian", sha256=asset.sha256, apply=True, source=drive)

    asset.refresh_from_db()
    assert asset.status == EmailAsset.Status.RETIRED
    assert not asset.approved_sha256


# -- 3. preflight proves download access ---------------------------------------


def test_preflight_reports_a_download_restricted_file(drive):
    acme = approved_asset(drive, key="acme", content=pdf_bytes("acme"))
    drive.files[acme.provider_file_id].can_download = False

    problem = check_asset_source(acme, source_for=lambda _provider: drive)

    assert problem is not None and problem.code == HoldCode.INACCESSIBLE
    assert "download" in problem.message


def test_preflight_downloads_even_when_the_checksum_matches(drive):
    acme = approved_asset(drive, key="acme", content=pdf_bytes("acme"))
    drive.files[acme.provider_file_id].download_error = AssetSourceError("permission", "download denied")

    problem = check_asset_source(acme, source_for=lambda _provider: drive)

    assert problem is not None and problem.code == HoldCode.INACCESSIBLE
    assert "download denied" in problem.message


def test_send_holds_a_download_restricted_file_with_a_clear_reason(campaign, gmail_service, drive, alerts):
    acme = approved_asset(drive, key="acme-marketplace-pdf", content=pdf_bytes("acme"))
    ada = _deal(campaign, "Ada", "Acme Cloud")
    assign_asset(key=acme.key, slot="marketplace_pdf", lead_ids=[ada.lead_id], assigned_by="Arian", apply=True)
    _send_email_one(ada, gmail_service)
    drive.files[acme.provider_file_id].can_download = False
    downloads_before = len(drive.downloads)

    task = _run(_email_two_task(ada))

    assert "download" in _held(task, gmail_service, sent_before=1)
    assert len(drive.downloads) == downloads_before


# -- 4. preflight checks the combination ---------------------------------------


@pytest.fixture
def two_attachment_campaign(campaign):
    version = campaign.active_message_version
    payload = {**version.payload, "messages": [dict(message) for message in version.payload["messages"]]}
    payload["messages"][1]["attachments"] = [
        {"slot": "marketplace_pdf", "required": True},
        {"slot": "case_study", "required": True},
    ]
    campaign.active_message_version = MessageProgramVersion.objects.create(
        program=version.program, version=2, schema_version=1, payload=payload,
        content_hash=_hash(payload), published_by="reviewer@example.com", based_on_version=version,
    )
    campaign.save(update_fields={"active_message_version"})
    return campaign


def _assign_pair(drive, deal, *, first_name, second_name, second_size=None):
    first = approved_asset(drive, key="pair-one", content=pdf_bytes("one"), filename=first_name)
    second = approved_asset(drive, key="pair-two", content=pdf_bytes("two"), filename=second_name)
    if second_size is not None:
        EmailAsset.objects.filter(pk=second.pk).update(size_bytes=second_size)
    assign_asset(key=first.key, slot="marketplace_pdf", lead_ids=[deal.lead_id], assigned_by="Arian", apply=True)
    assign_asset(key=second.key, slot="case_study", lead_ids=[deal.lead_id], assigned_by="Arian", apply=True)


def test_review_flags_two_files_with_one_filename(two_attachment_campaign, drive):
    ada = _deal(two_attachment_campaign, "Ada", "Acme Cloud")
    _assign_pair(drive, ada, first_name="Acme.pdf", second_name="acme.PDF")

    report = review_campaign_attachments(campaign_id=two_attachment_campaign.pk, check_files=True)

    assert [row.status for row in report.rows] == ["invalid_combination", "invalid_combination"]
    assert "one filename" in report.rows[0].reason


def test_review_flags_attachments_over_the_total_size_limit(two_attachment_campaign, drive):
    from email_assets.files import MAX_EMAIL_ATTACHMENT_BYTES

    ada = _deal(two_attachment_campaign, "Ada", "Acme Cloud")
    _assign_pair(drive, ada, first_name="One.pdf", second_name="Two.pdf", second_size=MAX_EMAIL_ATTACHMENT_BYTES)

    report = review_campaign_attachments(campaign_id=two_attachment_campaign.pk)

    assert {row.status for row in report.rows} == {"invalid_combination"}
    assert "total" in report.rows[0].reason


def test_send_holds_an_invalid_combination_before_downloading(
    two_attachment_campaign, gmail_service, drive, alerts,
):
    ada = _deal(two_attachment_campaign, "Ada", "Acme Cloud")
    _assign_pair(drive, ada, first_name="Acme.pdf", second_name="ACME.pdf")
    _send_email_one(ada, gmail_service)
    downloads_before = len(drive.downloads)

    task = _run(_email_two_task(ada))

    assert "one filename" in _held(task, gmail_service, sent_before=1)
    assert len(drive.downloads) == downloads_before


def test_resolution_reports_combination_problems_with_their_own_code(drive):
    from crm.models import Lead

    lead = Lead.objects.create(
        first_name="Ada", linkedin_url="https://www.linkedin.com/in/ada-combo/", public_identifier="ada-combo",
    )

    class _Deal:
        lead_id = lead.pk

    _assign_pair(drive, _Deal, first_name="Same.pdf", second_name="same.pdf")
    with pytest.raises(AttachmentHold) as hold:
        resolve_attachments(
            lead_id=lead.pk,
            requirements=[AttachmentRequirement(slot="marketplace_pdf"), AttachmentRequirement(slot="case_study")],
            source_for=lambda _provider: drive,
        )
    assert [problem.code for problem in hold.value.problems] == [HoldCode.INVALID_COMBINATION]
    assert LeadEmailAsset.objects.filter(lead=lead).count() == 2
