"""Scrape a Sales Navigator People search and dump a CSV to `leads/`.

Usage:
    python manage.py export_sales_search --url '<search_url>'

Where <search_url> is the Sales Nav search URL with filters applied:
    https://www.linkedin.com/sales/search/people?query=(...)

Output: leads/sales_nav_search_<sha8>.csv  (sha8 = first 8 chars of SHA1(url))

The CSV format matches what `manage.py add_seeds --csv` expects:
    Profile URL, First Name, Last Name, Company

Auth: same separate LinkedIn account as `export_sales_list`, via env vars:
    SALES_NAV_LINKEDIN_USERNAME
    SALES_NAV_LINKEDIN_PASSWORD
Cookies cached at `data/sales_nav_cookies.json`.
"""
from __future__ import annotations

import csv
import hashlib
import time
from pathlib import Path

from django.core.management.base import BaseCommand

CSV_HEADER = ["Profile URL", "First Name", "Last Name", "Company"]


class Command(BaseCommand):
    help = "Scrape a Sales Navigator People search to a CSV in leads/."

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            required=True,
            help="Full Sales Nav search URL with filters applied.",
        )
        parser.add_argument(
            "--output",
            default=None,
            help="Output path (default: leads/sales_nav_search_<sha8>.csv)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Stop after this many leads (useful for testing).",
        )
        parser.add_argument(
            "--delay-seconds",
            type=float,
            default=3.0,
            help="Sleep between profile resolution calls (default 3.0s).",
        )

    def handle(self, *args, **options):
        from linkedin.actions.sales_nav_list import (
            discover_search_url_template,
            iter_sales_nav_list,
        )
        from linkedin.actions.standalone_session import StandaloneLinkedInSession
        from linkedin.api.client import PlaywrightLinkedinAPI
        from linkedin.conf import ROOT_DIR

        search_url = options["url"]
        limit = options["limit"]
        delay = options["delay_seconds"]

        sha8 = hashlib.sha1(search_url.encode("utf-8")).hexdigest()[:8]
        default_path = ROOT_DIR / "leads" / f"sales_nav_search_{sha8}.csv"
        output_path = Path(options["output"]) if options["output"] else default_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with StandaloneLinkedInSession(label="Sales Nav") as session:
            api = PlaywrightLinkedinAPI(session)

            self.stdout.write("Auto-discovering Sales Nav search XHR...")
            url_template = discover_search_url_template(session, search_url)

            self.stdout.write(
                f"Fetching Sales Nav search as {session.username} -> {output_path}"
            )
            written, stats = self._dump_csv(api, url_template, limit, delay, output_path)

        self.stdout.write(self.style.SUCCESS(
            f"Wrote {written} rows to {output_path} "
            f"({stats['inaccessible']} inaccessible, "
            f"{stats['unresolvable']} unresolvable)"
        ))
        self.stdout.write(
            f"\nNext: import into a campaign with\n"
            f"  .venv/bin/python manage.py add_seeds <campaign_pk> --csv "
            f"< {output_path}"
        )

    def _dump_csv(self, api, url_template, limit, delay, output_path):
        from linkedin.actions.sales_nav_list import iter_sales_nav_list

        stats = {"inaccessible": 0, "unresolvable": 0}
        written = 0

        with output_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)

            # list_id is unused: search-derived templates have no {list_id} placeholder
            for row in iter_sales_nav_list(
                api, list_id="", max_results=limit, url_template=url_template,
            ):
                member_urn = row["member_urn"]
                try:
                    profile, _ = api.get_profile(public_identifier=member_urn)
                except Exception as e:
                    self.stderr.write(f"  ! {member_urn}: {e}")
                    stats["inaccessible"] += 1
                    time.sleep(delay)
                    continue

                if profile is None:
                    self.stdout.write(
                        f"  - {row['full_name']}: inaccessible (private/restricted)"
                    )
                    stats["inaccessible"] += 1
                    time.sleep(delay)
                    continue

                public_id = profile.get("public_identifier")
                if not public_id:
                    self.stderr.write(f"  ! {member_urn}: no publicIdentifier in response")
                    stats["unresolvable"] += 1
                    time.sleep(delay)
                    continue

                writer.writerow([
                    profile.get("url", f"https://www.linkedin.com/in/{public_id}/"),
                    row["first_name"] or profile.get("first_name", ""),
                    row["last_name"] or profile.get("last_name", ""),
                    row["company_name"],
                ])
                f.flush()  # crash-safe — partial CSV is still usable
                written += 1
                self.stdout.write(
                    f"  + {row['full_name']} ({row['company_name']}) -> {public_id}"
                )
                time.sleep(delay)

        return written, stats
