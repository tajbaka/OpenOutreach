"""Search and retrieve Granola notes without writing to the CRM."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db.models import CharField, Q, Value
from django.db.models.functions import Concat

from crm.models import Lead, Meeting
from linkedin.conf import (
    GRANOLA_API_BASE,
    GRANOLA_API_KEY,
    GRANOLA_HTTP_TIMEOUT_SECONDS,
)
from linkedin.exceptions import GranolaError
from linkedin.granola import GranolaClient


class Command(BaseCommand):
    help = (
        "List or search Granola meeting notes; search falls back to stored "
        "Gemini notes when Granola has no match."
    )

    def add_arguments(self, parser):
        selection = parser.add_mutually_exclusive_group()
        selection.add_argument(
            "--search",
            help="Case-insensitive title/owner search; add --deep-search for note content.",
        )
        selection.add_argument("--note-id", help="Retrieve one exact Granola not_ ID.")
        parser.add_argument(
            "--deep-search",
            action="store_true",
            help="Fetch note details while searching attendees, summaries, folders, and calendar data.",
        )
        parser.add_argument(
            "--include-transcript",
            action="store_true",
            help="Include paginated transcript data for exact-note or search results.",
        )
        parser.add_argument(
            "--no-gemini-fallback",
            action="store_true",
            help="Do not search crm.Meeting Gemini notes when Granola has no match.",
        )
        parser.add_argument(
            "--since-days",
            type=int,
            default=365,
            help="Only scan notes created in the last N days (default: 365).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Maximum returned notes (default: 20, maximum: 100).",
        )
        parser.add_argument(
            "--scan-limit",
            type=int,
            default=300,
            help="Maximum notes inspected while searching (default: 300, maximum: 3000).",
        )
        parser.add_argument("--folder-id", help="Restrict list/search to a Granola folder tree.")
        parser.add_argument("--output", help="Write JSON to this path instead of stdout.")

    def handle(self, *args, **options):
        self._validate_options(options)
        if not GRANOLA_API_KEY:
            raise CommandError("GRANOLA_API_KEY is not configured in .env.")
        client = GranolaClient(
            api_key=GRANOLA_API_KEY,
            base_url=GRANOLA_API_BASE,
            timeout=GRANOLA_HTTP_TIMEOUT_SECONDS,
        )
        try:
            payload = self._build_payload(client, options)
        except GranolaError as exc:
            raise CommandError(str(exc)) from exc

        if (
            options.get("search")
            and not options["no_gemini_fallback"]
            and payload["count"] == 0
        ):
            payload = self._apply_gemini_fallback(payload, options)

        encoded = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        output = options.get("output")
        if output:
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(encoded + "\n", encoding="utf-8")
            self.stdout.write(f"Wrote {payload['count']} meeting note(s) to {path}")
        else:
            self.stdout.write(encoded)

    def _validate_options(self, options: dict[str, Any]) -> None:
        if not 1 <= options["limit"] <= 100:
            raise CommandError("--limit must be between 1 and 100.")
        if not 1 <= options["scan_limit"] <= 3000:
            raise CommandError("--scan-limit must be between 1 and 3000.")
        if options["scan_limit"] < options["limit"]:
            raise CommandError("--scan-limit cannot be smaller than --limit.")
        if options["since_days"] <= 0:
            raise CommandError("--since-days must be positive.")
        if options["deep_search"] and not options.get("search"):
            raise CommandError("--deep-search requires --search.")
        if options["include_transcript"] and not (
            options.get("search") or options.get("note_id")
        ):
            raise CommandError("--include-transcript requires --search or --note-id.")

    def _build_payload(
        self, client: GranolaClient, options: dict[str, Any]
    ) -> dict[str, Any]:
        note_id = options.get("note_id")
        if note_id:
            note = client.get_note(note_id)
            if options["include_transcript"]:
                note["transcript"] = client.get_transcript(note_id)
            return {
                "mode": "note",
                "source": "granola",
                "sources_checked": ["granola"],
                "count": 1,
                "notes": [note],
            }

        created_after = (
            datetime.now(UTC) - timedelta(days=options["since_days"])
        ).isoformat().replace("+00:00", "Z")
        query = (options.get("search") or "").strip()
        notes: list[dict[str, Any]] = []
        scanned = 0
        for metadata in client.iter_notes(
            created_after=created_after,
            folder_id=options.get("folder_id"),
            max_notes=options["scan_limit"],
        ):
            scanned += 1
            detail = None
            if query:
                if options["deep_search"]:
                    detail = client.get_note(str(metadata.get("id") or ""))
                    if not _matches_search(query, detail):
                        continue
                elif not _matches_search(query, metadata):
                    continue
                if detail is None:
                    detail = client.get_note(str(metadata.get("id") or ""))
            selected = detail or metadata
            if options["include_transcript"]:
                selected["transcript"] = client.get_transcript(str(selected.get("id") or ""))
            notes.append(selected)
            if len(notes) >= options["limit"]:
                break
        payload = {
            "mode": "search" if query else "list",
            "source": "granola" if notes or not query else None,
            "sources_checked": ["granola"],
            "fallback_used": False,
            "query": query or None,
            "created_after": created_after,
            "scanned": scanned,
            "count": len(notes),
            "notes": notes,
        }
        return payload

    def _apply_gemini_fallback(
        self, payload: dict[str, Any], options: dict[str, Any]
    ) -> dict[str, Any]:
        query = options["search"].strip()
        notes = _find_gemini_notes(
            query=query,
            since_days=options["since_days"],
            limit=options["limit"],
        )
        return {
            **payload,
            "source": "gemini" if notes else None,
            "sources_checked": ["granola", "gemini"],
            "fallback_used": True,
            "granola_count": payload["count"],
            "count": len(notes),
            "notes": notes,
        }


def _find_gemini_notes(*, query: str, since_days: int, limit: int) -> list[dict[str, Any]]:
    """Find Gemini notes by meeting/account identity, never loose note-body text."""
    pattern = _search_pattern(query)
    lead_ids = set(
        Lead.objects.annotate(
            search_full_name=Concat(
                "first_name",
                Value(" "),
                "last_name",
                output_field=CharField(),
            )
        )
        .filter(
            Q(company_name__iregex=pattern)
            | Q(search_full_name__iregex=pattern)
            | Q(email__iexact=query)
        )
        .values_list("id", flat=True)
    )
    created_after = datetime.now(UTC) - timedelta(days=since_days)
    meetings = (
        Meeting.objects.filter(
            start_at__gte=created_after,
        )
        .exclude(gemini_notes_raw="")
        .select_related("lead")
        .order_by("-start_at")
    )

    selected = []
    for meeting in meetings:
        identity = {
            "title": meeting.title,
            "gemini_doc_title": meeting.gemini_doc_title,
            "attendees": meeting.attendees,
            "lead": {
                "name": meeting.lead.full_name,
                "company": meeting.lead.company_name,
                "email": meeting.lead.email,
            },
        }
        if meeting.lead_id not in lead_ids and not _matches_search(query, identity):
            continue
        selected.append(_serialize_gemini_meeting(meeting))
        if len(selected) >= limit:
            break
    return selected


def _serialize_gemini_meeting(meeting: Meeting) -> dict[str, Any]:
    return {
        "id": f"crm_meeting:{meeting.id}",
        "object": "meeting_note",
        "source": "gemini",
        "title": meeting.title,
        "start_at": meeting.start_at.isoformat(),
        "attendees": meeting.attendees,
        "lead": {
            "id": meeting.lead_id,
            "name": meeting.lead.full_name,
            "company": meeting.lead.company_name,
            "email": meeting.lead.email,
            "linkedin_url": meeting.lead.linkedin_url,
        },
        "gemini_doc_id": meeting.gemini_doc_id,
        "gemini_doc_title": meeting.gemini_doc_title,
        "gemini_notes_fetched_at": meeting.gemini_notes_fetched_at,
        "notes": meeting.gemini_notes_raw,
    }


def _search_text(value: Any) -> str:
    parts: list[str] = []

    def collect(item: Any) -> None:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            for nested in item.values():
                collect(nested)
        elif isinstance(item, list):
            for nested in item:
                collect(nested)

    collect(value)
    return "\n".join(parts)


def _matches_search(query: str, value: Any) -> bool:
    """Match account terms without treating Ramp as a substring of FedRAMP."""
    pattern = re.compile(_search_pattern(query), re.IGNORECASE)
    return bool(pattern.search(_search_text(value)))


def _search_pattern(query: str) -> str:
    return rf"(?<![a-zA-Z0-9]){re.escape(query)}(?![a-zA-Z0-9])"
