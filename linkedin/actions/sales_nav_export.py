"""Shared CSV export service for Sales Navigator searches and lead lists."""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from linkedin.actions.sales_nav_list import iter_sales_nav_list


CSV_HEADER = [
    "Profile URL",
    "First Name",
    "Last Name",
    "Company",
    "Title",
    "Geo Region",
    "Degree",
]


@dataclass(frozen=True)
class SalesNavExportStats:
    seen: int
    written: int
    inaccessible: int
    unresolvable: int


def _profile_cache_value(profile: dict) -> dict:
    """Keep only the profile fields the CSV writer needs across searches."""
    return {
        "url": profile.get("url"),
        "public_identifier": profile.get("public_identifier"),
        "first_name": profile.get("first_name", ""),
        "last_name": profile.get("last_name", ""),
    }


def export_sales_nav_csv(
    api,
    *,
    url_template: str,
    output_path: Path,
    list_id: str = "",
    max_results: int | None = None,
    delay_seconds: float = 3.0,
    profile_cache: dict[str, dict | None] | None = None,
    report: Callable[[str], None] | None = None,
) -> SalesNavExportStats:
    """Export one Sales Navigator result set to an atomically completed CSV.

    The final path appears only after the complete search succeeds. A failure
    leaves ``<name>.partial.csv`` for diagnosis and re-raises the exception.
    Profile resolution failures that LinkedIn explicitly represents as
    inaccessible return ``None`` and are counted; unexpected errors propagate.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = output_path.with_name(
        f"{output_path.stem}.partial{output_path.suffix}"
    )
    cache = profile_cache if profile_cache is not None else {}
    emit = report or (lambda _message: None)

    seen = 0
    written = 0
    inaccessible = 0
    unresolvable = 0

    with partial_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)

        for row in iter_sales_nav_list(
            api,
            list_id,
            max_results=max_results,
            url_template=url_template,
        ):
            seen += 1
            member_urn = row["member_urn"]
            fetched = member_urn not in cache
            if fetched:
                profile, _raw = api.get_profile(public_identifier=member_urn)
                cache[member_urn] = (
                    _profile_cache_value(profile) if profile is not None else None
                )
            profile = cache[member_urn]

            if profile is None:
                emit(f"  - {row['full_name']}: inaccessible (private/restricted)")
                inaccessible += 1
            else:
                public_id = profile.get("public_identifier")
                if not public_id:
                    emit(f"  ! {member_urn}: no publicIdentifier in response")
                    unresolvable += 1
                else:
                    writer.writerow([
                        profile.get("url") or f"https://www.linkedin.com/in/{public_id}/",
                        row["first_name"] or profile.get("first_name", ""),
                        row["last_name"] or profile.get("last_name", ""),
                        row["company_name"],
                        row["title"],
                        row["geo_region"],
                        row["degree"] or "",
                    ])
                    handle.flush()
                    written += 1
                    emit(
                        f"  + {row['full_name']} ({row['company_name']}) -> {public_id}"
                    )

            if fetched and delay_seconds:
                time.sleep(delay_seconds)

    partial_path.replace(output_path)
    return SalesNavExportStats(
        seen=seen,
        written=written,
        inaccessible=inaccessible,
        unresolvable=unresolvable,
    )
