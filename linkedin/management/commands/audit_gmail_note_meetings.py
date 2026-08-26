"""Report identity-invalid synthetic Gmail-note Meetings without writes."""
from __future__ import annotations

import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from gmail.data_sync import audit_gmail_note_meeting_identities


class Command(BaseCommand):
    help = (
        "Read-only audit of synthetic Gmail-note meetings whose original "
        "title does not validate against the linked Lead. Stdout is aggregate-only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            help=(
                "Optional private JSON path for record-level review. The file "
                "is created with owner-only permissions and is never printed."
            ),
        )

    def handle(self, *args, **options):
        issues = audit_gmail_note_meeting_identities()
        output_path = options.get("output")
        if output_path:
            path = Path(output_path).expanduser().resolve()
            if path.exists():
                raise CommandError("--output already exists; choose a new private path")
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"read_only": True, "issue_count": len(issues), "issues": issues},
                indent=2,
                ensure_ascii=False,
            ).encode("utf-8")
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
            except OSError as exc:
                raise CommandError("private audit output could not be written") from exc
        self.stdout.write(json.dumps(
            {
                "read_only": True,
                "issue_count": len(issues),
                "private_output_created": bool(output_path),
            },
            sort_keys=True,
        ))
