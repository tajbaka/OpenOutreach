from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


NONTERMINAL_ENROLLMENT_STATUSES = ("waiting", "active", "paused")
NONTERMINAL_LANE_STATUSES = (
    "waiting_current",
    "waiting_connection",
    "active",
    "paused",
)


class DripCampaign(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        RETIRED = "retired", "Retired"

    key = models.SlugField(max_length=100, unique=True)
    name = models.CharField(max_length=200)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    active_version = models.ForeignKey(
        "DripCampaignVersion",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="active_for_campaigns",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("key",)

    def clean(self) -> None:
        super().clean()
        if self.status == self.Status.ACTIVE and not self.active_version_id:
            raise ValidationError({"active_version": "An active campaign needs a published version."})
        if (
            self.active_version_id
            and self.active_version is not None
            and self.active_version.campaign_id != self.pk
        ):
            raise ValidationError({"active_version": "The active version belongs to another campaign."})

    def __str__(self) -> str:
        return f"{self.name} ({self.key})"


class DripCampaignVersion(models.Model):
    campaign = models.ForeignKey(
        DripCampaign,
        on_delete=models.PROTECT,
        related_name="versions",
    )
    version = models.PositiveIntegerField()
    manifest = models.JSONField()
    content_hash = models.CharField(max_length=64)
    published_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ("campaign_id", "version")
        constraints = [
            models.UniqueConstraint(
                fields=("campaign", "version"),
                name="drip_unique_campaign_version",
            ),
            models.UniqueConstraint(
                fields=("campaign", "content_hash"),
                name="drip_unique_campaign_content",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="drip_version_positive",
            ),
        ]

    def save(self, *args, **kwargs) -> None:
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "campaign_id",
                "version",
                "manifest",
                "content_hash",
                "published_at",
            )
            if any(getattr(original, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("Published drip campaign versions are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Published drip campaign versions are immutable.")

    def __str__(self) -> str:
        return f"{self.campaign.key} v{self.version}"


class DripEnrollment(models.Model):
    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting"
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        STOPPED = "stopped", "Stopped"
        COMPLETED = "completed", "Completed"

    campaign = models.ForeignKey(
        DripCampaign,
        on_delete=models.PROTECT,
        related_name="enrollments",
    )
    campaign_version = models.ForeignKey(
        DripCampaignVersion,
        on_delete=models.PROTECT,
        related_name="enrollments",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.PROTECT,
        related_name="drip_enrollments",
    )
    frozen_icp = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.WAITING,
        db_index=True,
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)
    stop_reason = models.CharField(max_length=80, blank=True, default="")
    stop_detail = models.TextField(blank=True, default="")
    stop_trigger_message = models.ForeignKey(
        "crm.Message",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stopped_drip_enrollments",
    )
    stop_trigger_meeting = models.ForeignKey(
        "crm.Meeting",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stopped_drip_enrollments",
    )
    enrolled_by = models.CharField(max_length=150)
    plan_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("lead",),
                condition=Q(status__in=NONTERMINAL_ENROLLMENT_STATUSES),
                name="drip_one_open_enrollment",
            ),
        ]
        indexes = [
            models.Index(fields=("status", "created_at"), name="drip_enroll_status_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        if (
            self.campaign_version_id
            and self.campaign_id
            and self.campaign_version.campaign_id != self.campaign_id
        ):
            raise ValidationError(
                {"campaign_version": "The campaign version belongs to another campaign."},
            )

    def __str__(self) -> str:
        return f"{self.campaign.key}: Lead {self.lead_id}"


class DripLane(models.Model):
    class Channel(models.TextChoices):
        LINKEDIN = "linkedin", "LinkedIn"
        GMAIL = "gmail", "Gmail"

    class Status(models.TextChoices):
        WAITING_CURRENT = "waiting_current", "Waiting for current sequence"
        WAITING_CONNECTION = "waiting_connection", "Waiting for connection"
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        STOPPED = "stopped", "Stopped"
        COMPLETED = "completed", "Completed"

    class CurrentSequenceStatus(models.TextChoices):
        PENDING = "pending", "Pending review/completion"
        COMPLETED = "completed", "Completed"
        NOT_APPLICABLE = "not_applicable", "Not applicable"

    enrollment = models.ForeignKey(
        DripEnrollment,
        on_delete=models.PROTECT,
        related_name="lanes",
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    operator = models.CharField(max_length=64)
    provider_account = models.CharField(max_length=128)
    sender_identity = models.CharField(max_length=254)
    recipient_identity = models.CharField(max_length=500, blank=True, default="")
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.WAITING_CURRENT,
        db_index=True,
    )
    current_sequence_status = models.CharField(
        max_length=24,
        choices=CurrentSequenceStatus.choices,
        default=CurrentSequenceStatus.PENDING,
    )
    current_sequence_reviewed_at = models.DateTimeField(null=True, blank=True)
    current_sequence_reviewed_by = models.CharField(max_length=150, blank=True, default="")
    handoff_evidence = models.JSONField(blank=True, default=dict)
    handed_off_at = models.DateTimeField(null=True, blank=True)
    current_theme_index = models.PositiveIntegerField(default=0)
    current_theme_key = models.CharField(max_length=100, blank=True, default="")
    theme_started_at = models.DateTimeField(null=True, blank=True)
    gmail_thread_id = models.CharField(max_length=200, blank=True, default="")
    gmail_thread_subject = models.CharField(max_length=998, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("enrollment_id", "channel")
        constraints = [
            models.UniqueConstraint(
                fields=("enrollment", "channel"),
                name="drip_one_lane_per_channel",
            ),
            models.UniqueConstraint(
                fields=("channel", "provider_account", "recipient_identity"),
                condition=(
                    Q(status__in=NONTERMINAL_LANE_STATUSES)
                    & ~Q(provider_account="")
                    & ~Q(recipient_identity="")
                ),
                name="drip_one_active_recipient_owner",
            ),
        ]
        indexes = [
            models.Index(fields=("status", "channel"), name="drip_lane_status_idx"),
            models.Index(
                fields=("channel", "provider_account", "recipient_identity"),
                name="drip_lane_identity_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.channel != self.Channel.GMAIL and (
            self.gmail_thread_id or self.gmail_thread_subject
        ):
            raise ValidationError("Gmail thread metadata is valid only on a Gmail lane.")
        if self.current_sequence_status == self.CurrentSequenceStatus.NOT_APPLICABLE:
            if not self.current_sequence_reviewed_at or not self.current_sequence_reviewed_by:
                raise ValidationError(
                    "A not-applicable current sequence requires reviewer and timestamp.",
                )

    def save(self, *args, **kwargs) -> None:
        self.operator = (self.operator or "").strip()
        self.provider_account = (self.provider_account or "").strip().lower()
        self.sender_identity = (self.sender_identity or "").strip().lower()
        self.recipient_identity = (self.recipient_identity or "").strip().lower()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "operator",
                "provider_account",
                "sender_identity",
                "recipient_identity",
            }
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.enrollment_id}/{self.channel}/{self.operator}"


class DripDelivery(models.Model):
    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        UNCLEAR = "unclear", "Unclear"
        STOPPED = "stopped", "Stopped"

    lane = models.ForeignKey(
        DripLane,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    theme_key = models.CharField(max_length=100)
    theme_index = models.PositiveIntegerField()
    step_index = models.PositiveIntegerField()
    frozen_subject = models.CharField(max_length=998, blank=True, default="")
    frozen_body = models.TextField()
    scheduled_at = models.DateTimeField()
    sent_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PLANNED,
        db_index=True,
    )
    current_task = models.OneToOneField(
        "linkedin.Task",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="drip_delivery",
    )
    outbound_message = models.ForeignKey(
        "crm.Message",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="drip_deliveries",
    )
    provider_account = models.CharField(max_length=128)
    provider_message_id = models.CharField(max_length=255, blank=True, default="")
    provider_thread_id = models.CharField(max_length=255, blank=True, default="")
    rfc_message_id = models.CharField(max_length=998, blank=True, default="")
    rfc_references = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("lane_id", "theme_index", "step_index")
        constraints = [
            models.UniqueConstraint(
                fields=("lane", "theme_index", "step_index"),
                name="drip_unique_lane_step",
            ),
        ]
        indexes = [
            models.Index(fields=("status", "scheduled_at"), name="drip_delivery_due_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        if self.lane_id and self.provider_account:
            normalized = self.provider_account.strip().lower()
            if self.lane.provider_account != normalized:
                raise ValidationError(
                    {"provider_account": "Delivery provider account must match its lane."},
                )
        if self.lane_id and self.lane.channel == DripLane.Channel.LINKEDIN:
            if self.frozen_subject:
                raise ValidationError({"frozen_subject": "LinkedIn deliveries do not have subjects."})

    def save(self, *args, **kwargs) -> None:
        self.provider_account = (self.provider_account or "").strip().lower()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.lane_id}:{self.theme_key}:{self.step_index}"


class DripDeliveryAttempt(models.Model):
    class Outcome(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        SENT = "sent", "Sent"
        NOT_SUBMITTED = "not_submitted", "Definitely not submitted"
        UNCLEAR = "unclear", "Unclear"

    delivery = models.ForeignKey(
        DripDelivery,
        on_delete=models.PROTECT,
        related_name="attempts",
    )
    attempt_number = models.PositiveIntegerField()
    started_at = models.DateTimeField(default=timezone.now)
    submission_attempted_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(
        max_length=24,
        choices=Outcome.choices,
        default=Outcome.RESERVED,
    )
    diagnostic_detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("delivery_id", "attempt_number")
        constraints = [
            models.UniqueConstraint(
                fields=("delivery", "attempt_number"),
                name="drip_unique_delivery_attempt",
            ),
            models.CheckConstraint(
                condition=Q(attempt_number__gte=1),
                name="drip_attempt_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.delivery_id} attempt {self.attempt_number}: {self.outcome}"
