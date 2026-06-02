from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from linkedin.conf import DIAGNOSTICS_DIR


CONNECT_DEBUG_DIR = Path("/tmp/connect-debug")


class Command(BaseCommand):
    help = "Delete old local Playwright diagnostics and connect debug artifacts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Delete files older than this many days. Default: 7.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be deleted without deleting files.",
        )
        parser.add_argument(
            "--connect-debug-only",
            action="store_true",
            help="Only clean /tmp/connect-debug.",
        )
        parser.add_argument(
            "--diagnostics-only",
            action="store_true",
            help="Only clean /tmp/openoutreach-diagnostics.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days < 0:
            raise CommandError("--days must be 0 or greater.")
        if options["connect_debug_only"] and options["diagnostics_only"]:
            raise CommandError("Use only one of --connect-debug-only or --diagnostics-only.")

        roots = []
        if not options["diagnostics_only"]:
            roots.append(CONNECT_DEBUG_DIR)
        if not options["connect_debug_only"]:
            roots.append(DIAGNOSTICS_DIR)

        cutoff = timezone.now() - timedelta(days=days)
        dry_run = options["dry_run"]
        total_files = 0
        total_bytes = 0

        for root in roots:
            files, bytes_removed = self._clean_root(root, cutoff=cutoff, dry_run=dry_run)
            total_files += files
            total_bytes += bytes_removed

        action = "Would delete" if dry_run else "Deleted"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} {total_files} file(s), {self._format_bytes(total_bytes)} older than {days} day(s)."
            )
        )

    def _clean_root(self, root: Path, *, cutoff, dry_run: bool) -> tuple[int, int]:
        if not root.exists():
            self.stdout.write(f"{root}: missing, skipped")
            return 0, 0

        files_deleted = 0
        bytes_deleted = 0
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            stat = path.stat()
            modified_at = timezone.datetime.fromtimestamp(stat.st_mtime, tz=timezone.get_current_timezone())
            if modified_at >= cutoff:
                continue
            files_deleted += 1
            bytes_deleted += stat.st_size
            if dry_run:
                self.stdout.write(f"would delete {path}")
            else:
                path.unlink()

        if not dry_run:
            self._remove_empty_dirs(root)
        self.stdout.write(f"{root}: {files_deleted} file(s), {self._format_bytes(bytes_deleted)}")
        return files_deleted, bytes_deleted

    def _remove_empty_dirs(self, root: Path) -> None:
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass

    def _format_bytes(self, size: int) -> str:
        units = ("B", "KB", "MB", "GB")
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}"
            value /= 1024
