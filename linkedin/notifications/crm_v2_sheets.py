"""Safe Sheet adapters for the concise, account-first CRM v2 views.

This module deliberately adds no Pipeline or Recovery adapter.  Both are
projections of Active Accounts and Actions, not additional managed data sources.
It reuses the battle-tested stable-key and three-way-merge machinery without
changing the existing publisher during the v2 rollout.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from linkedin.notifications import crm_sheets


ACTIVE_ACCOUNTS_TAB = "Active Accounts"
ACTIONS_TAB = "Actions"

# Reuse canonical column spellings so existing validators/importers can consume
# the human edits without translation.
COL_OPPORTUNITY_ID = crm_sheets.COL_OPPORTUNITY_ID
COL_ACCOUNT_ID = crm_sheets.COL_ACCOUNT_ID
COL_ACCOUNT = crm_sheets.COL_ACCOUNT
COL_OWNER = crm_sheets.COL_OWNER
COL_STAGE = crm_sheets.COL_STAGE
COL_NEXT_ACTION = crm_sheets.COL_NEXT_ACTION
COL_NEXT_ACTION_DUE = crm_sheets.COL_NEXT_ACTION_DUE
COL_WAITING_UNTIL = crm_sheets.COL_WAITING_UNTIL
COL_MANUAL_PIN = crm_sheets.COL_MANUAL_PIN
COL_HUMAN_BASELINE = crm_sheets.COL_HUMAN_BASELINE
COL_ACTION_ID = crm_sheets.COL_ACTION_ID
COL_LEAD_ID = crm_sheets.COL_LEAD_ID
COL_CONTACT = crm_sheets.COL_CONTACT
COL_CHANNEL = crm_sheets.COL_CHANNEL
COL_DRAFT = crm_sheets.COL_DRAFT
COL_HANDLED = crm_sheets.COL_HANDLED
COL_DISPOSITION = crm_sheets.COL_DISPOSITION

COL_WHY_ACTIVE = "Why active"
COL_EVIDENCE_TIER = "Evidence tier"
COL_ATTENTION = "Attention"
COL_LAST_MEANINGFUL_TOUCH = "Last meaningful touch"
COL_WHO_OWES = "Who owes"
COL_KEY_CONTACTS = "Key contacts"
COL_WHY_NOW = "Why now"
COL_OUTREACH = "Outreach"

OUTREACH_VALUES = (
    "Allowed",
    "Stopped",
)

ATTENTION_VALUES = (
    "Now",
    "Upcoming",
    "Waiting",
    "Review",
    "Needs contact",
    "None",
)


ACTIVE_ACCOUNT_HUMAN_FIELDS = (
    COL_OWNER,
    COL_STAGE,
    COL_NEXT_ACTION,
    COL_NEXT_ACTION_DUE,
    COL_WAITING_UNTIL,
    COL_MANUAL_PIN,
)

ACTIVE_ACCOUNT_HEADERS = (
    COL_ACCOUNT,
    COL_OWNER,
    COL_STAGE,
    COL_ATTENTION,
    COL_WHY_ACTIVE,
    COL_EVIDENCE_TIER,
    COL_OUTREACH,
    COL_LAST_MEANINGFUL_TOUCH,
    COL_NEXT_ACTION,
    COL_NEXT_ACTION_DUE,
    COL_WAITING_UNTIL,
    COL_WHO_OWES,
    COL_KEY_CONTACTS,
    COL_MANUAL_PIN,
    # Durable plumbing stays at the far right so a fresh tab is useful even
    # before its structural pass hides these technical columns.
    COL_OPPORTUNITY_ID,
    COL_ACCOUNT_ID,
    COL_HUMAN_BASELINE,
)

ACTIVE_ACCOUNT_TECHNICAL_FIELDS = (
    COL_OPPORTUNITY_ID,
    COL_ACCOUNT_ID,
    COL_HUMAN_BASELINE,
)

ACTION_HUMAN_FIELDS = (
    COL_WAITING_UNTIL,
    COL_CHANNEL,
    COL_DRAFT,
    COL_HANDLED,
    COL_DISPOSITION,
)

ACTION_HEADERS = (
    COL_ACCOUNT,
    COL_OWNER,
    COL_CONTACT,
    COL_WHY_NOW,
    COL_OUTREACH,
    COL_NEXT_ACTION,
    COL_NEXT_ACTION_DUE,
    COL_WAITING_UNTIL,
    COL_WHO_OWES,
    COL_CHANNEL,
    COL_DRAFT,
    COL_HANDLED,
    COL_DISPOSITION,
    COL_ACTION_ID,
    COL_OPPORTUNITY_ID,
    COL_ACCOUNT_ID,
    COL_LEAD_ID,
    COL_HUMAN_BASELINE,
)

ACTION_TECHNICAL_FIELDS = (
    COL_ACTION_ID,
    COL_OPPORTUNITY_ID,
    COL_ACCOUNT_ID,
    COL_LEAD_ID,
    COL_HUMAN_BASELINE,
)


class ActiveAccountsSheetAdapter(crm_sheets.DerivedSheetAdapter):
    """Replace the active projection in place while keeping stable row IDs.

    Rows that leave the admitted universe have their managed cells cleared, but
    their stable Opportunity ID and human baseline remain.  Unknown/operator
    columns, formulas, formatting, comments, and the worksheet itself are not
    removed.
    """

    def __init__(self, ws):
        super().__init__(
            ws,
            headers=ACTIVE_ACCOUNT_HEADERS,
            key_header=COL_OPPORTUNITY_ID,
            human_fields=ACTIVE_ACCOUNT_HUMAN_FIELDS,
            human_baseline_header=COL_HUMAN_BASELINE,
        )

    def plan(
        self,
        desired_rows: Iterable[Mapping[str, Any]],
        *,
        baseline_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> crm_sheets.TabMutationPlan:
        return super().plan(
            desired_rows,
            remove_missing=True,
            baseline_by_id=baseline_by_id,
        )


class ActionsSheetAdapter(crm_sheets.DerivedSheetAdapter):
    """One owner-filterable current-work queue keyed by durable Action ID."""

    def __init__(self, ws):
        super().__init__(
            ws,
            headers=ACTION_HEADERS,
            key_header=COL_ACTION_ID,
            human_fields=ACTION_HUMAN_FIELDS,
            human_baseline_header=COL_HUMAN_BASELINE,
        )

    def plan(
        self,
        desired_rows: Iterable[Mapping[str, Any]],
        *,
        baseline_by_id: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> crm_sheets.TabMutationPlan:
        return super().plan(
            desired_rows,
            remove_missing=True,
            baseline_by_id=baseline_by_id,
        )


def active_accounts_adapter(ws) -> ActiveAccountsSheetAdapter:
    return ActiveAccountsSheetAdapter(ws)


def actions_adapter(ws) -> ActionsSheetAdapter:
    return ActionsSheetAdapter(ws)
