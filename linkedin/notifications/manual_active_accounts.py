"""Read the operator-owned ``Active Accounts`` ledger without writing it.

The daily CRM workflow owns ``Actions``.  ``Active Accounts`` is deliberately
different: it is a compact, Attio-style company list that the operator may
reorder, extend, annotate, or prune manually.  Rows carrying a valid hidden
Opportunity ID may update only Owner and Stage in the database and define the
exact Actions inclusion scope. Unbound rows remain useful Sheet-only leads but
cannot authorize Actions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from gspread.exceptions import APIError

from linkedin.exceptions import SheetsError
from linkedin.notifications import crm_sheets


TAB_NAME = "Active Accounts"

COL_COMPANY = "Company"
COL_OWNER = "Owner"
COL_STAGE = "Stage"
COL_MAIN_POINT_OF_CONTACT = "Main point of contact"
COL_NEXT_STEP = "Next step"
COL_NEXT_STEP_DUE = "Next step due"
COL_NOTES = "Notes"
COL_OPPORTUNITY_ID = "Opportunity ID"
COL_ACCOUNT_ID = "Account ID"

HEADERS = (
    COL_COMPANY,
    COL_OWNER,
    COL_STAGE,
    COL_MAIN_POINT_OF_CONTACT,
    COL_NEXT_STEP,
    COL_NEXT_STEP_DUE,
    COL_NOTES,
    COL_OPPORTUNITY_ID,
    COL_ACCOUNT_ID,
)

TECHNICAL_FIELDS = (COL_OPPORTUNITY_ID, COL_ACCOUNT_ID)
READABLE_DATABASE_FIELDS = (COL_OWNER, COL_STAGE)


@dataclass(frozen=True)
class ManualActiveAccountsRead:
    imports: tuple[crm_sheets.HumanFieldImport, ...]
    bound_rows: int
    unbound_rows: int
    bound_opportunity_ids: frozenset[str]


def read_imports(worksheet: Any) -> ManualActiveAccountsRead:
    """Return exact-ID Owner/Stage edits; never mutate the worksheet.

    Company names are intentionally not identity.  A manually added row with
    no Opportunity ID is a valid ledger row, but it cannot affect the database
    until a future explicit binding workflow is introduced.
    """
    try:
        values = worksheet.get_all_values()
    except APIError as exc:
        raise SheetsError("failed reading manual Active Accounts") from exc
    if not values:
        raise SheetsError("Active Accounts is empty")
    headers = tuple(str(value).strip() for value in values[0])
    if len(headers) != len(set(headers)):
        raise SheetsError("Active Accounts contains duplicate headings")
    missing = [header for header in HEADERS if header not in headers]
    if missing:
        raise SheetsError(
            "Active Accounts is missing manual-ledger heading(s): "
            + ", ".join(missing)
        )
    indexes = {header: headers.index(header) for header in HEADERS}

    from crm.models import Opportunity

    parsed: list[tuple[int, str, dict[str, str]]] = []
    unbound_rows = 0
    seen: set[str] = set()
    for row_number, row in enumerate(values[1:], start=2):
        cells = {
            header: str(row[index] if index < len(row) else "").strip()
            for header, index in indexes.items()
        }
        if not any(cells.values()):
            continue
        raw_id = cells[COL_OPPORTUNITY_ID]
        if not raw_id:
            unbound_rows += 1
            continue
        try:
            stable_id = str(UUID(raw_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise SheetsError(
                f"Active Accounts row {row_number} has an invalid Opportunity ID"
            ) from exc
        if stable_id in seen:
            raise SheetsError("Active Accounts contains duplicate Opportunity IDs")
        seen.add(stable_id)
        parsed.append((row_number, stable_id, cells))

    opportunities = {
        str(item.id): item
        for item in Opportunity.objects.filter(pk__in=[item[1] for item in parsed])
        .select_related("account", "owner")
    }
    imports: list[crm_sheets.HumanFieldImport] = []
    for row_number, stable_id, cells in parsed:
        opportunity = opportunities.get(stable_id)
        if opportunity is None:
            raise SheetsError(
                f"Active Accounts row {row_number} references an unknown Opportunity ID"
            )
        account_id = cells[COL_ACCOUNT_ID]
        if account_id and account_id != str(opportunity.account_id):
            raise SheetsError(
                f"Active Accounts row {row_number} has conflicting stable IDs"
            )
        database_values = {
            COL_OWNER: opportunity.owner.handle if opportunity.owner_id else "",
            COL_STAGE: opportunity.get_stage_display(),
        }
        for field in READABLE_DATABASE_FIELDS:
            sheet_value = cells[field]
            if _semantic(field, sheet_value) == _semantic(field, database_values[field]):
                continue
            imports.append(crm_sheets.HumanFieldImport(
                stable_id=stable_id,
                field=field,
                value=sheet_value,
            ))
    return ManualActiveAccountsRead(
        imports=tuple(imports),
        bound_rows=len(parsed),
        unbound_rows=unbound_rows,
        bound_opportunity_ids=frozenset(stable_id for _, stable_id, _ in parsed),
    )


def _semantic(field: str, value: str) -> str:
    normalized = " ".join(str(value or "").strip().casefold().split())
    if field == COL_STAGE:
        return normalized.replace("_", " ").replace("/", " ")
    return normalized
