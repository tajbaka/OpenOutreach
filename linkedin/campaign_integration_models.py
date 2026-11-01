"""Caller receipts and callback delivery, never an outreach execution queue."""
import uuid
from django.db import models


class CampaignSubmission(models.Model):
    source = models.CharField(max_length=80)
    reference_id = models.CharField(max_length=200)
    input_hash = models.CharField(max_length=64)
    enrollment = models.OneToOneField("linkedin.CampaignMessageEnrollment", on_delete=models.PROTECT)
    callback_url = models.URLField(max_length=1000, blank=True, default="")
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    callback_payload = models.JSONField(default=dict)
    callback_ready_at = models.DateTimeField(null=True, blank=True)
    callback_attempts = models.PositiveIntegerField(default=0)
    callback_next_at = models.DateTimeField(null=True, blank=True)
    callback_delivered_at = models.DateTimeField(null=True, blank=True)
    callback_abandoned_at = models.DateTimeField(null=True, blank=True)
    callback_error = models.CharField(max_length=80, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "reference_id"], name="campaign_submission_reference")]
