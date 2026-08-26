"""Serialize canonical CRM records into managed Google Sheets view rows."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.utils import timezone

from linkedin.conf import ACTIVE_TIMEZONE
from linkedin.crm_service import ActionRefreshReport
from linkedin.granola_sync import resolve_meeting_context
from linkedin.notifications import crm_sheets


MAX_MEETING_CONTEXT_CHARS = 8000


@dataclass(frozen=True)
class CrmViewRows:
    opportunities: tuple[dict[str, str], ...]
    pipeline: tuple[dict[str, str], ...]
    recovery: tuple[dict[str, str], ...]
    followups_by_owner: dict[str, tuple[dict[str, str], ...]]


def build_crm_view_rows(
    action_report: ActionRefreshReport,
    *,
    granola_available: bool,
    synced_at: datetime | None = None,
) -> CrmViewRows:
    from crm.models import Opportunity, OpportunityAction

    timestamp = synced_at or timezone.now()
    evaluations = {
        item.opportunity_id: item
        for item in action_report.evaluations
    }
    opportunity_rows: list[dict[str, str]] = []
    pipeline_rows: list[dict[str, str]] = []
    recovery_rows: list[dict[str, str]] = []
    followups: dict[str, list[dict[str, str]]] = {}

    opportunities = (
        Opportunity.objects.select_related("account", "owner")
        .prefetch_related(
            "contacts__lead",
            "actions__target_lead",
            "actions__trigger_message__lead",
            "actions__trigger_meeting__lead",
            "meetings",
        )
        .order_by("id")
    )
    for opportunity in opportunities:
        evaluation = evaluations.get(str(opportunity.id))
        actions = list(opportunity.actions.all())
        current_action = next(
            (
                action for action in actions
                if action.status in {
                    OpportunityAction.Status.OPEN,
                    OpportunityAction.Status.WAITING,
                }
            ),
            None,
        )
        context = resolve_meeting_context(
            opportunity=opportunity,
            granola_available=granola_available,
        )
        placement = evaluation.placement if evaluation is not None else None
        target_lead = (
            _primary_action_lead(opportunity, current_action=current_action)
            if current_action is not None
            else None
        )
        if (
            target_lead is not None
            and evaluation is not None
            and (
                evaluation.action_id != str(current_action.id)
                or evaluation.target_lead_id != current_action.target_lead_id
            )
        ):
            # The Sheet publication must use the exact action decision that
            # produced this placement. A stale report/action pairing is
            # review work, never a reason to guess a recipient.
            target_lead = None
        derived = {
            crm_sheets.COL_OVERDUE: bool(
                current_action is not None
                and current_action.due_on is not None
                and current_action.due_on
                < timezone.localtime(timestamp, ZoneInfo(ACTIVE_TIMEZONE)).date()
            ),
            crm_sheets.COL_ACTION_CATEGORY: (
                placement.category if placement is not None else ""
            ),
            crm_sheets.COL_INACTIVITY_AGE: (
                placement.inactivity_days
                if placement is not None and placement.inactivity_days is not None
                else ""
            ),
            crm_sheets.COL_RECOVERY_ELIGIBILITY: (
                placement.surface == "recovery" if placement is not None else False
            ),
            crm_sheets.COL_PIPELINE_POSITION: opportunity.stage,
        }
        opportunity_rows.append(crm_sheets.opportunity_to_sheet_row(
            opportunity,
            action=current_action,
            meeting_context=(context.content[:MAX_MEETING_CONTEXT_CHARS] if context else ""),
            meeting_context_source=(context.source if context else ""),
            derived=derived,
            synced_at=timestamp,
        ))

        card = _pipeline_card(opportunity, current_action=current_action)
        pipeline_rows.append(crm_sheets.pipeline_stage_row(
            opportunity_id=str(opportunity.id),
            stage=opportunity.stage,
            card_summary=card,
        ))

        unroutable_daily = bool(
            placement is not None
            and placement.surface == "daily"
            and (opportunity.owner_id is None or target_lead is None)
        )
        if placement is not None and (
            placement.surface == "recovery"
            or unroutable_daily
        ):
            recovery_rows.append({
                crm_sheets.COL_OPPORTUNITY_ID: str(opportunity.id),
                crm_sheets.COL_ACCOUNT: opportunity.account.name,
                crm_sheets.COL_OWNER: opportunity.owner.handle if opportunity.owner_id else "",
                crm_sheets.COL_STAGE: opportunity.stage,
                crm_sheets.COL_LAST_MEANINGFUL_ACTIVITY: opportunity.last_meaningful_activity_at,
                crm_sheets.COL_INACTIVITY_AGE: (
                    placement.inactivity_days
                    if placement.inactivity_days is not None
                    else ""
                ),
                crm_sheets.COL_NEXT_ACTION: current_action.description if current_action else "",
                crm_sheets.COL_NEXT_ACTION_DUE: current_action.due_on if current_action else "",
                crm_sheets.COL_RECOVERY_ELIGIBILITY: (
                    "unassigned_current_action"
                    if unroutable_daily and opportunity.owner_id is None
                    else "unresolved_action_target"
                    if unroutable_daily and target_lead is None
                    else placement.reason
                ),
            })

        if (
            placement is None
            or placement.surface != "daily"
            or opportunity.owner_id is None
            or current_action is None
            or target_lead is None
        ):
            continue
        lead = target_lead
        owner_handle = opportunity.owner.handle
        followups.setdefault(owner_handle, []).append({
            crm_sheets.COL_ACTION_ID: str(current_action.id),
            crm_sheets.COL_OPPORTUNITY_ID: str(opportunity.id),
            crm_sheets.COL_LEAD_ID: str(lead.id),
            crm_sheets.COL_ACCOUNT: opportunity.account.name,
            crm_sheets.COL_CONTACT: _lead_name(lead),
            crm_sheets.COL_OWNER: owner_handle,
            crm_sheets.COL_ACTION_CATEGORY: placement.category,
            crm_sheets.COL_NEXT_ACTION: current_action.description,
            crm_sheets.COL_NEXT_ACTION_DUE: current_action.due_on,
            crm_sheets.COL_WAITING_UNTIL: current_action.waiting_until,
            crm_sheets.COL_LAST_MEANINGFUL_ACTIVITY: opportunity.last_meaningful_activity_at,
            crm_sheets.COL_CHANNEL: current_action.channel,
            crm_sheets.COL_DRAFT: current_action.draft,
            crm_sheets.COL_HANDLED: current_action.status == OpportunityAction.Status.COMPLETED,
            crm_sheets.COL_DISPOSITION: current_action.disposition,
            crm_sheets.COL_MANUAL_PIN: opportunity.manual_pin,
        })

    return CrmViewRows(
        opportunities=tuple(opportunity_rows),
        pipeline=tuple(pipeline_rows),
        recovery=tuple(recovery_rows),
        followups_by_owner={
            owner: tuple(rows)
            for owner, rows in sorted(followups.items())
        },
    )


def followup_db_human_values(action) -> dict[str, str]:
    from crm.models import OpportunityAction

    return {
        crm_sheets.COL_WAITING_UNTIL: (
            action.waiting_until.isoformat() if action.waiting_until else ""
        ),
        crm_sheets.COL_CHANNEL: action.channel or "",
        crm_sheets.COL_DRAFT: action.draft or "",
        crm_sheets.COL_HANDLED: (
            "TRUE" if action.status == OpportunityAction.Status.COMPLETED else "FALSE"
        ),
        crm_sheets.COL_DISPOSITION: action.disposition or "",
        crm_sheets.COL_MANUAL_PIN: "TRUE" if action.opportunity.manual_pin else "FALSE",
    }


def followup_imports_from_sheet_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Diff read-only sender cells against DB state using stable Action IDs."""
    from crm.models import OpportunityAction

    action_ids = [
        row.get(crm_sheets.COL_ACTION_ID, "").strip()
        for row in rows
        if row.get(crm_sheets.COL_ACTION_ID, "").strip()
    ]
    actions = {
        str(action.id): action
        for action in OpportunityAction.objects.select_related("opportunity").filter(
            pk__in=action_ids,
        )
    }
    imports: list[dict[str, str]] = []
    for row in rows:
        action_id = row.get(crm_sheets.COL_ACTION_ID, "").strip()
        action = actions.get(action_id)
        if action is None:
            continue
        database = followup_db_human_values(action)
        for field in crm_sheets.FOLLOWUP_HUMAN_FIELDS:
            sheet_value = str(row.get(field, "") or "")
            if _normalized_human_value(field, sheet_value) == _normalized_human_value(
                field,
                database[field],
            ):
                continue
            imports.append({
                "stable_id": action_id,
                "field": field,
                "value": sheet_value,
            })
    return imports


def _primary_action_lead(opportunity, *, current_action):
    # The persisted action target is authoritative.  Role/order fallback is
    # unsafe for multi-contact accounts because a Sheet edit can reorder roles
    # and silently route the same Action UUID to another recipient.
    if current_action.target_lead_id is None:
        return None
    linked_ids = {
        contact.lead_id
        for contact in opportunity.contacts.all()
    }
    if current_action.target_lead_id not in linked_ids:
        return None
    return current_action.target_lead


def _pipeline_card(opportunity, *, current_action) -> str:
    owner = opportunity.owner.handle if opportunity.owner_id else "Unassigned"
    lines = [opportunity.account.name, owner]
    if current_action is not None and current_action.description:
        lines.append(f"Next: {current_action.description}")
    if current_action is not None and current_action.due_on:
        lines.append(f"Due: {current_action.due_on.isoformat()}")
    return "\n".join(lines)


def _lead_name(lead) -> str:
    return f"{lead.first_name} {lead.last_name}".strip() or lead.public_identifier


def _normalized_human_value(field: str, value: Any) -> str:
    text = str(value or "").strip()
    if field in {crm_sheets.COL_HANDLED, crm_sheets.COL_MANUAL_PIN}:
        return "TRUE" if text.casefold() in {"true", "yes", "1", "y", "checked"} else "FALSE"
    if field == crm_sheets.COL_WAITING_UNTIL:
        return text[:10]
    return text
