"""Run-local quarantine for CRM identities that must not be guessed."""
from __future__ import annotations

from dataclasses import asdict, replace
from uuid import UUID

from django.db.models import Q

from crm.models import Account, Opportunity, OpportunityContact
from crm.models.sales import normalize_account_name
from linkedin.exceptions import SheetsError


class QuarantineExpanded(Exception):
    """Restart the whole database transaction with the expanded exclusion set."""


class IdentityQuarantine:
    def __init__(self):
        self.issues = []
        self.keys = set()
        self.lead_ids = set()
        self.opportunity_ids = set()
        self.account_ids = set()

    def check(self, report, rows, *, phase):
        if not report.issues:
            return report
        rows = tuple(rows)
        before = len(self.keys)
        for issue in report.issues:
            affected = [row for row in rows if row.account_key == issue.account_key]
            if not affected:
                raise SheetsError("CRM identity issue could not be scoped safely")
            self.issues.append({
                **asdict(issue), "phase": phase,
                "evidence": [
                    {"account_name": row.account_name, "lead_ids": list(row.lead_ids),
                     "opportunity_id": row.opportunity_id}
                    for row in affected
                ],
            })
            for row in affected:
                self.keys.add(row.account_key)
                self.lead_ids.update(row.lead_ids)
                if row.opportunity_id:
                    self.opportunity_ids.add(row.opportunity_id)
                self.account_ids.update(str(value) for value in Account.objects.filter(
                    normalized_name=normalize_account_name(row.account_name),
                ).values_list("id", flat=True))
            if issue.reason in {"duplicate_account_domain", "business_domain_already_used"}:
                self.account_ids.update(str(value) for value in Account.objects.filter(
                    domain=issue.detail,
                ).values_list("id", flat=True))
            # Some account-resolution issues identify conflicting account IDs.
            for value in issue.detail.split(","):
                try:
                    identity = UUID(value.strip())
                except ValueError:
                    continue
                self.account_ids.update(str(value) for value in Account.objects.filter(
                    pk=identity,
                ).values_list("id", flat=True))
        self._expand_links()
        self.filter(rows)
        if len(self.keys) == before:
            raise SheetsError("CRM identity quarantine made no progress")
        raise QuarantineExpanded()

    def _expand_links(self):
        valid_ids = []
        for value in self.opportunity_ids:
            try:
                valid_ids.append(UUID(value))
            except ValueError:
                pass
        linked = Opportunity.objects.filter(
            Q(id__in=valid_ids) | Q(contacts__lead_id__in=self.lead_ids)
            | Q(account_id__in=self.account_ids)
        )
        self.account_ids.update(str(value) for value in linked.values_list("account_id", flat=True))
        self.opportunity_ids.update(str(value) for value in Opportunity.objects.filter(
            account_id__in=self.account_ids,
        ).values_list("id", flat=True))
        self.lead_ids.update(OpportunityContact.objects.filter(
            opportunity__account_id__in=self.account_ids,
        ).values_list("lead_id", flat=True))

    def filter(self, rows):
        rows = tuple(rows)
        if not self.keys:
            return rows
        # Propagate through shared contacts/anchors even when recollection changes keys.
        while True:
            old = (len(self.keys), len(self.lead_ids), len(self.opportunity_ids))
            for row in rows:
                if (row.account_key in self.keys or self.lead_ids.intersection(row.lead_ids)
                        or row.opportunity_id in self.opportunity_ids):
                    self.keys.add(row.account_key)
                    self.lead_ids.update(row.lead_ids)
                    if row.opportunity_id:
                        self.opportunity_ids.add(row.opportunity_id)
            self._expand_links()
            if old == (len(self.keys), len(self.lead_ids), len(self.opportunity_ids)):
                break
        return tuple(row for row in rows if row.account_key not in self.keys)

    def preserve_actions(self, view, worksheet):
        if not self.keys:
            return view
        from linkedin.notifications import crm_sheets, crm_v2_sheets as sheets

        snapshot = crm_sheets.SheetSnapshot.read(
            worksheet, required_headers=sheets.ACTION_HEADERS, key_header=sheets.COL_ACTION_ID,
        )
        if snapshot.duplicate_keys or snapshot.unkeyed_nonempty_rows:
            raise SheetsError("CRM Actions has ambiguous Sheet row identities")
        protected = []
        protected_ids = set()
        baselines = dict(view.action_baselines)
        for stable_id, numbers in snapshot.rows_by_key.items():
            row = snapshot.row_dict(numbers[0])
            lead_id = str(row.get(sheets.COL_LEAD_ID, "")).strip()
            if (row.get(sheets.COL_OPPORTUNITY_ID) in self.opportunity_ids
                    or row.get(sheets.COL_ACCOUNT_ID) in self.account_ids
                    or (lead_id.isdigit() and int(lead_id) in self.lead_ids)):
                protected_ids.add(stable_id)
                baselines[stable_id] = {
                    field: row.get(field, "") for field in sheets.ACTION_HUMAN_FIELDS
                }
                if row.get(sheets.COL_ACCOUNT, "").strip():
                    protected.append(row)
        return replace(
            view,
            rows=replace(view.rows, actions=(*view.rows.actions, *protected)),
            protected_action_ids=frozenset(protected_ids),
            action_baselines=baselines,
        )

    def payload(self):
        return {
            "schema": "openoutreach.crm-v2-identity-review.v1",
            "issue_count": len(self.issues), "issues": self.issues,
            "skipped_account_keys": sorted(self.keys),
            "protected_account_ids": sorted(self.account_ids),
            "protected_opportunity_ids": sorted(self.opportunity_ids),
            "protected_lead_ids": sorted(self.lead_ids),
        }
