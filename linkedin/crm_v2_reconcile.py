"""Conservatively reconcile CRM v2 account evidence into durable sales rows.

This service is the only write bridge between read-only
``ResolvedAccountEvidence`` and the canonical ``Account`` / ``Opportunity``
models.  It never creates actions and never sends anything.  Dry-runs execute
the exact same database path inside a rolled-back transaction, so their counts
match apply behavior without leaving rows behind.

Identity resolution fails closed.  An explicit Opportunity ID or an existing
exact Lead-to-Opportunity contact link is authoritative.  Without either, a
single business domain and an exact conservative account name may resolve an
Account only when they do not disagree or produce duplicates.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from crm.models import (
    Account,
    Lead,
    Meeting,
    MeetingNote,
    MeetingParticipant,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    OpportunityStageEvent,
    SalesOwner,
)
from crm.models.sales import normalize_account_name
from linkedin.crm_v2_evidence import ResolvedAccountEvidence, email_domain
from linkedin.crm_v2_policy import EvidenceTier


__all__ = (
    "ReconciliationChange",
    "ReconciliationIssue",
    "ReconciliationReport",
    "apply_reconciliation",
    "dry_run_reconciliation",
    "reconcile_resolved_account_evidence",
)


_AUTHORITATIVE_SOURCES = frozenset({
    Opportunity.Source.MANUAL,
    Opportunity.Source.SHEET,
})
_AUTOMATED_SOURCES = frozenset({
    Opportunity.Source.BOOTSTRAP,
    Opportunity.Source.SYSTEM,
})
_EXISTING_MANUAL_REASON = "existing_manual_opportunity"
_EXISTING_SHEET_REASON = "existing_sheet_opportunity"


@dataclass(frozen=True)
class ReconciliationChange:
    account_key: str
    kind: str
    detail: str = ""


@dataclass(frozen=True)
class ReconciliationIssue:
    account_key: str
    reason: str
    detail: str = ""


@dataclass
class ReconciliationReport:
    applied: bool
    evaluated_at: datetime
    evidence_rows: int = 0
    admitted_rows: int = 0
    people_only_rows: int = 0
    accounts_created: int = 0
    opportunities_created: int = 0
    opportunities_activated: int = 0
    opportunities_deactivated: int = 0
    contacts_linked: int = 0
    meetings_linked: int = 0
    meeting_notes_linked: int = 0
    owners_assigned: int = 0
    domains_populated: int = 0
    opportunities_unchanged: int = 0
    changes: list[ReconciliationChange] = field(default_factory=list)
    issues: list[ReconciliationIssue] = field(default_factory=list)

    @property
    def skipped_ambiguous(self) -> int:
        return len(self.issues)


def dry_run_reconciliation(
    evidence_rows: Iterable[ResolvedAccountEvidence],
    *,
    evaluated_at: datetime | None = None,
) -> ReconciliationReport:
    """Return the exact reconciliation report while rolling back all writes."""
    return reconcile_resolved_account_evidence(
        evidence_rows,
        apply=False,
        evaluated_at=evaluated_at,
    )


def apply_reconciliation(
    evidence_rows: Iterable[ResolvedAccountEvidence],
    *,
    evaluated_at: datetime | None = None,
) -> ReconciliationReport:
    """Atomically apply safe account/opportunity/contact reconciliation."""
    return reconcile_resolved_account_evidence(
        evidence_rows,
        apply=True,
        evaluated_at=evaluated_at,
    )


def reconcile_resolved_account_evidence(
    evidence_rows: Iterable[ResolvedAccountEvidence],
    *,
    apply: bool = False,
    evaluated_at: datetime | None = None,
) -> ReconciliationReport:
    """Reconcile resolved evidence without overwriting human sales state.

    Safe derived fields (admission metadata, a newer meaningful-activity time,
    and a previously blank unique business domain) may change.  Existing
    owner, stage, sales-motion step, value, probability, name, and nonblank
    domain are never overwritten.  Human/manual/Sheet Opportunities remain
    active even when channel evidence is currently weak.  Only automated
    bootstrap/system Opportunities are deactivated, never deleted or closed.
    """
    rows = tuple(evidence_rows)
    observed_at = evaluated_at or timezone.now()
    if timezone.is_naive(observed_at):
        raise ValueError("evaluated_at must be timezone-aware")

    report = ReconciliationReport(
        applied=apply,
        evaluated_at=observed_at,
        evidence_rows=len(rows),
        admitted_rows=sum(row.decision.admitted for row in rows),
        people_only_rows=sum(not row.decision.admitted for row in rows),
    )
    duplicate_keys = {
        key
        for key, count in Counter(_stable_key(row.account_key) for row in rows).items()
        if count > 1
    }

    with transaction.atomic():
        for row in rows:
            stable_key = _stable_key(row.account_key)
            if not stable_key:
                report.issues.append(ReconciliationIssue(
                    account_key=row.account_key,
                    reason="missing_account_key",
                ))
                continue
            if stable_key in duplicate_keys:
                report.issues.append(ReconciliationIssue(
                    account_key=row.account_key,
                    reason="duplicate_evidence_account_key",
                ))
                continue
            # A People-only row with no durable Opportunity anchor cannot
            # change sales state.  Avoid thousands of per-row Lead/account
            # queries while still validating its evidence identity above.
            if not row.decision.admitted and not (row.opportunity_id or "").strip():
                report.opportunities_unchanged += 1
                continue
            _reconcile_row(row, evaluated_at=observed_at, report=report)

        if not apply:
            transaction.set_rollback(True)
    return report


def _reconcile_row(
    row: ResolvedAccountEvidence,
    *,
    evaluated_at: datetime,
    report: ReconciliationReport,
) -> None:
    leads = list(
        Lead.objects.select_for_update()
        .filter(id__in=row.lead_ids)
        .order_by("id")
    )
    found_lead_ids = {lead.id for lead in leads}
    missing_lead_ids = sorted(set(row.lead_ids) - found_lead_ids)
    if missing_lead_ids:
        _issue(
            report,
            row,
            "missing_exact_lead_ids",
            ",".join(str(value) for value in missing_lead_ids),
        )
        return

    explicit_opportunity = _explicit_opportunity(row, report=report)
    if row.opportunity_id and explicit_opportunity is None:
        return

    linked_opportunity_ids = list(
        OpportunityContact.objects.filter(lead_id__in=found_lead_ids)
        .values_list("opportunity_id", flat=True)
        .distinct()
    ) if found_lead_ids else []
    linked_opportunities = list(
        Opportunity.objects.select_for_update()
        .filter(id__in=linked_opportunity_ids)
        .select_related("account")
        .order_by("id")
    )
    linked_account_ids = {item.account_id for item in linked_opportunities}
    discardable_legacy_conflict = (
        len(linked_account_ids) > 1
        and _is_discardable_legacy_automation_conflict(
            row,
            explicit_opportunity=explicit_opportunity,
            linked_opportunities=linked_opportunities,
        )
    )
    if len(linked_account_ids) > 1 and not discardable_legacy_conflict:
        _issue(
            report,
            row,
            "exact_leads_link_multiple_accounts",
            ",".join(sorted(str(value) for value in linked_account_ids)),
        )
        return
    if discardable_legacy_conflict:
        _quarantine_discardable_linked_automation(
            row,
            linked_opportunities=linked_opportunities,
            canonical_opportunity=explicit_opportunity,
            evaluated_at=evaluated_at,
            report=report,
        )
    if (
        explicit_opportunity is not None
        and linked_account_ids
        and explicit_opportunity.account_id not in linked_account_ids
    ):
        _issue(
            report,
            row,
            "opportunity_id_conflicts_with_exact_leads",
        )
        return

    account = explicit_opportunity.account if explicit_opportunity else None
    if account is None and linked_account_ids:
        account = Account.objects.select_for_update().get(pk=next(iter(linked_account_ids)))

    safe_domain = _single_business_domain(leads)
    evidence_owner = _evidence_owner(row, report=report)
    if row.owner and evidence_owner is None:
        return
    if account is None:
        issue_count = len(report.issues)
        account = _resolve_account_without_anchor(
            row,
            safe_domain=safe_domain,
            report=report,
        )
        if len(report.issues) > issue_count:
            return

    if account is None:
        if not row.decision.admitted:
            report.opportunities_unchanged += 1
            return
        account_name = (row.account_name or "").strip()
        if not normalize_account_name(account_name):
            _issue(report, row, "missing_account_name_for_creation")
            return
        account = Account.objects.create(name=account_name)
        report.accounts_created += 1
        report.changes.append(ReconciliationChange(
            row.account_key,
            "account_created",
            account_name,
        ))

    opportunity = (
        Opportunity.objects.select_for_update()
        .filter(account=account, motion_key="primary")
        .first()
    )
    if opportunity is None and not row.decision.admitted:
        report.opportunities_unchanged += 1
        return
    created_opportunity = opportunity is None
    if opportunity is None:
        source = _source_for_new_opportunity(row)
        opportunity = Opportunity(
            account=account,
            motion_key="primary",
            name=(row.account_name or account.name).strip(),
            source=source,
            owner=evidence_owner,
            # Evidence admits an account; it never advances a sales stage.
            stage=Opportunity.Stage.PROSPECTING,
            sales_motion_step=1,
            manual_pin=row.facts.manual_pin,
            active_account=True,
            admission_reason=row.decision.primary_reason_code.value,
            admission_reasons=[reason.value for reason in row.decision.reason_codes],
            admission_tier=row.decision.evidence_tier.value,
            admission_evaluated_at=evaluated_at,
            last_meaningful_activity_at=row.last_meaningful_touch,
        )
        opportunity._stage_event_source = source
        opportunity.save()
        report.opportunities_created += 1
        if evidence_owner is not None:
            report.owners_assigned += 1
        report.changes.append(ReconciliationChange(
            row.account_key,
            "opportunity_created",
            source,
        ))

    effective_active, primary_reason, reasons, tier = _effective_admission(
        row,
        opportunity=opportunity,
    )
    changed_fields: set[str] = set()

    # Exact, recent sender evidence may fill an unowned record, but it never
    # overrides an owner chosen by a human or a prior authoritative workflow.
    if (
        evidence_owner is not None
        and (
            opportunity.owner_id is None
            or (
                row.owner_is_override
                and opportunity.owner_id != evidence_owner.id
            )
        )
    ):
        opportunity.owner = evidence_owner
        changed_fields.add("owner")
        report.owners_assigned += 1
        report.changes.append(ReconciliationChange(
            row.account_key,
            "owner_assigned",
            evidence_owner.handle,
        ))

    if effective_active:
        was_active = opportunity.active_account
        if not was_active:
            opportunity.active_account = True
            changed_fields.add("active_account")
            report.opportunities_activated += 1
            report.changes.append(ReconciliationChange(
                row.account_key,
                "opportunity_activated",
                primary_reason,
            ))
        if opportunity.inactive_at is not None:
            opportunity.inactive_at = None
            changed_fields.add("inactive_at")
        if opportunity.inactive_reason:
            opportunity.inactive_reason = ""
            changed_fields.add("inactive_reason")
        if row.facts.manual_pin and not opportunity.manual_pin:
            opportunity.manual_pin = True
            changed_fields.add("manual_pin")
    elif opportunity.source in _AUTOMATED_SOURCES and not opportunity.manual_pin:
        if opportunity.active_account:
            opportunity.active_account = False
            changed_fields.add("active_account")
            report.opportunities_deactivated += 1
            report.changes.append(ReconciliationChange(
                row.account_key,
                "opportunity_deactivated",
                primary_reason,
            ))
        if opportunity.inactive_at is None:
            opportunity.inactive_at = evaluated_at
            changed_fields.add("inactive_at")
        if opportunity.inactive_reason != primary_reason:
            opportunity.inactive_reason = primary_reason
            changed_fields.add("inactive_reason")
    else:
        # Human-owned records are authoritative.  Preserve their current
        # active/terminal state rather than letting weak derived evidence undo
        # an operator decision.
        report.opportunities_unchanged += 1
        return

    derived_values = {
        "admission_reason": primary_reason,
        "admission_reasons": reasons,
        "admission_tier": tier,
        "admission_evaluated_at": evaluated_at,
    }
    for field_name, value in derived_values.items():
        if getattr(opportunity, field_name) != value:
            setattr(opportunity, field_name, value)
            changed_fields.add(field_name)
    if (
        row.last_meaningful_touch is not None
        and (
            opportunity.last_meaningful_activity_at is None
            or row.last_meaningful_touch > opportunity.last_meaningful_activity_at
        )
    ):
        opportunity.last_meaningful_activity_at = row.last_meaningful_touch
        changed_fields.add("last_meaningful_activity_at")

    if changed_fields:
        opportunity.save(update_fields=changed_fields | {"updated_at"})
    elif not created_opportunity:
        report.opportunities_unchanged += 1

    if not effective_active:
        return
    _populate_domain_when_safe(
        row,
        account=account,
        safe_domain=safe_domain,
        report=report,
    )
    for lead in leads:
        if OpportunityContact.objects.filter(
            opportunity=opportunity,
            lead=lead,
        ).exists():
            continue
        OpportunityContact.objects.create(
            opportunity=opportunity,
            lead=lead,
            role=OpportunityContact.Role.STAKEHOLDER,
        )
        report.contacts_linked += 1
        report.changes.append(ReconciliationChange(
            row.account_key,
            "contact_linked",
            str(lead.id),
        ))
    _link_verified_meeting_context(
        row,
        opportunity=opportunity,
        report=report,
    )


_VERIFIED_MEETING_MATCH_METHODS = frozenset({
    MeetingParticipant.MatchMethod.ATTENDEE_EMAIL,
    MeetingParticipant.MatchMethod.ATTENDEE_IDENTITY,
    MeetingParticipant.MatchMethod.MANUAL,
})


def _link_verified_meeting_context(
    row: ResolvedAccountEvidence,
    *,
    opportunity: Opportunity,
    report: ReconciliationReport,
) -> None:
    """Backfill opportunity context only from verified exact participants.

    Legacy-primary and account/title matches are deliberately insufficient:
    those historical matchers can attach a meeting to a namesake.  Existing
    links are never moved, and a meeting whose known contacts span accounts is
    left untouched for human review.
    """
    row_lead_ids = set(row.lead_ids)
    if not row_lead_ids:
        return

    candidate_meeting_ids = set(
        MeetingParticipant.objects.filter(
            lead_id__in=row_lead_ids,
            match_method__in=_VERIFIED_MEETING_MATCH_METHODS,
        ).values_list("meeting_id", flat=True)
    )
    if not candidate_meeting_ids:
        return

    meetings = list(
        Meeting.objects.select_for_update()
        .filter(pk__in=candidate_meeting_ids)
        .order_by("id")
    )
    verified_by_meeting: dict[int, set[int]] = {}
    for meeting_id, lead_id in MeetingParticipant.objects.filter(
        meeting_id__in=candidate_meeting_ids,
        match_method__in=_VERIFIED_MEETING_MATCH_METHODS,
    ).values_list("meeting_id", "lead_id"):
        verified_by_meeting.setdefault(meeting_id, set()).add(lead_id)

    context_lead_ids = {
        lead_id
        for meeting in meetings
        for lead_id in (
            verified_by_meeting.get(meeting.id, set()) | {meeting.lead_id}
        )
    }
    account_ids_by_lead: dict[int, set] = {}
    for lead_id, account_id in OpportunityContact.objects.filter(
        lead_id__in=context_lead_ids,
    ).values_list("lead_id", "opportunity__account_id"):
        account_ids_by_lead.setdefault(lead_id, set()).add(account_id)

    for meeting in meetings:
        verified_lead_ids = verified_by_meeting.get(meeting.id, set())
        if not verified_lead_ids & row_lead_ids:
            continue
        if meeting.opportunity_id not in {None, opportunity.id}:
            _issue(
                report,
                row,
                "meeting_linked_to_other_opportunity",
                str(meeting.id),
            )
            continue

        meeting_lead_ids = verified_lead_ids | {meeting.lead_id}
        linked_account_ids = {
            account_id
            for lead_id in meeting_lead_ids
            for account_id in account_ids_by_lead.get(lead_id, set())
        }
        if linked_account_ids - {opportunity.account_id}:
            _issue(
                report,
                row,
                "meeting_contacts_span_accounts",
                str(meeting.id),
            )
            continue

        if meeting.opportunity_id is None:
            meeting.opportunity = opportunity
            meeting.save(update_fields={"opportunity", "update_date"})
            report.meetings_linked += 1
            report.changes.append(ReconciliationChange(
                row.account_key,
                "meeting_linked",
                str(meeting.id),
            ))

        notes = list(
            MeetingNote.objects.select_for_update()
            .filter(
                meeting=meeting,
                match_status=MeetingNote.MatchStatus.MATCHED,
            )
            .order_by("id")
        )
        for note in notes:
            if note.opportunity_id not in {None, opportunity.id}:
                _issue(
                    report,
                    row,
                    "meeting_note_linked_to_other_opportunity",
                    str(note.id),
                )
                continue
            if note.opportunity_id is not None:
                continue
            note.opportunity = opportunity
            note.save(update_fields={"opportunity", "updated_at"})
            report.meeting_notes_linked += 1
            report.changes.append(ReconciliationChange(
                row.account_key,
                "meeting_note_linked",
                str(note.id),
            ))


def _explicit_opportunity(row, *, report):
    raw_id = (row.opportunity_id or "").strip()
    if not raw_id:
        return None
    try:
        opportunity_id = UUID(raw_id)
    except (TypeError, ValueError):
        _issue(report, row, "invalid_opportunity_id", raw_id)
        return None
    opportunity = (
        Opportunity.objects.select_for_update()
        .select_related("account")
        .filter(pk=opportunity_id)
        .first()
    )
    if opportunity is None:
        _issue(report, row, "opportunity_id_not_found", raw_id)
    return opportunity


def _evidence_owner(row, *, report):
    handle = (row.owner or "").strip()
    if not handle:
        return None
    owner = SalesOwner.objects.filter(
        normalized_handle=handle.casefold(),
        active=True,
    ).first()
    if owner is None:
        _issue(report, row, "unknown_or_inactive_owner", handle)
    return owner


def _resolve_account_without_anchor(row, *, safe_domain, report):
    domain_matches = []
    if safe_domain:
        domain_matches = list(
            Account.objects.select_for_update()
            .filter(domain=safe_domain)
            .order_by("id")
        )
        if len(domain_matches) > 1:
            _issue(report, row, "duplicate_account_domain", safe_domain)
            return None

    normalized_name = normalize_account_name(row.account_name)
    name_matches = list(
        Account.objects.select_for_update()
        .filter(normalized_name=normalized_name)
        .order_by("id")
    ) if normalized_name else []
    if domain_matches:
        domain_account = domain_matches[0]
        if len(name_matches) == 1 and name_matches[0].id != domain_account.id:
            _issue(
                report,
                row,
                "domain_and_name_resolve_different_accounts",
                f"{domain_account.id},{name_matches[0].id}",
            )
            return None
        # A unique exact business domain is stronger than a duplicated display
        # name.  The latter is expected for subsidiaries and legal-name rows.
        return domain_account
    if len(name_matches) > 1:
        _issue(report, row, "duplicate_normalized_account_name", normalized_name)
        return None
    if len(name_matches) == 1:
        return name_matches[0]
    return None


def _is_discardable_legacy_automation_conflict(
    row,
    *,
    explicit_opportunity,
    linked_opportunities,
) -> bool:
    """Allow only inert legacy automation duplicates to remain unresolved.

    Old bulk CRM generation could link the same exact Lead to multiple
    bootstrap/system Accounts.  When the evidence row is not admitted, an
    explicit canonical Opportunity is present, and *every* linked Opportunity
    is either inactive or still carries the unevaluated rollout default and is
    untouched by a human, the duplicate links cannot affect either v2 view.
    Continuing against the explicit anchor is safer than blocking the entire
    refresh on dormant generated history.  ``active_account=True`` is not an
    active v2 decision until ``admission_evaluated_at`` has been populated; the
    migration deliberately kept old rows visible until their first refresh.

    Any admitted, evaluated-active, pinned, human-revised, terminal,
    human-staged, or current human-action-bearing conflict still fails closed.
    """
    if row.decision.admitted or explicit_opportunity is None:
        return False
    linked = tuple(linked_opportunities)
    if not linked or explicit_opportunity.account_id not in {
        opportunity.account_id for opportunity in linked
    }:
        return False
    if any(
        (
            opportunity.active_account
            and opportunity.admission_evaluated_at is not None
        )
        or opportunity.source not in _AUTOMATED_SOURCES
        or opportunity.manual_pin
        or opportunity.human_revision > 0
        or opportunity.stage in {
            Opportunity.Stage.CLOSED_WON,
            Opportunity.Stage.CLOSED_LOST,
        }
        for opportunity in linked
    ):
        return False

    opportunity_ids = [opportunity.id for opportunity in linked]
    if OpportunityStageEvent.objects.filter(
        opportunity_id__in=opportunity_ids,
        source__in=_AUTHORITATIVE_SOURCES,
    ).exists():
        return False
    for human_revision, idempotency_key in OpportunityAction.objects.filter(
        opportunity_id__in=opportunity_ids,
        status__in=(
            OpportunityAction.Status.OPEN,
            OpportunityAction.Status.WAITING,
        ),
    ).values_list("human_revision", "idempotency_key"):
        key = (idempotency_key or "").strip()
        if human_revision > 0 or not key.startswith(("system:", "v2:")):
            return False
    return True


def _quarantine_discardable_linked_automation(
    row,
    *,
    linked_opportunities,
    canonical_opportunity,
    evaluated_at,
    report,
) -> None:
    """Deactivate inert noncanonical duplicates without merging identities."""
    primary_reason = row.decision.primary_reason_code.value
    reasons = [reason.value for reason in row.decision.reason_codes]
    tier = row.decision.evidence_tier.value
    for opportunity in linked_opportunities:
        if opportunity.id == canonical_opportunity.id:
            continue
        was_active = opportunity.active_account
        first_evaluation = opportunity.admission_evaluated_at is None
        changed_fields: set[str] = set()
        derived_values = {
            "active_account": False,
            "admission_reason": primary_reason,
            "admission_reasons": reasons,
            "admission_tier": tier,
            "admission_evaluated_at": evaluated_at,
            "inactive_reason": primary_reason,
        }
        for field_name, value in derived_values.items():
            if getattr(opportunity, field_name) != value:
                setattr(opportunity, field_name, value)
                changed_fields.add(field_name)
        if opportunity.inactive_at is None:
            opportunity.inactive_at = evaluated_at
            changed_fields.add("inactive_at")
        if changed_fields:
            opportunity.save(update_fields=changed_fields | {"updated_at"})
        if was_active:
            report.opportunities_deactivated += 1
            report.changes.append(ReconciliationChange(
                row.account_key,
                "legacy_duplicate_deactivated",
                opportunity.source,
            ))
        elif first_evaluation:
            report.changes.append(ReconciliationChange(
                row.account_key,
                "legacy_duplicate_quarantined",
                opportunity.source,
            ))


def _source_for_new_opportunity(row) -> str:
    if row.facts.manual_pin:
        return Opportunity.Source.MANUAL
    if row.facts.sales_motion_active:
        return Opportunity.Source.SHEET
    return Opportunity.Source.BOOTSTRAP


def _effective_admission(row, *, opportunity):
    if row.decision.admitted:
        return (
            True,
            row.decision.primary_reason_code.value,
            [reason.value for reason in row.decision.reason_codes],
            row.decision.evidence_tier.value,
        )
    if opportunity.manual_pin:
        return True, "manual_pin", ["manual_pin"], EvidenceTier.AUTHORITATIVE.value
    if opportunity.source in _AUTHORITATIVE_SOURCES:
        reason = (
            _EXISTING_SHEET_REASON
            if opportunity.source == Opportunity.Source.SHEET
            else _EXISTING_MANUAL_REASON
        )
        diagnostics = [item.value for item in row.decision.reason_codes]
        return True, reason, [reason, *diagnostics], EvidenceTier.AUTHORITATIVE.value
    return (
        False,
        row.decision.primary_reason_code.value,
        [reason.value for reason in row.decision.reason_codes],
        row.decision.evidence_tier.value,
    )


def _single_business_domain(leads) -> str:
    domains = {domain for lead in leads if (domain := email_domain(lead.email))}
    return next(iter(domains)) if len(domains) == 1 else ""


def _populate_domain_when_safe(row, *, account, safe_domain, report) -> None:
    if not safe_domain or account.domain:
        return
    conflict = Account.objects.filter(domain=safe_domain).exclude(pk=account.pk).exists()
    if conflict:
        report.issues.append(ReconciliationIssue(
            account_key=row.account_key,
            reason="business_domain_already_used",
            detail=safe_domain,
        ))
        return
    account.domain = safe_domain
    account.save(update_fields={"domain", "updated_at"})
    report.domains_populated += 1
    report.changes.append(ReconciliationChange(
        row.account_key,
        "account_domain_populated",
        safe_domain,
    ))


def _stable_key(value: str) -> str:
    return (value or "").strip().casefold()


def _issue(report, row, reason: str, detail: str = "") -> None:
    report.issues.append(ReconciliationIssue(
        account_key=row.account_key,
        reason=reason,
        detail=detail,
    ))
