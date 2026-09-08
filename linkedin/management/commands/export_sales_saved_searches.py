"""Discover and export scoped Sales Navigator lead saved searches in one run."""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify


class Command(BaseCommand):
    help = (
        "Discover named Sales Navigator lead saved searches and export each "
        "to its own CSV using one authenticated session."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            default="https://www.linkedin.com/sales/search/people",
            help="Bootstrap Sales Navigator People-search URL.",
        )
        parser.add_argument(
            "--name-prefix",
            default="FMKT |",
            help="Export only saved lead searches whose names start with this prefix.",
        )
        parser.add_argument(
            "--name-suffix",
            default=None,
            help="Optionally require saved lead-search names to end with this suffix.",
        )
        parser.add_argument(
            "--output-dir",
            default=None,
            help=(
                "Batch output directory. Defaults to a timestamped directory under "
                "artifacts/leads/sales-nav-saved-searches/."
            ),
        )
        parser.add_argument(
            "--discover-only",
            action="store_true",
            help="Print matching saved-search names, IDs, and URLs without exporting.",
        )
        parser.add_argument(
            "--limit-per-search",
            type=int,
            default=None,
            help="Stop each saved-search export after this many results.",
        )
        parser.add_argument(
            "--delay-seconds",
            type=float,
            default=3.0,
            help="Sleep between uncached profile-resolution calls (default 3.0s).",
        )
        parser.add_argument(
            "--resume",
            action="store_true",
            help="Reuse an explicit output directory and skip completed CSVs.",
        )

    def handle(self, *args, **options):
        from linkedin.notifications.slack import notify_on_error

        account_user = os.getenv("SALES_NAV_LINKEDIN_USERNAME", "").strip()
        with notify_on_error(
            "export_sales_saved_searches",
            context={
                "account": account_user or "(unset)",
                "name_prefix": options.get("name_prefix"),
                "name_suffix": options.get("name_suffix"),
                "discover_only": options.get("discover_only"),
                "limit_per_search": options.get("limit_per_search"),
            },
        ):
            self._handle_impl(*args, **options)

    def _handle_impl(self, *args, **options):
        from linkedin.actions.sales_nav_export import export_sales_nav_csv
        from linkedin.actions.sales_nav_list import discover_search_url_template
        from linkedin.actions.sales_nav_saved_searches import (
            discover_saved_people_searches,
            validate_people_search_url,
        )
        from linkedin.actions.standalone_session import StandaloneLinkedInSession
        from linkedin.api.client import PlaywrightLinkedinAPI
        from linkedin.conf import ROOT_DIR
        from linkedin.exceptions import SalesNavigatorSurfaceError

        bootstrap_url = options["url"]
        name_prefix = options["name_prefix"]
        name_suffix = options["name_suffix"]
        discover_only = options["discover_only"]
        limit = options["limit_per_search"]
        delay = options["delay_seconds"]
        resume = options["resume"]

        try:
            validate_people_search_url(bootstrap_url)
        except SalesNavigatorSurfaceError as exc:
            raise CommandError(str(exc)) from exc
        if not name_prefix.strip():
            raise CommandError("--name-prefix must not be blank.")
        if name_suffix is not None and not name_suffix.strip():
            raise CommandError("--name-suffix must not be blank when provided.")
        if limit is not None and limit <= 0:
            raise CommandError("--limit-per-search must be positive.")
        if delay < 0:
            raise CommandError("--delay-seconds must not be negative.")
        if resume and not options["output_dir"]:
            raise CommandError("--resume requires an explicit --output-dir.")

        output_dir = self._resolve_output_dir(ROOT_DIR, options["output_dir"])
        if not discover_only:
            self._prepare_output_dir(output_dir, resume=resume)

        with StandaloneLinkedInSession(label="Sales Nav Saved Searches") as session:
            searches = discover_saved_people_searches(
                session,
                bootstrap_url=bootstrap_url,
                name_prefix=name_prefix,
                name_suffix=name_suffix,
            )
            scope = f"prefix {name_prefix!r}"
            if name_suffix:
                scope += f" and suffix {name_suffix!r}"
            self.stdout.write(
                self.style.SUCCESS(
                    f"Discovered {len(searches)} saved lead searches matching "
                    f"{scope}."
                )
            )
            for search in searches:
                self.stdout.write(
                    f"  {search.name} [{search.saved_search_id}] {search.url}"
                )

            if discover_only:
                return

            api = PlaywrightLinkedinAPI(session)
            profile_cache: dict[str, dict | None] = {}
            manifest_path = output_dir / "manifest.json"
            if resume:
                manifest = self._load_resume_manifest(
                    manifest_path,
                    username=session.username,
                    bootstrap_url=bootstrap_url,
                    name_prefix=name_prefix,
                    name_suffix=name_suffix,
                    output_dir=output_dir,
                    limit_per_search=limit,
                    searches=searches,
                )
                manifest["resumed_at"] = datetime.now(timezone.utc).isoformat()
            else:
                manifest = self._new_manifest(
                    session.username,
                    bootstrap_url,
                    name_prefix,
                    name_suffix,
                    output_dir,
                    limit,
                    searches,
                )
            self._write_manifest(manifest_path, manifest)
            completed_status = "limited_complete" if limit is not None else "complete"

            for index, search in enumerate(searches):
                row = manifest["searches"][index]
                output_path = output_dir / row["output_file"]
                if resume and row["status"] == completed_status:
                    if not output_path.exists():
                        raise CommandError(
                            f"Manifest marks {search.name!r} complete but its CSV "
                            f"is missing: {output_path}"
                        )
                    actual_rows = self._count_csv_rows(output_path)
                    if actual_rows != row["written"]:
                        raise CommandError(
                            f"Completed CSV row count changed for {search.name!r}: "
                            f"manifest={row['written']}, file={actual_rows}."
                        )
                    self._write_manifest(manifest_path, manifest)
                    self.stdout.write(
                        f"Skipping completed {search.name} -> {output_path}"
                    )
                    continue

                row.update(
                    status="running",
                    seen=0,
                    written=0,
                    inaccessible=0,
                    unresolvable=0,
                )
                row.pop("error", None)
                row.pop("completed_at", None)
                self._write_manifest(manifest_path, manifest)
                self.stdout.write(f"Exporting {search.name} -> {output_path}")
                try:
                    url_template = discover_search_url_template(session, search.url)
                    captured_ids = parse_qs(urlparse(url_template).query).get(
                        "savedSearchId", []
                    )
                    if captured_ids != [search.saved_search_id]:
                        raise SalesNavigatorSurfaceError(
                            f"Captured search endpoint did not preserve savedSearchId "
                            f"{search.saved_search_id}."
                        )
                    stats = export_sales_nav_csv(
                        api,
                        url_template=url_template,
                        output_path=output_path,
                        max_results=limit,
                        delay_seconds=delay,
                        profile_cache=profile_cache,
                        report=self.stdout.write,
                    )
                except Exception as exc:
                    row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                    self._write_manifest(manifest_path, manifest)
                    raise

                row.update(
                    status=completed_status,
                    seen=stats.seen,
                    written=stats.written,
                    inaccessible=stats.inaccessible,
                    unresolvable=stats.unresolvable,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                self._write_manifest(manifest_path, manifest)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Completed {search.name}: {stats.written} rows "
                        f"({stats.inaccessible} inaccessible, "
                        f"{stats.unresolvable} unresolvable)."
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Exported {len(searches)} saved searches to {output_dir}."
            )
        )

    @staticmethod
    def _resolve_output_dir(root_dir: Path, configured: str | None) -> Path:
        if configured:
            return Path(configured)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return (
            Path(root_dir)
            / "artifacts"
            / "leads"
            / "sales-nav-saved-searches"
            / stamp
        )

    @staticmethod
    def _prepare_output_dir(output_dir: Path, *, resume: bool) -> None:
        if resume:
            manifest_path = output_dir / "manifest.json"
            if not output_dir.is_dir() or not manifest_path.is_file():
                raise CommandError(
                    f"--resume requires an existing batch manifest: {manifest_path}"
                )
            return
        if output_dir.exists() and any(output_dir.iterdir()) and not resume:
            raise CommandError(
                f"Output directory is not empty: {output_dir}. "
                "Choose another directory or pass --resume."
            )
        output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _output_filename(name: str, saved_search_id: str) -> str:
        safe_name = slugify(name) or "saved-search"
        return f"{safe_name}-{saved_search_id}.csv"

    def _new_manifest(
        self,
        username: str,
        bootstrap_url: str,
        name_prefix: str,
        name_suffix: str | None,
        output_dir: Path,
        limit_per_search: int | None,
        searches,
    ) -> dict:
        return {
            "schema_version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "account": username,
            "bootstrap_url": bootstrap_url,
            "name_prefix": name_prefix,
            "name_suffix": name_suffix,
            "output_dir": str(output_dir.resolve()),
            "limit_per_search": limit_per_search,
            "searches": [
                {
                    "name": search.name,
                    "saved_search_id": search.saved_search_id,
                    "url": search.url,
                    "output_file": self._output_filename(
                        search.name, search.saved_search_id
                    ),
                    "status": "planned",
                    "seen": 0,
                    "written": 0,
                    "inaccessible": 0,
                    "unresolvable": 0,
                }
                for search in searches
            ],
        }

    def _load_resume_manifest(
        self,
        path: Path,
        *,
        username: str,
        bootstrap_url: str,
        name_prefix: str,
        name_suffix: str | None = None,
        output_dir: Path,
        limit_per_search: int | None,
        searches,
    ) -> dict:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Cannot read resume manifest {path}: {exc}") from exc

        expected = self._new_manifest(
            username,
            bootstrap_url,
            name_prefix,
            name_suffix,
            output_dir,
            limit_per_search,
            searches,
        )
        for key in (
            "schema_version",
            "account",
            "bootstrap_url",
            "name_prefix",
            "name_suffix",
            "output_dir",
            "limit_per_search",
        ):
            if manifest.get(key) != expected[key]:
                raise CommandError(
                    f"Resume manifest mismatch for {key}: "
                    f"expected {expected[key]!r}, found {manifest.get(key)!r}."
                )

        actual_rows = manifest.get("searches")
        expected_rows = expected["searches"]
        if not isinstance(actual_rows, list) or len(actual_rows) != len(expected_rows):
            raise CommandError(
                "Saved-search inventory changed since this batch began; "
                "start a new output directory."
            )
        identity_keys = ("name", "saved_search_id", "url", "output_file")
        for index, (actual, wanted) in enumerate(zip(actual_rows, expected_rows)):
            if not isinstance(actual, dict) or any(
                actual.get(key) != wanted[key] for key in identity_keys
            ):
                raise CommandError(
                    f"Saved-search inventory changed at manifest row {index + 1}; "
                    "start a new output directory."
                )
        return manifest

    @staticmethod
    def _write_manifest(path: Path, payload: dict) -> None:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)

    @staticmethod
    def _count_csv_rows(path: Path) -> int:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return max(sum(1 for _row in csv.reader(handle)) - 1, 0)
