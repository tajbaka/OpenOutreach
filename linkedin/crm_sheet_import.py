"""Validate and apply human-owned CRM Sheet edits to canonical models."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from gspread.exceptions import APIError, WorksheetNotFound

from linkedin.conf import ACTIVE_TIMEZONE
from linkedin.notifications import crm_sheets
from linkedin.operators import resolve_sales_owner_handle


@dataclass(frozen=True)
class InvalidSheetEdit:
    opportunity_id: str
    fields: tuple[str, ...]
    reason: str


@dataclass
class SheetImportReport:
    opportunities_updated: int = 0
    fields_imported: int = 0
    actions_created: int = 0
    actions_updated: int = 0
    actions_cancelled: int = 0
    contact_roles_updated: int = 0
    invalid: list[InvalidSheetEdit] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        result = asdict(self)
        result["invalid"] = len(self.invalid)
        return result


@dataclass
class FollowupImportReport:
    actions_updated: int = 0
    fields_imported: int = 0
    completed: int = 0
    reopened: int = 0
    opportunities_pinned: int = 0
    invalid: list[InvalidSheetEdit] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        result = asdict(self)
        result["invalid"] = len(self.invalid)
        return result


_ROLE_FIELDS = {
    crm_sheets.COL_CHAMPION: "champion",
    crm_sheets.COL_DECISION_MAKER: "decision_maker",
    crm_sheets.COL_STAKEHOLDERS: "stakeholder",
}

_STAGE_ALIASES = {
    "prospecting": "prospecting",
    "discovery": "discovery",
    "demo planning": "demo_planning",
    "demo_planning": "demo_planning",
    "evaluation": "evaluation",
    "sandbox pilot": "sandbox_pilot",
    "sandbox/pilot": "sandbox_pilot",
    "sandbox_pilot": "sandbox_pilot",
    "commercial": "commercial",
    "procurement legal": "procurement_legal",
    "procurement/legal": "procurement_legal",
    "procurement_legal": "procurement_legal",
    "closed won": "closed_won",
    "closed_won": "closed_won",
    "expansion": "expansion",
    "closed lost": "closed_lost",
    "closed_lost": "closed_lost",
}

_DEFAULT_STEP_BY_STAGE = {
    "prospecting": 1,
    "discovery": 2,
    "demo_planning": 3,
    "evaluation": 5,
    "sandbox_pilot": 7,
    "commercial": 11,
    "procurement_legal": 12,
    "expansion": 15,
}


def apply_opportunity_imports(
    imports: Iterable[Any],
    *,
    dry_run: bool,
    now: datetime | None = None,
) -> SheetImportReport:
    """Apply valid three-way merge imports one Opportunity at a time.

    Invalid rows are reported and skipped atomically.  One malformed owner,
    date, contact ID, or stage/step combination cannot partially alter a sale.
    """
    from crm.models import Opportunity

    grouped: dict[str, dict[str, str]] = defaultdict(dict)
    for item in imports:
        stable_id = _item_value(item, "stable_id")
        field_name = _item_value(item, "field")
        grouped[str(stable_id)][str(field_name)] = str(_item_value(item, "value") or "")

    report = SheetImportReport()
    current_time = now or timezone.now()
    for opportunity_id, values in grouped.items():
        try:
            with transaction.atomic():
                opportunity = Opportunity.objects.select_for_update().get(
                    pk=opportunity_id,
                )
                outcome = _apply_one_opportunity(
                    opportunity,
                    values,
                    now=current_time,
                    dry_run=dry_run,
                )
                if dry_run:
                    transaction.set_rollback(True)
        except (Opportunity.DoesNotExist, ValidationError, ValueError, InvalidOperation) as exc:
            report.invalid.append(InvalidSheetEdit(
                opportunity_id=opportunity_id,
                fields=tuple(sorted(values)),
                reason=_validation_message(exc),
            ))
            continue

        report.opportunities_updated += 1
        report.fields_imported += len(values)
        report.actions_created += outcome["actions_created"]
        report.actions_updated += outcome["actions_updated"]
        report.actions_cancelled += outcome["actions_cancelled"]
        report.contact_roles_updated += outcome["contact_roles_updated"]
    return report


def commit_sheet_baselines(
    baseline_updates: Iterable[Any],
    *,
    published_at: datetime | None = None,
) -> int:
    """Advance DB merge baselines only after the Sheet plan succeeds."""
    from crm.models import Opportunity, OpportunitySheetState

    timestamp = published_at or timezone.now()
    updated = 0
    with transaction.atomic():
        for item in baseline_updates:
            opportunity_id = str(_item_value(item, "stable_id"))
            values = dict(_item_value(item, "values") or {})
            opportunity = Opportunity.objects.select_for_update().get(pk=opportunity_id)
            state, _created = OpportunitySheetState.objects.select_for_update().get_or_create(
                opportunity=opportunity,
            )
            state.published_human_snapshot = {
                field: str(values.get(field) or "")
                for field in crm_sheets.OPPORTUNITY_HUMAN_FIELDS
            }
            state.published_revision = opportunity.human_revision
            state.published_action_id = _current_action_id(opportunity)
            state.last_published_at = timestamp
            state.save()
            updated += 1
    return updated


def apply_followup_imports(
    imports: Iterable[Any],
    *,
    dry_run: bool,
    now: datetime | None = None,
) -> FollowupImportReport:
    """Durably import drafts and action dispositions keyed by Action UUID."""
    from crm.models import Opportunity, OpportunityAction

    grouped: dict[str, dict[str, str]] = defaultdict(dict)
    for item in imports:
        action_id = str(_item_value(item, "stable_id"))
        grouped[action_id][str(_item_value(item, "field"))] = str(
            _item_value(item, "value") or ""
        )

    timestamp = now or timezone.now()
    report = FollowupImportReport()
    actions_for_pin = {
        str(action.id): action
        for action in OpportunityAction.objects.filter(
            pk__in=grouped,
        ).only("id", "opportunity_id")
    }
    pin_values_by_opportunity: dict[Any, dict[str, bool]] = defaultdict(dict)
    for action_id, values in grouped.items():
        if crm_sheets.COL_MANUAL_PIN not in values:
            continue
        action = actions_for_pin.get(action_id)
        if action is None:
            continue
        try:
            parsed_pin = _parse_bool(values[crm_sheets.COL_MANUAL_PIN])
        except ValidationError:
            # The ordinary per-action validation below reports malformed
            # values with the same atomic behavior as every other field.
            continue
        pin_values_by_opportunity[action.opportunity_id][action_id] = parsed_pin
    conflicting_pin_actions = {
        action_id
        for values in pin_values_by_opportunity.values()
        if len(set(values.values())) > 1
        for action_id in values
    }
    for action_id, values in grouped.items():
        if action_id in conflicting_pin_actions:
            report.invalid.append(InvalidSheetEdit(
                opportunity_id=action_id,
                fields=tuple(sorted(values)),
                reason=(
                    "conflicting Manual pin edits for one Opportunity; "
                    "resolve them to one value"
                ),
            ))
            continue
        try:
            opportunity_id = OpportunityAction.objects.filter(
                pk=action_id,
            ).values_list("opportunity_id", flat=True).first()
            if opportunity_id is None:
                raise OpportunityAction.DoesNotExist
            with transaction.atomic():
                # Canonical lock order is Opportunity -> Action everywhere.
                # Reversing it here can deadlock against decision/reconcile
                # workflows that already hold the Opportunity row.
                opportunity = Opportunity.objects.select_for_update().get(
                    pk=opportunity_id,
                )
                action = OpportunityAction.objects.select_for_update().get(
                    pk=action_id,
                )
                if action.opportunity_id != opportunity.id:
                    raise ValidationError(
                        "action moved opportunities while importing Sheet edits"
                    )
                action.opportunity = opportunity
                outcome = _apply_one_followup_action(
                    action,
                    values,
                    now=timestamp,
                    dry_run=dry_run,
                )
                if dry_run:
                    transaction.set_rollback(True)
        except (OpportunityAction.DoesNotExist, ValidationError, ValueError) as exc:
            report.invalid.append(InvalidSheetEdit(
                opportunity_id=action_id,
                fields=tuple(sorted(values)),
                reason=_validation_message(exc),
            ))
            continue
        report.actions_updated += 1
        report.fields_imported += len(values)
        report.completed += outcome["completed"]
        report.reopened += outcome["reopened"]
        report.opportunities_pinned += outcome["opportunities_pinned"]
    return report


def commit_followup_baselines(
    baseline_updates: Iterable[Any],
    *,
    published_at: datetime | None = None,
) -> int:
    from crm.models import OpportunityAction

    timestamp = published_at or timezone.now()
    updated = 0
    with transaction.atomic():
        for item in baseline_updates:
            action_id = str(_item_value(item, "stable_id"))
            values = dict(_item_value(item, "values") or {})
            action = OpportunityAction.objects.select_for_update().get(pk=action_id)
            action.sheet_human_snapshot = {
                field: str(values.get(field) or "")
                for field in crm_sheets.FOLLOWUP_HUMAN_FIELDS
            }
            action.sheet_published_at = timestamp
            action.save(update_fields={
                "sheet_human_snapshot",
                "sheet_published_at",
                "updated_at",
            })
            updated += 1
    return updated


def baseline_by_opportunity_id() -> dict[str, Mapping[str, Any]]:
    from crm.models import OpportunitySheetState

    return {
        str(state.opportunity_id): dict(state.published_human_snapshot or {})
        for state in OpportunitySheetState.objects.all()
    }


def read_people_dont_send_lead_ids(spreadsheet) -> set[int]:
    """Resolve People-level Don't send flags without ever using contact names."""
    from crm.models import Lead
    from linkedin.exceptions import SheetsError
    from linkedin.notifications import sheets

    try:
        ws = spreadsheet.worksheet(sheets.GOOGLE_SHEETS_TAB_NAME)
        values = ws.get_all_values()
    except (WorksheetNotFound, APIError) as exc:
        raise SheetsError(f"failed reading People Don't send flags: {exc}") from exc
    if not values:
        raise SheetsError("People is empty; cannot safely resolve Don't send state")
    headers = [str(value).strip() for value in values[0]]
    required = {sheets.COL_OUTREACH_STATUS, sheets.COL_LINKEDIN_URL}
    if not required.issubset(headers):
        missing = sorted(required.difference(headers))
        raise SheetsError(
            "People is missing safety-critical Don't send column(s): "
            + ", ".join(missing)
        )
    status_col = headers.index(sheets.COL_OUTREACH_STATUS)
    url_col = headers.index(sheets.COL_LINKEDIN_URL)
    lead_id_col = (
        headers.index(sheets.COL_LEAD_ID)
        if sheets.COL_LEAD_ID in headers
        else None
    )
    ids: set[int] = set()
    id_rows: list[tuple[int, int, str]] = []
    url_rows: list[tuple[int, str]] = []
    malformed_rows: list[int] = []
    target = sheets.STATUS_DONT_SEND.casefold()
    for row_number, row in enumerate(values[1:], start=2):
        status = str(row[status_col] if status_col < len(row) else "").strip()
        if status.casefold() != target:
            continue
        raw_id = str(
            row[lead_id_col]
            if lead_id_col is not None and lead_id_col < len(row)
            else ""
        ).strip()
        url = sheets.canonical_linkedin_url(
            str(row[url_col] if url_col < len(row) else "").strip()
        )
        if raw_id.isdigit():
            id_rows.append((row_number, int(raw_id), url))
            continue
        if url:
            url_rows.append((row_number, url))
            continue
        malformed_rows.append(row_number)
    if malformed_rows:
        raise SheetsError(
            "People Don't send row(s) have no resolvable stable identity: "
            + ", ".join(str(row) for row in malformed_rows)
        )

    requested_urls = {
        url
        for _row_number, url in url_rows
    } | {
        url
        for _row_number, _lead_id, url in id_rows
        if url
    }
    canonical_to_ids: dict[str, set[int]] = defaultdict(set)
    if requested_urls:
        for lead_id, linked_in_url in Lead.objects.exclude(
            linkedin_url="",
        ).values_list("id", "linkedin_url"):
            canonical = sheets.canonical_linkedin_url(linked_in_url or "")
            if canonical in requested_urls:
                canonical_to_ids[canonical].add(lead_id)

    leads_by_id = Lead.objects.in_bulk({lead_id for _row, lead_id, _url in id_rows})
    for row_number, lead_id, url in id_rows:
        lead = leads_by_id.get(lead_id)
        if lead is None:
            raise SheetsError(
                f"People Don't send row {row_number} references an unknown Lead ID"
            )
        stored_url = sheets.canonical_linkedin_url(lead.linkedin_url or "")
        if url and stored_url and url != stored_url:
            raise SheetsError(
                f"People Don't send identity conflict at row {row_number}"
            )
        if url:
            matches = canonical_to_ids.get(url, set())
            if len(matches) > 1:
                raise SheetsError(
                    f"People Don't send LinkedIn identity is ambiguous at row {row_number}"
                )
            if matches and matches != {lead_id}:
                raise SheetsError(
                    f"People Don't send identity conflict at row {row_number}"
                )
        ids.add(lead_id)
    for row_number, url in url_rows:
        matches = canonical_to_ids.get(url, set())
        if len(matches) > 1:
            raise SheetsError(
                f"People Don't send LinkedIn identity is ambiguous at row {row_number}"
            )
        if not matches:
            raise SheetsError(
                f"People Don't send row {row_number} does not match a Lead"
            )
        ids.update(matches)
    return ids


def _apply_one_followup_action(
    action,
    values: Mapping[str, str],
    *,
    now: datetime,
    dry_run: bool,
) -> dict[str, int]:
    from crm.models import OpportunityAction

    waiting_edited = crm_sheets.COL_WAITING_UNTIL in values
    if waiting_edited:
        action.waiting_until = _parse_date(values[crm_sheets.COL_WAITING_UNTIL])
    if crm_sheets.COL_CHANNEL in values:
        channel = values[crm_sheets.COL_CHANNEL].strip()
        if len(channel) > 32:
            raise ValidationError({crm_sheets.COL_CHANNEL: "maximum length is 32"})
        action.channel = channel
    if crm_sheets.COL_DRAFT in values:
        action.draft = values[crm_sheets.COL_DRAFT]
    if crm_sheets.COL_MANUAL_PIN in values:
        action.opportunity.manual_pin = _parse_bool(values[crm_sheets.COL_MANUAL_PIN])

    disposition_aliases = {
        "": "",
        "sent": OpportunityAction.Disposition.SENT,
        "handled": OpportunityAction.Disposition.HANDLED,
        "deferred": OpportunityAction.Disposition.DEFERRED,
        "polite decline": OpportunityAction.Disposition.POLITE_DECLINE,
        "polite_decline": OpportunityAction.Disposition.POLITE_DECLINE,
        "no action": OpportunityAction.Disposition.NO_ACTION,
        "no_action": OpportunityAction.Disposition.NO_ACTION,
    }
    if crm_sheets.COL_DISPOSITION in values:
        raw_disposition = " ".join(
            values[crm_sheets.COL_DISPOSITION].strip().casefold().split()
        )
        if raw_disposition not in disposition_aliases:
            raise ValidationError({crm_sheets.COL_DISPOSITION: "unknown disposition"})
        action.disposition = disposition_aliases[raw_disposition]

    handled = None
    if crm_sheets.COL_HANDLED in values:
        handled = _parse_bool(values[crm_sheets.COL_HANDLED])
    terminal_dispositions = {
        OpportunityAction.Disposition.SENT,
        OpportunityAction.Disposition.HANDLED,
        OpportunityAction.Disposition.POLITE_DECLINE,
        OpportunityAction.Disposition.NO_ACTION,
    }
    should_complete = handled is True or action.disposition in terminal_dispositions
    completed = reopened = 0
    if should_complete:
        if action.status != OpportunityAction.Status.COMPLETED:
            completed = 1
        action.status = OpportunityAction.Status.COMPLETED
        action.handled_at = action.handled_at or now
        action.completed_at = action.completed_at or now
        if action.disposition == OpportunityAction.Disposition.SENT:
            action.sent_at = action.sent_at or now
    elif handled is False and action.status == OpportunityAction.Status.COMPLETED:
        competing = OpportunityAction.objects.filter(
            opportunity=action.opportunity,
            status__in=[OpportunityAction.Status.OPEN, OpportunityAction.Status.WAITING],
        ).exclude(pk=action.pk).exists()
        if competing:
            raise ValidationError({
                crm_sheets.COL_HANDLED: "cannot reopen while another current action exists",
            })
        action.status = (
            OpportunityAction.Status.WAITING
            if action.waiting_until and action.waiting_until > _business_date(now)
            else OpportunityAction.Status.OPEN
        )
        action.handled_at = None
        action.completed_at = None
        action.sent_at = None
        reopened = 1
    elif waiting_edited and action.status in {
        OpportunityAction.Status.OPEN,
        OpportunityAction.Status.WAITING,
    }:
        action.status = (
            OpportunityAction.Status.WAITING
            if action.waiting_until and action.waiting_until > _business_date(now)
            else OpportunityAction.Status.OPEN
        )

    if action.disposition == OpportunityAction.Disposition.DEFERRED and not action.waiting_until:
        raise ValidationError({
            crm_sheets.COL_DISPOSITION: "Deferred requires Waiting until on Opportunities",
        })
    action.human_revision += 1
    action.full_clean()
    if not dry_run:
        action.opportunity.save(update_fields={"manual_pin", "updated_at"})
        action.save()
    return {
        "completed": completed,
        "reopened": reopened,
        "opportunities_pinned": int(crm_sheets.COL_MANUAL_PIN in values),
    }


def _apply_one_opportunity(
    opportunity,
    values: Mapping[str, str],
    *,
    now: datetime,
    dry_run: bool,
) -> dict[str, int]:
    from crm.models import Lead, Opportunity, OpportunityAction, SalesOwner

    stage = opportunity.stage
    step = opportunity.sales_motion_step
    if crm_sheets.COL_STAGE in values:
        stage_key = " ".join(values[crm_sheets.COL_STAGE].strip().casefold().split())
        stage = _STAGE_ALIASES.get(stage_key, "")
        if not stage:
            raise ValidationError({crm_sheets.COL_STAGE: "unknown stage"})
    if crm_sheets.COL_SALES_MOTION_STEP in values:
        raw_step = values[crm_sheets.COL_SALES_MOTION_STEP].strip()
        step = int(raw_step) if raw_step else None
    elif stage != opportunity.stage:
        step = _DEFAULT_STEP_BY_STAGE.get(stage)

    if crm_sheets.COL_OWNER in values:
        raw_owner = values[crm_sheets.COL_OWNER].strip()
        if raw_owner:
            handle = resolve_sales_owner_handle(raw_owner)
            if not handle:
                raise ValidationError({crm_sheets.COL_OWNER: "unknown owner"})
            owner = SalesOwner.objects.filter(handle=handle, active=True).first()
            if owner is None:
                raise ValidationError({crm_sheets.COL_OWNER: "owner is inactive or missing"})
            opportunity.owner = owner
        else:
            opportunity.owner = None

    if crm_sheets.COL_MANUAL_PIN in values:
        opportunity.manual_pin = _parse_bool(values[crm_sheets.COL_MANUAL_PIN])
    if crm_sheets.COL_VALUE in values:
        opportunity.value = _parse_decimal(values[crm_sheets.COL_VALUE], blank=None)
    if crm_sheets.COL_CURRENCY in values:
        currency = values[crm_sheets.COL_CURRENCY].strip().upper()
        if not currency:
            currency = "USD"
        if len(currency) != 3 or not currency.isalpha():
            raise ValidationError({crm_sheets.COL_CURRENCY: "use a three-letter currency"})
        opportunity.currency = currency
    if crm_sheets.COL_PROBABILITY in values:
        opportunity.probability = _parse_decimal(
            values[crm_sheets.COL_PROBABILITY],
            blank=None,
        )
    if crm_sheets.COL_CLOSED_WON_AT in values:
        opportunity.closed_won_at = _parse_datetime(values[crm_sheets.COL_CLOSED_WON_AT])
    if crm_sheets.COL_CLOSED_LOST_AT in values:
        opportunity.closed_lost_at = _parse_datetime(values[crm_sheets.COL_CLOSED_LOST_AT])
    if crm_sheets.COL_CLOSED_LOST_REASON in values:
        opportunity.closed_lost_reason = values[crm_sheets.COL_CLOSED_LOST_REASON].strip()

    if stage == Opportunity.Stage.CLOSED_WON:
        opportunity.closed_won_at = opportunity.closed_won_at or now
        opportunity.closed_lost_at = None
        opportunity.closed_lost_reason = ""
    elif stage == Opportunity.Stage.CLOSED_LOST:
        opportunity.closed_lost_at = opportunity.closed_lost_at or now
        opportunity.closed_won_at = None
        if not opportunity.closed_lost_reason:
            raise ValidationError({
                crm_sheets.COL_CLOSED_LOST_REASON: "Closed Lost requires a reason",
            })
    else:
        if opportunity.stage == Opportunity.Stage.CLOSED_WON:
            opportunity.closed_won_at = None
        if opportunity.stage == Opportunity.Stage.CLOSED_LOST:
            opportunity.closed_lost_at = None
            opportunity.closed_lost_reason = ""

    opportunity.stage = stage
    opportunity.sales_motion_step = step
    opportunity.source = Opportunity.Source.SHEET
    opportunity.human_revision += 1
    opportunity._stage_event_source = Opportunity.Source.SHEET
    opportunity.full_clean()

    role_changes = 0
    parsed_roles: dict[str, set[int]] = {}
    for field_name, role in _ROLE_FIELDS.items():
        if field_name not in values:
            continue
        ids = _parse_lead_ids(values[field_name])
        if role in {"champion", "decision_maker"} and len(ids) > 1:
            raise ValidationError({field_name: "only one Lead ID is allowed"})
        existing_ids = set(Lead.objects.filter(pk__in=ids).values_list("id", flat=True))
        if existing_ids != ids:
            missing = sorted(ids - existing_ids)
            raise ValidationError({field_name: f"unknown Lead ID(s): {missing}"})
        parsed_roles[role] = ids

    current_action = opportunity.actions.filter(
        status__in=[OpportunityAction.Status.OPEN, OpportunityAction.Status.WAITING],
    ).first()
    if current_action is not None and current_action.target_lead_id is not None:
        # Sheet role edits transition an existing contact in place rather than
        # unlinking it.  Existing contacts therefore remain valid action
        # targets even when their role is cleared (to OTHER) or moved.
        prospective_contact_ids = set(
            opportunity.contacts.values_list("lead_id", flat=True)
        )
        for lead_ids in parsed_roles.values():
            prospective_contact_ids.update(lead_ids)
        if current_action.target_lead_id not in prospective_contact_ids:
            raise ValidationError({
                crm_sheets.COL_CONTACT_LEAD_IDS: (
                    "cannot unlink the current action target; retarget or "
                    "complete the action first"
                ),
            })
    action_fields = {
        crm_sheets.COL_NEXT_ACTION,
        crm_sheets.COL_NEXT_ACTION_DUE,
        crm_sheets.COL_WAITING_UNTIL,
    }
    has_action_edit = bool(action_fields & set(values))
    description = (
        values.get(crm_sheets.COL_NEXT_ACTION, current_action.description if current_action else "")
    ).strip()
    due_on = (
        _parse_date(values[crm_sheets.COL_NEXT_ACTION_DUE])
        if crm_sheets.COL_NEXT_ACTION_DUE in values
        else current_action.due_on if current_action else None
    )
    waiting_until = (
        _parse_date(values[crm_sheets.COL_WAITING_UNTIL])
        if crm_sheets.COL_WAITING_UNTIL in values
        else current_action.waiting_until if current_action else None
    )
    action_outcome = {"actions_created": 0, "actions_updated": 0, "actions_cancelled": 0}

    if not dry_run:
        opportunity.save()
        role_changes = _reconcile_sheet_contact_roles(
            opportunity,
            parsed_roles,
            apply=True,
        )

        if has_action_edit:
            if not description and due_on is None and waiting_until is None:
                if current_action is not None:
                    current_action.status = OpportunityAction.Status.CANCELLED
                    current_action.save(update_fields={"status", "updated_at"})
                    action_outcome["actions_cancelled"] = 1
            else:
                if current_action is None:
                    current_action = OpportunityAction(
                        opportunity=opportunity,
                        kind=OpportunityAction.Kind.NEXT_STEP,
                        description=description or "Follow up",
                        idempotency_key=f"sheet:{opportunity.human_revision}",
                    )
                    action_outcome["actions_created"] = 1
                else:
                    current_action.description = description or "Follow up"
                    action_outcome["actions_updated"] = 1
                if current_action.target_lead_id is None:
                    current_action.target_lead_id = _unambiguous_action_target_id(
                        opportunity,
                        parsed_roles=parsed_roles,
                    )
                if current_action.target_lead_id is None:
                    raise ValidationError({
                        crm_sheets.COL_NEXT_ACTION: (
                            "choose one Champion, Decision Maker, primary contact, "
                            "or a single linked contact before assigning an action"
                        ),
                    })
                current_action.due_on = due_on
                current_action.waiting_until = waiting_until
                current_action.status = (
                    OpportunityAction.Status.WAITING
                    if waiting_until is not None and waiting_until > _business_date(now)
                    else OpportunityAction.Status.OPEN
                )
                current_action.full_clean()
                current_action.save()
        elif (
            current_action is not None
            and current_action.target_lead_id is None
            and parsed_roles
        ):
            target_id = _unambiguous_action_target_id(
                opportunity,
                parsed_roles=parsed_roles,
            )
            if target_id is not None:
                current_action.target_lead_id = target_id
                current_action.full_clean()
                current_action.save(update_fields={"target_lead", "updated_at"})
                action_outcome["actions_updated"] = 1
    else:
        role_changes = _reconcile_sheet_contact_roles(
            opportunity,
            parsed_roles,
            apply=False,
        )
        # Validate action shape without persisting it.
        if has_action_edit and (description or due_on is not None or waiting_until is not None):
            candidate = current_action or OpportunityAction(
                opportunity=opportunity,
                kind=OpportunityAction.Kind.NEXT_STEP,
                description=description or "Follow up",
            )
            candidate.description = description or "Follow up"
            if candidate.target_lead_id is None:
                candidate.target_lead_id = _unambiguous_action_target_id(
                    opportunity,
                    parsed_roles=parsed_roles,
                )
            if candidate.target_lead_id is None:
                raise ValidationError({
                    crm_sheets.COL_NEXT_ACTION: (
                        "choose one Champion, Decision Maker, primary contact, "
                        "or a single linked contact before assigning an action"
                    ),
                })
            candidate.due_on = due_on
            candidate.waiting_until = waiting_until
            candidate.status = (
                OpportunityAction.Status.WAITING
                if waiting_until is not None and waiting_until > _business_date(now)
                else OpportunityAction.Status.OPEN
            )
            candidate.full_clean(exclude={"idempotency_key"})
        elif (
            current_action is not None
            and current_action.target_lead_id is None
            and parsed_roles
            and _unambiguous_action_target_id(
                opportunity,
                parsed_roles=parsed_roles,
            ) is not None
        ):
            action_outcome["actions_updated"] = 1

    return {**action_outcome, "contact_roles_updated": role_changes}


def _reconcile_sheet_contact_roles(
    opportunity,
    parsed_roles: Mapping[str, set[int]],
    *,
    apply: bool,
) -> int:
    """Apply Sheet-owned role assignments without replacing contact records.

    A role cell is authoritative only when it is present in ``parsed_roles``.
    Contacts removed from an edited role are reused for another edited role of
    the same Lead when possible, otherwise they are demoted to OTHER.  This
    preserves the contact UUID and human/system metadata that the Sheet does
    not own (notes, primary status, and creation time).

    Role swaps are parked under transaction-local temporary values before the
    final roles are written so the unique opportunity/lead/role constraint is
    never used as a reason to delete and recreate a contact.
    """
    from crm.models import OpportunityContact

    if not parsed_roles:
        return 0

    contacts_query = opportunity.contacts.order_by("created_at", "id")
    if apply:
        contacts_query = contacts_query.select_for_update()
    contacts = list(contacts_query)
    edited_roles = set(parsed_roles)
    desired = {
        (lead_id, role)
        for role, lead_ids in parsed_roles.items()
        for lead_id in lead_ids
    }
    existing_keys = {(contact.lead_id, contact.role) for contact in contacts}

    released = [
        contact
        for contact in contacts
        if contact.role in edited_roles
        and (contact.lead_id, contact.role) not in desired
    ]
    reusable_by_lead: dict[int, list[Any]] = defaultdict(list)
    for contact in released:
        reusable_by_lead[contact.lead_id].append(contact)
    for contact in contacts:
        if contact.role == OpportunityContact.Role.OTHER:
            reusable_by_lead[contact.lead_id].append(contact)

    role_order = (
        OpportunityContact.Role.CHAMPION,
        OpportunityContact.Role.DECISION_MAKER,
        OpportunityContact.Role.STAKEHOLDER,
    )
    assignments: dict[Any, str] = {}
    used_ids: set[Any] = set()
    creates: list[tuple[int, str]] = []
    for role in role_order:
        for lead_id in sorted(parsed_roles.get(role, set())):
            if (lead_id, role) in existing_keys:
                continue
            candidates = [
                contact
                for contact in reusable_by_lead.get(lead_id, [])
                if contact.pk not in used_ids
            ]
            if candidates:
                contact = candidates[0]
                assignments[contact.pk] = role
                used_ids.add(contact.pk)
            else:
                creates.append((lead_id, role))

    for contact in released:
        if contact.pk not in used_ids:
            assignments[contact.pk] = OpportunityContact.Role.OTHER

    final_keys: set[tuple[int, str]] = set()
    for contact in contacts:
        key = (contact.lead_id, assignments.get(contact.pk, contact.role))
        if key in final_keys:
            raise ValidationError({
                crm_sheets.COL_CONTACT_LEAD_IDS: (
                    "role edit would collapse two contact records for the same Lead; "
                    "resolve the duplicate roles manually"
                ),
            })
        final_keys.add(key)
    for key in creates:
        if key in final_keys:
            raise ValidationError({
                crm_sheets.COL_CONTACT_LEAD_IDS: (
                    "role edit would create a duplicate contact role"
                ),
            })
        final_keys.add(key)

    changed_contacts = [
        contact
        for contact in contacts
        if contact.pk in assignments and assignments[contact.pk] != contact.role
    ]
    change_count = len(changed_contacts) + len(creates)
    if not apply or change_count == 0:
        return change_count

    temporary_roles = {
        contact.pk: f"_sheet_transition_{index}"
        for index, contact in enumerate(changed_contacts)
    }
    occupied_roles = {contact.role for contact in contacts}
    if any(role.startswith("_sheet_transition_") for role in occupied_roles):
        raise ValidationError({
            crm_sheets.COL_CONTACT_LEAD_IDS: "reserved role transition value is already in use",
        })
    for contact in changed_contacts:
        OpportunityContact.objects.filter(pk=contact.pk).update(
            role=temporary_roles[contact.pk],
        )
    for contact in changed_contacts:
        contact.role = assignments[contact.pk]
        contact.save(update_fields={"role", "updated_at"})
    for lead_id, role in creates:
        OpportunityContact.objects.create(
            opportunity=opportunity,
            lead_id=lead_id,
            role=role,
        )
    return change_count


def _item_value(item: Any, name: str):
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name)


def _unambiguous_action_target_id(
    opportunity,
    *,
    parsed_roles: Mapping[str, set[int]] | None = None,
) -> int | None:
    """Select a recipient only from explicit, unambiguous contact evidence."""
    parsed = parsed_roles or {}
    for role in ("champion", "decision_maker"):
        if role in parsed:
            lead_ids = parsed[role]
            if len(lead_ids) == 1:
                return next(iter(lead_ids))
            if not lead_ids:
                continue
        existing = set(
            opportunity.contacts.filter(role=role).values_list("lead_id", flat=True)
        )
        if len(existing) == 1:
            return next(iter(existing))
    primary = set(
        opportunity.contacts.filter(is_primary=True).values_list("lead_id", flat=True)
    )
    if len(primary) == 1:
        return next(iter(primary))
    contacts = set(opportunity.contacts.values_list("lead_id", flat=True))
    return next(iter(contacts)) if len(contacts) == 1 else None


def _parse_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"true", "yes", "1", "y", "checked"}:
        return True
    if normalized in {"false", "no", "0", "n", "", "unchecked"}:
        return False
    raise ValidationError(f"invalid boolean: {value!r}")


def _parse_decimal(value: str, *, blank):
    raw = value.strip().replace(",", "").replace("$", "")
    return blank if not raw else Decimal(raw)


def _parse_date(value: str) -> date | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise ValidationError(f"invalid ISO date: {value!r}") from exc


def _parse_datetime(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            local_midnight = datetime.combine(date.fromisoformat(raw), datetime.min.time())
            return local_midnight.replace(tzinfo=ZoneInfo(ACTIVE_TIMEZONE)).astimezone(UTC)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"invalid ISO datetime: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(ACTIVE_TIMEZONE)).astimezone(UTC)


def _parse_lead_ids(value: str) -> set[int]:
    raw_items = [item.strip() for item in value.replace("\n", ",").split(",")]
    try:
        return {int(item) for item in raw_items if item}
    except ValueError as exc:
        raise ValidationError(f"Lead IDs must be comma-separated integers: {value!r}") from exc


def _business_date(value: datetime) -> date:
    return timezone.localtime(value, ZoneInfo(ACTIVE_TIMEZONE)).date()


def _current_action_id(opportunity):
    from crm.models import OpportunityAction

    return opportunity.actions.filter(
        status__in=[OpportunityAction.Status.OPEN, OpportunityAction.Status.WAITING],
    ).values_list("id", flat=True).first()


def _validation_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        if hasattr(exc, "message_dict"):
            return "; ".join(
                f"{field}: {', '.join(messages)}"
                for field, messages in exc.message_dict.items()
            )
        return "; ".join(exc.messages)
    return str(exc)
