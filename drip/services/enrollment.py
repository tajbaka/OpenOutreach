from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from drip.exceptions import EnrollmentPlanError
from drip.models import DripCampaign, DripEnrollment, DripLane


PLAN_SCHEMA_VERSION = 1
PLAN_KIND = "drip_enrollment_plan"


@dataclass(frozen=True)
class EnrollmentApplyResult:
    created_enrollment_ids: tuple[int, ...]


def _canonical_json_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _gmail_mapping(operator: str) -> dict[str, str]:
    from gmail.auth import GMAIL_OPERATOR_MAPPING

    mapping = GMAIL_OPERATOR_MAPPING.get(operator)
    if not mapping:
        raise EnrollmentPlanError(f"Operator {operator!r} has no Gmail account mapping.")
    return {
        "provider_account": mapping["gmail_account"].strip().lower(),
        "sender_identity": mapping["send_as"].strip().lower(),
    }


def _linkedin_recipient_identity(lead) -> str:
    from linkedin.notifications.sheets import linkedin_identity_key

    identity = linkedin_identity_key(lead.linkedin_url or "")
    if identity:
        return identity
    public_identifier = (lead.public_identifier or "").strip().lower()
    return f"public:{public_identifier}" if public_identifier else ""


def _sender_evidence(lead) -> set[str]:
    from crm.models import Deal, Message
    from linkedin.operators import CANONICAL_OPERATOR_HANDLES, resolve_operator

    evidence: set[str] = set()
    deals = Deal.objects.filter(lead=lead).select_related("campaign__user")
    for deal in deals:
        for raw in (deal.invitation_sender, deal.campaign.user.username):
            resolved = resolve_operator(raw)
            if resolved in CANONICAL_OPERATOR_HANDLES:
                evidence.add(resolved)
    outbound = Message.objects.filter(
        lead=lead,
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.OUTBOUND,
    ).select_related("operator")
    for message in outbound:
        for raw in (
            message.sender,
            message.operator.handle if message.operator_id else "",
        ):
            resolved = resolve_operator(raw)
            if resolved in CANONICAL_OPERATOR_HANDLES:
                evidence.add(resolved)
    return evidence


def _known_stop_blockers(lead) -> list[str]:
    from crm.models import Meeting, Message
    from linkedin.suppression import lead_suppression_match

    blockers: list[str] = []
    if lead.disqualified:
        blockers.append("lead_disqualified")
    if lead_suppression_match(lead) is not None:
        blockers.append("outreach_suppressed")
    if Message.objects.filter(
        lead=lead,
        source__in=(Message.Source.LINKEDIN, Message.Source.GMAIL),
        direction=Message.Direction.INBOUND,
    ).exists():
        blockers.append("historical_inbound_reply")
    if Meeting.objects.filter(Q(lead=lead) | Q(participants=lead)).distinct().exists():
        blockers.append("persisted_meeting")
    if DripEnrollment.objects.filter(
        lead=lead,
        status__in=(
            DripEnrollment.Status.WAITING,
            DripEnrollment.Status.ACTIVE,
            DripEnrollment.Status.PAUSED,
        ),
    ).exists():
        blockers.append("existing_nonterminal_enrollment")
    return blockers


def _sender_channels(manifest: dict[str, Any], *, icp: str, operator: str) -> dict[str, bool]:
    audience = manifest["audiences"].get(icp)
    if audience is None:
        return {DripLane.Channel.LINKEDIN: False, DripLane.Channel.GMAIL: False}
    return {
        channel: any(channel in theme["senders"].get(operator, {}) for theme in audience["themes"])
        for channel in (DripLane.Channel.LINKEDIN, DripLane.Channel.GMAIL)
    }


def _entry_for_lead(*, campaign: DripCampaign, lead, operator: str) -> dict[str, Any]:
    from crm.models import Deal

    version = campaign.active_version
    if version is None:
        raise EnrollmentPlanError(f"Campaign {campaign.key!r} has no active version.")
    manifest = version.manifest
    icp = (lead.icp or "").strip()
    blockers = _known_stop_blockers(lead)
    if not icp:
        blockers.append("missing_icp")
    elif icp not in manifest["audiences"]:
        blockers.append("icp_not_in_campaign")

    channels = _sender_channels(manifest, icp=icp, operator=operator)
    if icp in manifest["audiences"] and not any(channels.values()):
        blockers.append("sender_not_configured_for_icp")

    linkedin_identity = _linkedin_recipient_identity(lead)
    sender_evidence = _sender_evidence(lead)
    if channels[DripLane.Channel.LINKEDIN]:
        if not linkedin_identity:
            blockers.append("missing_linkedin_identity")
        if not Deal.objects.filter(lead=lead).exists():
            blockers.append("linkedin_requires_deal")
        if not sender_evidence:
            blockers.append("linkedin_sender_unproven")
        elif sender_evidence != {operator}:
            blockers.append("linkedin_sender_ambiguous_or_mismatched")

    normalized_email = (lead.email or "").strip().lower()
    gmail = _gmail_mapping(operator)
    if channels[DripLane.Channel.GMAIL] and not normalized_email:
        blockers.append("missing_gmail_recipient")

    entry: dict[str, Any] = {
        "lead_id": lead.pk,
        "snapshot": {
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "company_name": lead.company_name,
            "icp": icp,
            "email": normalized_email,
            "linkedin_identity": linkedin_identity,
        },
        "sender_evidence": sorted(sender_evidence),
        "channels": {
            DripLane.Channel.LINKEDIN: {
                "configured": channels[DripLane.Channel.LINKEDIN],
                "provider_account": operator.lower(),
                "sender_identity": operator.lower(),
                "recipient_identity": linkedin_identity,
            },
            DripLane.Channel.GMAIL: {
                "configured": channels[DripLane.Channel.GMAIL],
                **gmail,
                "recipient_identity": normalized_email,
            },
        },
        "blockers": sorted(set(blockers)),
    }
    entry["eligible"] = not entry["blockers"]
    entry["entry_hash"] = _canonical_json_hash(entry)
    return entry


def build_enrollment_plan(
    *,
    campaign_key: str,
    operator: str,
    lead_ids: Iterable[int],
) -> dict[str, Any]:
    from crm.models import Lead
    from linkedin.operators import CANONICAL_OPERATOR_HANDLES, resolve_operator

    canonical_operator = resolve_operator(operator)
    if canonical_operator not in CANONICAL_OPERATOR_HANDLES:
        raise EnrollmentPlanError(f"Unknown canonical operator: {operator!r}")
    normalized_ids = list(dict.fromkeys(int(lead_id) for lead_id in lead_ids))
    if not normalized_ids:
        raise EnrollmentPlanError("At least one explicit Lead ID is required.")
    if any(lead_id <= 0 for lead_id in normalized_ids):
        raise EnrollmentPlanError("Lead IDs must be positive integers.")

    campaign = DripCampaign.objects.select_related("active_version").filter(
        key=campaign_key,
    ).first()
    if campaign is None:
        raise EnrollmentPlanError(f"Unknown drip campaign: {campaign_key!r}")
    if campaign.status != DripCampaign.Status.ACTIVE or campaign.active_version is None:
        raise EnrollmentPlanError(f"Campaign {campaign_key!r} is not active with a published version.")

    leads_by_id = Lead.objects.in_bulk(normalized_ids)
    missing = [lead_id for lead_id in normalized_ids if lead_id not in leads_by_id]
    if missing:
        raise EnrollmentPlanError(f"Unknown Lead ID(s): {', '.join(map(str, missing))}")

    entries = [
        _entry_for_lead(campaign=campaign, lead=leads_by_id[lead_id], operator=canonical_operator)
        for lead_id in normalized_ids
    ]
    plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "kind": PLAN_KIND,
        "created_at": timezone.now().isoformat(),
        "campaign_key": campaign.key,
        "campaign_id": campaign.pk,
        "campaign_version_id": campaign.active_version_id,
        "campaign_version": campaign.active_version.version,
        "campaign_content_hash": campaign.active_version.content_hash,
        "operator": canonical_operator,
        "leads": entries,
    }
    plan["plan_hash"] = _canonical_json_hash(plan)
    return plan


def write_enrollment_plan(plan: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(plan, indent=2, ensure_ascii=False) + "\n"
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise EnrollmentPlanError(f"Refusing to overwrite enrollment plan: {target}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(encoded)
    return target


def load_enrollment_plan(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise EnrollmentPlanError(f"Enrollment plan does not exist: {target}")
    try:
        plan = json.loads(target.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnrollmentPlanError(f"Enrollment plan is not valid UTF-8 JSON: {target}") from exc
    if not isinstance(plan, dict):
        raise EnrollmentPlanError("Enrollment plan must be a JSON object.")
    return plan


def _validate_plan_envelope(plan: dict[str, Any], *, campaign_key: str) -> None:
    required = {
        "schema_version",
        "kind",
        "created_at",
        "campaign_key",
        "campaign_id",
        "campaign_version_id",
        "campaign_version",
        "campaign_content_hash",
        "operator",
        "leads",
        "plan_hash",
    }
    if set(plan) != required:
        raise EnrollmentPlanError("Enrollment plan has missing or unknown top-level keys.")
    if plan["schema_version"] != PLAN_SCHEMA_VERSION or plan["kind"] != PLAN_KIND:
        raise EnrollmentPlanError("Unsupported enrollment plan schema or kind.")
    if plan["campaign_key"] != campaign_key:
        raise EnrollmentPlanError("Enrollment plan campaign does not match the command argument.")
    unsigned = {key: value for key, value in plan.items() if key != "plan_hash"}
    if plan["plan_hash"] != _canonical_json_hash(unsigned):
        raise EnrollmentPlanError("Enrollment plan hash is invalid; regenerate the reviewed plan.")
    if not isinstance(plan["leads"], list) or not plan["leads"]:
        raise EnrollmentPlanError("Enrollment plan must contain explicit Lead rows.")
    lead_ids = [entry.get("lead_id") for entry in plan["leads"] if isinstance(entry, dict)]
    if len(lead_ids) != len(plan["leads"]) or len(set(lead_ids)) != len(lead_ids):
        raise EnrollmentPlanError("Enrollment plan Lead rows must be objects with unique Lead IDs.")


@transaction.atomic
def validate_reviewed_plan(
    *,
    campaign_key: str,
    plan: dict[str, Any],
) -> tuple[DripCampaign, list[dict[str, Any]]]:
    from crm.models import Lead

    _validate_plan_envelope(plan, campaign_key=campaign_key)
    campaign = DripCampaign.objects.select_for_update().select_related("active_version").filter(
        key=campaign_key,
    ).first()
    if campaign is None or campaign.active_version is None:
        raise EnrollmentPlanError(f"Campaign {campaign_key!r} has no active version.")
    if campaign.status != DripCampaign.Status.ACTIVE:
        raise EnrollmentPlanError(f"Campaign {campaign_key!r} is not active.")
    if (
        campaign.pk != plan["campaign_id"]
        or campaign.active_version_id != plan["campaign_version_id"]
        or campaign.active_version.version != plan["campaign_version"]
        or campaign.active_version.content_hash != plan["campaign_content_hash"]
    ):
        raise EnrollmentPlanError("Campaign version changed after review; regenerate the plan.")

    lead_ids = [entry["lead_id"] for entry in plan["leads"]]
    leads_by_id = {
        lead.pk: lead
        for lead in Lead.objects.select_for_update().filter(pk__in=lead_ids)
    }
    missing = [lead_id for lead_id in lead_ids if lead_id not in leads_by_id]
    if missing:
        raise EnrollmentPlanError(f"Lead deleted after review: {', '.join(map(str, missing))}")

    current_entries: list[dict[str, Any]] = []
    for reviewed in plan["leads"]:
        current = _entry_for_lead(
            campaign=campaign,
            lead=leads_by_id[reviewed["lead_id"]],
            operator=plan["operator"],
        )
        if reviewed.get("entry_hash") != current["entry_hash"]:
            raise EnrollmentPlanError(
                f"Lead {reviewed['lead_id']} changed after review; regenerate the plan.",
            )
        if not current["eligible"]:
            reasons = ", ".join(current["blockers"])
            raise EnrollmentPlanError(f"Lead {reviewed['lead_id']} is not eligible: {reasons}")
        current_entries.append(current)
    return campaign, current_entries


@transaction.atomic
def apply_reviewed_plan(
    *,
    campaign_key: str,
    plan: dict[str, Any],
    reviewed_by: str,
) -> EnrollmentApplyResult:
    actor = (reviewed_by or "").strip()
    if not actor:
        raise EnrollmentPlanError("--reviewed-by is required for an audited enrollment apply.")
    if len(actor) > 150:
        raise EnrollmentPlanError("--reviewed-by must be at most 150 characters.")

    campaign, entries = validate_reviewed_plan(campaign_key=campaign_key, plan=plan)
    now = timezone.now()
    created_ids: list[int] = []
    for entry in entries:
        enrollment = DripEnrollment(
            campaign=campaign,
            campaign_version=campaign.active_version,
            lead_id=entry["lead_id"],
            frozen_icp=entry["snapshot"]["icp"],
            status=DripEnrollment.Status.WAITING,
            activated_at=now,
            enrolled_by=actor,
            plan_hash=plan["plan_hash"],
        )
        enrollment.full_clean()
        enrollment.save()
        first_theme = campaign.active_version.manifest["audiences"][
            entry["snapshot"]["icp"]
        ]["themes"][0]
        for channel in (DripLane.Channel.LINKEDIN, DripLane.Channel.GMAIL):
            channel_plan = entry["channels"][channel]
            configured = channel_plan["configured"]
            lane = DripLane(
                enrollment=enrollment,
                channel=channel,
                operator=plan["operator"],
                provider_account=channel_plan["provider_account"],
                sender_identity=channel_plan["sender_identity"],
                recipient_identity=channel_plan["recipient_identity"],
                status=(
                    DripLane.Status.WAITING_CURRENT
                    if configured
                    else DripLane.Status.COMPLETED
                ),
                current_sequence_status=(
                    DripLane.CurrentSequenceStatus.PENDING
                    if configured
                    else DripLane.CurrentSequenceStatus.NOT_APPLICABLE
                ),
                current_sequence_reviewed_at=None if configured else now,
                current_sequence_reviewed_by="" if configured else actor,
                handoff_evidence=(
                    {"source": "reviewed_enrollment_plan", "plan_hash": plan["plan_hash"]}
                    if configured
                    else {"source": "manifest", "reason": "channel_not_configured"}
                ),
                current_theme_index=0,
                current_theme_key=first_theme["key"],
            )
            lane.full_clean()
            lane.save()
        created_ids.append(enrollment.pk)
    return EnrollmentApplyResult(created_enrollment_ids=tuple(created_ids))
