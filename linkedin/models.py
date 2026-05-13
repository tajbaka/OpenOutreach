# linkedin/models.py
from __future__ import annotations

import logging
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from linkedin.conf import (
    CONNECT_DAILY_LIMIT,
    CONNECT_WEEKLY_LIMIT,
    FOLLOW_UP_DAILY_LIMIT,
    MAX_TOTAL_DAILY_ACTIONS,
)

_LIMIT_OVERRIDES = {
    "connect_daily_limit": CONNECT_DAILY_LIMIT,
    "connect_weekly_limit": CONNECT_WEEKLY_LIMIT,
    "follow_up_daily_limit": FOLLOW_UP_DAILY_LIMIT,
}

logger = logging.getLogger(__name__)

# action_type → (daily_limit_field, weekly_limit_field)
_RATE_LIMIT_FIELDS = {
    "connect": ("connect_daily_limit", "connect_weekly_limit"),
    "follow_up": ("follow_up_daily_limit", None),
}


class Campaign(models.Model):
    name = models.CharField(max_length=200, unique=True)
    users = models.ManyToManyField(User, blank=True, related_name="campaigns")
    product_docs = models.TextField(blank=True)
    campaign_objective = models.TextField(blank=True)
    booking_link = models.URLField(max_length=500, blank=True)
    is_freemium = models.BooleanField(default=False)
    action_fraction = models.FloatField(default=0.2)
    seed_public_ids = models.JSONField(default=list, blank=True)
    model_blob = models.BinaryField(null=True, blank=True)

    def __str__(self):
        return self.name

    class Meta:
        app_label = "linkedin"


class LinkedInProfile(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="linkedin_profile",
    )
    linkedin_username = models.CharField(max_length=200)
    linkedin_password = models.CharField(max_length=200)
    subscribe_newsletter = models.BooleanField(default=True)
    active = models.BooleanField(default=True)
    connect_daily_limit = models.PositiveIntegerField(default=20)
    connect_weekly_limit = models.PositiveIntegerField(default=100)
    follow_up_daily_limit = models.PositiveIntegerField(default=30)
    legal_accepted = models.BooleanField(default=False)
    newsletter_processed = models.BooleanField(default=False)
    # Cookies: stored on disk at `data/cookies-<safe_username>.json`
    # (`linkedin.browser.cookie_store.cookie_path_for(username)`).
    # The DB `cookie_data` JSONField was removed 2026-05-12 to mirror
    # the standalone scripts' on-disk pattern — see migration 0004.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._exhausted: dict[str, date] = {}

    def can_execute(self, action_type: str) -> bool:
        """Check if the action is allowed under daily/weekly rate limits."""
        # Reset exhaustion flag on a new day
        exhausted_date = self._exhausted.get(action_type)
        if exhausted_date is not None and exhausted_date != date.today():
            del self._exhausted[action_type]
        if action_type in self._exhausted:
            return False

        daily_field, weekly_field = _RATE_LIMIT_FIELDS[action_type]

        self.refresh_from_db(fields=[daily_field] + ([weekly_field] if weekly_field else []))

        if MAX_TOTAL_DAILY_ACTIONS and self._total_daily_count() >= MAX_TOTAL_DAILY_ACTIONS:
            return False

        daily_limit = _LIMIT_OVERRIDES.get(daily_field) or getattr(self, daily_field)
        if daily_limit is not None and self._daily_count(action_type) >= daily_limit:
            return False

        if weekly_field:
            weekly_limit = _LIMIT_OVERRIDES.get(weekly_field) or getattr(self, weekly_field)
            if weekly_limit is not None and self._weekly_count(action_type) >= weekly_limit:
                return False

        return True

    def record_action(self, action_type: str, campaign: Campaign) -> None:
        """Persist a rate-limited action."""
        ActionLog.objects.create(
            linkedin_profile=self, campaign=campaign, action_type=action_type,
        )

    def mark_exhausted(self, action_type: str) -> None:
        """Mark the action type as externally exhausted for today."""
        self._exhausted[action_type] = date.today()
        logger.warning("Rate limit: %s externally exhausted for today", action_type)

    def _daily_count(self, action_type: str) -> int:
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return ActionLog.objects.filter(
            linkedin_profile=self, action_type=action_type,
            created_at__gte=today_start,
        ).count()

    def _total_daily_count(self) -> int:
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return ActionLog.objects.filter(
            linkedin_profile=self,
            created_at__gte=today_start,
        ).count()

    def _weekly_count(self, action_type: str) -> int:
        now = timezone.now()
        monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        return ActionLog.objects.filter(
            linkedin_profile=self, action_type=action_type,
            created_at__gte=monday,
        ).count()

    def __str__(self):
        return f"{self.user.username} ({self.linkedin_username})"

    class Meta:
        app_label = "linkedin"


class SearchKeyword(models.Model):
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="search_keywords",
    )
    keyword = models.CharField(max_length=500)
    used = models.BooleanField(default=False)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "linkedin"
        unique_together = [("campaign", "keyword")]

    def __str__(self):
        return self.keyword


class ActionLog(models.Model):
    class ActionType(models.TextChoices):
        CONNECT = "connect", "Connect"
        FOLLOW_UP = "follow_up", "Follow Up"

    linkedin_profile = models.ForeignKey(
        LinkedInProfile,
        on_delete=models.CASCADE,
        related_name="action_logs",
    )
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="action_logs",
    )
    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "linkedin"
        indexes = [
            models.Index(fields=["linkedin_profile", "action_type", "created_at"]),
        ]

    def __str__(self):
        return f"{self.action_type} by {self.linkedin_profile} at {self.created_at}"


class TaskQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(status=Task.Status.PENDING).order_by("scheduled_at")

    def due(self):
        return self.pending().filter(scheduled_at__lte=timezone.now())

    def claim_next(self, operator: str | None = None) -> "Task | None":
        """Pop the next due Task, optionally filtered to the current operator.

        `operator` is the canonical handle of the LinkedIn account the
        daemon is logged in as. When supplied, follow_up Tasks are
        pre-filtered to those whose `payload.operator` matches (or is
        empty/missing, for legacy Tasks enqueued before the field
        existed). Other task types (connect / sweep_connections) are
        account-agnostic — they pass through regardless.

        Without this filter, a daemon logged in as Arian could pop a
        follow_up Task for one of Chuka's connections and try to send
        from the wrong account (Travis incident, 2026-05-12). The
        in-handler `lead_outbound_operators` guard catches it as a
        second line of defense.
        """
        from django.db.models import Q

        qs = self.due()
        if operator:
            not_follow_up = ~Q(task_type=Task.TaskType.FOLLOW_UP)
            mine_or_legacy = Q(task_type=Task.TaskType.FOLLOW_UP) & (
                Q(payload__operator=operator)
                | Q(payload__operator__isnull=True)
                | Q(payload__operator="")
            )
            qs = qs.filter(not_follow_up | mine_or_legacy)
        return qs.first()

    def seconds_to_next(self, operator: str | None = None) -> float | None:
        """Seconds until the next pending task (optionally operator-scoped).

        Mirrors `claim_next`'s filter so a daemon doesn't sleep waiting
        on a task it would never pop. Without the filter, a follow_up
        Task scheduled for another operator would dictate the sleep
        duration even though this daemon will skip it.
        """
        from django.db.models import Q

        qs = self.pending().only("scheduled_at", "task_type", "payload")
        if operator:
            not_follow_up = ~Q(task_type=Task.TaskType.FOLLOW_UP)
            mine_or_legacy = Q(task_type=Task.TaskType.FOLLOW_UP) & (
                Q(payload__operator=operator)
                | Q(payload__operator__isnull=True)
                | Q(payload__operator="")
            )
            qs = qs.filter(not_follow_up | mine_or_legacy)
        next_task = qs.first()
        if next_task is None:
            return None
        return max((next_task.scheduled_at - timezone.now()).total_seconds(), 0)


class Task(models.Model):
    class TaskType(models.TextChoices):
        CONNECT = "connect"
        CHECK_PENDING = "check_pending"
        FOLLOW_UP = "follow_up"
        SWEEP_CONNECTIONS = "sweep_connections"

    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        COMPLETED = "completed"
        FAILED = "failed"

    task_type = models.CharField(max_length=20, choices=TaskType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    scheduled_at = models.DateTimeField()
    payload = models.JSONField(default=dict)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = TaskQuerySet.as_manager()

    class Meta:
        app_label = "linkedin"
        indexes = [
            models.Index(fields=["status", "scheduled_at"]),
        ]

    def __str__(self):
        return f"{self.task_type} [{self.status}] scheduled={self.scheduled_at}"

    def mark_running(self):
        self.status = self.Status.RUNNING
        self.started_at = timezone.now()
        self.save(update_fields=["status", "started_at"])

    def mark_completed(self):
        self.status = self.Status.COMPLETED
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "completed_at"])

    def mark_failed(self, error: str):
        self.status = self.Status.FAILED
        self.error = error
        self.save(update_fields=["status", "error"])


class WorkflowRun(models.Model):
    """Audit + freshness signal for high-level workflows the operator runs.

    The followup workflow's Phase 0.5 staleness check queries this table to
    answer "when did Chuka last run backfill_messages?" and similar — and
    flags which upstream(s) the operator should re-run before drafting.

    Workflow names are free-form strings; canonical values today:
      - "data-sync"        Calendar + Gmail + Drive Gemini → DB + sheet
      - "followup"         Drafts → Followups tabs
      - "import-connections"  CSV → Lead/Deal/Message rows
      - "backfill-messages"   LinkedIn DM threads → Message rows

    `operator` is "Chuka" / "Arian" for per-account workflows, "" (empty)
    for whole-system workflows (followup). Free-form string so we don't
    need a foreign key to LinkedInProfile / User; callers just write the
    display name the session resolves to. The `(name, operator,
    -completed_at)` index makes the staleness lookup O(1) per workflow.

    `counts` holds whatever the workflow wants to surface for telemetry —
    e.g. {"leads_created": 6, "threads_persisted": 33}. Schemaless so each
    workflow controls its own keys without needing migrations.
    """

    name = models.CharField(max_length=64, db_index=True)
    operator = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
    )
    completed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    summary = models.TextField(blank=True, default="")
    counts = models.JSONField(blank=True, default=dict)

    class Meta:
        app_label = "linkedin"
        ordering = ["-completed_at"]
        indexes = [
            models.Index(fields=["name", "operator", "-completed_at"]),
        ]

    def __str__(self):
        op = f"({self.operator}) " if self.operator else ""
        return f"{self.name} {op}@ {self.completed_at:%Y-%m-%d %H:%M}"
