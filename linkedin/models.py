# linkedin/models.py
from __future__ import annotations

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from linkedin.conf import (
    ACTIVE_TIMEZONE,
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


def active_local_date():
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    return timezone.localtime(timezone=tz).date()


def active_day_start():
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)
    return timezone.make_aware(
        now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None),
        timezone=tz,
    )


def active_week_start():
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=None,
    )
    return timezone.make_aware(monday, timezone=tz)


class Campaign(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DISABLED = "disabled", "Disabled"
        FINISHED = "finished", "Finished"

    name = models.CharField(max_length=200, unique=True)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="campaigns",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )
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


class OutreachSuppression(models.Model):
    class Kind(models.TextChoices):
        COMPANY = "company", "Company"
        LEAD = "lead", "Lead"

    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    value = models.CharField(max_length=200)
    normalized_value = models.CharField(max_length=200, editable=False, db_index=True)
    aliases = models.JSONField(default=list, blank=True)
    normalized_aliases = models.JSONField(default=list, blank=True, editable=False)
    domain = models.CharField(max_length=200, blank=True, default="", db_index=True)
    email = models.EmailField(max_length=200, blank=True, default="", db_index=True)
    linkedin_url = models.URLField(max_length=500, blank=True, default="", db_index=True)
    public_identifier = models.CharField(max_length=200, blank=True, default="", db_index=True)
    reason = models.TextField(blank=True, default="")
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        from linkedin.suppression import (
            normalize_company_name,
            normalize_domain,
            normalize_person_name,
        )

        normalizer = (
            normalize_company_name
            if self.kind == self.Kind.COMPANY
            else normalize_person_name
        )
        self.normalized_value = normalizer(self.value)
        self.normalized_aliases = [
            norm for alias in (self.aliases or [])
            if (norm := normalizer(str(alias)))
        ]
        self.domain = normalize_domain(self.domain)
        self.email = (self.email or "").strip().lower()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_kind_display()}: {self.value}"

    class Meta:
        app_label = "linkedin"
        indexes = [
            models.Index(
                fields=["active", "kind", "normalized_value"],
                name="linkedin_ou_active_1d460f_idx",
            ),
            models.Index(fields=["active", "domain"], name="linkedin_ou_active_d26b9b_idx"),
            models.Index(fields=["active", "email"], name="linkedin_ou_active_e27ce4_idx"),
            models.Index(
                fields=["active", "public_identifier"],
                name="linkedin_ou_active_5c24e8_idx",
            ),
        ]


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
        if exhausted_date is not None and exhausted_date != active_local_date():
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
        self._exhausted[action_type] = active_local_date()
        logger.warning("Rate limit: %s externally exhausted for today", action_type)

    def _daily_count(self, action_type: str) -> int:
        return ActionLog.objects.filter(
            linkedin_profile=self, action_type=action_type,
            created_at__gte=active_day_start(),
        ).count()

    def _total_daily_count(self) -> int:
        return ActionLog.objects.filter(
            linkedin_profile=self,
            created_at__gte=active_day_start(),
        ).count()

    def _weekly_count(self, action_type: str) -> int:
        return ActionLog.objects.filter(
            linkedin_profile=self, action_type=action_type,
            created_at__gte=active_week_start(),
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


class ConnectIssueLog(models.Model):
    class IssueType(models.TextChoices):
        CONNECT_BUTTON_MISSING = "connect_button_missing", "Connect Button Missing"
        MORE_CONNECT_NO_SURFACE = "more_connect_no_surface", "More Connect No Surface"
        NOTE_UI_MISSING = "note_ui_missing", "Note UI Missing"
        NOTE_TEXTAREA_MISSING = "note_textarea_missing", "Note Textarea Missing"
        SEND_BUTTON_MISSING = "send_button_missing", "Send Button Missing"
        SKIP_PROFILE = "skip_profile", "Skip Profile"

    linkedin_profile = models.ForeignKey(
        LinkedInProfile,
        on_delete=models.CASCADE,
        related_name="connect_issue_logs",
    )
    campaign = models.ForeignKey(
        Campaign,
        on_delete=models.CASCADE,
        related_name="connect_issue_logs",
        null=True,
        blank=True,
    )
    public_id = models.CharField(max_length=200, db_index=True)
    profile_url = models.URLField(max_length=500, blank=True, default="")
    issue_type = models.CharField(max_length=40, choices=IssueType.choices)
    reason = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "linkedin"
        indexes = [
            models.Index(fields=["linkedin_profile", "issue_type", "created_at"]),
            models.Index(fields=["public_id", "created_at"]),
        ]

    def __str__(self):
        return f"{self.issue_type} for {self.public_id} at {self.created_at}"


def log_connect_issue(
    *,
    linkedin_profile: LinkedInProfile,
    campaign: Campaign | None,
    public_id: str,
    profile_url: str = "",
    issue_type: str,
    reason: str = "",
    metadata: dict | None = None,
) -> None:
    """Persist a queryable connect skip/failure event for later manual cleanup."""
    ConnectIssueLog.objects.create(
        linkedin_profile=linkedin_profile,
        campaign=campaign,
        public_id=public_id,
        profile_url=profile_url,
        issue_type=issue_type,
        reason=reason,
        metadata=metadata or {},
    )


def _operator_scope_q(operator: str, campaign_ids: "list[int] | None"):
    """Q filter restricting the task queue to work a daemon owns.

    Every per-account task is scoped so a daemon can only claim work for
    the LinkedIn account it is logged in as. This closed a real leak:
    between Apr–May 2026 a daemon logged in as Arian sent 418 connection
    requests and 32 follow-up DMs for Chuka's campaign, from the wrong
    account.

    - follow_up / manual_reply: claimable when `payload.operator` matches.
    - connect: claimable only when `payload.campaign_id` is one of this
      daemon's campaigns — the connection request goes out from the
      account that owns the campaign.
    - sweep_connections: account-agnostic; its handler only touches the
      claiming daemon's own campaigns regardless of which task it pops.
    """
    from django.db.models import Q

    owned = list(campaign_ids or [])
    in_owned = Q(payload__campaign_id__in=owned)
    mine_followup = Q(task_type=Task.TaskType.FOLLOW_UP) & Q(payload__operator=operator)
    mine_manual = Q(task_type=Task.TaskType.MANUAL_REPLY) & Q(payload__operator=operator)
    mine_connect = Q(task_type=Task.TaskType.CONNECT) & in_owned
    account_agnostic = ~Q(task_type__in=Task.linked_account_scoped_task_types())
    return mine_followup | mine_manual | mine_connect | account_agnostic


class TaskQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(status=Task.Status.PENDING).order_by("scheduled_at")

    def due(self):
        return self.pending().filter(scheduled_at__lte=timezone.now())

    def claim_next(
        self,
        operator: str | None = None,
        campaign_ids: "list[int] | None" = None,
        task_types: "list[str] | set[str] | tuple[str, ...] | None" = None,
    ) -> "Task | None":
        """Pop the next due Task, scoped to the work this daemon owns.

        `operator` is the canonical handle of the LinkedIn account the
        daemon is logged in as; `campaign_ids` are the pks of the
        campaigns that account owns. When `operator` is supplied:

          - follow_up/manual_reply Tasks are filtered to those whose
            `payload.operator` matches;
          - connect Tasks are filtered to those whose
            `payload.campaign_id` is one of `campaign_ids` — a connection
            request must go out from the account that owns the campaign;
          - sweep_connections passes through (account-agnostic).

        Without this, a daemon logged in as Arian could pop a follow_up
        Task for one of Chuka's connections (Travis incident,
        2026-05-12), or a daemon logged in as Chuka could pop a connect
        Task for Arian's campaign and invite from the wrong account
        (2026-05-19). See `_operator_scope_q`.
        """
        qs = self.due().exclude(task_type__in=Task.non_linkedin_outbound_task_types())
        if task_types is not None:
            qs = qs.filter(task_type__in=list(task_types))
        if operator:
            qs = qs.filter(_operator_scope_q(operator, campaign_ids))
        manual = qs.filter(task_type=Task.TaskType.MANUAL_REPLY).first()
        if manual is not None:
            return manual
        return qs.first()

    def seconds_to_next(
        self,
        operator: str | None = None,
        campaign_ids: "list[int] | None" = None,
        task_types: "list[str] | set[str] | tuple[str, ...] | None" = None,
    ) -> float | None:
        """Seconds until the next pending task (optionally operator-scoped).

        Mirrors `claim_next`'s filter so a daemon doesn't sleep waiting
        on a task it would never pop — a follow_up for another operator,
        or a connect for a campaign this daemon's account doesn't own.
        """
        qs = self.pending().exclude(task_type__in=Task.non_linkedin_outbound_task_types()).only(
            "scheduled_at", "task_type", "payload",
        )
        if task_types is not None:
            qs = qs.filter(task_type__in=list(task_types))
        if operator:
            qs = qs.filter(_operator_scope_q(operator, campaign_ids))
        next_task = qs.first()
        if next_task is None:
            return None
        return max((next_task.scheduled_at - timezone.now()).total_seconds(), 0)

    def next_enrichment(self) -> "Task | None":
        """The next due enrichment task — the EnrichmentWorker's claim query.

        Separate from `claim_next` (which excludes enrichment tasks) so the
        outbound task loop and the single enrichment worker thread never compete
        for the same row. NOTE: this is a plain ordered read, not a locking
        claim — safe only because exactly one worker thread calls it.
        """
        return self.due().filter(
            task_type__in=[
                Task.TaskType.ENRICH_PHONE,
                Task.TaskType.ENRICH_EMAIL,
            ],
        ).first()

    def next_gmail(self) -> "Task | None":
        """The next due browserless Gmail follow-up task."""
        return self.due().filter(task_type=Task.TaskType.GMAIL_FOLLOW_UP).first()


class Task(models.Model):
    class TaskType(models.TextChoices):
        CONNECT = "connect"
        CHECK_PENDING = "check_pending"
        FOLLOW_UP = "follow_up"
        SWEEP_CONNECTIONS = "sweep_connections"
        ENRICH_PHONE = "enrich_phone"
        ENRICH_EMAIL = "enrich_email"
        GMAIL_FOLLOW_UP = "gmail_follow_up"
        MANUAL_REPLY = "manual_reply"

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

    @classmethod
    def linked_account_scoped_task_types(cls) -> list[str]:
        return [
            cls.TaskType.FOLLOW_UP,
            cls.TaskType.CONNECT,
            cls.TaskType.MANUAL_REPLY,
        ]

    @classmethod
    def non_linkedin_outbound_task_types(cls) -> list[str]:
        return [
            cls.TaskType.ENRICH_PHONE,
            cls.TaskType.ENRICH_EMAIL,
            cls.TaskType.GMAIL_FOLLOW_UP,
        ]

    class Meta:
        app_label = "linkedin"
        indexes = [
            models.Index(fields=["status", "scheduled_at"]),
        ]

    def __str__(self):
        return f"{self.task_type} [{self.status}] scheduled={self.scheduled_at}"

    def clean(self):
        super().clean()
        payload = self.payload or {}
        errors: list[str] = []

        if (
            self.status in {self.Status.PENDING, self.Status.RUNNING}
            and self.task_type == self.TaskType.CONNECT
            and "campaign_id" not in payload
        ):
            errors.append("connect tasks require payload.campaign_id")

        if (
            self.status in {self.Status.PENDING, self.Status.RUNNING}
            and self.task_type == self.TaskType.FOLLOW_UP
        ):
            if "campaign_id" not in payload:
                errors.append("follow_up tasks require payload.campaign_id")
            if not payload.get("public_id"):
                errors.append("follow_up tasks require payload.public_id")
            if not payload.get("operator"):
                errors.append("follow_up tasks require non-empty payload.operator")

        if (
            self.status in {self.Status.PENDING, self.Status.RUNNING}
            and self.task_type == self.TaskType.MANUAL_REPLY
        ):
            if not payload.get("lead_id"):
                errors.append("manual_reply tasks require payload.lead_id")
            if not payload.get("operator"):
                errors.append("manual_reply tasks require non-empty payload.operator")
            if not (payload.get("message") or "").strip():
                errors.append("manual_reply tasks require non-empty payload.message")

        if (
            self.status in {self.Status.PENDING, self.Status.RUNNING}
            and self.task_type == self.TaskType.ENRICH_EMAIL
        ):
            if not payload.get("lead_id"):
                errors.append("enrich_email tasks require payload.lead_id")
            if not payload.get("operator"):
                errors.append("enrich_email tasks require non-empty payload.operator")

        if (
            self.status in {self.Status.PENDING, self.Status.RUNNING}
            and self.task_type == self.TaskType.GMAIL_FOLLOW_UP
        ):
            if not payload.get("lead_id"):
                errors.append("gmail_follow_up tasks require payload.lead_id")
            if not payload.get("operator"):
                errors.append("gmail_follow_up tasks require non-empty payload.operator")
            if payload.get("step_index") is None:
                errors.append("gmail_follow_up tasks require payload.step_index")

        if errors:
            raise ValidationError({"payload": errors})

    def save(self, *args, **kwargs):
        self.full_clean(exclude=["payload"])
        return super().save(*args, **kwargs)

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


class DaemonHeartbeat(models.Model):
    """One row per daemon node, keyed by sender (the resolved operator
    handle — "Arian" / "Chuka").

    Peer-node liveness monitoring: each daemon's `NodeMonitor` thread
    stamps `last_alive` every `MONITOR_INTERVAL_SECONDS` and reads every
    other node's row. A node whose `last_alive` is older than
    `PEER_STALE_MINUTES` is considered down, and a peer posts a Slack
    alert. `down_alerted_at` is the claim+cooldown marker: the peer that
    wins the atomic UPDATE posts (so N peers don't all alert), and it is
    re-claimable only after `DEGRADED_REALERT_HOURS`.

    `last_alive = NULL` means "intentionally stopped" — a daemon clears
    its own row on a clean exit (empty queue) so peers don't false-alarm.
    Revival re-stamps `last_alive` and clears `down_alerted_at`.
    """

    sender = models.CharField(max_length=100, unique=True)
    last_alive = models.DateTimeField(null=True, blank=True)
    down_alerted_at = models.DateTimeField(null=True, blank=True)
    activity_alerted_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "linkedin"

    def __str__(self):
        state = self.last_alive.isoformat() if self.last_alive else "stopped"
        return f"{self.sender} — {state}"
