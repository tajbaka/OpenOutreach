from django.db import models
from django.utils import timezone


class Message(models.Model):
    """A single message between us and a Lead, persisted across sources.

    Sources today: LinkedIn DMs (populated by linkedin.actions.conversations
    via the get_conversation hook, plus by manage.py import_connections from
    CSVs). Gmail/Calendar reserved for future phases.

    Idempotency: (source, external_id) is unique — re-fetching a thread is
    a no-op upsert.
    """

    class Source(models.TextChoices):
        LINKEDIN = "linkedin", "LinkedIn"
        GMAIL = "gmail", "Gmail"
        CALENDAR = "calendar", "Calendar"

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound (from lead)"
        OUTBOUND = "outbound", "Outbound (from us)"

    lead = models.ForeignKey(
        "crm.Lead", on_delete=models.CASCADE, related_name="messages",
    )
    operator = models.ForeignKey(
        "crm.SalesOwner",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="messages",
    )
    source = models.CharField(max_length=16, choices=Source.choices)
    external_id = models.CharField(max_length=200)
    direction = models.CharField(max_length=16, choices=Direction.choices)
    sender = models.CharField(max_length=200, blank=True, default="")
    body = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(db_index=True)
    thread_external_id = models.CharField(max_length=200, blank=True, default="")
    raw = models.JSONField(blank=True, default=dict)
    creation_date = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("source", "external_id")
        indexes = [
            models.Index(fields=["lead", "sent_at"]),
            models.Index(fields=["lead", "operator", "sent_at"]),
        ]
        ordering = ["sent_at"]

    def __str__(self):
        return f"[{self.source}/{self.direction}] {self.lead_id}: {self.body[:60]}"
