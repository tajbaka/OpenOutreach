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
                             "person": {"mobile": {"mobile": "+14155550199"}}}):
        result = ProspeoProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.FOUND
    assert result.phone == "+14155550199"


def test_not_found_when_no_mobile(monkeypatch):
    monkeypatch.setattr("linkedin.enrichment.providers.prospeo.PROSPEO_API_KEY", "key")
    with patch("linkedin.enrichment.providers.prospeo.post_json",
               return_value={"error": False,
                             "person": {"mobile": None}}):
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
               side_effect=HttpError("503", status=503)):
        result = ProspeoProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.API_FAILURE


def test_no_match_http_400_is_not_found(monkeypatch):
    """Prospeo reports 'no verified mobile' as HTTP 400 / error_code NO_MATCH.
    That is a clean terminal NOT_FOUND, not an API_FAILURE — otherwise the
    enrich_phone task would be marked failed and re-attempted needlessly."""
    monkeypatch.setattr("linkedin.enrichment.providers.prospeo.PROSPEO_API_KEY", "key")
    with patch("linkedin.enrichment.providers.prospeo.post_json",
               side_effect=HttpError("HTTP 400", status=400,
                                     body={"error": True, "error_code": "NO_MATCH"})):
        result = ProspeoProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.NOT_FOUND


def test_rate_limit_http_400_is_api_failure(monkeypatch):
    """A 400 that is NOT a NO_MATCH (e.g. rate limit) stays an API_FAILURE."""
    monkeypatch.setattr("linkedin.enrichment.providers.prospeo.PROSPEO_API_KEY", "key")
    with patch("linkedin.enrichment.providers.prospeo.post_json",
               side_effect=HttpError("HTTP 400", status=400,
                                     body={"error": True,
                                           "error_code": "Rate limit exceeded"})):
        result = ProspeoProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.API_FAILURE
