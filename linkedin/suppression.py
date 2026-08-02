"""Suppression checks for outbound outreach."""
from __future__ import annotations

from urllib.parse import urlparse

_LEGAL_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "sa", "sas", "bv", "ag",
}


def normalize_company_name(value: str) -> str:
    words = []
    cleaned = []
    for ch in (value or "").lower().replace("&", " and "):
        cleaned.append(ch if ch.isalnum() else " ")
    for word in "".join(cleaned).split():
        if word not in _LEGAL_SUFFIXES:
            words.append(word)
    return "".join(words)


def normalize_domain(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if "@" in raw and "://" not in raw:
        raw = raw.rsplit("@", 1)[-1]
    if "://" in raw:
        parsed = urlparse(raw)
        raw = parsed.netloc or parsed.path
    raw = raw.split("/", 1)[0].split(":", 1)[0].strip(".")
    if raw.startswith("www."):
        raw = raw[4:]
    return raw


def normalize_person_name(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def suppression_matches_lead(suppression, lead) -> bool:
    aliases = set(suppression.normalized_aliases or [])
    domain = normalize_domain(getattr(lead, "email", ""))

    if suppression.kind == suppression.Kind.COMPANY:
        if domain and suppression.domain and domain == suppression.domain:
            return True
        normalized_company = normalize_company_name(getattr(lead, "company_name", ""))
        return bool(
            normalized_company
            and normalized_company in ({suppression.normalized_value} | aliases)
        )

    full_name = " ".join(
        part for part in (
            getattr(lead, "first_name", ""),
            getattr(lead, "last_name", ""),
        )
        if part
    )
    normalized_person = normalize_person_name(full_name)
    email = (getattr(lead, "email", "") or "").strip().lower()
    linkedin_url = (getattr(lead, "linkedin_url", "") or "").strip()
    public_identifier = (getattr(lead, "public_identifier", "") or "").strip()
    return bool(
        (normalized_person and normalized_person in ({suppression.normalized_value} | aliases))
        or (suppression.email and email == suppression.email)
        or (suppression.linkedin_url and linkedin_url == suppression.linkedin_url)
        or (suppression.public_identifier and public_identifier == suppression.public_identifier)
    )


def lead_suppression_match(lead):
    """Return the matching active OutreachSuppression for a Lead, or None."""
    from linkedin.models import OutreachSuppression

    # Keep matching behavior identical across Postgres production and SQLite
    # tests. JSONField ``contains`` is not supported by SQLite, and suppression
    # lists are intentionally small enough to evaluate with the canonical
    # Python matcher.
    for suppression in OutreachSuppression.objects.filter(active=True):
        if suppression_matches_lead(suppression, lead):
            return suppression
    return None


def apply_suppression_to_existing_leads(suppression) -> dict[str, int]:
    """Disqualify matching leads and fail nonterminal Deals/Tasks."""
    if not suppression.active:
        return {"leads": 0, "deals": 0, "tasks": 0}

    from crm.models import ClosingReason, Deal, Lead
    from linkedin.enums import ProfileState
    from linkedin.models import Task

    matching_ids = [
        lead.pk
        for lead in Lead.objects.all().only(
            "id", "first_name", "last_name", "company_name",
            "email", "linkedin_url", "public_identifier",
        ).iterator()
        if suppression_matches_lead(suppression, lead)
    ]
    if not matching_ids:
        return {"leads": 0, "deals": 0, "tasks": 0}

    reason = f"Suppression: {suppression.value}"
    leads = Lead.objects.filter(pk__in=matching_ids).update(disqualified=True)
    active_states = [
        ProfileState.QUALIFIED,
        ProfileState.READY_TO_CONNECT,
        ProfileState.PENDING,
        ProfileState.CONNECTED,
    ]
    deals = Deal.objects.filter(lead_id__in=matching_ids, state__in=active_states).update(
        state=ProfileState.FAILED,
        closing_reason=ClosingReason.DISQUALIFIED,
        reason=reason,
    )
    public_ids = list(
        Lead.objects.filter(pk__in=matching_ids)
        .exclude(public_identifier="")
        .values_list("public_identifier", flat=True)
    )
    tasks = 0
    if public_ids:
        tasks = Task.objects.filter(
            status=Task.Status.PENDING,
            task_type=Task.TaskType.FOLLOW_UP,
            payload__public_id__in=public_ids,
        ).update(status=Task.Status.COMPLETED, error=reason)
    return {"leads": leads, "deals": deals, "tasks": tasks}
