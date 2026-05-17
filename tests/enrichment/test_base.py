"""Tests for the enrichment base types."""
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus


def test_enrichment_status_values():
    assert EnrichmentStatus.FOUND == "found"
    assert EnrichmentStatus.NOT_FOUND == "not_found"
    assert EnrichmentStatus.API_FAILURE == "api_failure"


def test_enrichment_result_defaults():
    result = EnrichmentResult(status=EnrichmentStatus.NOT_FOUND, provider="leadmagic")
    assert result.phone is None
    assert result.raw == {}


def test_enrichment_result_found():
    result = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="prospeo",
        phone="+14155550199", raw={"ok": True},
    )
    assert result.phone == "+14155550199"
    assert result.raw == {"ok": True}


def test_enrichment_error_is_exception():
    from linkedin.exceptions import EnrichmentError

    assert issubclass(EnrichmentError, Exception)
