"""Tests for the Prospeo provider."""
from types import SimpleNamespace
from unittest.mock import patch

from linkedin.enrichment.base import EnrichmentStatus
from linkedin.enrichment.http import HttpError
from linkedin.enrichment.providers.prospeo import ProspeoProvider


def _lead():
    return SimpleNamespace(
        id=1, first_name="Ada", last_name="Lovelace", company_name="AE",
        linkedin_url="https://www.linkedin.com/in/ada/",
    )


def test_missing_api_key_returns_api_failure(monkeypatch):
    monkeypatch.setattr("linkedin.enrichment.providers.prospeo.PROSPEO_API_KEY", "")
    result = ProspeoProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.API_FAILURE


def test_found(monkeypatch):
    monkeypatch.setattr("linkedin.enrichment.providers.prospeo.PROSPEO_API_KEY", "key")
    with patch("linkedin.enrichment.providers.prospeo.post_json",
               return_value={"error": False,
                             "response": {"person": {"mobile": {"mobile": "+14155550199"}}}}):
        result = ProspeoProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.FOUND
    assert result.phone == "+14155550199"


def test_not_found_when_no_mobile(monkeypatch):
    monkeypatch.setattr("linkedin.enrichment.providers.prospeo.PROSPEO_API_KEY", "key")
    with patch("linkedin.enrichment.providers.prospeo.post_json",
               return_value={"error": False,
                             "response": {"person": {"mobile": None}}}):
        result = ProspeoProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.NOT_FOUND


def test_error_flag_returns_api_failure(monkeypatch):
    monkeypatch.setattr("linkedin.enrichment.providers.prospeo.PROSPEO_API_KEY", "key")
    with patch("linkedin.enrichment.providers.prospeo.post_json",
               return_value={"error": True, "message": "rate limited"}):
        result = ProspeoProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.API_FAILURE


def test_http_error_returns_api_failure(monkeypatch):
    monkeypatch.setattr("linkedin.enrichment.providers.prospeo.PROSPEO_API_KEY", "key")
    with patch("linkedin.enrichment.providers.prospeo.post_json",
               side_effect=HttpError("503")):
        result = ProspeoProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.API_FAILURE
