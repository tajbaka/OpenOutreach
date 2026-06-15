"""Tests for the BetterContact provider."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from linkedin.enrichment.base import EnrichmentStatus
from linkedin.enrichment.http import HttpError
from linkedin.enrichment.providers.bettercontact import (
    BetterContactEmailProvider,
    BetterContactProvider,
)


def _lead(**over):
    base = dict(
        id=1, first_name="Ada", last_name="Lovelace",
        company_name="Analytical Engines",
        linkedin_url="https://www.linkedin.com/in/ada/",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _task(request_id=""):
    saved = {}
    task = SimpleNamespace(payload={"bettercontact_request_id": request_id})
    task.save = lambda **kw: saved.update(kw)
    return task


def test_missing_api_key_returns_api_failure(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "",
    )
    result = BetterContactProvider().enrich(_lead(), _task())
    assert result.status == EnrichmentStatus.API_FAILURE


def test_missing_last_name_short_circuits_to_api_failure(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "key",
    )
    with patch("linkedin.enrichment.providers.bettercontact.post_json") as mock_post:
        result = BetterContactProvider().enrich(_lead(last_name=""), _task())
    assert result.status == EnrichmentStatus.API_FAILURE
    mock_post.assert_not_called()  # no API call when required fields missing


def test_submit_then_poll_terminated_found(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "key",
    )
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_POLL_INTERVAL_SECONDS", 0,
    )
    task = _task()
    with patch("linkedin.enrichment.providers.bettercontact.post_json",
               return_value={"id": "req-123"}), \
         patch("linkedin.enrichment.providers.bettercontact.get_json",
               return_value={"status": "terminated",
                             "data": [{"contact_phone_number": "+14155550199"}]}):
        result = BetterContactProvider().enrich(_lead(), task)
    assert result.status == EnrichmentStatus.FOUND
    assert result.phone == "+14155550199"
    assert task.payload["bettercontact_request_id"] == "req-123"


def test_poll_terminated_no_phone_is_not_found(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "key",
    )
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_POLL_INTERVAL_SECONDS", 0,
    )
    with patch("linkedin.enrichment.providers.bettercontact.post_json",
               return_value={"id": "req-1"}), \
         patch("linkedin.enrichment.providers.bettercontact.get_json",
               return_value={"status": "terminated",
                             "data": [{"contact_phone_number": None}]}):
        result = BetterContactProvider().enrich(_lead(), _task())
    assert result.status == EnrichmentStatus.NOT_FOUND


def test_resume_skips_submit_when_request_id_present(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "key",
    )
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_POLL_INTERVAL_SECONDS", 0,
    )
    with patch("linkedin.enrichment.providers.bettercontact.post_json") as mock_post, \
         patch("linkedin.enrichment.providers.bettercontact.get_json",
               return_value={"status": "terminated",
                             "data": [{"contact_phone_number": "+1999"}]}):
        result = BetterContactProvider().enrich(_lead(), _task(request_id="resumed-id"))
    mock_post.assert_not_called()  # resumed — no re-submit, no re-billing
    assert result.status == EnrichmentStatus.FOUND


def test_poll_timeout_returns_api_failure(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "key",
    )
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_POLL_INTERVAL_SECONDS", 0,
    )
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.ENRICHMENT_MAX_DURATION_SECONDS", 0,
    )
    with patch("linkedin.enrichment.providers.bettercontact.post_json",
               return_value={"id": "req-1"}), \
         patch("linkedin.enrichment.providers.bettercontact.get_json",
               return_value={"status": "in_progress"}):
        result = BetterContactProvider().enrich(_lead(), _task())
    assert result.status == EnrichmentStatus.API_FAILURE


def test_http_error_returns_api_failure(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "key",
    )
    with patch("linkedin.enrichment.providers.bettercontact.post_json",
               side_effect=HttpError("502")):
        result = BetterContactProvider().enrich(_lead(), _task())
    assert result.status == EnrichmentStatus.API_FAILURE


def test_email_provider_submits_email_only_and_parses_email(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "key",
    )
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_POLL_INTERVAL_SECONDS", 0,
    )
    task = _task()
    with patch(
        "linkedin.enrichment.providers.bettercontact.post_json",
        return_value={"id": "email-req"},
    ) as mock_post, patch(
        "linkedin.enrichment.providers.bettercontact.get_json",
        return_value={
            "status": "terminated",
            "data": [{"contact_email_address": "Ada@Example.com"}],
        },
    ):
        result = BetterContactEmailProvider().enrich(_lead(), task)

    payload = mock_post.call_args.kwargs["payload"]
    assert payload["enrich_email_address"] is True
    assert payload["enrich_phone_number"] is False
    assert result.status == EnrichmentStatus.FOUND
    assert result.email == "Ada@Example.com"
    assert task.payload["bettercontact_email_request_id"] == "email-req"


def test_email_provider_terminated_no_email_is_not_found(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "key",
    )
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_POLL_INTERVAL_SECONDS", 0,
    )
    with patch(
        "linkedin.enrichment.providers.bettercontact.post_json",
        return_value={"id": "email-req"},
    ), patch(
        "linkedin.enrichment.providers.bettercontact.get_json",
        return_value={
            "status": "terminated",
            "data": [{"contact_email_address": ""}],
        },
    ):
        result = BetterContactEmailProvider().enrich(_lead(), _task())

    assert result.status == EnrichmentStatus.NOT_FOUND
