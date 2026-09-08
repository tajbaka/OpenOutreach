"""Scrape a Sales Navigator People search and dump a CSV to `leads/`.

Usage:
    python manage.py export_sales_search --url '<search_url>'

Where <search_url> is the Sales Nav search URL with filters applied:
    https://www.linkedin.com/sales/search/people?query=(...)

Output: leads/sales_nav_search_<sha8>.csv  (sha8 = first 8 chars of SHA1(url))

The CSV format remains compatible with `manage.py add_seeds --csv`:
    Profile URL, First Name, Last Name, Company, Title, Geo Region, Degree

Auth: same separate LinkedIn account as `export_sales_list`, via env vars:
    SALES_NAV_LINKEDIN_USERNAME
    SALES_NAV_LINKEDIN_PASSWORD
Cookies are cached per username under `data/`.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from django.core.management.base import BaseCommand


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
        import os
        from linkedin.notifications.slack import notify_on_error
        try:
            from linkedin.operators import resolve_operator
            account_user = os.getenv("SALES_NAV_LINKEDIN_USERNAME", "").strip()
            operator = resolve_operator(account_user) if account_user else ""
        except Exception:
            operator, account_user = "", ""
        with notify_on_error(
            "export_sales_search",
            context={
                "operator": operator or "(sales-nav)",
                "account": account_user or "(unset)",
                "url": options.get("url"),
                "limit": options.get("limit"),
            },
        ):
            self._handle_impl(*args, **options)

    def _handle_impl(self, *args, **options):
        from linkedin.actions.sales_nav_export import export_sales_nav_csv
        from linkedin.actions.sales_nav_list import discover_search_url_template
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
            stats = export_sales_nav_csv(
                api,
                url_template=url_template,
                output_path=output_path,
                max_results=limit,
                delay_seconds=delay,
                report=self.stdout.write,
            )

        self.stdout.write(self.style.SUCCESS(
            f"Wrote {stats.written} rows to {output_path} "
            f"({stats.inaccessible} inaccessible, "
            f"{stats.unresolvable} unresolvable)"
        ))
        self.stdout.write(
            f"\nNext: import into a campaign with\n"
            f"  .venv/bin/python manage.py add_seeds <campaign_pk> --csv "
            f"< {output_path}"
        )
