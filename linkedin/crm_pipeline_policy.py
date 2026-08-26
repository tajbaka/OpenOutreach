"""Conservative promotion from the broad CRM radar into pipeline triage."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from crm.models import Opportunity, OpportunityPipelineEvent
from linkedin.crm_v2_evidence import ResolvedAccountEvidence
from linkedin.crm_v2_policy import AdmissionReasonCode


_AUTHORITATIVE_REASONS = frozenset({
    AdmissionReasonCode.MANUAL_PIN,
    AdmissionReasonCode.SALES_MOTION_ACTIVE,
    AdmissionReasonCode.HUMAN_MANAGED_OPPORTUNITY,
    AdmissionReasonCode.HUMAN_CURRENT_ACTION,
})
_GMAIL_REASONS = frozenset({
    AdmissionReasonCode.RECENT_GMAIL_BIDIRECTIONAL_THREAD,
    AdmissionReasonCode.RECENT_GMAIL_HUMAN_INBOUND,
})


@dataclass
class PipelineTriageReport:
    evaluated: int = 0
    eligible: int = 0
    promoted: int = 0
    preserved: int = 0
    skipped: int = 0
    issues: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "evaluated": self.evaluated,
            "eligible": self.eligible,
            "promoted": self.promoted,
            "preserved": self.preserved,
            "skipped": self.skipped,
            "issues": len(self.issues),
        }


def qualifies_for_pipeline_triage(row: ResolvedAccountEvidence) -> bool:
    """Return true only for explicit intent or two independent primary sources.

    One matched meeting by itself is deliberately insufficient: it belongs on
    the broad Active Accounts radar until a human confirms the sales motion or
    independent Gmail evidence corroborates the relationship.
    """
    if not row.decision.admitted:
        return False
    reasons = set(row.decision.reason_codes)
    if reasons & _AUTHORITATIVE_REASONS:
        return True
    return bool(
        AdmissionReasonCode.RECENT_COMPLETED_EXTERNAL_MEETING in reasons
        and reasons & _GMAIL_REASONS
    )


def reconcile_pipeline_triage(
    rows: Iterable[ResolvedAccountEvidence],
    *,
    apply: bool,
    evaluated_at: datetime | None = None,
) -> PipelineTriageReport:
    """Set only a blank pipeline stage to Triage; never advance or demote it."""
    observed_at = evaluated_at or timezone.now()
    if timezone.is_naive(observed_at):
        raise ValueError("evaluated_at must be timezone-aware")
    report = PipelineTriageReport()
    parsed: list[tuple[UUID, ResolvedAccountEvidence]] = []
    for row in rows:
        report.evaluated += 1
        if not qualifies_for_pipeline_triage(row):
            report.skipped += 1
            continue
        report.eligible += 1
        try:
            opportunity_id = UUID((row.opportunity_id or "").strip())
        except (TypeError, ValueError, AttributeError):
            report.issues.append("eligible_row_missing_stable_opportunity")
            continue
        parsed.append((opportunity_id, row))

    with transaction.atomic():
        for opportunity_id, _row in sorted(parsed, key=lambda item: str(item[0])):
            opportunity = (
                Opportunity.objects.select_for_update()
                .filter(pk=opportunity_id)
                .first()
            )
            if opportunity is None:
                report.issues.append("eligible_opportunity_not_found")
                continue
            if opportunity.pipeline_stage:
                report.preserved += 1
                continue
            opportunity.pipeline_stage = Opportunity.PipelineStage.TRIAGE
            opportunity.pipeline_stage_entered_at = observed_at
            opportunity.save(update_fields={
                "pipeline_stage",
                "pipeline_stage_entered_at",
                "updated_at",
            })
            OpportunityPipelineEvent.objects.create(
                opportunity=opportunity,
                from_stage="",
                to_stage=Opportunity.PipelineStage.TRIAGE,
                source=OpportunityPipelineEvent.Source.SYSTEM,
                changed_at=observed_at,
            )
            report.promoted += 1
        if not apply:
            transaction.set_rollback(True)
    return report
