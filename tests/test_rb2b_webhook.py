from copy import deepcopy
from io import BytesIO
import json
from unittest.mock import Mock

from django.db import connection
import pytest

from api import rb2b_webhook as api
from crm.models import Lead, Deal, Message
from linkedin.models import Campaign, LinkedInDiscoveryLead, LinkedInProfile, Task
from tests.factories import UserFactory


def visitor(**overrides):
    return {"LinkedIn URL": "https://www.linkedin.com/in/example-person/?trk=rb2b",
            "First Name": "Example", "Last Name": "Person", "Title": "Security lead",
            "Company Name": "Synthetic Example", "Seen At": "2026-10-30T18:00:00Z",
            "Captured URL": "https://example.com/resource", **overrides}


@pytest.fixture
def owner(db):
    if connection.vendor != "postgresql":
        pytest.skip("Raw receiver SQL is exercised by the isolated rb2b-intake PostgreSQL suite")
    user = UserFactory(username="ariantajbakh")
    return LinkedInProfile.objects.create(user=user, linkedin_username="ariantajbakh", active=True)


@pytest.mark.parametrize("url", ["https://linkedin.com.evil.test/in/x", "https://evil.test/in/x",
                                      "https://linkedin.com/company/x", "https://u:p@linkedin.com/in/x",
                                      "https://linkedin.com/in/x%2Fy", "https://linkedin.com/in/x:123"])
def test_invalid_linkedin_urls(url):
    with pytest.raises(api.InvalidVisitor):
        api.normalize_visitor(visitor(**{"LinkedIn URL": url}))


def test_query_token_auth():
    key = "x" * 40
    assert api.authorized("/api/rb2b_webhook?token=" + key, key)
    assert not api.authorized("/api/rb2b_webhook?token=" + key + "&token=" + key, key)
    assert not api.authorized("/api/rb2b_webhook?token=no", key)
    assert not api.authorized("/api/rb2b_webhook?token=short", "short")


def test_normalization_deduplicates_tracking_params():
    a = api.normalize_visitor(visitor())
    b = api.normalize_visitor(visitor(**{"LinkedIn URL": "https://linkedin.com/in/Example-Person"}))
    assert a["fingerprint"] == b["fingerprint"]


def test_receipt_merge_preserves_review_and_original_source():
    v = api.normalize_visitor(visitor())
    saved, _ = api.merge_receipt({"headline": "collector-owned"}, v, received_at="first")
    receipt = saved[api.META_KEY]["events"][v["fingerprint"]]
    receipt.update(review_status="sent", review={"note": "approved", "confirmed": True})
    original = deepcopy(saved)
    merged, duplicate = api.merge_receipt(saved, v, received_at="second")
    assert duplicate and saved == original
    assert merged[api.META_KEY]["events"][v["fingerprint"]]["review"] == receipt["review"]
    assert merged[api.META_KEY]["events"][v["fingerprint"]]["receipt_count"] == 2


def test_real_postgres_intake_does_not_start_outreach(owner):
    before = [m.objects.count() for m in (Lead, Deal, Campaign, Task, Message)]
    v = api.normalize_visitor(visitor())
    result = api.store_visitor(connection.connection, v)
    row = LinkedInDiscoveryLead.objects.get(pk=result["discovery_id"])
    assert row.potential_icp == "RB2B: needs review"
    assert row.profile_data["identity_verified"] is False
    assert row.stored_by_operator == "Arian"
    assert row.linkedin_url == v["url"]
    assert [m.objects.count() for m in (Lead, Deal, Campaign, Task, Message)] == before
    result2 = api.store_visitor(connection.connection, v)
    assert result2["duplicate"] and result2["discovery_id"] == row.pk
    assert LinkedInDiscoveryLead.objects.count() == 1


def test_existing_discovery_fields_and_existing_crm_preserved(owner):
    v = api.normalize_visitor(visitor())
    lead = Lead.objects.create(linkedin_url=v["url"], public_identifier=v["public_id"], icp="Advisors")
    result = api.store_visitor(connection.connection, v)
    row = LinkedInDiscoveryLead.objects.get(pk=result["discovery_id"])
    assert row.profile_data["existing_crm_lead_ids_at_intake"] == [lead.pk]
    row.headline = "verified collector headline"
    row.potential_icp = "reviewed ICP"
    row.save()
    api.store_visitor(connection.connection, api.normalize_visitor(visitor(**{"Seen At": "2026-10-31T18:00:00Z"})))
    row.refresh_from_db()
    lead.refresh_from_db()
    assert row.headline == "verified collector headline" and row.potential_icp == "reviewed ICP"
    assert lead.icp == "Advisors"
    assert len(row.profile_data[api.META_KEY]["events"]) == 2


def test_provider_test_and_company_only_never_create_people(owner):
    sample = visitor(**{"First Name": "RB2B", "Last Name": "Test Payload", "Title": "Webhook Test Payload from RB2B"})
    for raw in (sample, visitor(**{"LinkedIn URL": ""})):
        result = api.store_visitor(connection.connection, api.normalize_visitor(raw))
        assert not result["stored"]
    assert not LinkedInDiscoveryLead.objects.exists()


def fake_handler(raw, token, content_type="application/json"):
    h = object.__new__(api.handler)
    h.path = "/api/rb2b_webhook?token=" + token
    body = json.dumps(raw).encode()
    h.headers = {"Content-Length": str(len(body)), "Content-Type": content_type}
    h.rfile, h.wfile = BytesIO(body), BytesIO()
    h.send_response, h.send_header, h.end_headers = Mock(), Mock(), Mock()
    return h


def test_http_rejects_before_database_access(monkeypatch):
    monkeypatch.setenv("RB2B_WEBHOOK_TOKEN", "s" * 40)
    monkeypatch.setenv("RB2B_DATABASE_URL", "synthetic-never-used")
    connect = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(api.psycopg, "connect", connect)
    for token, payload, content_type, expected in [
        ("wrong", visitor(), "application/json", 401),
        ("s" * 40, [], "application/json", 400),
        ("s" * 40, visitor(), "text/plain", 415),
    ]:
        h = fake_handler(payload, token, content_type)
        h.do_POST()
        h.send_response.assert_called_once_with(expected)
    connect.assert_not_called()


def test_http_storage_failure_never_acknowledged(monkeypatch):
    monkeypatch.setenv("RB2B_WEBHOOK_TOKEN", "s" * 40)
    monkeypatch.setenv("RB2B_DATABASE_URL", "synthetic-never-used")
    monkeypatch.setattr(api.psycopg, "connect", Mock(side_effect=api.psycopg.OperationalError("private diagnostic")))
    h = fake_handler(visitor(), "s" * 40)
    h.do_POST()
    h.send_response.assert_called_once_with(503)
    assert b"private diagnostic" not in h.wfile.getvalue()
