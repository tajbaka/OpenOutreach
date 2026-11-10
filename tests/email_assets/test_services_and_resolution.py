import pytest

from crm.models import Deal, Lead
from email_assets.models import EmailAsset, LeadEmailAsset
from email_assets.requirements import AttachmentRequirement
from email_assets.resolution import AttachmentHold, HoldCode, plan_attachments, resolve_attachments
from email_assets.services import (
    EmailAssetError,
    approve_asset,
    assign_asset,
    leads_for_company,
    refresh_asset,
    register_asset,
    remove_assignments,
    retire_asset,
)
from linkedin.models import Campaign
from tests.email_assets.helpers import FakeSource, approved_asset, pdf_bytes, sha
from tests.factories import UserFactory


pytestmark = pytest.mark.django_db

SLOT = AttachmentRequirement(slot="marketplace_pdf")


@pytest.fixture
def source():
    return FakeSource()


def _lead(name, company="Acme Cloud"):
    return Lead.objects.create(
        first_name=name,
        last_name="Tester",
        company_name=company,
        linkedin_url=f"https://www.linkedin.com/in/{name.lower()}-assets/",
        public_identifier=f"{name.lower()}-assets",
        email=f"{name.lower()}@example.com",
    )


def test_register_previews_without_writing_then_saves_awaiting_approval(source):
    file_id = source.add("filepreview000001", pdf_bytes("acme"), name="Acme Marketplace.pdf")
    url = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"

    preview, snapshot = register_asset(
        url=url, key="acme-marketplace-pdf", name="Acme Marketplace PDF",
        registered_by="Arian", source=source,
    )
    assert preview.pk is None and not EmailAsset.objects.exists()
    assert preview.sha256 == snapshot.sha256 == sha(pdf_bytes("acme"))
    assert preview.filename == "Acme Marketplace.pdf"

    asset, _ = register_asset(
        url=url, key="acme-marketplace-pdf", name="Acme Marketplace PDF",
        registered_by="Arian", apply=True, source=source,
    )
    assert asset.status == EmailAsset.Status.PENDING
    assert not asset.is_sendable
    assert asset.source_url == f"https://drive.google.com/file/d/{file_id}/view"

    with pytest.raises(EmailAssetError, match="already registered"):
        register_asset(
            url=url, key="another-key", name="Dup", registered_by="Arian", source=source,
        )


def test_register_refuses_unsupported_files(source):
    source.add("fileexecutable001", b"MZ\x90\x00", name="setup.exe", mime_type="application/x-msdownload")
    with pytest.raises(EmailAssetError, match="not an allowed"):
        register_asset(
            url="https://drive.google.com/file/d/fileexecutable001/view",
            key="setup", name="Setup", registered_by="Arian", source=source,
        )


def test_approval_binds_to_the_exact_reviewed_hash(source):
    file_id = source.add("fileapproval00001", pdf_bytes("v1"))
    asset, _ = register_asset(
        url=f"https://drive.google.com/file/d/{file_id}/view", key="acme",
        name="Acme", registered_by="Arian", apply=True, source=source,
    )
    with pytest.raises(EmailAssetError, match="is not the registered file hash"):
        approve_asset(key="acme", approved_by="Arian", sha256="0" * 64, apply=True, source=source)

    # The Drive file changes after the reviewer looked at it: approval refuses.
    source.change(file_id, pdf_bytes("v2"))
    with pytest.raises(EmailAssetError, match="changed since it was registered"):
        approve_asset(key="acme", approved_by="Arian", sha256=asset.sha256, apply=True, source=source)
    asset.refresh_from_db()
    assert asset.status == EmailAsset.Status.PENDING

    refreshed, changed, _ = refresh_asset(key="acme", apply=True, source=source)
    assert changed and refreshed.sha256 == sha(pdf_bytes("v2"))
    approved, already = approve_asset(
        key="acme", approved_by="Arian", sha256=refreshed.sha256, apply=True, source=source,
    )
    assert not already and approved.is_sendable
    assert approved.approved_by == "Arian"


def test_refresh_returns_a_changed_approved_asset_to_review(source):
    asset = approved_asset(source, key="acme", content=pdf_bytes("v1"))
    source.change(asset.provider_file_id, pdf_bytes("v2"))

    refreshed, changed, _ = refresh_asset(key="acme", apply=True, source=source)

    assert changed
    assert refreshed.status == EmailAsset.Status.PENDING
    assert refreshed.approved_sha256 == sha(pdf_bytes("v1"))
    assert not refreshed.is_sendable


def test_several_contacts_at_one_company_share_one_asset(source):
    asset = approved_asset(source, key="acme", content=pdf_bytes("acme"))
    campaign = Campaign.objects.create(name="Assets campaign", user=UserFactory())
    ada, bea = _lead("Ada"), _lead("Bea", company=" acme   CLOUD ")
    _lead("Cy", company="Other Co")
    for lead in (ada, bea):
        Deal.objects.create(lead=lead, campaign=campaign)

    matches = leads_for_company(campaign_id=campaign.pk, company="Acme Cloud")
    assert [lead.pk for lead in matches] == [ada.pk, bea.pk]

    preview = assign_asset(
        key="acme", slot="marketplace_pdf", lead_ids=[lead.pk for lead in matches],
        assigned_by="Arian",
    )
    assert [change.action for change in preview] == ["create", "create"]
    assert not LeadEmailAsset.objects.exists()

    assign_asset(
        key="acme", slot="marketplace_pdf", lead_ids=[ada.pk, bea.pk],
        assigned_by="Arian", apply=True,
    )
    assert set(asset.lead_assignments.values_list("lead_id", flat=True)) == {ada.pk, bea.pk}
    for lead in (ada, bea):
        [resolved] = resolve_attachments(
            lead_id=lead.pk,
            requirements=[SLOT],
            source_for=lambda _provider: source,
        )
        assert resolved.asset_key == "acme"
        assert resolved.file.sha256 == sha(pdf_bytes("acme"))

    removed = remove_assignments(slot="marketplace_pdf", lead_ids=[bea.pk], apply=True)
    assert [change.action for change in removed] == ["remove"]
    assert list(asset.lead_assignments.values_list("lead_id", flat=True)) == [ada.pk]


def test_resolution_holds_for_every_unsendable_state(source):
    lead = _lead("Ada")
    fetch = lambda _provider: source  # noqa: E731

    with pytest.raises(AttachmentHold) as missing:
        resolve_attachments(lead_id=lead.pk, requirements=[SLOT], source_for=fetch)
    assert [problem.code for problem in missing.value.problems] == [HoldCode.MISSING_ASSIGNMENT]

    file_id = source.add("filepending000001", pdf_bytes("pending"))
    register_asset(
        url=f"https://drive.google.com/file/d/{file_id}/view", key="pending",
        name="Pending", registered_by="Arian", apply=True, source=source,
    )
    assign_asset(key="pending", slot="marketplace_pdf", lead_ids=[lead.pk], assigned_by="Arian", apply=True)
    with pytest.raises(AttachmentHold) as pending:
        resolve_attachments(lead_id=lead.pk, requirements=[SLOT], source_for=fetch)
    assert pending.value.problems[0].code == HoldCode.PENDING_APPROVAL
    assert source.downloads.count(file_id) == 1  # only the registration read it

    asset = approved_asset(source, key="acme", content=pdf_bytes("acme"))
    assign_asset(key="acme", slot="marketplace_pdf", lead_ids=[lead.pk], assigned_by="Arian", apply=True)

    source.fail(asset.provider_file_id, "permission", "sharing removed")
    with pytest.raises(AttachmentHold) as inaccessible:
        resolve_attachments(lead_id=lead.pk, requirements=[SLOT], source_for=fetch)
    assert inaccessible.value.problems[0].code == HoldCode.INACCESSIBLE
    assert "sharing removed" in str(inaccessible.value)

    source.files[asset.provider_file_id].error = None
    source.change(asset.provider_file_id, pdf_bytes("edited after approval"))
    with pytest.raises(AttachmentHold) as changed:
        resolve_attachments(lead_id=lead.pk, requirements=[SLOT], source_for=fetch)
    assert changed.value.problems[0].code == HoldCode.CHANGED

    source.change(asset.provider_file_id, pdf_bytes("acme"))
    retire_asset(key="acme", apply=True)
    with pytest.raises(AttachmentHold) as retired:
        resolve_attachments(lead_id=lead.pk, requirements=[SLOT], source_for=fetch)
    assert retired.value.problems[0].code == HoldCode.RETIRED

    with pytest.raises(AttachmentHold) as unknown:
        resolve_attachments(
            lead_id=lead.pk,
            requirements=[AttachmentRequirement(asset="never-registered")],
            source_for=fetch,
        )
    assert unknown.value.problems[0].code == HoldCode.UNKNOWN_ASSET


def test_optional_slot_without_assignment_is_skipped_but_never_downloads(source):
    lead = _lead("Ada")
    optional = AttachmentRequirement(slot="case_study", required=False)

    assert resolve_attachments(lead_id=lead.pk, requirements=[optional], source_for=lambda _p: source) == ()
    [planned] = plan_attachments(lead_id=lead.pk, requirements=[optional])
    assert planned.skipped and planned.asset is None
    assert source.downloads == []
