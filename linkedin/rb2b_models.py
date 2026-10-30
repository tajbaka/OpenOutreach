"""Campaign-independent visitor intake, invitation receipts and Slack outbox."""
from django.db import models


class RB2BVisitor(models.Model):
    public_identifier = models.CharField(max_length=200, unique=True)
    linkedin_url = models.URLField(max_length=500)
    linkedin_profile = models.ForeignKey("linkedin.LinkedInProfile", on_delete=models.PROTECT)
    events = models.JSONField(default=dict)
    review = models.JSONField(default=dict)
    lead = models.OneToOneField("crm.Lead", null=True, blank=True, on_delete=models.PROTECT)
    member_urn = models.CharField(max_length=200, blank=True, default="")
    note = models.TextField(blank=True, default="")
    # attempting is a durable sender reservation. Never expire it into a retry.
    status = models.CharField(max_length=24, default="new", db_index=True)
    attempt_token = models.UUIDField(null=True, blank=True, unique=True)
    attempted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    outcome = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["member_urn"], condition=~models.Q(member_urn=""),
                                    name="rb2b_unique_member_urn"),
            models.CheckConstraint(condition=models.Q(status__in=[
                "new", "held", "ready", "attempting", "sent", "uncertain", "failed",
            ]), name="rb2b_valid_status"),
        ]


class RB2BNotification(models.Model):
    key = models.CharField(max_length=200, unique=True)
    visitor = models.ForeignKey(RB2BVisitor, on_delete=models.PROTECT)
    message = models.ForeignKey("crm.Message", null=True, blank=True, on_delete=models.PROTECT)
    kind = models.CharField(max_length=24)
    detail = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
