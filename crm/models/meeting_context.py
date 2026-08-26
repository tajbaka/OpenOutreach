"""External meeting-note cache and multi-contact meeting associations."""
from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q


class MeetingParticipant(models.Model):
    class MatchMethod(models.TextChoices):
        LEGACY_PRIMARY = "legacy_primary", "Legacy Primary Lead"
        ATTENDEE_EMAIL = "attendee_email", "Attendee Email"
        ATTENDEE_IDENTITY = "attendee_identity", "Attendee Identity"
        ACCOUNT_DATE_TITLE = "account_date_title", "Account + Date + Title"
        MANUAL = "manual", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meeting = models.ForeignKey(
        "crm.Meeting",
        on_delete=models.CASCADE,
        related_name="participant_links",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.PROTECT,
        related_name="meeting_participations",
    )
    attendee_email = models.EmailField(blank=True, default="")
    attendee_name = models.CharField(max_length=200, blank=True, default="")
    response_status = models.CharField(max_length=40, blank=True, default="")
    match_method = models.CharField(
        max_length=32,
        choices=MatchMethod.choices,
        default=MatchMethod.MANUAL,
    )
    match_evidence = models.JSONField(blank=True, default=dict)
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["meeting", "lead"],
                name="unique_meeting_participant_lead",
            ),
        ]
        indexes = [
            models.Index(fields=["lead", "meeting"], name="crm_meeting_part_lead_idx"),
        ]

    def save(self, *args, **kwargs):
        self.attendee_email = (self.attendee_email or "").strip().casefold()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"attendee_email"}
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"meeting={self.meeting_id} lead={self.lead_id}"


class MeetingNote(models.Model):
    """Cached Granola/Gemini note, retained even while unmatched."""

    class Source(models.TextChoices):
        GRANOLA = "granola", "Granola"
        GEMINI = "gemini", "Gemini"

    class DetailStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETE = "complete", "Complete"
        RETRY = "retry", "Retry"
        UNAVAILABLE = "unavailable", "Unavailable"

    class MatchStatus(models.TextChoices):
        UNMATCHED = "unmatched", "Unmatched"
        MATCHED = "matched", "Matched"
        AMBIGUOUS = "ambiguous", "Ambiguous"

    class MatchMethod(models.TextChoices):
        ATTENDEE_EMAIL = "attendee_email", "Attendee Email"
        ATTENDEE_IDENTITY = "attendee_identity", "Attendee Identity"
        ACCOUNT_DATE_TITLE = "account_date_title", "Account + Date + Title"
        EXISTING_EVENT_ID = "existing_event_id", "Existing Calendar Event ID"
        LEGACY_PRIMARY = "legacy_primary", "Legacy Primary Lead"
        MANUAL = "manual", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=16, choices=Source.choices, db_index=True)
    external_id = models.CharField(max_length=255)
    meeting = models.ForeignKey(
        "crm.Meeting",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notes",
    )
    opportunity = models.ForeignKey(
        "crm.Opportunity",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="meeting_notes",
    )
    title = models.CharField(max_length=500, blank=True, default="")
    owner_name = models.CharField(max_length=200, blank=True, default="")
    owner_email = models.EmailField(blank=True, default="")
    web_url = models.URLField(max_length=1000, blank=True, default="")
    calendar_event_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    scheduled_start_at = models.DateTimeField(null=True, blank=True, db_index=True)
    scheduled_end_at = models.DateTimeField(null=True, blank=True)
    attendees = models.JSONField(blank=True, default=list)
    content = models.TextField(blank=True, default="")
    summary_text = models.TextField(blank=True, default="")
    summary_markdown = models.TextField(blank=True, default="")
    transcript = models.JSONField(blank=True, default=list)
    raw = models.JSONField(blank=True, default=dict)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    fetched_at = models.DateTimeField(null=True, blank=True)
    transcript_fetched_at = models.DateTimeField(null=True, blank=True)
    detail_status = models.CharField(
        max_length=16,
        choices=DetailStatus.choices,
        default=DetailStatus.PENDING,
        db_index=True,
    )
    match_status = models.CharField(
        max_length=16,
        choices=MatchStatus.choices,
        default=MatchStatus.UNMATCHED,
        db_index=True,
    )
    match_method = models.CharField(
        max_length=32,
        choices=MatchMethod.choices,
        blank=True,
        default="",
    )
    match_evidence = models.JSONField(blank=True, default=dict)
    matcher_version = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-scheduled_start_at", "-source_updated_at", "source", "external_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"],
                name="unique_external_meeting_note",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(match_status="matched")
                    | Q(meeting__isnull=False)
                    | Q(opportunity__isnull=False)
                ),
                name="matched_note_has_crm_link",
            ),
        ]
        indexes = [
            models.Index(
                fields=["source", "source_updated_at"],
                name="crm_note_source_updated_idx",
            ),
            models.Index(
                fields=["source", "match_status"],
                name="crm_note_source_match_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        self.owner_email = (self.owner_email or "").strip().casefold()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"owner_email"}
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"[{self.source}/{self.match_status}] {self.title or self.external_id}"


class MeetingNoteSyncState(models.Model):
    class Status(models.TextChoices):
        IDLE = "idle", "Idle"
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        DISABLED = "disabled", "Disabled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=16, choices=MeetingNote.Source.choices, unique=True)
    successful_watermark = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.IDLE)
    last_error_kind = models.CharField(max_length=64, blank=True, default="")
    last_error_message = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.source} sync [{self.status}]"
