"""Strict apply boundary for Codex-authored canonical CRM follow-up drafts.

The queue serializer owns eligibility, routing, and the canonical context
fingerprint.  This module accepts only decisions copied from a freshly built
queue and can make exactly one kind of CRM mutation: populate a blank
``OpportunityAction`` draft and its channel.  It never sends a message or
changes opportunity/action lifecycle fields.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import UUID

from django.db import transaction

from crm.models import Opportunity, OpportunityAction
from linkedin.models import WorkflowRun


MAX_DECISION_FILE_BYTES = 2_000_000
MAX_DECISIONS = 1_000
MAX_DRAFT_CHARS = 20_000
MAX_CONTEXT_TEXT_CHARS = 10_000
WORKFLOW_NAME = "crm-followup-decision-apply"
EMAIL_CHANNEL = "Email"
LINKEDIN_CHANNEL = "LinkedIn"

_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_FIELDS = frozenset({
    "action_id",
    "opportunity_id",
    "lead_ids",
    "context_fingerprint",
})
_OPTIONAL_FIELDS = frozenset({
    "recommended_next_step",
    "relationship_summary",
    "draft_email",
    "draft_linkedin",
    "needs_human_review",
    "review_reason",
})


class CrmFollowupDecisionError(ValueError):
    """A decision file or canonical identity check failed closed."""


@dataclass(frozen=True)
class CrmFollowupDecision:
    action_id: str
    opportunity_id: str
    lead_ids: tuple[int, ...]
    context_fingerprint: str
    recommended_next_step: str = ""
    relationship_summary: str = ""
    draft_email: str = ""
    draft_linkedin: str = ""
    needs_human_review: bool = False
    review_reason: str = ""


@dataclass(frozen=True)
class CrmFollowupApplyResult:
    decisions_validated: int = 0
    drafts_requested: int = 0
    drafts_applied: int = 0
    email_drafts_applied: int = 0
    linkedin_drafts_applied: int = 0
    existing_drafts_preserved: int = 0
    no_op_decisions: int = 0
    human_reviews_requested: int = 0
    workflow_run_id: int | None = None

    def counts(self) -> dict[str, int]:
        return {
            "decisions_validated": self.decisions_validated,
            "drafts_requested": self.drafts_requested,
            "drafts_applied": self.drafts_applied,
            "email_drafts_applied": self.email_drafts_applied,
            "linkedin_drafts_applied": self.linkedin_drafts_applied,
            "existing_drafts_preserved": self.existing_drafts_preserved,
            "no_op_decisions": self.no_op_decisions,
            "human_reviews_requested": self.human_reviews_requested,
        }


@dataclass(frozen=True)
class _CanonicalCandidate:
    action_id: str
    opportunity_id: str
    lead_ids: tuple[int, ...]
    context_fingerprint: str
    action_status: str
    target_lead_id: int
    state_fingerprint: str


def load_crm_followup_decisions(
    path: str | Path,
) -> list[CrmFollowupDecision]:
    """Load and fully validate a bounded Codex decision JSON file."""
    decision_path = Path(path)
    try:
        size = decision_path.stat().st_size
    except OSError as exc:
        raise CrmFollowupDecisionError(
            "Could not read the CRM follow-up decision file."
        ) from exc
    if size > MAX_DECISION_FILE_BYTES:
        raise CrmFollowupDecisionError(
            f"CRM follow-up decision JSON exceeds {MAX_DECISION_FILE_BYTES} bytes."
        )
    try:
        payload = json.loads(decision_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CrmFollowupDecisionError(
            "CRM follow-up decision file must contain valid UTF-8 JSON."
        ) from exc

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("decisions")
        if not isinstance(rows, list):
            raise CrmFollowupDecisionError(
                "CRM follow-up decision JSON must contain a decisions list."
            )
    else:
        raise CrmFollowupDecisionError(
            "CRM follow-up decision JSON must be a list or an object with decisions."
        )
    if len(rows) > MAX_DECISIONS:
        raise CrmFollowupDecisionError(
            f"CRM follow-up decision JSON exceeds {MAX_DECISIONS} decisions."
        )

    decisions = [
        crm_followup_decision_from_mapping(row, row_number=index)
        for index, row in enumerate(rows, start=1)
    ]
    _reject_duplicate_decisions(decisions)
    return decisions


def crm_followup_decision_from_mapping(
    row: object,
    *,
    row_number: int | None = None,
) -> CrmFollowupDecision:
    """Validate one untrusted JSON decision without coercing malformed types."""
    label = f"Decision {row_number}" if row_number is not None else "Decision"
    if not isinstance(row, dict):
        raise CrmFollowupDecisionError(f"{label} must be a JSON object.")
    keys = set(row)
    missing = _REQUIRED_FIELDS - keys
    if missing:
        raise CrmFollowupDecisionError(
            f"{label} is missing required fields: {', '.join(sorted(missing))}."
        )
    unknown = keys - _REQUIRED_FIELDS - _OPTIONAL_FIELDS
    if unknown:
        raise CrmFollowupDecisionError(
            f"{label} contains unsupported fields: {', '.join(sorted(unknown))}."
        )

    action_id = _canonical_uuid(row["action_id"], field=f"{label} action_id")
    opportunity_id = _canonical_uuid(
        row["opportunity_id"],
        field=f"{label} opportunity_id",
    )
    lead_ids = _lead_ids(row["lead_ids"], field=f"{label} lead_ids")
    fingerprint = _fingerprint(
        row["context_fingerprint"],
        field=f"{label} context_fingerprint",
    )
    recommended_next_step = _text(
        row.get("recommended_next_step", ""),
        field=f"{label} recommended_next_step",
        limit=MAX_CONTEXT_TEXT_CHARS,
    )
    relationship_summary = _text(
        row.get("relationship_summary", ""),
        field=f"{label} relationship_summary",
        limit=MAX_CONTEXT_TEXT_CHARS,
    )
    draft_email = _text(
        row.get("draft_email", ""),
        field=f"{label} draft_email",
        limit=MAX_DRAFT_CHARS,
    )
    draft_linkedin = _text(
        row.get("draft_linkedin", ""),
        field=f"{label} draft_linkedin",
        limit=MAX_DRAFT_CHARS,
    )
    needs_human_review = row.get("needs_human_review", False)
    if not isinstance(needs_human_review, bool):
        raise CrmFollowupDecisionError(
            f"{label} needs_human_review must be a boolean."
        )
    review_reason = _text(
        row.get("review_reason", ""),
        field=f"{label} review_reason",
        limit=MAX_CONTEXT_TEXT_CHARS,
    )
    if draft_email and draft_linkedin:
        raise CrmFollowupDecisionError(
            f"{label} may contain only one nonblank email or LinkedIn draft."
        )
    if needs_human_review and not review_reason:
        raise CrmFollowupDecisionError(
            f"{label} review_reason is required when needs_human_review is true."
        )
    return CrmFollowupDecision(
        action_id=action_id,
        opportunity_id=opportunity_id,
        lead_ids=lead_ids,
        context_fingerprint=fingerprint,
        recommended_next_step=recommended_next_step,
        relationship_summary=relationship_summary,
        draft_email=draft_email,
        draft_linkedin=draft_linkedin,
        needs_human_review=needs_human_review,
        review_reason=review_reason,
    )


def apply_crm_followup_decisions(
    decisions: Iterable[CrmFollowupDecision],
    *,
    canonical_queue: Mapping[str, Any],
    record_workflow: bool = True,
) -> CrmFollowupApplyResult:
    """Atomically apply validated drafts to blank canonical Action rows only."""
    decision_list = list(decisions)
    if len(decision_list) > MAX_DECISIONS:
        raise CrmFollowupDecisionError(
            f"Cannot apply more than {MAX_DECISIONS} CRM follow-up decisions."
        )
    _validate_decision_objects(decision_list)
    _reject_duplicate_decisions(decision_list)
    candidates = _canonical_candidate_index(canonical_queue)

    validated: list[tuple[CrmFollowupDecision, _CanonicalCandidate]] = []
    for decision in decision_list:
        candidate = candidates.get(decision.action_id)
        if candidate is None:
            raise CrmFollowupDecisionError(
                f"Unknown action_id in CRM follow-up decisions: {decision.action_id}."
            )
        if decision.opportunity_id != candidate.opportunity_id:
            raise CrmFollowupDecisionError(
                f"Stale opportunity_id for action_id {decision.action_id}."
            )
        if decision.lead_ids != candidate.lead_ids:
            raise CrmFollowupDecisionError(
                f"Stale lead_ids for action_id {decision.action_id}."
            )
        if decision.context_fingerprint != candidate.context_fingerprint:
            raise CrmFollowupDecisionError(
                f"Stale context_fingerprint for action_id {decision.action_id}."
            )
        validated.append((decision, candidate))

    with transaction.atomic():
        locked_opportunities = {
            str(opportunity.id): opportunity
            # ``select_related("account")`` is required for the locked-state
            # fingerprint, but PostgreSQL must not implicitly lock the joined
            # Account rows. Multiple motions can share an Account, so taking
            # those locks while ordering by Opportunity PK creates a
            # cross-batch lock-order inversion. Scope FOR UPDATE to the
            # Opportunity table; Actions are then locked in their own stable
            # PK order below.
            for opportunity in Opportunity.objects.select_for_update(of=("self",))
            .select_related("account")
            .filter(
                id__in=[candidate.opportunity_id for _decision, candidate in validated]
            )
            .order_by("pk")
        }
        actions = {
            str(action.id): action
            for action in OpportunityAction.objects.select_for_update()
            .filter(id__in=[decision.action_id for decision in decision_list])
            .order_by("pk")
        }
        missing = sorted(
            decision.action_id
            for decision in decision_list
            if decision.action_id not in actions
        )
        if missing:
            raise CrmFollowupDecisionError(
                f"Canonical action no longer exists: {', '.join(missing)}."
            )

        # Validate every locked row before the first write, so a stale member
        # rejects the whole decision set without partially populating drafts.
        for decision, candidate in validated:
            action = actions[decision.action_id]
            opportunity = locked_opportunities.get(candidate.opportunity_id)
            if str(action.opportunity_id) != candidate.opportunity_id:
                raise CrmFollowupDecisionError(
                    f"Canonical action moved opportunities: {decision.action_id}."
                )
            if opportunity is None:
                raise CrmFollowupDecisionError(
                    f"Canonical opportunity no longer exists: {candidate.opportunity_id}."
                )
            if action.target_lead_id != candidate.target_lead_id:
                raise CrmFollowupDecisionError(
                    f"Canonical action target changed: {decision.action_id}."
                )
            if (
                _locked_state_fingerprint(action, opportunity=opportunity)
                != candidate.state_fingerprint
            ):
                raise CrmFollowupDecisionError(
                    f"Canonical action or opportunity changed: {decision.action_id}."
                )
            if (
                action.status != candidate.action_status
                or action.status not in {
                    OpportunityAction.Status.OPEN,
                    OpportunityAction.Status.WAITING,
                }
            ):
                raise CrmFollowupDecisionError(
                    f"Canonical action is stale or no longer active: {decision.action_id}."
                )

        counts = {
            "decisions_validated": len(validated),
            "drafts_requested": 0,
            "drafts_applied": 0,
            "email_drafts_applied": 0,
            "linkedin_drafts_applied": 0,
            "existing_drafts_preserved": 0,
            "no_op_decisions": 0,
            "human_reviews_requested": sum(
                decision.needs_human_review for decision, _ in validated
            ),
        }
        for decision, _candidate in validated:
            selected = _selected_draft(decision)
            if selected is None:
                counts["no_op_decisions"] += 1
                continue
            channel, draft = selected
            counts["drafts_requested"] += 1
            action = actions[decision.action_id]
            if action.draft.strip() or action.channel.strip():
                # Channel is human-owned routing intent just like Draft. A
                # blank draft does not authorize Codex to replace a channel
                # the operator already selected.
                counts["existing_drafts_preserved"] += 1
                continue
            action.channel = channel
            action.draft = draft
            action.human_revision += 1
            action.save(update_fields={
                "channel",
                "draft",
                "human_revision",
                "updated_at",
            })
            counts["drafts_applied"] += 1
            key = (
                "email_drafts_applied"
                if channel == EMAIL_CHANNEL
                else "linkedin_drafts_applied"
            )
            counts[key] += 1

        result = CrmFollowupApplyResult(**counts)
        if record_workflow:
            run = WorkflowRun.objects.create(
                name=WORKFLOW_NAME,
                summary=(
                    f"Validated {result.decisions_validated} canonical decisions; "
                    f"populated {result.drafts_applied} blank action drafts."
                ),
                counts=result.counts(),
            )
            result = replace(result, workflow_run_id=run.id)
        return result


def _canonical_candidate_index(
    canonical_queue: Mapping[str, Any],
) -> dict[str, _CanonicalCandidate]:
    if not isinstance(canonical_queue, Mapping):
        raise CrmFollowupDecisionError("Canonical CRM queue must be an object.")
    rows = canonical_queue.get("candidates")
    if not isinstance(rows, list):
        raise CrmFollowupDecisionError(
            "Canonical CRM queue must contain a candidates list."
        )
    declared_count = canonical_queue.get("candidate_count")
    if declared_count is not None and (
        type(declared_count) is not int or declared_count != len(rows)
    ):
        raise CrmFollowupDecisionError(
            "Canonical CRM queue candidate_count does not match candidates."
        )

    out: dict[str, _CanonicalCandidate] = {}
    for index, row in enumerate(rows, start=1):
        label = f"Canonical candidate {index}"
        if not isinstance(row, dict):
            raise CrmFollowupDecisionError(f"{label} must be an object.")
        try:
            action_id = _canonical_uuid(row["action_id"], field=f"{label} action_id")
            opportunity_id = _canonical_uuid(
                row["opportunity_id"],
                field=f"{label} opportunity_id",
            )
            lead_ids = _lead_ids(row["lead_ids"], field=f"{label} lead_ids")
            fingerprint = _fingerprint(
                row["context_fingerprint"],
                field=f"{label} context_fingerprint",
            )
            action_payload = row["action"]
        except KeyError as exc:
            raise CrmFollowupDecisionError(
                f"{label} is missing required canonical identity fields."
            ) from exc
        if not isinstance(action_payload, dict) or not isinstance(
            action_payload.get("status"), str
        ):
            raise CrmFollowupDecisionError(
                f"{label} action.status must be a string."
            )
        target_lead_id = action_payload.get("target_lead_id")
        if (
            type(target_lead_id) is not int
            or len(lead_ids) != 1
            or lead_ids[0] != target_lead_id
        ):
            raise CrmFollowupDecisionError(
                f"{label} must contain one lead_id matching action.target_lead_id."
            )
        fingerprint_payload = dict(row)
        fingerprint_payload.pop("context_fingerprint", None)
        expected_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        if fingerprint != expected_fingerprint:
            raise CrmFollowupDecisionError(
                f"{label} has an invalid or stale context_fingerprint."
            )
        if action_id in out:
            raise CrmFollowupDecisionError(
                f"Canonical CRM queue contains duplicate action_id {action_id}."
            )
        out[action_id] = _CanonicalCandidate(
            action_id=action_id,
            opportunity_id=opportunity_id,
            lead_ids=lead_ids,
            context_fingerprint=fingerprint,
            action_status=action_payload["status"],
            target_lead_id=target_lead_id,
            state_fingerprint=_state_fingerprint_from_candidate(row),
        )
    return out


def _state_fingerprint_from_candidate(row: Mapping[str, Any]) -> str:
    return _semantic_fingerprint({
        "lead_ids": row.get("lead_ids"),
        "owner": row.get("owner"),
        "opportunity": row.get("opportunity"),
        "action": row.get("action"),
    })


def _locked_state_fingerprint(
    action: OpportunityAction,
    *,
    opportunity: Opportunity,
) -> str:
    owner = opportunity.owner
    return _semantic_fingerprint({
        "lead_ids": [action.target_lead_id],
        "owner": {
            "id": str(owner.id) if owner is not None else "",
            "handle": owner.handle if owner is not None else "",
        },
        "opportunity": {
            "account_id": str(opportunity.account_id),
            "account_name": opportunity.account.name,
            "stage": opportunity.stage,
            "sales_motion_step": opportunity.sales_motion_step,
            "human_revision": opportunity.human_revision,
            "manual_pin": opportunity.manual_pin,
        },
        "action": {
            "kind": action.kind,
            "status": action.status,
            "description": action.description,
            "due_on": action.due_on.isoformat() if action.due_on else None,
            "waiting_until": (
                action.waiting_until.isoformat() if action.waiting_until else None
            ),
            "disposition": action.disposition,
            "channel": action.channel,
            "draft": action.draft,
            "human_revision": action.human_revision,
            "target_lead_id": action.target_lead_id,
            "trigger_message_id": action.trigger_message_id,
            "trigger_meeting_id": action.trigger_meeting_id,
        },
    })


def _semantic_fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _validate_decision_objects(
    decisions: Iterable[CrmFollowupDecision],
) -> None:
    for index, decision in enumerate(decisions, start=1):
        if not isinstance(decision, CrmFollowupDecision):
            raise CrmFollowupDecisionError(
                f"Decision {index} must be loaded as CrmFollowupDecision."
            )
        # Frozen dataclasses can still be constructed directly with invalid
        # runtime values. Revalidate without accepting coercions.
        mapping = {
            "action_id": decision.action_id,
            "opportunity_id": decision.opportunity_id,
            "lead_ids": (
                list(decision.lead_ids)
                if isinstance(decision.lead_ids, tuple)
                else decision.lead_ids
            ),
            "context_fingerprint": decision.context_fingerprint,
            "recommended_next_step": decision.recommended_next_step,
            "relationship_summary": decision.relationship_summary,
            "draft_email": decision.draft_email,
            "draft_linkedin": decision.draft_linkedin,
            "needs_human_review": decision.needs_human_review,
            "review_reason": decision.review_reason,
        }
        validated = crm_followup_decision_from_mapping(mapping, row_number=index)
        if decision != validated:
            raise CrmFollowupDecisionError(
                f"Decision {index} contains noncanonical values; reload it from JSON."
            )


def _reject_duplicate_decisions(
    decisions: Iterable[CrmFollowupDecision],
) -> None:
    seen: set[str] = set()
    for decision in decisions:
        if decision.action_id in seen:
            raise CrmFollowupDecisionError(
                f"Duplicate CRM follow-up decision for action_id {decision.action_id}."
            )
        seen.add(decision.action_id)


def _selected_draft(
    decision: CrmFollowupDecision,
) -> tuple[str, str] | None:
    if decision.draft_email:
        return EMAIL_CHANNEL, decision.draft_email
    if decision.draft_linkedin:
        return LINKEDIN_CHANNEL, decision.draft_linkedin
    return None


def _canonical_uuid(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise CrmFollowupDecisionError(f"{field} must be a canonical UUID string.")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise CrmFollowupDecisionError(
            f"{field} must be a canonical UUID string."
        ) from exc
    if str(parsed) != value:
        raise CrmFollowupDecisionError(f"{field} must be a canonical UUID string.")
    return value


def _lead_ids(value: object, *, field: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise CrmFollowupDecisionError(f"{field} must be a nonempty integer list.")
    if any(type(item) is not int or item <= 0 for item in value):
        raise CrmFollowupDecisionError(f"{field} must contain only positive integers.")
    if len(set(value)) != len(value):
        raise CrmFollowupDecisionError(f"{field} must not contain duplicate IDs.")
    return tuple(value)


def _fingerprint(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _FINGERPRINT_PATTERN.fullmatch(value):
        raise CrmFollowupDecisionError(
            f"{field} must be a lowercase SHA-256 string."
        )
    return value


def _text(value: object, *, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise CrmFollowupDecisionError(f"{field} must be a string.")
    stripped = value.strip()
    if len(stripped) > limit:
        raise CrmFollowupDecisionError(f"{field} exceeds {limit} characters.")
    return stripped


__all__ = [
    "CrmFollowupApplyResult",
    "CrmFollowupDecision",
    "CrmFollowupDecisionError",
    "apply_crm_followup_decisions",
    "crm_followup_decision_from_mapping",
    "load_crm_followup_decisions",
]
