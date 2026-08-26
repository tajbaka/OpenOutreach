"""Conservative repair of identity-invalid synthetic Gmail-note meetings."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from django.db import transaction

from crm.models import (
    Lead,
    Meeting,
    MeetingNote,
    MeetingParticipant,
    Opportunity,
    OpportunityAction,
)
from gmail.data_sync import (
    _original_gmail_note_title,
    _unique_lead_for_note_title,
    gmail_note_meeting_identity_is_valid,
)


@dataclass
class MeetingIdentityRepairReport:
    applied: bool
    inspected: int = 0
    invalid: int = 0
    uniquely_resolved: int = 0
    repaired: int = 0
    unchanged: int = 0
    unresolved: int = 0
    blocked: int = 0
    issue_reasons: dict[str, int] = field(default_factory=dict)

    def issue(self, reason: str) -> None:
        self.issue_reasons[reason] = self.issue_reasons.get(reason, 0) + 1

    def counts(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "inspected": self.inspected,
            "invalid": self.invalid,
            "uniquely_resolved": self.uniquely_resolved,
            "repaired": self.repaired,
            "unchanged": self.unchanged,
            "unresolved": self.unresolved,
            "blocked": self.blocked,
            "issue_reasons": dict(sorted(self.issue_reasons.items())),
        }


def repair_gmail_note_meeting_identities(
    *,
    apply: bool,
    meeting_ids: Iterable[int] = (),
) -> MeetingIdentityRepairReport:
    """Repair only uniquely re-identifiable, untouched synthetic meetings.

    Ambiguous rows remain unchanged.  Human-managed opportunities, current
    human actions, and already curated pipeline cards block reassociation.  No
    meeting or note is deleted; the original Gmail provenance stays intact.
    """
    selected_ids = {int(value) for value in meeting_ids}
    candidates = Meeting.objects.filter(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id__startswith="gmail-note:",
    ).order_by("id")
    if selected_ids:
        candidates = candidates.filter(id__in=selected_ids)
    candidate_ids = list(candidates.values_list("id", flat=True))
    all_leads = list(Lead.objects.all().order_by("id"))
    report = MeetingIdentityRepairReport(applied=apply)

    with transaction.atomic():
        meetings = list(
            # ``opportunity`` is nullable and select_related therefore uses a
            # LEFT OUTER JOIN. PostgreSQL cannot apply FOR UPDATE to that
            # nullable side; lock only the canonical Meeting row while still
            # hydrating the related objects needed for the safety review.
            Meeting.objects.select_for_update(of=("self",))
            .select_related("lead", "opportunity")
            .filter(id__in=candidate_ids)
            .order_by("id")
        )
        for meeting in meetings:
            report.inspected += 1
            if gmail_note_meeting_identity_is_valid(meeting):
                report.unchanged += 1
                continue
            report.invalid += 1
            title = _original_gmail_note_title(meeting)
            replacement = _unique_lead_for_note_title(title, all_leads) if title else None
            if replacement is None or replacement.id == meeting.lead_id:
                report.unresolved += 1
                report.issue("no_unique_replacement")
                continue
            report.uniquely_resolved += 1
            if _meeting_has_human_managed_context(meeting):
                report.blocked += 1
                report.issue("human_managed_context")
                continue
            _reassign_meeting(meeting, replacement=replacement)
            report.repaired += 1
        if not apply:
            transaction.set_rollback(True)
    return report


def _meeting_has_human_managed_context(meeting: Meeting) -> bool:
    opportunity = meeting.opportunity
    if opportunity is None:
        return False
    if (
        opportunity.source in {Opportunity.Source.MANUAL, Opportunity.Source.SHEET}
        or opportunity.manual_pin
        or opportunity.human_revision > 0
        or bool(getattr(opportunity, "pipeline_stage", ""))
    ):
        return True
    return OpportunityAction.objects.filter(
        opportunity=opportunity,
        status__in=(OpportunityAction.Status.OPEN, OpportunityAction.Status.WAITING),
    ).exclude(idempotency_key__startswith="v2:").exists()


def _reassign_meeting(meeting: Meeting, *, replacement: Lead) -> None:
    old_lead_id = meeting.lead_id
    meeting.lead = replacement
    meeting.opportunity = None
    meeting.save(update_fields={"lead", "opportunity", "update_date"})

    replacement_link = MeetingParticipant.objects.select_for_update().filter(
        meeting=meeting,
        lead=replacement,
    ).first()
    old_link = MeetingParticipant.objects.select_for_update().filter(
        meeting=meeting,
        lead_id=old_lead_id,
    ).first()
    if old_link is not None and replacement_link is None:
        old_link.lead = replacement
        old_link.attendee_email = replacement.email
        old_link.attendee_name = f"{replacement.first_name} {replacement.last_name}".strip()
        old_link.save(update_fields={
            "lead",
            "attendee_email",
            "attendee_name",
            "updated_at",
        })
    elif old_link is not None and replacement_link is not None:
        # This row encodes only the proven-invalid legacy primary association;
        # the exact replacement participant is already present.
        old_link.delete()
        if not replacement_link.is_primary:
            replacement_link.is_primary = True
            replacement_link.save(update_fields={"is_primary", "updated_at"})

    MeetingNote.objects.filter(meeting=meeting).update(opportunity=None)
