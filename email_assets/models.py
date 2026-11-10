"""Reusable email attachment assets and their per-Lead assignments.

An ``EmailAsset`` names one file in a storage provider (Google Drive first)
plus the exact SHA-256 an operator approved. The bytes stay in the provider;
OpenOutreach fetches them through authorized access when it sends and refuses
to attach anything whose bytes no longer match the approval.

``LeadEmailAsset`` maps a Lead and a named slot (for example
``marketplace_pdf``) to one asset. Many Leads may share one asset, which is
how several contacts at one company receive the same approved PDF.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from email_assets.files import is_sha256
from email_assets.requirements import (
    AttachmentRequirementError,
    validate_asset_key,
    validate_slot_key,
)


class EmailAsset(models.Model):
    class Provider(models.TextChoices):
        GOOGLE_DRIVE = "google_drive", "Google Drive"

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting approval"
        APPROVED = "approved", "Approved"
        RETIRED = "retired", "Retired"

    key = models.SlugField(max_length=160, unique=True)
    name = models.CharField(max_length=200)
    provider = models.CharField(max_length=32, choices=Provider.choices)
    provider_file_id = models.CharField(max_length=200)
    source_url = models.URLField(max_length=1000)
    filename = models.CharField(
        max_length=150,
        help_text="Attachment filename recipients see.",
    )
    mime_type = models.CharField(max_length=150)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(
        max_length=64,
        help_text="SHA-256 of the provider bytes last registered or refreshed.",
    )
    provider_checksum = models.CharField(max_length=64, blank=True, default="")
    provider_revision = models.CharField(max_length=200, blank=True, default="")
    provider_modified_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    registered_by = models.CharField(max_length=150)
    approved_by = models.CharField(max_length=150, blank=True, default="")
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_sha256 = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="SHA-256 of the exact bytes approved for sending.",
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("key",)
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "provider_file_id"),
                name="email_asset_unique_provider_file",
            ),
            models.CheckConstraint(
                condition=~Q(status="approved")
                | (
                    Q(approved_sha256=F("sha256"))
                    & ~Q(approved_by="")
                    & Q(approved_at__isnull=False)
                ),
                name="email_asset_approved_matches_bytes",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        errors = {}
        try:
            validate_asset_key(self.key)
        except AttachmentRequirementError as exc:
            errors["key"] = str(exc)
        if not is_sha256(self.sha256):
            errors["sha256"] = "SHA-256 must be 64 lowercase hex characters."
        if self.approved_sha256 and not is_sha256(self.approved_sha256):
            errors["approved_sha256"] = "Approved SHA-256 must be lowercase hex."
        if errors:
            raise ValidationError(errors)

    @property
    def is_sendable(self) -> bool:
        return (
            self.status == self.Status.APPROVED
            and bool(self.approved_sha256)
            and self.approved_sha256 == self.sha256
        )

    def __str__(self) -> str:
        return f"{self.name} ({self.key})"


class LeadEmailAsset(models.Model):
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.CASCADE,
        related_name="email_asset_assignments",
    )
    slot = models.CharField(max_length=80)
    asset = models.ForeignKey(
        EmailAsset,
        on_delete=models.PROTECT,
        related_name="lead_assignments",
    )
    assigned_by = models.CharField(max_length=150)
    assigned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("lead_id", "slot")
        constraints = [
            models.UniqueConstraint(
                fields=("lead", "slot"),
                name="email_asset_unique_lead_slot",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        try:
            validate_slot_key(self.slot)
        except AttachmentRequirementError as exc:
            raise ValidationError({"slot": str(exc)}) from None

    def __str__(self) -> str:
        return f"Lead {self.lead_id} {self.slot} -> {self.asset_id}"
