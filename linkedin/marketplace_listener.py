"""Diff official FedRAMP marketplace JSON feeds into durable review signals."""
from __future__ import annotations

import hashlib
import json
import ssl
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Callable
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import quote

import certifi
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from linkedin.exceptions import MarketplaceListenerError
from linkedin.models import FedRAMPMarketplaceSignal, FedRAMPMarketplaceSourceState

CHANGELOG_SOURCE = "status_changelog"
SNAPSHOT_SOURCE = "marketplace_snapshot"
MARKETPLACE_PRODUCT_URL = "https://marketplace.fedramp.gov/products/{product_id}/"
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
_READY_STATUSES = {"frr", "fedramp ready"}
_INITIAL_IMPLEMENTATION = "initial implementation"


@dataclass(frozen=True)
class MarketplaceSignalCandidate:
    event_key: str
    source_kind: str
    source_event_id: str
    signal_type: str
    icp_bucket: str
    product_id: str
    provider_name: str
    offering_name: str
    certification_path: str
    from_status: str
    to_status: str
    transition_at: datetime | None
    recorded_at: datetime | None
    source_url: str
    marketplace_url: str
    product_context: dict
    raw_payload: dict


def fetch_json(url: str, *, timeout: int) -> dict:
    """Fetch and decode one official marketplace JSON document."""
    req = request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "OpenOutreach-FedRAMP-Marketplace-Listener/1.0",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as response:
            if response.status != 200:
                raise MarketplaceListenerError(
                    f"FedRAMP marketplace source returned HTTP {response.status}: {url}"
                )
            raw = response.read()
    except (HTTPError, URLError, TimeoutError) as exc:
        raise MarketplaceListenerError(
            f"Failed fetching FedRAMP marketplace source {url}: {exc}"
        ) from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketplaceListenerError(
            f"FedRAMP marketplace source was not valid UTF-8 JSON: {url}"
        ) from exc
    if not isinstance(payload, dict):
        raise MarketplaceListenerError(f"FedRAMP marketplace source root must be an object: {url}")
    return payload


def collect_fedramp_marketplace(
    *,
    changelog_url: str,
    data_url: str,
    timeout: int,
    lookback_days: int | None = None,
    dry_run: bool = False,
    fetcher: Callable[..., dict] = fetch_json,
) -> dict:
    """Fetch both official sources and persist newly detected target signals."""
    changelog_payload = fetcher(changelog_url, timeout=timeout)
    data_payload = fetcher(data_url, timeout=timeout)
    return ingest_marketplace_payloads(
        changelog_payload=changelog_payload,
        data_payload=data_payload,
        changelog_url=changelog_url,
        data_url=data_url,
        lookback_days=lookback_days,
        dry_run=dry_run,
    )


def ingest_marketplace_payloads(
    *,
    changelog_payload: dict,
    data_payload: dict,
    changelog_url: str,
    data_url: str,
    lookback_days: int | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict:
    """Diff validated payloads against DB baselines and persist target signals.

    The first run is baseline-only unless ``lookback_days`` is supplied. Every
    later run compares changelog IDs and current product status against the
    previous durable source snapshots.
    """
    if lookback_days is not None and lookback_days <= 0:
        raise ValueError("lookback_days must be positive when provided")
    now = now or timezone.now()
    entries = _changelog_entries(changelog_payload)
    products = _snapshot_products(data_payload)

    states = {
        state.source_name: state
        for state in FedRAMPMarketplaceSourceState.objects.filter(
            source_name__in=[CHANGELOG_SOURCE, SNAPSHOT_SOURCE]
        )
    }
    changelog_state = states.get(CHANGELOG_SOURCE)
    snapshot_state = states.get(SNAPSHOT_SOURCE)
    changelog_initialized = bool(
        changelog_state and changelog_state.snapshot.get("initialized")
    )
    snapshot_initialized = bool(snapshot_state and snapshot_state.snapshot.get("initialized"))

    previous_seen_ids = set(
        (changelog_state.snapshot.get("seen_ids") or []) if changelog_state else []
    )
    current_seen_ids = {
        str(entry.get("unique_id") or "").strip()
        for entry in entries
        if str(entry.get("unique_id") or "").strip()
    }
    if changelog_initialized:
        new_entries = [
            entry
            for entry in entries
            if str(entry.get("unique_id") or "").strip() not in previous_seen_ids
        ]
    elif lookback_days is not None:
        cutoff = now - timedelta(days=lookback_days)
        new_entries = [entry for entry in entries if _entry_timestamp(entry) >= cutoff]
    else:
        new_entries = []

    current_products = {
        str(product["id"]): _compact_product(product)
        for product in products
        if str(product.get("id") or "").strip()
    }
    previous_products = (
        snapshot_state.snapshot.get("products") or {} if snapshot_state else {}
    )

    candidates: list[MarketplaceSignalCandidate] = []
    for entry in new_entries:
        candidate = _candidate_from_changelog(
            entry,
            source_url=changelog_url,
            product_context=current_products.get(str(entry.get("product_id") or ""), {}),
        )
        if candidate is not None:
            candidates.append(candidate)

    snapshot_ready_transitions = 0
    if snapshot_initialized:
        for product_id, product in current_products.items():
            previous = previous_products.get(product_id) or {}
            if (
                _normalized_status(product.get("status")) == "fedramp ready"
                and _normalized_status(previous.get("status")) != "fedramp ready"
            ):
                snapshot_ready_transitions += 1
                candidates.append(
                    _candidate_from_snapshot(product, source_url=data_url)
                )

    deduped_candidates = _dedupe_candidates(candidates)
    created_count = 0
    existing_count = 0
    if not dry_run:
        with transaction.atomic():
            for candidate in deduped_candidates:
                _signal, created = _upsert_signal(candidate, now=now)
                created_count += int(created)
                existing_count += int(not created)

            _save_source_state(
                source_name=CHANGELOG_SOURCE,
                source_url=changelog_url,
                payload=changelog_payload,
                source_exported_at=_parse_datetime(
                    (changelog_payload.get("metadata") or {}).get("export_timestamp")
                ),
                snapshot={"initialized": True, "seen_ids": sorted(current_seen_ids)},
                polled_at=now,
            )
            _save_source_state(
                source_name=SNAPSHOT_SOURCE,
                source_url=data_url,
                payload=data_payload,
                source_exported_at=_parse_datetime(
                    (data_payload.get("meta") or {}).get("last_change")
                ),
                snapshot={"initialized": True, "products": current_products},
                polled_at=now,
            )

    by_type: dict[str, int] = {}
    for candidate in deduped_candidates:
        by_type[candidate.signal_type] = by_type.get(candidate.signal_type, 0) + 1
    return {
        "dry_run": dry_run,
        "baseline_created": not (changelog_initialized and snapshot_initialized),
        "lookback_days": lookback_days,
        "changelog_entries_seen": len(entries),
        "new_changelog_entries": len(new_entries),
        "snapshot_products_seen": len(current_products),
        "snapshot_ready_transitions": snapshot_ready_transitions,
        "target_candidates": len(deduped_candidates),
        "target_candidates_by_type": by_type,
        "signals_created": created_count,
        "signals_already_present": existing_count,
    }


def _changelog_entries(payload: dict) -> list[dict]:
    data = payload.get("data")
    entries = data.get("certprocessstatuschangelog") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise MarketplaceListenerError(
            "fedramp-status-changelog.json is missing data.certprocessstatuschangelog[]"
        )
    return entries


def _snapshot_products(payload: dict) -> list[dict]:
    data = payload.get("data")
    products = data.get("Products") if isinstance(data, dict) else None
    if not isinstance(products, list) or not all(isinstance(product, dict) for product in products):
        raise MarketplaceListenerError("data.json is missing data.Products[]")
    return products


def _candidate_from_changelog(
    entry: dict,
    *,
    source_url: str,
    product_context: dict,
) -> MarketplaceSignalCandidate | None:
    to_status = str(entry.get("to_status") or "").strip()
    certification_path = str(entry.get("cert_path") or "").strip()
    normalized_status = _normalized_status(to_status)
    normalized_path = certification_path.casefold()
    if normalized_status == _INITIAL_IMPLEMENTATION and normalized_path == "program":
        signal_type = FedRAMPMarketplaceSignal.SignalType.TWENTYX_INITIAL
        icp_bucket = "20x Pipeline"
    elif normalized_status in _READY_STATUSES and normalized_path != "program":
        signal_type = FedRAMPMarketplaceSignal.SignalType.REV5_READY
        icp_bucket = "Rev5 Ready"
    else:
        return None

    product_id = str(entry.get("product_id") or "").strip()
    source_event_id = str(entry.get("unique_id") or "").strip()
    if not product_id or not source_event_id:
        raise MarketplaceListenerError(
            "Target changelog entry is missing product_id or unique_id"
        )
    transition_at = _parse_datetime(entry.get("transition_date"))
    recorded_at = _parse_datetime(entry.get("recorded_date"))
    event_key = _event_key(signal_type, product_id)
    offering = str(entry.get("cso") or product_context.get("cso") or "").strip()
    provider = str(entry.get("csp") or product_context.get("csp") or "").strip()
    return MarketplaceSignalCandidate(
        event_key=event_key,
        source_kind=FedRAMPMarketplaceSignal.SourceKind.CHANGELOG,
        source_event_id=source_event_id,
        signal_type=signal_type,
        icp_bucket=icp_bucket,
        product_id=product_id,
        provider_name=provider[:300] or "Unknown provider",
        offering_name=offering[:300],
        certification_path=certification_path[:80],
        from_status=str(entry.get("from_status") or "").strip()[:100],
        to_status=to_status[:100],
        transition_at=transition_at,
        recorded_at=recorded_at,
        source_url=source_url,
        marketplace_url=MARKETPLACE_PRODUCT_URL.format(product_id=quote(product_id)),
        product_context=product_context,
        raw_payload={"change": entry},
    )


def _candidate_from_snapshot(product: dict, *, source_url: str) -> MarketplaceSignalCandidate:
    product_id = str(product.get("id") or "").strip()
    transition_at = _parse_datetime(product.get("ready_date") or product.get("fedramp_ready"))
    event_key = _event_key(
        FedRAMPMarketplaceSignal.SignalType.REV5_READY,
        product_id,
    )
    return MarketplaceSignalCandidate(
        event_key=event_key,
        source_kind=FedRAMPMarketplaceSignal.SourceKind.SNAPSHOT,
        source_event_id="",
        signal_type=FedRAMPMarketplaceSignal.SignalType.REV5_READY,
        icp_bucket="Rev5 Ready",
        product_id=product_id,
        provider_name=str(product.get("csp") or product.get("name") or "Unknown provider")[:300],
        offering_name=str(product.get("cso") or product.get("service_offering") or "")[:300],
        certification_path=str(product.get("auth_type") or "")[:80],
        from_status="",
        to_status="FedRAMP Ready",
        transition_at=transition_at,
        recorded_at=transition_at,
        source_url=source_url,
        marketplace_url=MARKETPLACE_PRODUCT_URL.format(product_id=quote(product_id)),
        product_context=product,
        raw_payload={"product": product},
    )


def _dedupe_candidates(
    candidates: list[MarketplaceSignalCandidate],
) -> list[MarketplaceSignalCandidate]:
    by_key: dict[str, MarketplaceSignalCandidate] = {}
    for candidate in candidates:
        existing = by_key.get(candidate.event_key)
        if existing is None:
            by_key[candidate.event_key] = candidate
            continue
        if (
            candidate.source_kind == FedRAMPMarketplaceSignal.SourceKind.CHANGELOG
            and existing.source_kind != FedRAMPMarketplaceSignal.SourceKind.CHANGELOG
        ):
            candidate = replace(
                candidate,
                product_context=candidate.product_context or existing.product_context,
                raw_payload={**existing.raw_payload, **candidate.raw_payload},
            )
            by_key[candidate.event_key] = candidate
        else:
            by_key[candidate.event_key] = replace(
                existing,
                product_context=existing.product_context or candidate.product_context,
                raw_payload={**candidate.raw_payload, **existing.raw_payload},
            )
    return sorted(
        by_key.values(),
        key=lambda item: (
            item.recorded_at
            or item.transition_at
            or datetime.min.replace(tzinfo=UTC),
            item.event_key,
        ),
    )


def _upsert_signal(
    candidate: MarketplaceSignalCandidate,
    *,
    now: datetime,
) -> tuple[FedRAMPMarketplaceSignal, bool]:
    defaults = asdict(candidate)
    defaults.update({"first_seen_at": now, "last_seen_at": now})
    signal, created = FedRAMPMarketplaceSignal.objects.get_or_create(
        event_key=candidate.event_key,
        defaults=defaults,
    )
    if created:
        return signal, True

    update_fields = ["last_seen_at", "updated_at"]
    signal.last_seen_at = now
    if (
        candidate.source_kind == FedRAMPMarketplaceSignal.SourceKind.CHANGELOG
        and signal.source_kind != FedRAMPMarketplaceSignal.SourceKind.CHANGELOG
    ):
        for field in (
            "source_kind", "source_event_id", "certification_path", "from_status",
            "to_status", "transition_at", "recorded_at", "source_url",
        ):
            setattr(signal, field, getattr(candidate, field))
            update_fields.append(field)
    if candidate.product_context and candidate.product_context != signal.product_context:
        signal.product_context = candidate.product_context
        update_fields.append("product_context")
    merged_raw = {**(signal.raw_payload or {}), **candidate.raw_payload}
    if merged_raw != signal.raw_payload:
        signal.raw_payload = merged_raw
        update_fields.append("raw_payload")
    signal.save(update_fields=list(dict.fromkeys(update_fields)))
    return signal, False


def _save_source_state(
    *,
    source_name: str,
    source_url: str,
    payload: dict,
    source_exported_at: datetime | None,
    snapshot: dict,
    polled_at: datetime,
) -> None:
    FedRAMPMarketplaceSourceState.objects.update_or_create(
        source_name=source_name,
        defaults={
            "source_url": source_url,
            "content_sha256": _payload_sha256(payload),
            "source_exported_at": source_exported_at,
            "snapshot": snapshot,
            "last_polled_at": polled_at,
        },
    )


def _compact_product(product: dict) -> dict:
    fields = (
        "id", "name", "csp", "cso", "service_offering", "status",
        "ready_date", "fedramp_ready", "auth_type", "partnering_agency",
        "impact_level", "small_business", "website", "sales_email",
        "security_email",
    )
    return {field: product.get(field) for field in fields}


def _event_key(
    signal_type: str,
    product_id: str,
) -> str:
    # The full snapshot's ready_date frequently differs from the changelog's
    # transition_date by one or more days. A type + product key makes those two
    # official representations one durable account signal instead of two Slack
    # alerts. The listener is intentionally about newly observed products, not
    # repeated status cycling for an already-known product.
    return f"{signal_type}:{product_id}"


def _entry_timestamp(entry: dict) -> datetime:
    return (
        _parse_datetime(entry.get("recorded_date"))
        or _parse_datetime(entry.get("transition_date"))
        or datetime.min.replace(tzinfo=UTC)
    )


def _parse_datetime(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    parsed = parse_datetime(value.strip())
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _normalized_status(value) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _payload_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
