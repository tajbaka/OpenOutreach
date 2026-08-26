"""External meeting events + raw meeting notes from Google Calendar + Drive.

Lives in its own table (not folded into `crm.Message` with a `calendar`
source) because the shape diverges meaningfully from a "message":
  - no `direction` (a meeting is multi-party, not us-vs-them)
  - structured `start_at` / `end_at` instead of one `sent_at`
  - `attendees` is an array, not a single sender
  - `gemini_notes_raw` holds 30-80KB Gemini-generated transcript text, which
    would bloat Message rows used in hot ball-on-court queries.

Population owner: the data-sync workflow (see
`docs/data-sync-workflow.md`). Data-sync pulls Calendar events via the
Google Calendar MCP, finds matching Drive `Notes by Gemini` docs, reads
their raw text, and upserts here. Idempotent on `(source, external_id)`.

Consumers: the followup workflow's Phase 1 row builder for Met-cohort
leads. Drafter prompt uses `gemini_notes_raw` (truncated) for "what
happened in the call" context; `start_at` feeds the `Days since` math via
`max(latest_message.sent_at, latest_meeting.start_at)`.
"""
from django.db import models


class Meeting(models.Model):
    class Source(models.TextChoices):
        GOOGLE_CALENDAR = "google_calendar", "Google Calendar"
        GRANOLA = "granola", "Granola"

    source = models.CharField(max_length=32, choices=Source.choices)
    external_id = models.CharField(max_length=255)

    lead = models.ForeignKey(
        "crm.Lead", on_delete=models.CASCADE, related_name="meetings",
    )
    opportunity = models.ForeignKey(
        "crm.Opportunity",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="meetings",
    )
    participants = models.ManyToManyField(
        "crm.Lead",
        through="crm.MeetingParticipant",
        related_name="participating_meetings",
        blank=True,
    )

    # ── Calendar event facts ─────────────────────────────────────────────
    start_at = models.DateTimeField(db_index=True)
    end_at = models.DateTimeField(null=True, blank=True)
    title = models.CharField(max_length=500, blank=True, default="")
    description = models.TextField(blank=True, default="")
    # Each entry: {"email": ..., "name": ..., "responseStatus": ...}
    attendees = models.JSONField(blank=True, default=list)

    # ── Drive Gemini doc — raw, not summarized ───────────────────────────
    # The synthesized digest lives separately on the People-tab AI Notes
    # column (operator-readable). This field holds the full unsummarized
    # text so the drafter LLM can read whatever section is relevant per
    # lead without losing fidelity to a pre-summarization pass.
    gemini_doc_id = models.CharField(max_length=255, blank=True, default="")
    gemini_doc_title = models.CharField(max_length=500, blank=True, default="")
    gemini_notes_raw = models.TextField(blank=True, default="")
    gemini_notes_fetched_at = models.DateTimeField(null=True, blank=True)

    # Full event payload for forensic replay (debugging "why did this
    # meeting end up shaped this way?" without re-fetching from MCP).
    raw = models.JSONField(blank=True, default=dict)

    creation_date = models.DateTimeField(auto_now_add=True)
    update_date = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("source", "external_id")
        ordering = ["-start_at"]
        indexes = [
            models.Index(fields=["lead", "-start_at"]),
        ]

    def __str__(self):
        when = self.start_at.date() if self.start_at else "?"
        return f"[{self.source}] {when} {self.title[:60]} (lead={self.lead_id})"
