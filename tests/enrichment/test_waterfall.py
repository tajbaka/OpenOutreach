"""Tests for run_waterfall escalation logic."""
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.waterfall import run_waterfall


class _FakeProvider:
    def __init__(self, name, status, phone=None):
        self.name = name
        self._status = status
        self._phone = phone
        self.called = False

    def enrich(self, lead, task):
        self.called = True
        return EnrichmentResult(status=self._status, provider=self.name, phone=self._phone)


def test_found_stops_chain():
    p1 = _FakeProvider("a", EnrichmentStatus.FOUND, phone="+1")
    p2 = _FakeProvider("b", EnrichmentStatus.FOUND, phone="+2")
    result = run_waterfall(None, None, chain=[p1, p2])
    assert result.status == EnrichmentStatus.FOUND
    assert result.provider == "a"
    assert p2.called is False  # short-circuited


def test_not_found_stops_chain_without_escalating():
    p1 = _FakeProvider("a", EnrichmentStatus.NOT_FOUND)
    p2 = _FakeProvider("b", EnrichmentStatus.FOUND, phone="+2")
    result = run_waterfall(None, None, chain=[p1, p2])
    assert result.status == EnrichmentStatus.NOT_FOUND
    assert p2.called is False  # NOT_FOUND is authoritative — no escalation


def test_api_failure_escalates_to_next():
    p1 = _FakeProvider("a", EnrichmentStatus.API_FAILURE)
    p2 = _FakeProvider("b", EnrichmentStatus.FOUND, phone="+2")
    result = run_waterfall(None, None, chain=[p1, p2])
    assert result.status == EnrichmentStatus.FOUND
    assert result.provider == "b"
    assert p1.called is True


def test_all_failed_returns_last_api_failure():
    p1 = _FakeProvider("a", EnrichmentStatus.API_FAILURE)
    p2 = _FakeProvider("b", EnrichmentStatus.API_FAILURE)
    result = run_waterfall(None, None, chain=[p1, p2])
    assert result.status == EnrichmentStatus.API_FAILURE
    assert result.provider == "b"


def test_default_chain_has_three_providers():
    from linkedin.enrichment.waterfall import PROVIDER_CHAIN

    assert [p.name for p in PROVIDER_CHAIN] == ["bettercontact", "leadmagic", "prospeo"]
