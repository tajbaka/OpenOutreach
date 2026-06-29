"""Scrape a Sales Navigator saved-leads list and dump a CSV to `leads/`.

Usage:
    python manage.py export_sales_list <list_id>

The list_id is the numeric ID from the URL:
    linkedin.com/sales/lists/people/<list_id>

Output: leads/sales_nav_<list_id>.csv
The CSV format remains compatible with `manage.py add_seeds --csv`:
    Profile URL, First Name, Last Name, Company, Title, Geo Region, Degree

So the typical workflow is:
    1. Export:  python manage.py export_sales_list 7453236290411655169
    2. Import:  python manage.py add_seeds <campaign_pk> --csv \\
                  < leads/sales_nav_7453236290411655169.csv

Auth: a separate LinkedIn account from the daemon's outreach account, via
env vars only:
    SALES_NAV_LINKEDIN_USERNAME
    SALES_NAV_LINKEDIN_PASSWORD
Cookies cached at `data/sales_nav_cookies.json` so reruns don't re-login.
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

from django.core.management.base import BaseCommand

CSV_HEADER = [
    "Profile URL",
    "First Name",
    "Last Name",
    "Company",
    "Title",
    "Geo Region",
    "Degree",
]


class Command(BaseCommand):
    help = "Scrape a Sales Navigator saved-leads list to a CSV in leads/."

    def add_arguments(self, parser):
        parser.add_argument(
            "list_id",
            help="Numeric ID from linkedin.com/sales/lists/people/<list_id>",
        )
        parser.add_argument(
            "--output",
            default=None,
            help="Output path (default: leads/sales_nav_<list_id>.csv)",
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
        parser.add_argument(
            "--url-template",
            default=None,
            help=(
                "Override the Sales Nav list URL template. Use {list_id}, "
                "{start}, {count} placeholders. Paste the actual request URL "
                "from Arc DevTools Network tab if the default fails."
            ),
        )

    def handle(self, *args, **options):
        import os
        from linkedin.notifications.slack import notify_on_error
        # Sales-nav uses its own dedicated LinkedIn account (separate from
        # the outreach operators), so the account email is the most useful
        # identifier — include both for ops clarity.
        try:
            from linkedin.operators import resolve_operator
            account_user = os.getenv("SALES_NAV_LINKEDIN_USERNAME", "").strip()
            operator = resolve_operator(account_user) if account_user else ""
        except Exception:
            operator, account_user = "", ""
        with notify_on_error(
            "export_sales_list",
            context={
                "operator": operator or "(sales-nav)",
                "account": account_user or "(unset)",
                "list_id": options.get("list_id"),
                "limit": options.get("limit"),
            },
        ):
            self._handle_impl(*args, **options)

    def _handle_impl(self, *args, **options):
        from linkedin.actions.sales_nav_list import discover_list_url_template
        from linkedin.actions.standalone_session import StandaloneLinkedInSession
        from linkedin.api.client import PlaywrightLinkedinAPI
        from linkedin.conf import ROOT_DIR

        list_id = options["list_id"]
        limit = options["limit"]
        delay = options["delay_seconds"]
        url_template_override = options["url_template"]

        default_path = ROOT_DIR / "leads" / f"sales_nav_{list_id}.csv"
        output_path = Path(options["output"]) if options["output"] else default_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with StandaloneLinkedInSession(label="Sales Nav") as session:
            api = PlaywrightLinkedinAPI(session)

            if url_template_override:
                url_template = url_template_override
                self.stdout.write(f"Using URL template override")
            else:
                self.stdout.write("Auto-discovering Sales Nav list endpoint...")
                url_template = discover_list_url_template(session, list_id)

            self.stdout.write(
                f"Fetching Sales Nav list {list_id} as {session.username} → {output_path}"
            )
            written, stats = self._dump_csv(
                api, list_id, url_template, limit, delay, output_path,
            )

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

    def _dump_csv(self, api, list_id, url_template, limit, delay, output_path):
        from linkedin.actions.sales_nav_list import iter_sales_nav_list

        stats = {"inaccessible": 0, "unresolvable": 0}
        written = 0

        with output_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)

            for row in iter_sales_nav_list(
                api, list_id, max_results=limit, url_template=url_template,
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
                    row["title"],
                    row["geo_region"],
                    row["degree"] or "",
                ])
                f.flush()  # crash-safe — partial CSV is still usable
                written += 1
                self.stdout.write(
                    f"  + {row['full_name']} ({row['company_name']}) → {public_id}"
                )
                time.sleep(delay)

        return written, stats
