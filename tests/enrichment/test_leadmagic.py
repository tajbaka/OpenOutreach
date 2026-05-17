"""Tests for the LeadMagic provider."""
from types import SimpleNamespace
from unittest.mock import patch

from linkedin.enrichment.base import EnrichmentStatus
from linkedin.enrichment.http import HttpError
from linkedin.enrichment.providers.leadmagic import LeadMagicProvider


def _lead():
    return SimpleNamespace(
        id=1, first_name="Ada", last_name="Lovelace", company_name="AE",
        linkedin_url="https://www.linkedin.com/in/ada/",
    )


def test_missing_api_key_returns_api_failure(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.leadmagic.LEADMAGIC_API_KEY", "",
    )
    result = LeadMagicProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.API_FAILURE


def test_found(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.leadmagic.LEADMAGIC_API_KEY", "key",
    )
    with patch("linkedin.enrichment.providers.leadmagic.post_json",
               return_value={"mobile_number": "+14155550199"}):
        result = LeadMagicProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.FOUND
    assert result.phone == "+14155550199"


def test_not_found(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.leadmagic.LEADMAGIC_API_KEY", "key",
    )
    with patch("linkedin.enrichment.providers.leadmagic.post_json",
               return_value={"mobile_number": None}):
        result = LeadMagicProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.NOT_FOUND


def test_http_error_returns_api_failure(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.leadmagic.LEADMAGIC_API_KEY", "key",
    )
    with patch("linkedin.enrichment.providers.leadmagic.post_json",
               side_effect=HttpError("500")):
        result = LeadMagicProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.API_FAILURE
