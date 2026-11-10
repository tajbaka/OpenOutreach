"""Pre-send review: which file each recipient gets on which email step.

Read-only. For every Deal in a Campaign and every Gmail step whose published
copy declares attachments, report the asset that would be attached, its
approved hash and whether the send would go ahead or be held, plus what was
actually attached for steps already sent.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field

from email_assets.models import EmailAsset, LeadEmailAsset
from email_assets.providers import source_for_provider
from email_assets.requirements import AttachmentRequirement
from email_assets.resolution import (
    AttachmentProblem,
    SourceFactory,
    check_asset_source,
    combination_problem,
    plan_attachments,
)


@dataclass(frozen=True)
class ReviewRow:
    deal_id: int
    lead_id: int
    lead_name: str
    company: str
    email: str
    operator: str
    step_key: str
    step_index: int
    delivery_id: int | None
    delivery_status: str
    requirement: str
    asset_key: str
    asset_name: str
    filename: str
    sha256: str
    status: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class ReviewReport:
    campaign_id: int
    campaign_name: str
    campaign_status: str
    checked_files: bool
    steps: list[dict[str, object]] = field(default_factory=list)
    rows: list[ReviewRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return dict(sorted(Counter(row.status for row in self.rows).items()))

    def as_dict(self) -> dict[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "campaign_name": self.campaign_name,
            "campaign_status": self.campaign_status,
            "checked_files": self.checked_files,
            "steps": self.steps,
            "counts": self.counts,
            "notes": self.notes,
            "rows": [row.as_dict() for row in self.rows],
        }


# Statuses that mean "this email will send with its files" or is finished.
OK_STATUSES = frozenset({"ready", "ready_unchecked", "skipped_optional", "sent"})


def _lead_name(lead) -> str:
    return " ".join(f"{lead.first_name or ''} {lead.last_name or ''}".split())


def _campaign_operator(campaign) -> str:
    from linkedin.models import LinkedInProfile
    from linkedin.operators import resolve_operator

    profile = LinkedInProfile.objects.filter(user_id=campaign.user_id).first()
    return resolve_operator(profile.linkedin_username) if profile else ""


def _gmail_steps_for(index, *, audience_key: str, operator: str, enrollment=None):
    """Yield (step_key, step_index, requirements|None) for Gmail steps.

    ``None`` means variants disagree and the Deal is not enrolled yet, so the
    exact variant (and its attachments) is not decided.
    """
    from linkedin.message_delivery import select_message
    from linkedin.models import OutboundDelivery

    groups = index.effective_routes(
        audience_key=audience_key,
        operator=operator,
        channel=OutboundDelivery.Channel.GMAIL,
    )
    for group in groups:
        first = group[0]
        variants = {variant.attachments for variant in group}
        if len(variants) == 1:
            # Every variant declares the same files; no need to pick one.
            yield first.step_key, first.step_index, next(iter(variants))
            continue
        if enrollment is not None:
            selected = select_message(
                enrollment=enrollment,
                channel=first.channel,
                step_key=first.step_key,
                step_index=first.step_index,
            )
            yield first.step_key, first.step_index, selected.attachments
            continue
        yield first.step_key, first.step_index, None


def review_campaign_attachments(
    *,
    campaign_id: int,
    check_files: bool = False,
    source_for: SourceFactory = source_for_provider,
) -> ReviewReport:
    from crm.models import Deal, Message
    from linkedin.message_delivery import MessageDeliveryError, index_message_version
    from linkedin.models import Campaign, CampaignMessageEnrollment, OutboundDelivery

    campaign = (
        Campaign.objects.select_related("active_message_version__program")
        .filter(pk=campaign_id)
        .first()
    )
    if campaign is None:
        raise ValueError(f"Campaign {campaign_id} does not exist")
    report = ReviewReport(
        campaign_id=campaign.pk,
        campaign_name=campaign.name,
        campaign_status=campaign.status,
        checked_files=check_files,
    )

    enrollments = {
        enrollment.deal_id: enrollment
        for enrollment in CampaignMessageEnrollment.objects.select_related(
            "message_version__program", "deal__lead", "deal__campaign",
        ).filter(deal__campaign_id=campaign.pk)
    }
    deliveries = {
        (delivery.enrollment_id, delivery.step_key): delivery
        for delivery in OutboundDelivery.objects.select_related("task").filter(
            enrollment__deal__campaign_id=campaign.pk,
            channel=OutboundDelivery.Channel.GMAIL,
        )
    }
    sent_evidence: dict[int, list[dict]] = {}
    sent_ids = [
        delivery.pk
        for delivery in deliveries.values()
        if delivery.status == OutboundDelivery.Status.SENT
    ]
    if sent_ids:
        for raw in Message.objects.filter(
            source=Message.Source.GMAIL,
            direction=Message.Direction.OUTBOUND,
            raw__delivery_id__in=sent_ids,
        ).values_list("raw", flat=True):
            if isinstance(raw, dict):
                sent_evidence.setdefault(raw.get("delivery_id"), []).extend(
                    raw.get("attachments") or []
                )

    indexes: dict[int, object] = {}

    def index_for(version):
        if version.pk not in indexes:
            indexes[version.pk] = index_message_version(version)
        return indexes[version.pk]

    def has_attachments(version) -> bool:
        return any(message.attachments for message in index_for(version).messages)

    # Bulk-load every slot assignment for this campaign's Leads and every
    # fixed asset its versions name, so the per-recipient plan is DB-free.
    assignments: dict[int, dict[str, EmailAsset]] = {}
    for row in LeadEmailAsset.objects.select_related("asset").filter(
        lead__deal__campaign_id=campaign.pk,
    ).distinct():
        assignments.setdefault(row.lead_id, {})[row.slot] = row.asset
    versions = {enrollment.message_version for enrollment in enrollments.values()}
    if campaign.active_message_version is not None:
        versions.add(campaign.active_message_version)
    fixed_keys = {
        requirement.asset
        for version in versions
        for message in index_for(version).messages
        for requirement in message.attachments
        if requirement.asset
    }
    fixed_assets = {asset.key: asset for asset in EmailAsset.objects.filter(key__in=fixed_keys)}
    assets_by_id: dict[int, EmailAsset | None] = {}

    def asset_by_id(asset_id):
        if asset_id not in assets_by_id:
            assets_by_id[asset_id] = EmailAsset.objects.filter(pk=asset_id).first()
        return assets_by_id[asset_id]

    default_operator = _campaign_operator(campaign)
    step_summary: dict[tuple[int, str, int], set[str]] = {}
    asset_checks: dict[int, AttachmentProblem | None] = {}
    unenrolled = 0

    def add_row(deal, *, operator, step_key, step_index, delivery, requirement_label,
                asset=None, status, reason="", sha256="", filename=""):
        lead = deal.lead
        report.rows.append(ReviewRow(
            deal_id=deal.pk,
            lead_id=lead.pk,
            lead_name=_lead_name(lead),
            company=lead.company_name or "",
            email=lead.email or "",
            operator=operator,
            step_key=step_key,
            step_index=step_index,
            delivery_id=delivery.pk if delivery is not None else None,
            delivery_status=delivery.status if delivery is not None else "not_created",
            requirement=requirement_label,
            asset_key=asset.key if asset is not None else "",
            asset_name=asset.name if asset is not None else "",
            filename=filename or (asset.filename if asset is not None else ""),
            sha256=sha256 or (
                (asset.approved_sha256 or asset.sha256) if asset is not None else ""
            ),
            status=status,
            reason=reason,
        ))

    for deal in Deal.objects.select_related("lead").filter(campaign_id=campaign.pk).order_by("pk"):
        enrollment = enrollments.get(deal.pk)
        if enrollment is not None:
            version = enrollment.message_version
            audience_key = enrollment.audience_key
            operator = enrollment.operator
        else:
            version = campaign.active_message_version
            if version is None or not has_attachments(version):
                continue
            operator = default_operator
            try:
                audience_key = index_for(version).resolve_audience(str(deal.lead.icp or ""))
            except MessageDeliveryError:
                add_row(
                    deal, operator=operator, step_key="", step_index=0, delivery=None,
                    requirement_label="", status="unresolved_audience",
                    reason="Deal is not enrolled and its Lead ICP matches no audience",
                )
                continue
            unenrolled += 1
        if not has_attachments(version):
            continue
        try:
            steps = list(_gmail_steps_for(
                index_for(version),
                audience_key=audience_key,
                operator=operator,
                enrollment=enrollment,
            ))
        except MessageDeliveryError as exc:
            report.notes.append(f"Deal {deal.pk}: {exc}")
            continue
        for step_key, step_index, requirements in steps:
            delivery = (
                deliveries.get((enrollment.pk, step_key)) if enrollment is not None else None
            )
            if requirements is None:
                add_row(
                    deal, operator=operator, step_key=step_key, step_index=step_index,
                    delivery=delivery, requirement_label="", status="variant_undecided",
                    reason="variants of this step declare different attachments; "
                    "decided at enrollment",
                )
                continue
            if not requirements:
                continue
            step_summary.setdefault((version.pk, step_key, step_index), set()).update(
                requirement.label for requirement in requirements
            )
            if delivery is not None and delivery.status == OutboundDelivery.Status.SENT:
                evidence = sent_evidence.get(delivery.pk, [])
                if not evidence:
                    add_row(
                        deal, operator=operator, step_key=step_key, step_index=step_index,
                        delivery=delivery, requirement_label=", ".join(
                            requirement.label for requirement in requirements
                        ),
                        status="sent_without_evidence",
                        reason="sent message has no attachment evidence",
                    )
                for item in evidence:
                    add_row(
                        deal, operator=operator, step_key=step_key, step_index=step_index,
                        delivery=delivery,
                        requirement_label=AttachmentRequirement(
                            **(item.get("requirement") or {"asset": item.get("asset_key")})
                        ).label,
                        asset=asset_by_id(item.get("asset_id")),
                        status="sent",
                        sha256=str(item.get("sha256") or ""),
                        filename=str(item.get("filename") or ""),
                    )
                continue
            if delivery is not None and delivery.status in {
                OutboundDelivery.Status.STOPPED,
                OutboundDelivery.Status.UNCLEAR,
                OutboundDelivery.Status.FAILED,
            }:
                add_row(
                    deal, operator=operator, step_key=step_key, step_index=step_index,
                    delivery=delivery, requirement_label=", ".join(
                        requirement.label for requirement in requirements
                    ),
                    status=f"delivery_{delivery.status}",
                    reason="this email will not be sent by automation",
                )
                continue
            hold_reason = ""
            task = delivery.task if delivery is not None else None
            if task is not None and (task.error or "").startswith("Attachment hold: "):
                hold_reason = task.error
            planned = plan_attachments(
                lead_id=deal.lead_id,
                requirements=requirements,
                assignments=assignments.get(deal.lead_id, {}),
                fixed_assets=fixed_assets,
            )
            # The same rule sending applies: duplicate filenames or a combined
            # size over the limit hold the whole email, before any download.
            combination = combination_problem(planned)
            for item in planned:
                if item.problem is not None:
                    add_row(
                        deal, operator=operator, step_key=step_key, step_index=step_index,
                        delivery=delivery, requirement_label=item.requirement.label,
                        asset=item.asset, status=item.problem.code.value,
                        reason=item.problem.message,
                    )
                    continue
                if item.skipped:
                    if item.asset is None:
                        add_row(
                            deal, operator=operator, step_key=step_key, step_index=step_index,
                            delivery=delivery, requirement_label=item.requirement.label,
                            status="skipped_optional",
                            reason="optional slot has no file for this lead; sends without it",
                        )
                    continue
                status = "ready_unchecked"
                reason = hold_reason
                if combination is not None:
                    status = combination.code.value
                    reason = combination.message
                elif check_files:
                    if item.asset.pk not in asset_checks:
                        asset_checks[item.asset.pk] = check_asset_source(
                            item.asset,
                            source_for=source_for,
                        )
                    problem = asset_checks[item.asset.pk]
                    if problem is None:
                        status = "ready"
                    else:
                        status = problem.code.value
                        reason = problem.message
                add_row(
                    deal, operator=operator, step_key=step_key, step_index=step_index,
                    delivery=delivery, requirement_label=item.requirement.label,
                    asset=item.asset, status=status, reason=reason,
                )

    report.steps = [
        {
            "message_version_id": version_id,
            "step_key": step_key,
            "step_index": step_index,
            "attachments": sorted(labels),
        }
        for (version_id, step_key, step_index), labels in sorted(step_summary.items())
    ]
    if unenrolled:
        report.notes.append(
            f"{unenrolled} Deal(s) are not enrolled yet; their routes were resolved "
            "from Lead ICP and the campaign owner."
        )
    if not check_files:
        report.notes.append(
            "Files were not checked against the provider; rerun with --check-files "
            "to confirm each approved file is still readable and unchanged."
        )
    return report
