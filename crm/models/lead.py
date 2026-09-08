import numpy as np
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class LeadRoleTag(models.TextChoices):
    """Operator-reviewed normalized role classifications."""

    FOUNDER_CEO = "Founder/CEO", _("Founder/CEO")
    CFO_FINANCE = "CFO/Finance", _("CFO/Finance")
    COO_OPERATIONS = "COO/Operations", _("COO/Operations")
    CRO_REVENUE = "CRO/Revenue", _("CRO/Revenue")
    PRODUCT_EXECUTIVE = "Product Executive", _("Product Executive")
    TECHNOLOGY_ENGINEERING_EXECUTIVE = (
        "Technology/Engineering Executive",
        _("Technology/Engineering Executive"),
    )
    CIO_INTERNAL_IT_EXECUTIVE = (
        "CIO/Internal IT Executive",
        _("CIO/Internal IT Executive"),
    )
    SECURITY_TRUST_EXECUTIVE = (
        "Security/Trust Executive",
        _("Security/Trust Executive"),
    )
    PRODUCT_ENGINEERING_N1 = "Product/Engineering N-1", _("Product/Engineering N-1")
    SECURITY_COMPLIANCE_N1 = (
        "Security/Compliance N-1",
        _("Security/Compliance N-1"),
    )
    COMPLIANCE_RISK_PRIVACY_EXECUTIVE = (
        "Compliance/Risk/Privacy Executive",
        _("Compliance/Risk/Privacy Executive"),
    )
    FEDRAMP_OWNER_OPERATOR = (
        "FedRAMP Owner/Operator",
        _("FedRAMP Owner/Operator"),
    )
    GRC_MANAGER_LEAD = "GRC Manager/Lead", _("GRC Manager/Lead")
    GRC_ENGINEER = "GRC Engineer", _("GRC Engineer")
    GRC_ANALYST_PRACTITIONER = (
        "GRC Analyst/Practitioner",
        _("GRC Analyst/Practitioner"),
    )
    FEDERAL_PUBLIC_SECTOR_EXECUTIVE = (
        "Federal/Public Sector Executive",
        _("Federal/Public Sector Executive"),
    )
    FEDERAL_SALES_BD_IC = "Federal Sales/BD IC", _("Federal Sales/BD IC")
    FEDERAL_SOLUTIONS_ENGINEER_ARCHITECT = (
        "Federal Solutions Engineer/Architect",
        _("Federal Solutions Engineer/Architect"),
    )
    PUBLIC_SECTOR_PARTNERSHIPS_ALLIANCES_CS = (
        "Public Sector Partnerships/Alliances/CS",
        _("Public Sector Partnerships/Alliances/CS"),
    )
    FIELD_CTO_CISO_TECHNICAL_EVANGELIST = (
        "Field CTO/CISO/Technical Evangelist",
        _("Field CTO/CISO/Technical Evangelist"),
    )


LEAD_ROLE_TAG_VALUES = tuple(LeadRoleTag.values)
_LEAD_ROLE_TAG_BY_CASEFOLD = {value.casefold(): value for value in LEAD_ROLE_TAG_VALUES}


def normalize_lead_role_tag(value: object) -> str:
    """Return one canonical role tag, or raise for an unknown nonblank value."""

    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        return ""
    canonical = _LEAD_ROLE_TAG_BY_CASEFOLD.get(cleaned.casefold())
    if canonical is None:
        raise ValueError(
            f"unknown Role Tag {cleaned!r}; expected one of "
            f"{list(LEAD_ROLE_TAG_VALUES)!r}"
        )
    return canonical


class Lead(models.Model):
    RoleTag = LeadRoleTag

    class Meta:
        verbose_name = _("Lead")
        verbose_name_plural = _("Leads")
        constraints = [
            models.UniqueConstraint(
                fields=["linkedin_url"],
                condition=~models.Q(linkedin_url=""),
                name="unique_nonblank_lead_linkedin_url",
            ),
            models.CheckConstraint(
                condition=models.Q(role_tag="")
                | models.Q(role_tag__in=LEAD_ROLE_TAG_VALUES),
                name="lead_role_tag_is_canonical",
            ),
        ]

    first_name = models.CharField(max_length=100, blank=True, default="")
    last_name = models.CharField(max_length=100, blank=True, default="")
    company_name = models.CharField(max_length=200, blank=True, default="")
    # CRM contacts may originate in Gmail or a meeting before LinkedIn is
    # known.  Only real LinkedIn identities are unique; many email-first
    # contacts legitimately carry the empty value.
    linkedin_url = models.URLField(max_length=200, blank=True, default="", db_index=True)
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
    # Also holds an explicitly selected shared-program audience key. Match
    # CampaignMessageEnrollment.audience_key's limit; never derive it from role.
    icp = models.CharField(max_length=160, blank=True, default="", db_index=True)
    # Human-reviewed normalized role classification. The raw LinkedIn title
    # remains separate in the operator-maintained People sheet.
    role_tag = models.CharField(
        max_length=64,
        choices=LeadRoleTag.choices,
        blank=True,
        default="",
        db_default="",
        db_index=True,
    )
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
