import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from email_assets.models import EmailAsset
from tests.email_assets.helpers import FakeSource, pdf_bytes, sha


pytestmark = pytest.mark.django_db


@pytest.fixture
def drive(monkeypatch):
    import email_assets.providers as providers

    source = FakeSource()
    monkeypatch.setitem(providers._SOURCE_FACTORIES, "google_drive", lambda: source)
    return source


@pytest.fixture
def admin_client(client):
    user = User.objects.create_superuser("operator", "operator@example.com", "pw")
    client.force_login(user)
    return client


def test_admin_registers_a_pasted_drive_link_then_approves_it(admin_client, drive):
    file_id = drive.add("1AdminPastedFile0001", pdf_bytes("acme"), name="Acme Marketplace.pdf")
    add_url = reverse("admin:email_assets_emailasset_add")
    assert admin_client.get(add_url).status_code == 200

    response = admin_client.post(add_url, {
        "link": f"https://drive.google.com/file/d/{file_id}/view?usp=sharing",
        "key": "acme-marketplace-pdf",
        "name": "Acme Marketplace PDF",
        "filename": "",
        "notes": "",
    })
    assert response.status_code == 302, response.content[:2000]
    asset = EmailAsset.objects.get()
    assert asset.registered_by == "operator"
    assert asset.status == EmailAsset.Status.PENDING
    assert asset.sha256 == sha(pdf_bytes("acme"))
    assert admin_client.get(reverse("admin:email_assets_emailasset_change", args=[asset.pk])).status_code == 200

    admin_client.post(reverse("admin:email_assets_emailasset_changelist"), {
        "action": "approve_current_bytes",
        "_selected_action": [asset.pk],
    })
    asset.refresh_from_db()
    assert asset.is_sendable
    assert asset.approved_by == "operator"


def test_admin_shows_a_registration_error_instead_of_saving(admin_client, drive):
    response = admin_client.post(reverse("admin:email_assets_emailasset_add"), {
        "link": "https://example.com/not-drive.pdf",
        "key": "bad",
        "name": "Bad",
    })
    assert response.status_code == 200
    assert b"only Google Drive" in response.content
    assert not EmailAsset.objects.exists()


def _race_before_save(monkeypatch, admin_class, concurrent_change):
    """Run ``concurrent_change`` after Admin loaded the row, right before it saves."""
    original = admin_class.save_model

    def save_model(self, request, obj, form, change):
        concurrent_change()
        return original(self, request, obj, form, change)

    monkeypatch.setattr(admin_class, "save_model", save_model)


def test_editing_name_or_notes_never_undoes_a_concurrent_retirement(admin_client, drive, monkeypatch):
    from email_assets.admin import EmailAssetAdmin
    from email_assets.services import retire_asset
    from tests.email_assets.helpers import approved_asset

    asset = approved_asset(drive, key="acme-marketplace-pdf", content=pdf_bytes("acme"))
    _race_before_save(monkeypatch, EmailAssetAdmin, lambda: retire_asset(key=asset.key, apply=True))

    response = admin_client.post(
        reverse("admin:email_assets_emailasset_change", args=[asset.pk]),
        {"name": "Acme Marketplace PDF (final)", "notes": "Sent from Email 2"},
    )

    assert response.status_code == 302, response.content[:2000]
    asset.refresh_from_db()
    assert asset.status == EmailAsset.Status.RETIRED
    assert not asset.is_sendable
    assert (asset.name, asset.notes) == ("Acme Marketplace PDF (final)", "Sent from Email 2")


def test_assignment_admin_keeps_a_concurrent_reassignment_it_did_not_edit(admin_client, drive, monkeypatch):
    from crm.models import Lead
    from email_assets.admin import LeadEmailAssetAdmin
    from email_assets.models import LeadEmailAsset
    from email_assets.services import assign_asset
    from tests.email_assets.helpers import approved_asset

    first = approved_asset(drive, key="acme-v1", content=pdf_bytes("one"))
    second = approved_asset(drive, key="acme-v2", content=pdf_bytes("two"))
    lead = Lead.objects.create(
        first_name="Ada", linkedin_url="https://www.linkedin.com/in/ada-admin/", public_identifier="ada-admin",
    )
    assign_asset(key=first.key, slot="marketplace_pdf", lead_ids=[lead.pk], assigned_by="cli", apply=True)
    row = LeadEmailAsset.objects.get()
    _race_before_save(
        monkeypatch,
        LeadEmailAssetAdmin,
        lambda: assign_asset(key=second.key, slot="marketplace_pdf", lead_ids=[lead.pk], assigned_by="cli", apply=True),
    )

    # The operator submits the form without changing the file.
    response = admin_client.post(
        reverse("admin:email_assets_leademailasset_change", args=[row.pk]),
        {"lead": lead.pk, "slot": "marketplace_pdf", "asset": first.pk},
    )

    assert response.status_code == 302, response.content[:2000]
    row.refresh_from_db()
    assert row.asset_id == second.pk
    assert row.assigned_by == "cli"


def test_assignment_admin_writes_the_file_the_operator_chose(admin_client, drive):
    from crm.models import Lead
    from email_assets.models import LeadEmailAsset
    from email_assets.services import assign_asset
    from tests.email_assets.helpers import approved_asset

    first = approved_asset(drive, key="acme-v1", content=pdf_bytes("one"))
    second = approved_asset(drive, key="acme-v2", content=pdf_bytes("two"))
    lead = Lead.objects.create(
        first_name="Ada", linkedin_url="https://www.linkedin.com/in/ada-admin2/", public_identifier="ada-admin2",
    )
    assign_asset(key=first.key, slot="marketplace_pdf", lead_ids=[lead.pk], assigned_by="cli", apply=True)
    row = LeadEmailAsset.objects.get()

    response = admin_client.post(
        reverse("admin:email_assets_leademailasset_change", args=[row.pk]),
        {"lead": lead.pk, "slot": "marketplace_pdf", "asset": second.pk},
    )

    assert response.status_code == 302, response.content[:2000]
    row.refresh_from_db()
    assert (row.asset_id, row.assigned_by) == (second.pk, "operator")


def test_assignment_admin_refuses_a_retired_asset(admin_client, drive):
    from crm.models import Lead
    from email_assets.models import LeadEmailAsset
    from email_assets.services import retire_asset
    from tests.email_assets.helpers import approved_asset

    asset = approved_asset(drive, key="old-pdf", content=pdf_bytes("old"))
    retire_asset(key=asset.key, apply=True)
    lead = Lead.objects.create(
        first_name="Ada", linkedin_url="https://www.linkedin.com/in/ada-admin3/", public_identifier="ada-admin3",
    )

    response = admin_client.post(
        reverse("admin:email_assets_leademailasset_add"),
        {"lead": lead.pk, "slot": "marketplace_pdf", "asset": asset.pk},
    )

    assert response.status_code == 200
    assert b"retired" in response.content
    assert not LeadEmailAsset.objects.exists()
