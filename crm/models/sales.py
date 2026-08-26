"""Canonical sales CRM records, separate from outreach automation Deals.

``Deal`` remains a per-Lead/per-Campaign delivery state machine.  The models
in this module describe the durable account sale, its stakeholders, and the
single next action an operator should work.
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


def normalize_account_name(value: str) -> str:
    """Return a conservative identity key without claiming global uniqueness."""
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[\W_]+", " ", normalized, flags=re.UNICODE).strip()


class SalesOwner(models.Model):
    """A strict, durable CRM owner keyed by the canonical operator handle."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    handle = models.CharField(max_length=64, unique=True)
    normalized_handle = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
    )
    display_name = models.CharField(max_length=100, blank=True, default="")
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["handle"]

    def save(self, *args, **kwargs):
        self.handle = (self.handle or "").strip()
        self.normalized_handle = self.handle.casefold()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "handle",
                "normalized_handle",
            }
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.display_name or self.handle


class Account(models.Model):
    """A stable company/account identity used by one or more sales motions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    normalized_name = models.CharField(
        max_length=200,
        blank=True,
        default="",
        editable=False,
        db_index=True,
    )
    domain = models.CharField(max_length=200, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        self.normalized_name = normalize_account_name(self.name)
        self.domain = (self.domain or "").strip().casefold()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {
                "name",
                "normalized_name",
                "domain",
            }
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Opportunity(models.Model):
    """The canonical account-level sale shown on Opportunities/Pipeline."""

    class Stage(models.TextChoices):
        PROSPECTING = "prospecting", "Prospecting"
        DISCOVERY = "discovery", "Discovery"
        DEMO_PLANNING = "demo_planning", "Demo Planning"
        EVALUATION = "evaluation", "Evaluation"
        SANDBOX_PILOT = "sandbox_pilot", "Sandbox/Pilot"
        COMMERCIAL = "commercial", "Commercial"
        PROCUREMENT_LEGAL = "procurement_legal", "Procurement/Legal"
        CLOSED_WON = "closed_won", "Closed Won"
        EXPANSION = "expansion", "Expansion"
        CLOSED_LOST = "closed_lost", "Closed Lost"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        SHEET = "sheet", "Google Sheet"
        BOOTSTRAP = "bootstrap", "Bootstrap"
        SYSTEM = "system", "System"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        "crm.Account",
        on_delete=models.PROTECT,
        related_name="opportunities",
    )
    motion_key = models.CharField(max_length=64, default="primary")
    name = models.CharField(max_length=200, blank=True, default="")
    owner = models.ForeignKey(
        "crm.SalesOwner",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="opportunities",
    )
    stage = models.CharField(
        max_length=32,
        choices=Stage.choices,
        default=Stage.PROSPECTING,
        db_index=True,
    )
    stage_entered_at = models.DateTimeField(default=timezone.now, db_index=True)
    sales_motion_step = models.PositiveSmallIntegerField(
        default=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(15)],
    )
    last_meaningful_activity_at = models.DateTimeField(null=True, blank=True, db_index=True)
    manual_pin = models.BooleanField(default=False, db_index=True)
    value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    probability = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
    )
    closed_won_at = models.DateTimeField(null=True, blank=True)
    closed_lost_at = models.DateTimeField(null=True, blank=True)
    closed_lost_reason = models.TextField(blank=True, default="")
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.MANUAL)
    human_revision = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "motion_key"],
                name="unique_opportunity_per_account_motion",
            ),
            models.CheckConstraint(
                condition=Q(sales_motion_step__isnull=True)
                | Q(sales_motion_step__gte=1, sales_motion_step__lte=15),
                name="opportunity_step_between_1_and_15",
            ),
            models.CheckConstraint(
                condition=Q(probability__isnull=True)
                | Q(probability__gte=0, probability__lte=100),
                name="opportunity_probability_0_to_100",
            ),
            models.CheckConstraint(
                condition=Q(closed_won_at__isnull=True) | Q(closed_lost_at__isnull=True),
                name="opportunity_not_both_won_and_lost",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(stage="closed_won")
                    | Q(closed_won_at__isnull=False, closed_lost_at__isnull=True)
                ),
                name="closed_won_opportunity_has_timestamp",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(stage="closed_lost")
                    | (
                        Q(closed_lost_at__isnull=False, closed_won_at__isnull=True)
                        & ~Q(closed_lost_reason="")
                    )
                ),
                name="closed_lost_opportunity_has_reason",
            ),
            models.CheckConstraint(
                condition=(
                    Q(stage="prospecting", sales_motion_step=1)
                    | Q(stage="discovery", sales_motion_step=2)
                    | Q(stage="demo_planning", sales_motion_step__in=[3, 4])
                    | Q(stage="evaluation", sales_motion_step__in=[5, 6])
                    | Q(stage="sandbox_pilot", sales_motion_step__in=[7, 8, 9, 10])
                    | Q(stage="commercial", sales_motion_step=11)
                    | Q(stage="procurement_legal", sales_motion_step__in=[12, 13, 14])
                    | Q(stage="expansion", sales_motion_step=15)
                    | Q(stage__in=["closed_won", "closed_lost"])
                ),
                name="opportunity_stage_matches_sales_step",
            ),
        ]
        indexes = [
            models.Index(fields=["owner", "stage"], name="crm_opp_owner_stage_idx"),
            models.Index(
                fields=["stage", "last_meaningful_activity_at"],
                name="crm_opp_stage_activity_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        self.motion_key = (self.motion_key or "primary").strip().casefold()
        self.currency = (self.currency or "USD").strip().upper()
        update_fields = kwargs.get("update_fields")
        writes_stage = update_fields is None or "stage" in update_fields
        previous_stage = None
        if writes_stage and not self._state.adding and self.pk:
            previous_stage = type(self).objects.filter(pk=self.pk).values_list(
                "stage", flat=True,
            ).first()

        stage_changed = self._state.adding or (
            writes_stage and previous_stage != self.stage
        )
        if stage_changed:
            entered_at = getattr(self, "_stage_entered_at_override", None) or timezone.now()
            self.stage_entered_at = entered_at
            if self.stage == self.Stage.CLOSED_WON and self.closed_won_at is None:
                self.closed_won_at = entered_at
            if self.stage == self.Stage.CLOSED_LOST and self.closed_lost_at is None:
                self.closed_lost_at = entered_at

        if update_fields is not None:
            normalized_fields = {"motion_key", "currency"}
            if stage_changed:
                normalized_fields.add("stage_entered_at")
                if self.stage == self.Stage.CLOSED_WON:
                    normalized_fields.add("closed_won_at")
                elif self.stage == self.Stage.CLOSED_LOST:
                    normalized_fields.add("closed_lost_at")
            kwargs["update_fields"] = set(update_fields) | normalized_fields

        with transaction.atomic():
            result = super().save(*args, **kwargs)
            if stage_changed:
                OpportunityStageEvent.objects.create(
                    opportunity=self,
                    from_stage=previous_stage or "",
                    to_stage=self.stage,
                    source=getattr(
                        self,
                        "_stage_event_source",
                        self.source or self.Source.SYSTEM,
                    ),
                    actor=getattr(self, "_stage_event_actor", None),
                    changed_at=self.stage_entered_at,
                )
        for attribute in (
            "_stage_entered_at_override",
            "_stage_event_source",
            "_stage_event_actor",
        ):
            if hasattr(self, attribute):
                delattr(self, attribute)
        return result

    def transition_to(
        self,
        stage: str,
        *,
        sales_motion_step: int | None = None,
        source: str = Source.MANUAL,
        actor: "SalesOwner | None" = None,
        changed_at=None,
    ) -> bool:
        """Atomically move stage and write its audit event."""
        if self._state.adding or not self.pk:
            raise ValueError("Save an Opportunity before transitioning it.")
        if stage not in self.Stage.values:
            raise ValueError(f"Unknown Opportunity stage: {stage!r}")
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            if locked.stage == stage and (
                sales_motion_step is None or locked.sales_motion_step == sales_motion_step
            ):
                return False
            locked.stage = stage
            if sales_motion_step is not None:
                locked.sales_motion_step = sales_motion_step
            locked._stage_event_source = source
            locked._stage_event_actor = actor
            locked._stage_entered_at_override = changed_at or timezone.now()
            locked.save(update_fields={"stage", "sales_motion_step", "updated_at"})
            self.refresh_from_db()
        return True

    def __str__(self):
        return self.name or f"{self.account} — {self.get_stage_display()}"


class OpportunityContact(models.Model):
    class Role(models.TextChoices):
        CHAMPION = "champion", "Champion"
        DECISION_MAKER = "decision_maker", "Decision Maker"
        STAKEHOLDER = "stakeholder", "Stakeholder"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    opportunity = models.ForeignKey(
        "crm.Opportunity",
        on_delete=models.CASCADE,
        related_name="contacts",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.PROTECT,
        related_name="opportunity_roles",
    )
    role = models.CharField(max_length=32, choices=Role.choices, default=Role.STAKEHOLDER)
    is_primary = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["opportunity", "lead", "role"],
                name="unique_opportunity_contact_role",
            ),
        ]
        indexes = [
            models.Index(fields=["opportunity", "role"], name="crm_opp_contact_role_idx"),
        ]

    def __str__(self):
        return f"{self.lead} — {self.get_role_display()}"


class OpportunityAction(models.Model):
    class Kind(models.TextChoices):
        NEEDS_RESPONSE = "needs_response", "Needs Response"
        NEXT_STEP = "next_step", "Next Step"
        MEETING_PREP = "meeting_prep", "Meeting Preparation"
        POST_MEETING_COMMITMENT = "post_meeting_commitment", "Post-meeting Commitment"
        FOLLOWUP = "followup", "Follow-up"
        RECOVERY_REVIEW = "recovery_review", "Recovery Review"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        WAITING = "waiting", "Waiting"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    class Disposition(models.TextChoices):
        SENT = "sent", "Sent"
        HANDLED = "handled", "Handled"
        DEFERRED = "deferred", "Deferred"
        POLITE_DECLINE = "polite_decline", "Polite Decline"
        NO_ACTION = "no_action", "No Action"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    opportunity = models.ForeignKey(
        "crm.Opportunity",
        on_delete=models.CASCADE,
        related_name="actions",
    )
    target_lead = models.ForeignKey(
        "crm.Lead",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="targeted_opportunity_actions",
    )
    kind = models.CharField(max_length=40, choices=Kind.choices, default=Kind.NEXT_STEP)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True)
    description = models.TextField()
    due_on = models.DateField(null=True, blank=True, db_index=True)
    waiting_until = models.DateField(null=True, blank=True, db_index=True)
    disposition = models.CharField(
        max_length=24,
        choices=Disposition.choices,
        blank=True,
        default="",
    )
    channel = models.CharField(max_length=32, blank=True, default="")
    draft = models.TextField(blank=True, default="")
    human_revision = models.PositiveBigIntegerField(default=0)
    sheet_human_snapshot = models.JSONField(blank=True, default=dict)
    sheet_published_at = models.DateTimeField(null=True, blank=True)
    handled_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    trigger_message = models.ForeignKey(
        "crm.Message",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="opportunity_actions",
    )
    trigger_meeting = models.ForeignKey(
        "crm.Meeting",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="opportunity_actions",
    )
    idempotency_key = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "due_on", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["opportunity"],
                condition=Q(status__in=["open", "waiting"]),
                name="one_current_action_per_opportunity",
            ),
            models.UniqueConstraint(
                fields=["opportunity", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="unique_action_idempotency_key",
            ),
            models.CheckConstraint(
                condition=~Q(status="waiting") | Q(waiting_until__isnull=False),
                name="waiting_action_has_waiting_date",
            ),
            models.CheckConstraint(
                condition=~Q(status="completed") | Q(completed_at__isnull=False),
                name="completed_action_has_timestamp",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "due_on"], name="crm_action_status_due_idx"),
            models.Index(
                fields=["opportunity", "status"],
                name="crm_action_opp_status_idx",
            ),
        ]

    def _validate_target_lead(self) -> None:
        """Fail closed when an action is routed outside its opportunity.

        ``target_lead`` is the authoritative delivery contact.  A target that
        is merely present in ``crm.Lead`` but is not linked to this
        Opportunity must never be accepted: doing so could publish an action
        under the wrong account or sender queue.
        """
        if self.target_lead_id is None:
            return
        if self.opportunity_id is None:
            raise ValidationError({
                "target_lead": "Save the opportunity before assigning an action target.",
            })
        if not OpportunityContact.objects.filter(
            opportunity_id=self.opportunity_id,
            lead_id=self.target_lead_id,
        ).exists():
            raise ValidationError({
                "target_lead": "The target lead must be linked to this opportunity.",
            })

    def clean(self):
        super().clean()
        self._validate_target_lead()

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if (
            self._state.adding
            or update_fields is None
            or bool(
                {
                    "target_lead",
                    "target_lead_id",
                    "opportunity",
                    "opportunity_id",
                }
                & set(update_fields)
            )
        ):
            self._validate_target_lead()
        if self.status == self.Status.COMPLETED and self.completed_at is None:
            self.completed_at = timezone.now()
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {"completed_at"}
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.opportunity} — {self.get_kind_display()} [{self.status}]"


class OpportunityStageEvent(models.Model):
    class Source(models.TextChoices):
        SHEET = "sheet", "Google Sheet"
        MANUAL = "manual", "Manual"
        BOOTSTRAP = "bootstrap", "Bootstrap"
        SYSTEM = "system", "System"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    opportunity = models.ForeignKey(
        "crm.Opportunity",
        on_delete=models.CASCADE,
        related_name="stage_events",
    )
    from_stage = models.CharField(max_length=32, blank=True, default="")
    to_stage = models.CharField(max_length=32, choices=Opportunity.Stage.choices)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.SYSTEM)
    actor = models.ForeignKey(
        "crm.SalesOwner",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stage_events",
    )
    changed_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["changed_at", "created_at", "id"]
        indexes = [
            models.Index(
                fields=["opportunity", "-changed_at"],
                name="crm_stage_event_opp_time_idx",
            ),
        ]

    def __str__(self):
        return f"{self.opportunity_id}: {self.from_stage or 'new'} → {self.to_stage}"


class OpportunitySheetState(models.Model):
    """Last published human-field snapshot for conservative three-way merges."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    opportunity = models.OneToOneField(
        "crm.Opportunity",
        on_delete=models.CASCADE,
        related_name="sheet_state",
    )
    published_human_snapshot = models.JSONField(blank=True, default=dict)
    published_revision = models.PositiveBigIntegerField(default=0)
    published_action_id = models.UUIDField(null=True, blank=True)
    last_published_at = models.DateTimeField(null=True, blank=True)
    last_imported_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"sheet-state opportunity={self.opportunity_id} rev={self.published_revision}"
