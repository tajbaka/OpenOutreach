"""Pure serializers for the concise, account-first CRM Sheet views.

Admission into ``Active Accounts`` is intentionally outside this module.  The
caller supplies the already-reviewed active account universe plus its evidence;
this serializer only makes that decision legible and produces one stable row
per account.  This prevents a Sheet view from quietly re-inventing qualification
from whatever source happens to contain the most records.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

from linkedin.exceptions import SheetsError
from linkedin.notifications import crm_v2_sheets


@dataclass(frozen=True)
class ActiveAccountRecord:
    """One admitted account and the canonical opportunity representing it."""

    opportunity_id: Any
    account_id: Any
    account: str
    owner: str = ""
    stage: str = ""
    attention: str = "None"
    why_active: str = ""
    evidence_tier: str = ""
    outreach: str = "Allowed"
    last_meaningful_touch: date | datetime | str | None = None
    next_action: str = ""
    next_action_due: date | datetime | str | None = None
    waiting_until: date | datetime | str | None = None
    who_owes_whom: str = ""
    key_contacts: str = ""
    manual_pin: bool = False


@dataclass(frozen=True)
class ActionRecord:
    """One current operator action belonging to an admitted account."""

    action_id: Any
    opportunity_id: Any
    account_id: Any
    account: str
    owner: str
    contact: str = ""
    lead_id: Any = ""
    why_now: str = ""
    outreach: str = "Allowed"
    next_action: str = ""
    next_action_due: date | datetime | str | None = None
    waiting_until: date | datetime | str | None = None
    who_owes_whom: str = ""
    channel: str = ""
    draft: str = ""
    handled: bool = False
    disposition: str = ""


@dataclass(frozen=True)
class CrmV2ViewRows:
    """The only two managed v2 sales-work surfaces."""

    active_accounts: tuple[dict[str, str], ...]
    actions: tuple[dict[str, str], ...]


def active_account_row(record: ActiveAccountRecord) -> dict[str, str]:
    """Serialize one admitted account without inferring why it is active."""
    return {
        crm_v2_sheets.COL_OPPORTUNITY_ID: _required_id(
            record.opportunity_id,
            field=crm_v2_sheets.COL_OPPORTUNITY_ID,
        ),
        crm_v2_sheets.COL_ACCOUNT_ID: _required_id(
            record.account_id,
            field=crm_v2_sheets.COL_ACCOUNT_ID,
        ),
        crm_v2_sheets.COL_ACCOUNT: _required_text(
            record.account,
            field=crm_v2_sheets.COL_ACCOUNT,
        ),
        crm_v2_sheets.COL_OWNER: _text(record.owner),
        crm_v2_sheets.COL_STAGE: _required_text(
            record.stage,
            field=crm_v2_sheets.COL_STAGE,
        ),
        crm_v2_sheets.COL_ATTENTION: _attention_value(record.attention),
        crm_v2_sheets.COL_WHY_ACTIVE: _required_text(
            record.why_active,
            field=crm_v2_sheets.COL_WHY_ACTIVE,
        ),
        crm_v2_sheets.COL_EVIDENCE_TIER: _required_text(
            record.evidence_tier,
            field=crm_v2_sheets.COL_EVIDENCE_TIER,
        ),
        crm_v2_sheets.COL_OUTREACH: _outreach_value(record.outreach),
        crm_v2_sheets.COL_LAST_MEANINGFUL_TOUCH: _sheet_value(
            record.last_meaningful_touch,
        ),
        crm_v2_sheets.COL_NEXT_ACTION: _text(record.next_action),
        crm_v2_sheets.COL_NEXT_ACTION_DUE: _date_value(record.next_action_due),
        crm_v2_sheets.COL_WAITING_UNTIL: _date_value(record.waiting_until),
        crm_v2_sheets.COL_WHO_OWES: _text(record.who_owes_whom),
        crm_v2_sheets.COL_KEY_CONTACTS: _text(record.key_contacts),
        crm_v2_sheets.COL_MANUAL_PIN: _sheet_value(record.manual_pin),
    }


def action_row(record: ActionRecord) -> dict[str, str]:
    """Serialize one current action; sender ownership remains explicit."""
    return {
        crm_v2_sheets.COL_ACTION_ID: _required_id(
            record.action_id,
            field=crm_v2_sheets.COL_ACTION_ID,
        ),
        crm_v2_sheets.COL_OPPORTUNITY_ID: _required_id(
            record.opportunity_id,
            field=crm_v2_sheets.COL_OPPORTUNITY_ID,
        ),
        crm_v2_sheets.COL_ACCOUNT_ID: _required_id(
            record.account_id,
            field=crm_v2_sheets.COL_ACCOUNT_ID,
        ),
        crm_v2_sheets.COL_ACCOUNT: _required_text(
            record.account,
            field=crm_v2_sheets.COL_ACCOUNT,
        ),
        crm_v2_sheets.COL_OWNER: _required_text(
            record.owner,
            field=crm_v2_sheets.COL_OWNER,
        ),
        # Some authoritative account-level reminders (for example a Sales
        # Motion account missing a mapped contact) deliberately have no Lead
        # target.  Keep that absence explicit instead of guessing a person.
        crm_v2_sheets.COL_LEAD_ID: _sheet_value(record.lead_id).strip(),
        crm_v2_sheets.COL_CONTACT: _text(record.contact) or "Account-level",
        crm_v2_sheets.COL_WHY_NOW: _required_text(
            record.why_now,
            field=crm_v2_sheets.COL_WHY_NOW,
        ),
        crm_v2_sheets.COL_OUTREACH: _outreach_value(record.outreach),
        crm_v2_sheets.COL_NEXT_ACTION: _required_text(
            record.next_action,
            field=crm_v2_sheets.COL_NEXT_ACTION,
        ),
        crm_v2_sheets.COL_NEXT_ACTION_DUE: _date_value(record.next_action_due),
        crm_v2_sheets.COL_WAITING_UNTIL: _date_value(record.waiting_until),
        crm_v2_sheets.COL_WHO_OWES: _text(record.who_owes_whom),
        crm_v2_sheets.COL_CHANNEL: _text(record.channel),
        crm_v2_sheets.COL_DRAFT: _text(record.draft),
        crm_v2_sheets.COL_HANDLED: _sheet_value(record.handled),
        crm_v2_sheets.COL_DISPOSITION: _text(record.disposition),
    }


def build_crm_v2_view_rows(
    active_accounts: Iterable[ActiveAccountRecord],
    actions: Iterable[ActionRecord],
) -> CrmV2ViewRows:
    """Build a compact, internally consistent Active Accounts + Actions pair.

    The active-account payload must contain exactly one row per Account ID and
    one row per Opportunity ID.  Every Action must point at that same admitted
    account/opportunity pair.  Failing closed here prevents a noisy action from
    re-admitting an account that the evidence policy rejected.
    """
    account_records = tuple(active_accounts)
    action_records = tuple(actions)
    account_rows = tuple(active_account_row(record) for record in account_records)
    action_rows = tuple(action_row(record) for record in action_records)

    _assert_unique(
        account_rows,
        crm_v2_sheets.COL_ACCOUNT_ID,
        subject="active account",
    )
    _assert_unique(
        account_rows,
        crm_v2_sheets.COL_OPPORTUNITY_ID,
        subject="active opportunity",
    )
    _assert_unique(action_rows, crm_v2_sheets.COL_ACTION_ID, subject="action")

    admitted_pairs = {
        (
            row[crm_v2_sheets.COL_OPPORTUNITY_ID],
            row[crm_v2_sheets.COL_ACCOUNT_ID],
        )
        for row in account_rows
    }
    outside = [
        row[crm_v2_sheets.COL_ACTION_ID]
        for row in action_rows
        if (
            row[crm_v2_sheets.COL_OPPORTUNITY_ID],
            row[crm_v2_sheets.COL_ACCOUNT_ID],
        ) not in admitted_pairs
    ]
    if outside:
        raise SheetsError(
            f"{len(outside)} action(s) do not belong to an admitted active account"
        )

    return CrmV2ViewRows(
        active_accounts=account_rows,
        actions=action_rows,
    )


def _assert_unique(
    rows: Iterable[dict[str, str]],
    field: str,
    *,
    subject: str,
) -> None:
    values = [row[field] for row in rows]
    if len(values) != len(set(values)):
        raise SheetsError(f"CRM v2 payload contains duplicate {subject} {field}")


def _required_id(value: Any, *, field: str) -> str:
    result = _sheet_value(value).strip()
    if not result:
        raise SheetsError(f"CRM v2 payload is missing {field}")
    return result


def _required_text(value: Any, *, field: str) -> str:
    result = _text(value)
    if not result:
        raise SheetsError(f"CRM v2 payload is missing {field}")
    return result


def _attention_value(value: Any) -> str:
    normalized = " ".join(_text(value).casefold().split()) or "none"
    accepted = {item.casefold(): item for item in crm_v2_sheets.ATTENTION_VALUES}
    if normalized not in accepted:
        raise SheetsError(
            "CRM v2 payload has invalid Attention; expected one of "
            f"{', '.join(crm_v2_sheets.ATTENTION_VALUES)}"
        )
    return accepted[normalized]


def _outreach_value(value: Any) -> str:
    normalized = " ".join(_text(value).casefold().split()) or "allowed"
    accepted = {item.casefold(): item for item in crm_v2_sheets.OUTREACH_VALUES}
    if normalized not in accepted:
        raise SheetsError(
            "CRM v2 payload has invalid Outreach; expected one of "
            f"{', '.join(crm_v2_sheets.OUTREACH_VALUES)}"
        )
    return accepted[normalized]


def _date_value(value: date | datetime | str | None) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _text(value)


def _sheet_value(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _text(value: Any) -> str:
    # Preserve meaningful internal whitespace (especially multi-line drafts and
    # action descriptions); only trim accidental cell-edge whitespace.
    return _sheet_value(value).strip()
