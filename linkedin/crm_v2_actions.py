"""Reconcile CRM v2 reminder recommendations into canonical actions.

This module is deliberately a task ledger, not an outreach engine.  It never
sends messages and never generates copy.  It consumes already-resolved
``ResolvedAccountEvidence`` after account reconciliation and maintains at most
one replaceable ``v2:`` current action per active, admitted, owned Opportunity.

Human work is authoritative.  A current action is replaceable only when its
idempotency key starts with ``v2:`` *and* it has no human revision.  Every
other current action is left byte-for-byte untouched.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable
from uuid import NAMESPACE_URL, UUID, uuid5

from django.db import transaction
from django.utils import timezone

from crm.models import Message, Meeting, Opportunity, OpportunityAction
from linkedin.crm_v2_evidence import ResolvedAccountEvidence
from linkedin.crm_v2_policy import ReminderState


__all__ = (
    "ActionReconciliationChange",
    "ActionReconciliationIssue",
    "ActionReconciliationReport",
    "apply_action_reconciliation",
    "dry_run_action_reconciliation",
    "reconcile_v2_actions",
)


_CURRENT_STATUSES = (
    OpportunityAction.Status.OPEN,
    OpportunityAction.Status.WAITING,
)
_CLOSED_STAGES = (
    Opportunity.Stage.CLOSED_WON,
    Opportunity.Stage.CLOSED_LOST,
)


@dataclass(frozen=True)
class ActionReconciliationChange:
    account_key: str
    opportunity_id: str
    action_id: str
    kind: str
    detail: str = ""


@dataclass(frozen=True)
class ActionReconciliationIssue:
    account_key: str
    reason: str
    detail: str = ""


@dataclass
class ActionReconciliationReport:
    applied: bool
    evaluated_at: datetime
    evidence_rows: int = 0
    actionable_rows: int = 0
    actions_created: int = 0
    actions_updated: int = 0
    actions_reused: int = 0
    actions_cancelled: int = 0
    actions_unchanged: int = 0
    human_actions_preserved: int = 0
    unowned_skipped: int = 0
    ineligible_rows: int = 0
    changes: list[ActionReconciliationChange] = field(default_factory=list)
    issues: list[ActionReconciliationIssue] = field(default_factory=list)


@dataclass(frozen=True)
class _ActionProposal:
    idempotency_key: str
    action_id: UUID
    kind: str
    description: str
    due_on: date | None
    target_lead_id: int | None
    trigger_message_id: int | None
    trigger_meeting_id: int | None
    channel: str


def dry_run_action_reconciliation(
    evidence_rows: Iterable[ResolvedAccountEvidence],
    *,
    evaluated_at: datetime | None = None,
) -> ActionReconciliationReport:
    """Execute the exact write path in a transaction that is rolled back."""
    return reconcile_v2_actions(
        evidence_rows,
        apply=False,
        evaluated_at=evaluated_at,
    )


def apply_action_reconciliation(
    evidence_rows: Iterable[ResolvedAccountEvidence],
    *,
    evaluated_at: datetime | None = None,
) -> ActionReconciliationReport:
    """Atomically apply v2 reminder actions without sending anything."""
    return reconcile_v2_actions(
        evidence_rows,
        apply=True,
        evaluated_at=evaluated_at,
    )


def reconcile_v2_actions(
    evidence_rows: Iterable[ResolvedAccountEvidence],
    *,
    apply: bool = False,
    evaluated_at: datetime | None = None,
) -> ActionReconciliationReport:
    """Create, update, reuse, or cancel only replaceable ``v2:`` actions.

    Rows must point to the exact Opportunity produced by account
    reconciliation.  Missing/stale IDs fail closed; this layer never guesses
    an Opportunity or contact from names.  Locks are acquired in deterministic
    Opportunity-ID order, and each Opportunity is locked before its actions.
    """
    rows = tuple(evidence_rows)
    observed_at = evaluated_at or timezone.now()
    if timezone.is_naive(observed_at):
        raise ValueError("evaluated_at must be timezone-aware")

    report = ActionReconciliationReport(
        applied=apply,
        evaluated_at=observed_at,
        evidence_rows=len(rows),
        actionable_rows=sum(
            bool(row.decision.reminder.should_create_reminder)
            for row in rows
        ),
    )
    parsed_rows: list[tuple[UUID, ResolvedAccountEvidence]] = []
    for row in rows:
        raw_id = (row.opportunity_id or "").strip()
        if not raw_id:
            # Ordinary People-only groups have never had an Opportunity and
            # therefore have no v2 action to cancel.  They are not an identity
            # error.  An admitted row missing its post-reconciliation ID still
            # fails closed, while any stale row that does have an Opportunity
            # continues through the ineligible path below so replaceable v2
            # work is cancelled.
            if not row.decision.admitted:
                report.ineligible_rows += 1
                continue
            _issue(report, row, "missing_opportunity_id")
            continue
        try:
            opportunity_id = UUID(raw_id)
        except (TypeError, ValueError, AttributeError):
            _issue(report, row, "invalid_opportunity_id", raw_id)
            continue
        parsed_rows.append((opportunity_id, row))

    duplicate_ids = {
        opportunity_id
        for opportunity_id, count in Counter(
            opportunity_id for opportunity_id, _row in parsed_rows
        ).items()
        if count > 1
    }

    with transaction.atomic():
        for opportunity_id, row in sorted(
            parsed_rows,
            key=lambda item: (str(item[0]), item[1].account_key),
        ):
            if opportunity_id in duplicate_ids:
                _issue(report, row, "duplicate_evidence_opportunity_id")
                continue
            _reconcile_row(
                row,
                opportunity_id=opportunity_id,
                report=report,
            )
        if not apply:
            transaction.set_rollback(True)
    return report


def _reconcile_row(
    row: ResolvedAccountEvidence,
    *,
    opportunity_id: UUID,
    report: ActionReconciliationReport,
) -> None:
    # Lock ordering is intentional: Opportunity first, then its Action rows.
    opportunity = (
        # Scope the PostgreSQL row lock to Opportunity itself.  ``owner`` is
        # nullable, so locking the outer-joined SalesOwner side is unsupported
        # and can make an otherwise safe refresh fail at runtime.
        Opportunity.objects.select_for_update(of=("self",))
        .select_related("owner")
        .filter(pk=opportunity_id)
        .first()
    )
    if opportunity is None:
        _issue(report, row, "opportunity_not_found", str(opportunity_id))
        return

    actions = list(
        OpportunityAction.objects.select_for_update()
        .filter(opportunity=opportunity)
        .order_by("created_at", "id")
    )
    current = next(
        (action for action in actions if action.status in _CURRENT_STATUSES),
        None,
    )
    replaceable_current = current if _is_replaceable_v2_action(current) else None

    eligible = bool(
        row.decision.admitted
        and opportunity.active_account
        and opportunity.stage not in _CLOSED_STAGES
    )
    if not eligible:
        report.ineligible_rows += 1
        _cancel_current_if_replaceable(
            replaceable_current,
            row=row,
            opportunity=opportunity,
            report=report,
            detail="account_not_active",
        )
        if current is not None and replaceable_current is None:
            _preserve_human_action(
                current,
                row=row,
                opportunity=opportunity,
                report=report,
            )
        return

    if opportunity.owner_id is None:
        report.unowned_skipped += 1
        _cancel_current_if_replaceable(
            replaceable_current,
            row=row,
            opportunity=opportunity,
            report=report,
            detail="unowned_opportunity",
        )
        if current is not None and replaceable_current is None:
            _preserve_human_action(
                current,
                row=row,
                opportunity=opportunity,
                report=report,
            )
        return

    recommendation = row.decision.reminder
    if not recommendation.should_create_reminder:
        _cancel_current_if_replaceable(
            replaceable_current,
            row=row,
            opportunity=opportunity,
            report=report,
            detail=recommendation.state.value,
        )
        if current is not None and replaceable_current is None:
            _preserve_human_action(
                current,
                row=row,
                opportunity=opportunity,
                report=report,
            )
        return

    if current is not None and replaceable_current is None:
        _preserve_human_action(
            current,
            row=row,
            opportunity=opportunity,
            report=report,
        )
        return

    proposal = _proposal_for(
        row,
        opportunity=opportunity,
        current_v2_action=replaceable_current,
        report=report,
    )
    if proposal is None:
        _cancel_current_if_replaceable(
            replaceable_current,
            row=row,
            opportunity=opportunity,
            report=report,
            detail="invalid_or_missing_exact_target",
        )
        return

    if (
        replaceable_current is not None
        and replaceable_current.idempotency_key != proposal.idempotency_key
    ):
        _cancel_current_if_replaceable(
            replaceable_current,
            row=row,
            opportunity=opportunity,
            report=report,
            detail="superseded_v2_evidence",
        )
        current = None
    else:
        current = replaceable_current

    matching = next(
        (
            action for action in actions
            if action.idempotency_key == proposal.idempotency_key
        ),
        None,
    )
    if current is not None:
        _update_current_action(
            current,
            proposal=proposal,
            row=row,
            opportunity=opportunity,
            report=report,
        )
        return

    if matching is not None:
        if _is_reusable_cancelled_v2_action(matching):
            _reuse_cancelled_action(
                matching,
                proposal=proposal,
                row=row,
                opportunity=opportunity,
                report=report,
            )
        else:
            report.actions_unchanged += 1
            report.changes.append(ActionReconciliationChange(
                account_key=row.account_key,
                opportunity_id=str(opportunity.id),
                action_id=str(matching.id),
                kind="terminal_action_preserved",
                detail=matching.status,
            ))
        return

    action = OpportunityAction(
        id=proposal.action_id,
        opportunity=opportunity,
        target_lead_id=proposal.target_lead_id,
        kind=proposal.kind,
        status=OpportunityAction.Status.OPEN,
        description=proposal.description,
        due_on=proposal.due_on,
        waiting_until=None,
        channel=proposal.channel,
        draft="",
        trigger_message_id=proposal.trigger_message_id,
        trigger_meeting_id=proposal.trigger_meeting_id,
        idempotency_key=proposal.idempotency_key,
    )
    action.save(force_insert=True)
    report.actions_created += 1
    report.changes.append(ActionReconciliationChange(
        account_key=row.account_key,
        opportunity_id=str(opportunity.id),
        action_id=str(action.id),
        kind="action_created",
        detail=proposal.kind,
    ))


def _proposal_for(
    row: ResolvedAccountEvidence,
    *,
    opportunity: Opportunity,
    current_v2_action: OpportunityAction | None,
    report: ActionReconciliationReport,
) -> _ActionProposal | None:
    recommendation = row.decision.reminder
    target_lead_id = row.reminder_target_lead_id
    contact_ids = set(
        opportunity.contacts.values_list("lead_id", flat=True)
    )
    if target_lead_id is not None and target_lead_id not in contact_ids:
        _issue(
            report,
            row,
            "reminder_target_not_linked_to_opportunity",
            str(target_lead_id),
        )
        return None

    account_level_allowed = bool(
        target_lead_id is None
        and (
            recommendation.state == ReminderState.DEFINE_NEXT_STEP
            or (
                (row.facts.manual_pin or row.facts.sales_motion_active)
                and row.trigger_message_id is None
                and row.trigger_meeting_id is None
            )
            or _is_current_account_level_v2_action(
                current_v2_action,
                opportunity=opportunity,
            )
        )
    )
    if target_lead_id is None and not account_level_allowed:
        _issue(report, row, "missing_exact_reminder_target")
        return None

    if row.trigger_message_id is not None:
        message = Message.objects.filter(pk=row.trigger_message_id).first()
        if message is None:
            _issue(
                report,
                row,
                "trigger_message_not_found",
                str(row.trigger_message_id),
            )
            return None
        if message.lead_id not in contact_ids:
            _issue(
                report,
                row,
                "trigger_message_lead_not_linked",
                str(message.lead_id),
            )
            return None
        if target_lead_id is not None and message.lead_id != target_lead_id:
            _issue(
                report,
                row,
                "trigger_message_target_mismatch",
                f"message_lead={message.lead_id}",
            )
            return None

    if row.trigger_meeting_id is not None:
        meeting = Meeting.objects.filter(pk=row.trigger_meeting_id).first()
        if meeting is None:
            _issue(
                report,
                row,
                "trigger_meeting_not_found",
                str(row.trigger_meeting_id),
            )
            return None
        meeting_lead_ids = {meeting.lead_id}
        meeting_lead_ids.update(
            meeting.participants.values_list("id", flat=True)
        )
        if not meeting_lead_ids & contact_ids:
            _issue(report, row, "trigger_meeting_not_linked_to_opportunity")
            return None
        if target_lead_id is not None and target_lead_id not in meeting_lead_ids:
            _issue(
                report,
                row,
                "trigger_meeting_target_mismatch",
                str(target_lead_id),
            )
            return None

    basis = _idempotency_basis(
        row,
        opportunity=opportunity,
        current_v2_action=current_v2_action,
        account_level_allowed=account_level_allowed,
    )
    idempotency_key = f"v2:{opportunity.id}:{basis}"[:255]
    kind, description = _translated_action(recommendation.state)
    channel = ""
    if not row.reminder_do_not_outreach:
        reason = recommendation.reason_code.value
        if "gmail" in reason:
            channel = "email"
        elif "linkedin" in reason:
            channel = "linkedin"
        elif (
            current_v2_action is not None
            and current_v2_action.idempotency_key == idempotency_key
            and recommendation.state in {
                ReminderState.OVERDUE_NEXT_ACTION,
                ReminderState.DUE_TODAY,
                ReminderState.SCHEDULED_NEXT_ACTION,
            }
        ):
            # A fresh evidence pass sees this v2 task's own due date.  Keep
            # the source channel rather than erasing it as the task ages.
            channel = current_v2_action.channel

    if (
        current_v2_action is not None
        and current_v2_action.idempotency_key == idempotency_key
        and recommendation.state in {
            ReminderState.OVERDUE_NEXT_ACTION,
            ReminderState.DUE_TODAY,
            ReminderState.SCHEDULED_NEXT_ACTION,
        }
    ):
        # The explicit date is this same v2 task reflecting back through the
        # policy facts.  Preserve what the task actually is (reply, meeting
        # prep, follow-up, etc.) while updating its due state in place.
        kind = current_v2_action.kind
        description = current_v2_action.description

    return _ActionProposal(
        idempotency_key=idempotency_key,
        action_id=uuid5(NAMESPACE_URL, idempotency_key),
        kind=kind,
        description=description,
        due_on=recommendation.due_on,
        target_lead_id=target_lead_id,
        trigger_message_id=row.trigger_message_id,
        trigger_meeting_id=row.trigger_meeting_id,
        channel=channel,
    )


def _translated_action(state: ReminderState) -> tuple[str, str]:
    translations = {
        ReminderState.OVERDUE_NEXT_ACTION: (
            OpportunityAction.Kind.NEXT_STEP,
            "Complete the overdue next action",
        ),
        ReminderState.DUE_TODAY: (
            OpportunityAction.Kind.NEXT_STEP,
            "Complete today's next action",
        ),
        ReminderState.NEEDS_RESPONSE: (
            OpportunityAction.Kind.NEEDS_RESPONSE,
            "Respond to the latest human inbound",
        ),
        ReminderState.MEETING_PREP: (
            OpportunityAction.Kind.MEETING_PREP,
            "Prepare for the upcoming meeting",
        ),
        ReminderState.POST_MEETING_FOLLOWUP: (
            OpportunityAction.Kind.POST_MEETING_COMMITMENT,
            "Complete the post-meeting follow-up",
        ),
        ReminderState.FOLLOW_UP_DUE: (
            OpportunityAction.Kind.FOLLOWUP,
            "Follow up on the latest conversation",
        ),
        ReminderState.SCHEDULED_NEXT_ACTION: (
            OpportunityAction.Kind.NEXT_STEP,
            "Complete the scheduled next action",
        ),
        ReminderState.DEFINE_NEXT_STEP: (
            OpportunityAction.Kind.NEXT_STEP,
            "Define and schedule the next step",
        ),
    }
    try:
        return translations[state]
    except KeyError as exc:  # Defensive: policy owns actionability.
        raise ValueError(f"No v2 action translation for reminder state {state!r}") from exc


def _idempotency_basis(
    row: ResolvedAccountEvidence,
    *,
    opportunity: Opportunity,
    current_v2_action: OpportunityAction | None,
    account_level_allowed: bool,
) -> str:
    if row.trigger_message_id is not None:
        return f"message:{row.trigger_message_id}"
    if row.trigger_meeting_id is not None:
        return f"meeting:{row.trigger_meeting_id}"
    if account_level_allowed:
        expected_prefix = f"v2:{opportunity.id}:account:"
        if (
            current_v2_action is not None
            and current_v2_action.idempotency_key.startswith(expected_prefix)
        ):
            return current_v2_action.idempotency_key[len(f"v2:{opportunity.id}:"):]
        return (
            "account:authoritative-next-step"
            if row.facts.manual_pin or row.facts.sales_motion_active
            else "account:define-next-step"
        )
    target = row.reminder_target_lead_id or "account"
    reason = row.decision.reminder.reason_code.value
    due_on = row.decision.reminder.due_on
    due = due_on.isoformat() if due_on else "none"
    return f"target:{target}:{reason}:{due}"


def _update_current_action(
    action: OpportunityAction,
    *,
    proposal: _ActionProposal,
    row: ResolvedAccountEvidence,
    opportunity: Opportunity,
    report: ActionReconciliationReport,
) -> None:
    desired = {
        "target_lead_id": proposal.target_lead_id,
        "kind": proposal.kind,
        "status": OpportunityAction.Status.OPEN,
        "description": proposal.description,
        "due_on": proposal.due_on,
        "waiting_until": None,
        "channel": proposal.channel,
        "draft": "",
        "trigger_message_id": proposal.trigger_message_id,
        "trigger_meeting_id": proposal.trigger_meeting_id,
    }
    changed = set()
    for field_name, value in desired.items():
        if getattr(action, field_name) != value:
            setattr(action, field_name, value)
            changed.add(field_name)
    if not changed:
        report.actions_unchanged += 1
        return
    action.save(update_fields=changed | {"updated_at"})
    report.actions_updated += 1
    report.changes.append(ActionReconciliationChange(
        account_key=row.account_key,
        opportunity_id=str(opportunity.id),
        action_id=str(action.id),
        kind="action_updated",
        detail=",".join(sorted(changed)),
    ))


def _reuse_cancelled_action(
    action: OpportunityAction,
    *,
    proposal: _ActionProposal,
    row: ResolvedAccountEvidence,
    opportunity: Opportunity,
    report: ActionReconciliationReport,
) -> None:
    action.target_lead_id = proposal.target_lead_id
    action.kind = proposal.kind
    action.status = OpportunityAction.Status.OPEN
    action.description = proposal.description
    action.due_on = proposal.due_on
    action.waiting_until = None
    action.disposition = ""
    action.channel = proposal.channel
    action.draft = ""
    action.handled_at = None
    action.sent_at = None
    action.completed_at = None
    action.trigger_message_id = proposal.trigger_message_id
    action.trigger_meeting_id = proposal.trigger_meeting_id
    action.save(update_fields={
        "target_lead_id",
        "kind",
        "status",
        "description",
        "due_on",
        "waiting_until",
        "disposition",
        "channel",
        "draft",
        "handled_at",
        "sent_at",
        "completed_at",
        "trigger_message_id",
        "trigger_meeting_id",
        "updated_at",
    })
    report.actions_reused += 1
    report.changes.append(ActionReconciliationChange(
        account_key=row.account_key,
        opportunity_id=str(opportunity.id),
        action_id=str(action.id),
        kind="action_reused",
        detail=proposal.kind,
    ))


def _cancel_current_if_replaceable(
    action: OpportunityAction | None,
    *,
    row: ResolvedAccountEvidence,
    opportunity: Opportunity,
    report: ActionReconciliationReport,
    detail: str,
) -> None:
    if action is None:
        return
    action.status = OpportunityAction.Status.CANCELLED
    action.save(update_fields={"status", "updated_at"})
    report.actions_cancelled += 1
    report.changes.append(ActionReconciliationChange(
        account_key=row.account_key,
        opportunity_id=str(opportunity.id),
        action_id=str(action.id),
        kind="action_cancelled",
        detail=detail,
    ))


def _preserve_human_action(
    action: OpportunityAction,
    *,
    row: ResolvedAccountEvidence,
    opportunity: Opportunity,
    report: ActionReconciliationReport,
) -> None:
    report.human_actions_preserved += 1
    report.changes.append(ActionReconciliationChange(
        account_key=row.account_key,
        opportunity_id=str(opportunity.id),
        action_id=str(action.id),
        kind="human_action_preserved",
        detail=action.idempotency_key,
    ))


def _is_replaceable_v2_action(action: OpportunityAction | None) -> bool:
    return bool(
        action is not None
        and action.human_revision == 0
        and action.idempotency_key.startswith(("v2:", "system:"))
    )


def _is_reusable_cancelled_v2_action(action: OpportunityAction) -> bool:
    return bool(
        action.status == OpportunityAction.Status.CANCELLED
        and _is_replaceable_v2_action(action)
    )


def _is_current_account_level_v2_action(
    action: OpportunityAction | None,
    *,
    opportunity: Opportunity,
) -> bool:
    return bool(
        action is not None
        and _is_replaceable_v2_action(action)
        and action.target_lead_id is None
        and action.idempotency_key.startswith(f"v2:{opportunity.id}:account:")
    )


def _issue(
    report: ActionReconciliationReport,
    row: ResolvedAccountEvidence,
    reason: str,
    detail: str = "",
) -> None:
    report.issues.append(ActionReconciliationIssue(
        account_key=row.account_key,
        reason=reason,
        detail=detail,
    ))
