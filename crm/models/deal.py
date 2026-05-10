from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from linkedin.enums import ProfileState


class ClosingReason(models.TextChoices):
    COMPLETED = "Completed"
    FAILED = "Failed"
    DISQUALIFIED = "Disqualified"


class Deal(models.Model):
    class Meta:
        verbose_name = _("Deal")
        verbose_name_plural = _("Deals")
        constraints = [
            models.UniqueConstraint(fields=["lead", "campaign"], name="unique_deal_per_campaign"),
        ]

    lead = models.ForeignKey("Lead", on_delete=models.CASCADE)
    campaign = models.ForeignKey(
        "linkedin.Campaign", on_delete=models.CASCADE, related_name="deals",
    )
    state = models.CharField(
        max_length=20,
        choices=ProfileState.choices,
        default=ProfileState.QUALIFIED,
    )
    closing_reason = models.CharField(
        max_length=20,
        choices=ClosingReason.choices,
        blank=True,
        default="",
    )
    reason = models.TextField(blank=True, default="")
    sent_note = models.TextField(blank=True, default="")
    connect_attempts = models.IntegerField(default=0)
    backoff_hours = models.IntegerField(default=0)
    last_reply_at = models.DateTimeField(null=True, blank=True)
    last_synthesized_at = models.DateTimeField(null=True, blank=True)
    wants_meeting_detected_at = models.DateTimeField(null=True, blank=True)
    # Stamped once when sweep_connections flips state PENDING → CONNECTED.
    # Stable signal for "days since connection" — distinct from update_date
    # which churns on every save. Null on legacy rows pre-dating this field.
    connected_at = models.DateTimeField(null=True, blank=True, db_index=True)
    creation_date = models.DateTimeField(default=timezone.now)
    update_date = models.DateTimeField(auto_now=True)

    def __str__(self):
        lead_str = str(self.lead) if self.lead_id else "?"
        return f"{lead_str} [{self.state}]"
