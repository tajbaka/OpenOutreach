"""Build the two concise CRM v2 Sheet payloads from reconciled database state."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Mapping

from crm.models import Opportunity, OpportunityAction, OpportunitySheetState
from linkedin.crm_v2_evidence import ResolvedAccountEvidence
from linkedin.crm_v2_policy import (
    AdmissionReasonCode,
    EvidenceTier,
    ReminderState,
)
from linkedin.crm_v2_publish import (
    ActionRecord,
    ActiveAccountRecord,
    CrmV2ViewRows,
    build_crm_v2_view_rows,
)


@dataclass(frozen=True)
class CrmV2DatabaseView:
    rows: CrmV2ViewRows
    active_baselines: Mapping[str, Mapping[str, str]]
    action_baselines: Mapping[str, Mapping[str, str]]


def build_crm_v2_database_view(
    evidence_rows: Iterable[ResolvedAccountEvidence],
) -> CrmV2DatabaseView:
    """Serialize admitted reconciled Opportunities and only work due now.

    Waiting and scheduled relationships remain legible on Active Accounts but
    do not clutter Actions.  Policy-qualified recovery reviews and unowned
    work are published without guessing a target or owner.
    """
    admitted = [row for row in evidence_rows if row.decision.admitted]
    missing_ids = [row.account_key for row in admitted if not row.opportunity_id]
    if missing_ids:
        raise ValueError(
            "Admitted evidence must be recollected after reconciliation; "
            f"{len(missing_ids)} row(s) have no Opportunity ID."
        )
    by_opportunity_id = {row.opportunity_id: row for row in admitted}
    if len(by_opportunity_id) != len(admitted):
        raise ValueError("CRM v2 evidence contains duplicate Opportunity IDs")

    opportunities = list(
        Opportunity.objects.filter(id__in=by_opportunity_id, active_account=True)
        .select_related("account", "owner")
        .prefetch_related("contacts__lead")
        .order_by("account__normalized_name", "id")
    )
    found = {str(opportunity.id) for opportunity in opportunities}
    missing = sorted(set(by_opportunity_id) - found)
    if missing:
        raise ValueError(
            f"{len(missing)} admitted Opportunity row(s) are missing or inactive"
        )

    current_actions = {
        str(action.opportunity_id): action
        for action in OpportunityAction.objects.filter(
            opportunity_id__in=found,
            status__in=(OpportunityAction.Status.OPEN, OpportunityAction.Status.WAITING),
        ).select_related("target_lead", "opportunity__owner")
    }
    active_records = []
    action_records = []
    for opportunity in opportunities:
        stable_id = str(opportunity.id)
        evidence = by_opportunity_id[stable_id]
        recommendation = evidence.decision.reminder
        current_action = current_actions.get(stable_id)
        outreach = (
            "Stopped"
            if evidence.facts.do_not_outreach
            or evidence.reminder_do_not_outreach
            else "Allowed"
        )
        owner_handle = opportunity.owner.handle if opportunity.owner_id else ""
        owner = owner_handle or "Unassigned"
        next_action = (
            current_action.description
            if current_action is not None
            else _next_action_label(recommendation.state)
        )
        active_records.append(ActiveAccountRecord(
            opportunity_id=opportunity.id,
            account_id=opportunity.account_id,
            account=opportunity.account.name,
            owner=owner,
            stage=(
                opportunity.get_pipeline_stage_display()
                if opportunity.pipeline_stage
                else "Radar only"
            ),
            attention=_attention(evidence, owner=owner_handle),
            why_active=_why_active(evidence),
            evidence_tier=_tier_label(evidence.decision.evidence_tier),
            outreach=outreach,
            last_meaningful_touch=evidence.last_meaningful_touch,
            next_action=next_action,
            next_action_due=(
                current_action.due_on
                if current_action is not None
                else recommendation.due_on
            ),
            waiting_until=(
                current_action.waiting_until
                if current_action is not None
                else evidence.facts.waiting_until
            ),
            who_owes_whom=_who_owes(recommendation.state),
            key_contacts=_compact_contacts(evidence.key_contacts),
            manual_pin=opportunity.manual_pin,
        ))

        if (
            not recommendation.should_create_reminder
            or current_action is None
            or (
                not owner_handle
                and evidence.decision.evidence_tier not in {
                    EvidenceTier.AUTHORITATIVE,
                    EvidenceTier.PRIMARY,
                }
            )
        ):
            continue
        target = current_action.target_lead
        action_records.append(ActionRecord(
            action_id=current_action.id,
            opportunity_id=opportunity.id,
            account_id=opportunity.account_id,
            account=opportunity.account.name,
            owner=owner,
            contact=(target.full_name if target is not None else "Account-level"),
            lead_id=(target.id if target is not None else ""),
            why_now=_why_now(recommendation.state),
            outreach=outreach,
            next_action=current_action.description,
            next_action_due=current_action.due_on,
            waiting_until=current_action.waiting_until,
            who_owes_whom=_who_owes(recommendation.state),
            channel=("" if outreach == "Stopped" else current_action.channel),
            draft=("" if outreach == "Stopped" else current_action.draft),
            handled=False,
            disposition=current_action.disposition,
        ))

    # The default view should answer "what needs attention?" without requiring
    # an operator to sort it first.  Stable IDs remain the durable identity;
    # this presentation order is deliberately urgency-first, then due date.
    active_records.sort(key=_active_account_sort_key)
    action_records.sort(key=_action_sort_key)
    view_rows = build_crm_v2_view_rows(active_records, action_records)
    active_baselines = {
        str(state.opportunity_id): dict(state.published_human_snapshot or {})
        for state in OpportunitySheetState.objects.filter(opportunity_id__in=found)
    }
    action_ids = {row["Action ID"] for row in view_rows.actions}
    action_baselines = {
        str(action.id): dict(action.sheet_human_snapshot or {})
        for action in OpportunityAction.objects.filter(id__in=action_ids)
    }
    return CrmV2DatabaseView(
        rows=view_rows,
        active_baselines=active_baselines,
        action_baselines=action_baselines,
    )


_ATTENTION_RANK = {
    "Now": 0,
    "Needs contact": 1,
    "Upcoming": 2,
    "Waiting": 3,
    "Review": 4,
    "None": 5,
}

_WHY_NOW_RANK = {
    "New human reply": 0,
    "Overdue next step": 1,
    "Meeting approaching": 2,
    "Meeting completed": 3,
    "Due today": 4,
    "Reply window elapsed": 5,
    "No next step defined": 6,
}


def _active_account_sort_key(record: ActiveAccountRecord):
    return (
        _ATTENTION_RANK.get(record.attention, 99),
        _date_sort_key(record.next_action_due),
        record.owner.casefold(),
        record.account.casefold(),
    )


def _action_sort_key(record: ActionRecord):
    return (
        _date_sort_key(record.next_action_due),
        _WHY_NOW_RANK.get(record.why_now, 99),
        record.owner.casefold(),
        record.account.casefold(),
    )


def _date_sort_key(value) -> int:
    if isinstance(value, datetime):
        return value.date().toordinal()
    if isinstance(value, date):
        return value.toordinal()
    text = str(value or "").strip()
    if text:
        try:
            return date.fromisoformat(text[:10]).toordinal()
        except ValueError:
            pass
    return date.max.toordinal()


def _attention(evidence: ResolvedAccountEvidence, *, owner: str) -> str:
    if not owner:
        return "Needs contact"
    state = evidence.decision.reminder.state
    if state == ReminderState.WAITING:
        return "Waiting"
    if state == ReminderState.REVIEW:
        return "Review"
    if state == ReminderState.SCHEDULED_NEXT_ACTION:
        return "Upcoming"
    if state == ReminderState.NONE:
        return "None"
    if state == ReminderState.DEFINE_NEXT_STEP:
        return "Needs contact"
    return "Now"


def _why_active(evidence: ResolvedAccountEvidence) -> str:
    labels = {
        AdmissionReasonCode.MANUAL_PIN: "Pinned by you",
        AdmissionReasonCode.SALES_MOTION_ACTIVE: "Active Sales Motion",
        AdmissionReasonCode.HUMAN_MANAGED_OPPORTUNITY: "Human-managed opportunity",
        AdmissionReasonCode.HUMAN_CURRENT_ACTION: "Open human next step",
        AdmissionReasonCode.UPCOMING_EXTERNAL_MEETING: "Upcoming external meeting",
        AdmissionReasonCode.RECENT_COMPLETED_EXTERNAL_MEETING: "Recent external meeting",
        AdmissionReasonCode.RECENT_GMAIL_BIDIRECTIONAL_THREAD: "Recent Gmail conversation",
        AdmissionReasonCode.RECENT_GMAIL_HUMAN_INBOUND: "Recent Gmail reply",
        AdmissionReasonCode.RECENT_LINKEDIN_SUBSTANTIVE_BIDIRECTIONAL: (
            "Substantive LinkedIn conversation"
        ),
    }
    return labels[evidence.decision.primary_reason_code]


def _tier_label(tier: EvidenceTier) -> str:
    return {
        EvidenceTier.AUTHORITATIVE: "Authoritative",
        EvidenceTier.PRIMARY: "Meeting / Gmail",
        EvidenceTier.SECONDARY: "LinkedIn",
        EvidenceTier.WEAK: "Weak",
        EvidenceTier.NONE: "None",
    }[tier]


def _next_action_label(state: ReminderState) -> str:
    return {
        ReminderState.NONE: "",
        ReminderState.WAITING: "Wait for reply",
        ReminderState.OVERDUE_NEXT_ACTION: "Complete overdue next step",
        ReminderState.DUE_TODAY: "Complete today's next step",
        ReminderState.NEEDS_RESPONSE: "Reply to the latest message",
        ReminderState.MEETING_PREP: "Prepare for the meeting",
        ReminderState.POST_MEETING_FOLLOWUP: "Send the post-meeting follow-up",
        ReminderState.FOLLOW_UP_DUE: "Follow up",
        ReminderState.REVIEW: "Review account context",
        ReminderState.SCHEDULED_NEXT_ACTION: "Scheduled next step",
        ReminderState.DEFINE_NEXT_STEP: "Define the next step",
    }[state]


def _who_owes(state: ReminderState) -> str:
    if state == ReminderState.WAITING:
        return "Them"
    if state in {ReminderState.NONE, ReminderState.SCHEDULED_NEXT_ACTION}:
        return ""
    return "Us"


def _why_now(state: ReminderState) -> str:
    return {
        ReminderState.OVERDUE_NEXT_ACTION: "Overdue next step",
        ReminderState.DUE_TODAY: "Due today",
        ReminderState.NEEDS_RESPONSE: "New human reply",
        ReminderState.MEETING_PREP: "Meeting approaching",
        ReminderState.POST_MEETING_FOLLOWUP: "Meeting completed",
        ReminderState.FOLLOW_UP_DUE: "Reply window elapsed",
        ReminderState.DEFINE_NEXT_STEP: "No next step defined",
    }.get(state, state.value.replace("_", " ").title())


def _compact_contacts(values: Iterable[str], *, limit: int = 3) -> str:
    contacts = [value.strip() for value in values if value and value.strip()]
    visible = contacts[:limit]
    remaining = len(contacts) - len(visible)
    if remaining > 0:
        visible.append(f"+{remaining}")
    return "; ".join(visible)
