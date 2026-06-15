import numpy as np
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Lead(models.Model):
    class Meta:
        verbose_name = _("Lead")
        verbose_name_plural = _("Leads")

    first_name = models.CharField(max_length=100, blank=True, default="")
    last_name = models.CharField(max_length=100, blank=True, default="")
    company_name = models.CharField(max_length=200, blank=True, default="")
    linkedin_url = models.URLField(max_length=200, blank=True, default="", unique=True)
    email = models.EmailField(max_length=200, blank=True, default="", db_index=True)
    # Phone enrichment — a lead can carry multiple numbers, one per provider
    # that returned a hit. Each entry: {"number", "provider", "found_at"}.
    # See linkedin/enrichment/ and linkedin/tasks/enrich_phone.py.
    phones = models.JSONField(default=list, blank=True)
    # Provider names that returned a definitive result (FOUND or NOT_FOUND)
    # for this lead — used to skip re-running a provider that already
    # answered. API_FAILURE is not recorded here, so it stays retryable.
    phone_providers_tried = models.JSONField(default=list, blank=True)
    # Email enrichment provider names that returned a definitive result
    # (FOUND or NOT_FOUND). Kept separate from phone_providers_tried so a
    # phone lookup never suppresses an email lookup, or vice versa.
    email_providers_tried = models.JSONField(default=list, blank=True)
    public_identifier = models.CharField(max_length=200, blank=True, default="")
    description = models.TextField(blank=True, default="")
    embedding = models.BinaryField(null=True, blank=True)
    # Canonical ICP bucket this lead sits in for template routing — both
    # the connect-note picker and the follow-up template path read it.
    # Populated at import (CSV `ICP` column via `add_seeds`) or at first
    # scrape (lazy backfill via `linkedin.icp_outbound.resolve_icp`).
    # See `linkedin.notifications.sheets.LEAD_ICP_BUCKETS` for the vocab.
    icp = models.CharField(max_length=64, blank=True, default="", db_index=True)
    disqualified = models.BooleanField(default=False)
    creation_date = models.DateTimeField(default=timezone.now)
    update_date = models.DateTimeField(auto_now=True)

    def __str__(self):
        name = f"{self.first_name} {self.last_name}".strip()
        if self.disqualified:
            name = f"({_('Disqualified')}) {name}"
        if self.company_name:
            return f"{name}, {self.company_name}"
        return name or self.public_identifier or self.linkedin_url

    @property
    def phone_numbers(self) -> list[str]:
        """Just the number strings from `phones`, in discovery order."""
        return [e["number"] for e in self.phones if e.get("number")]

    @property
    def full_name(self):
        name = f"{self.first_name} {self.last_name}".strip()
        if self.disqualified:
            name = f"({_('Disqualified')}) {name}"
        return name

    @property
    def embedding_array(self) -> np.ndarray | None:
        """384-dim float32 numpy array from stored bytes, or None."""
        if self.embedding is None:
            return None
        return np.frombuffer(bytes(self.embedding), dtype=np.float32).copy()

    @embedding_array.setter
    def embedding_array(self, arr: np.ndarray):
        self.embedding = np.asarray(arr, dtype=np.float32).tobytes()

    @classmethod
    def get_labeled_arrays(cls, campaign) -> tuple[np.ndarray, np.ndarray]:
        """Labeled embeddings for a campaign as (X, y) numpy arrays for warm start.

        Labels are derived from Deal state and closing_reason:
        - label=1: Deals at any non-FAILED state (QUALIFIED and beyond)
        - label=0: FAILED Deals with closing_reason "Disqualified" (LLM rejection)
        - Skipped: FAILED Deals with other closing reasons (operational failures)
        """
        from crm.models import ClosingReason
        from crm.models.deal import Deal
        from linkedin.enums import ProfileState

        deals = Deal.objects.filter(
            campaign=campaign, lead_id__isnull=False,
        ).values_list("lead_id", "state", "closing_reason")

        label_by_lead: dict[int, int] = {}
        for lid, state, cr in deals:
            if state == ProfileState.FAILED:
                if cr == ClosingReason.DISQUALIFIED:
                    label_by_lead[lid] = 0
            else:
                label_by_lead[lid] = 1

        if not label_by_lead:
            return np.empty((0, 384), dtype=np.float32), np.empty(0, dtype=np.int32)

        leads_with_emb = dict(
            cls.objects.filter(pk__in=label_by_lead, embedding__isnull=False)
            .values_list("pk", "embedding")
        )

        X_list, y_list = [], []
        for lid, label in label_by_lead.items():
            emb = leads_with_emb.get(lid)
            if emb is None:
                continue
            X_list.append(np.frombuffer(bytes(emb), dtype=np.float32))
            y_list.append(label)

        if not X_list:
            return np.empty((0, 384), dtype=np.float32), np.empty(0, dtype=np.int32)

        return np.array(X_list, dtype=np.float32), np.array(y_list, dtype=np.int32)
