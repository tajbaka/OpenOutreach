from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from linkedin.exceptions import MarketplaceListenerError
from linkedin.marketplace_listener import ingest_marketplace_payloads
from linkedin.models import FedRAMPMarketplaceSignal, FedRAMPMarketplaceSourceState

CHANGELOG_URL = "https://example.test/fedramp-status-changelog.json"
DATA_URL = "https://example.test/data.json"
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _changelog(entries: list[dict]) -> dict:
    return {
        "metadata": {"export_timestamp": "2026-07-22T11:00:00Z"},
        "data": {"certprocessstatuschangelog": entries},
    }


def _data(products: list[dict]) -> dict:
    return {
        "meta": {"last_change": "2026-07-22T10:00:00Z"},
        "data": {"Products": products},
    }


def _entry(
    unique_id: str,
    *,
    product_id: str,
    csp: str,
    cso: str,
    to_status: str,
    cert_path: str,
    transition_date: str = "2026-07-22T09:00:00Z",
    recorded_date: str = "2026-07-22T09:01:00Z",
) -> dict:
    return {
        "unique_id": unique_id,
        "product_id": product_id,
        "csp": csp,
        "cso": cso,
        "cert_path": cert_path,
        "from_status": "",
        "to_status": to_status,
        "transition_date": transition_date,
        "recorded_date": recorded_date,
        "source": "test",
        "comments": "",
    }


def _product(
    product_id: str,
    *,
    csp: str,
    cso: str,
    status: str,
    ready_date: str = "2026-07-22T09:00:00Z",
) -> dict:
    return {
        "id": product_id,
        "name": csp,
        "csp": csp,
        "cso": cso,
        "service_offering": cso,
        "status": status,
        "ready_date": ready_date,
        "fedramp_ready": ready_date,
        "auth_type": "Agency",
        "partnering_agency": "",
        "impact_level": "Moderate",
        "small_business": "Yes",
        "website": f"https://{csp.lower().replace(' ', '')}.example",
        "sales_email": "sales@example.test",
        "security_email": "security@example.test",
    }


@pytest.mark.django_db
def test_first_run_creates_baseline_without_historical_signals():
    summary = ingest_marketplace_payloads(
        changelog_payload=_changelog([
            _entry(
                "old-initial",
                product_id="FR1",
                csp="Acme",
                cso="Acme Cloud",
                to_status="Initial Implementation",
                cert_path="Program",
            )
        ]),
        data_payload=_data([
            _product("FR2", csp="Ready Co", cso="Ready Cloud", status="FedRAMP Ready")
        ]),
        changelog_url=CHANGELOG_URL,
        data_url=DATA_URL,
        now=NOW,
    )

    assert summary["baseline_created"] is True
    assert summary["target_candidates"] == 0
    assert summary["signals_created"] == 0
    assert FedRAMPMarketplaceSignal.objects.count() == 0
    assert FedRAMPMarketplaceSourceState.objects.count() == 2


@pytest.mark.django_db
def test_second_run_detects_20x_initial_and_rev5_ready_once():
    ingest_marketplace_payloads(
        changelog_payload=_changelog([]),
        data_payload=_data([
            _product("FR20X", csp="Acme", cso="Acme Cloud", status="FedRAMP In Process"),
            _product("FRREADY", csp="Ready Co", cso="Ready Cloud", status="Agency In Process"),
        ]),
        changelog_url=CHANGELOG_URL,
        data_url=DATA_URL,
        now=NOW - timedelta(days=1),
    )
    entries = [
        _entry(
            "new-20x",
            product_id="FR20X",
            csp="Acme",
            cso="Acme Cloud",
            to_status="Initial Implementation",
            cert_path="Program",
        ),
        _entry(
            "new-ready",
            product_id="FRREADY",
            csp="Ready Co",
            cso="Ready Cloud",
            to_status="FRR",
            cert_path="Agency",
        ),
        _entry(
            "irrelevant",
            product_id="FR3",
            csp="Review Co",
            cso="Review Cloud",
            to_status="Agency Review",
            cert_path="Agency",
        ),
    ]
    products = [
        _product("FR20X", csp="Acme", cso="Acme Cloud", status="FedRAMP In Process"),
        _product("FRREADY", csp="Ready Co", cso="Ready Cloud", status="FedRAMP Ready"),
    ]

    summary = ingest_marketplace_payloads(
        changelog_payload=_changelog(entries),
        data_payload=_data(products),
        changelog_url=CHANGELOG_URL,
        data_url=DATA_URL,
        now=NOW,
    )

    assert summary["new_changelog_entries"] == 3
    assert summary["snapshot_ready_transitions"] == 1
    assert summary["target_candidates"] == 2
    assert summary["signals_created"] == 2
    assert set(FedRAMPMarketplaceSignal.objects.values_list("signal_type", flat=True)) == {
        FedRAMPMarketplaceSignal.SignalType.TWENTYX_INITIAL,
        FedRAMPMarketplaceSignal.SignalType.REV5_READY,
    }
    ready = FedRAMPMarketplaceSignal.objects.get(
        signal_type=FedRAMPMarketplaceSignal.SignalType.REV5_READY
    )
    assert ready.source_kind == FedRAMPMarketplaceSignal.SourceKind.CHANGELOG
    assert ready.icp_bucket == "Rev5 Ready"
    assert ready.product_context["status"] == "FedRAMP Ready"

    repeated = ingest_marketplace_payloads(
        changelog_payload=_changelog(entries),
        data_payload=_data(products),
        changelog_url=CHANGELOG_URL,
        data_url=DATA_URL,
        now=NOW + timedelta(hours=1),
    )
    assert repeated["new_changelog_entries"] == 0
    assert repeated["target_candidates"] == 0
    assert FedRAMPMarketplaceSignal.objects.count() == 2


@pytest.mark.django_db
def test_snapshot_diff_catches_ready_when_changelog_has_no_new_row():
    ingest_marketplace_payloads(
        changelog_payload=_changelog([]),
        data_payload=_data([
            _product("FRREADY", csp="Ready Co", cso="Ready Cloud", status="Agency In Process")
        ]),
        changelog_url=CHANGELOG_URL,
        data_url=DATA_URL,
        now=NOW - timedelta(days=1),
    )

    summary = ingest_marketplace_payloads(
        changelog_payload=_changelog([]),
        data_payload=_data([
            _product("FRREADY", csp="Ready Co", cso="Ready Cloud", status="FedRAMP Ready")
        ]),
        changelog_url=CHANGELOG_URL,
        data_url=DATA_URL,
        now=NOW,
    )

    assert summary["snapshot_ready_transitions"] == 1
    signal = FedRAMPMarketplaceSignal.objects.get()
    assert signal.signal_type == FedRAMPMarketplaceSignal.SignalType.REV5_READY
    assert signal.source_kind == FedRAMPMarketplaceSignal.SourceKind.SNAPSHOT


@pytest.mark.django_db
def test_ready_changelog_and_snapshot_dates_still_create_one_signal():
    ingest_marketplace_payloads(
        changelog_payload=_changelog([]),
        data_payload=_data([
            _product("FRREADY", csp="Ready Co", cso="Ready Cloud", status="Agency In Process")
        ]),
        changelog_url=CHANGELOG_URL,
        data_url=DATA_URL,
        now=NOW - timedelta(days=1),
    )

    summary = ingest_marketplace_payloads(
        changelog_payload=_changelog([
            _entry(
                "new-ready",
                product_id="FRREADY",
                csp="Ready Co",
                cso="Ready Cloud",
                to_status="FRR",
                cert_path="Agency",
                transition_date="2026-07-22T09:00:00Z",
            )
        ]),
        data_payload=_data([
            _product(
                "FRREADY",
                csp="Ready Co",
                cso="Ready Cloud",
                status="FedRAMP Ready",
                ready_date="2026-07-20T20:00:00Z",
            )
        ]),
        changelog_url=CHANGELOG_URL,
        data_url=DATA_URL,
        now=NOW,
    )

    assert summary["snapshot_ready_transitions"] == 1
    assert summary["target_candidates"] == 1
    assert FedRAMPMarketplaceSignal.objects.count() == 1
    assert FedRAMPMarketplaceSignal.objects.get().source_kind == "changelog"


@pytest.mark.django_db
def test_first_run_lookback_seeds_only_recent_target_rows():
    recent = _entry(
        "recent",
        product_id="FRRECENT",
        csp="Recent Co",
        cso="Recent Cloud",
        to_status="Initial Implementation",
        cert_path="Program",
        recorded_date="2026-07-21T12:00:00Z",
    )
    old = _entry(
        "old",
        product_id="FROLD",
        csp="Old Co",
        cso="Old Cloud",
        to_status="Initial Implementation",
        cert_path="Program",
        recorded_date="2026-06-01T12:00:00Z",
    )

    summary = ingest_marketplace_payloads(
        changelog_payload=_changelog([old, recent]),
        data_payload=_data([]),
        changelog_url=CHANGELOG_URL,
        data_url=DATA_URL,
        lookback_days=7,
        now=NOW,
    )

    assert summary["new_changelog_entries"] == 1
    assert summary["signals_created"] == 1
    assert FedRAMPMarketplaceSignal.objects.get().product_id == "FRRECENT"


@pytest.mark.django_db
def test_dry_run_does_not_create_baselines_or_signals():
    summary = ingest_marketplace_payloads(
        changelog_payload=_changelog([]),
        data_payload=_data([]),
        changelog_url=CHANGELOG_URL,
        data_url=DATA_URL,
        dry_run=True,
        now=NOW,
    )

    assert summary["dry_run"] is True
    assert FedRAMPMarketplaceSourceState.objects.count() == 0
    assert FedRAMPMarketplaceSignal.objects.count() == 0


def test_malformed_changelog_fails_without_silent_baseline_replacement():
    with pytest.raises(MarketplaceListenerError, match="certprocessstatuschangelog"):
        ingest_marketplace_payloads(
            changelog_payload={"data": {}},
            data_payload=_data([]),
            changelog_url=CHANGELOG_URL,
            data_url=DATA_URL,
            now=NOW,
        )
