"""Resolve a step's attachment requirements for one recipient.

``plan_attachments`` is DB-only: it says which asset each requirement points
to for a Lead and whether that asset is approved. ``resolve_attachments``
additionally downloads each file and proves its bytes still equal the approved
SHA-256. Any problem raises ``AttachmentHold`` naming every reason, so the
caller can hold the email instead of sending it without its file.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Iterable, Mapping

from django.db.models import Q

from email_assets.files import (
    AttachmentFile,
    AttachmentFileError,
    attachment_set_problem,
    validate_attachment_set,
)
from email_assets.models import EmailAsset, LeadEmailAsset
from email_assets.providers import AssetSource, AssetSourceError, source_for_provider
from email_assets.requirements import AttachmentRequirement


class HoldCode(StrEnum):
    MISSING_ASSIGNMENT = "missing_assignment"
    UNKNOWN_ASSET = "unknown_asset"
    PENDING_APPROVAL = "pending_approval"
    RETIRED = "retired"
    INACCESSIBLE = "inaccessible"
    CHANGED = "changed_since_approval"
    INVALID_FILE = "invalid_file"
    INVALID_COMBINATION = "invalid_combination"
    ASSIGNMENT_CHANGED = "assignment_changed"


@dataclass(frozen=True)
class AttachmentProblem:
    requirement: AttachmentRequirement
    code: HoldCode
    message: str
    asset_key: str = ""


class AttachmentHold(Exception):
    """At least one required attachment cannot be sent right now."""

    def __init__(self, problems: Iterable[AttachmentProblem]):
        self.problems = tuple(problems)
        super().__init__("; ".join(problem.message for problem in self.problems))


@dataclass(frozen=True)
class PlannedAttachment:
    requirement: AttachmentRequirement
    asset: EmailAsset | None
    problem: AttachmentProblem | None = None
    skipped: bool = False

    @property
    def ready(self) -> bool:
        return self.asset is not None and self.problem is None and not self.skipped


@dataclass(frozen=True)
class ResolvedAttachment:
    requirement: AttachmentRequirement
    asset_id: int
    asset_key: str
    asset_name: str
    provider: str
    provider_file_id: str
    file: AttachmentFile

    def evidence(self) -> dict[str, object]:
        """Stable evidence persisted with the sent message."""
        return {
            "asset_id": self.asset_id,
            "asset_key": self.asset_key,
            "requirement": self.requirement.payload(),
            "provider": self.provider,
            "provider_file_id": self.provider_file_id,
            **self.file.identity(),
        }


def _asset_problem(requirement: AttachmentRequirement, asset: EmailAsset) -> AttachmentProblem | None:
    if asset.status == EmailAsset.Status.RETIRED:
        return AttachmentProblem(
            requirement,
            HoldCode.RETIRED,
            f"{requirement.label}: asset {asset.key!r} is retired",
            asset.key,
        )
    if not asset.is_sendable:
        return AttachmentProblem(
            requirement,
            HoldCode.PENDING_APPROVAL,
            f"{requirement.label}: asset {asset.key!r} is awaiting approval",
            asset.key,
        )
    return None


def plan_attachments(
    *,
    lead_id: int,
    requirements: Iterable[AttachmentRequirement],
    assignments: Mapping[str, EmailAsset] | None = None,
    fixed_assets: Mapping[str, EmailAsset] | None = None,
) -> tuple[PlannedAttachment, ...]:
    """Resolve requirements to assets for one Lead without fetching files.

    ``assignments`` (slot -> asset for this Lead) and ``fixed_assets``
    (key -> asset) may be preloaded by bulk callers such as the review.
    """
    requirements = tuple(requirements)
    slots = [requirement.slot for requirement in requirements if requirement.slot]
    keys = [requirement.asset for requirement in requirements if requirement.asset]
    if assignments is None:
        assignments = {
            row.slot: row.asset
            for row in LeadEmailAsset.objects.select_related("asset").filter(
                lead_id=lead_id,
                slot__in=slots,
            )
        } if slots else {}
    if fixed_assets is None:
        fixed_assets = {
            asset.key: asset
            for asset in EmailAsset.objects.filter(key__in=keys)
        } if keys else {}
    fixed = fixed_assets

    planned: list[PlannedAttachment] = []
    seen_assets: set[int] = set()
    for requirement in requirements:
        if requirement.slot:
            asset = assignments.get(requirement.slot)
            if asset is None:
                if not requirement.required:
                    planned.append(PlannedAttachment(requirement, None, skipped=True))
                    continue
                planned.append(PlannedAttachment(requirement, None, AttachmentProblem(
                    requirement,
                    HoldCode.MISSING_ASSIGNMENT,
                    f"{requirement.label}: no file is assigned to this lead",
                )))
                continue
        else:
            asset = fixed.get(requirement.asset)
            if asset is None:
                planned.append(PlannedAttachment(requirement, None, AttachmentProblem(
                    requirement,
                    HoldCode.UNKNOWN_ASSET,
                    f"{requirement.label}: no such email asset is registered",
                    requirement.asset,
                )))
                continue
        if asset.pk in seen_assets:
            # A slot and a fixed requirement may name the same file; attach it once.
            planned.append(PlannedAttachment(requirement, asset, skipped=True))
            continue
        seen_assets.add(asset.pk)
        planned.append(PlannedAttachment(requirement, asset, _asset_problem(requirement, asset)))
    return tuple(planned)


SourceFactory = Callable[[str], AssetSource]


def fetch_approved_file(
    asset: EmailAsset,
    *,
    requirement: AttachmentRequirement,
    source_for: SourceFactory = source_for_provider,
) -> AttachmentFile:
    """Download one asset and prove it is byte-identical to its approval."""
    from email_assets.services import fetch_source_snapshot

    problem = _asset_problem(requirement, asset)
    if problem is not None:
        raise AttachmentHold([problem])
    try:
        snapshot = fetch_source_snapshot(
            provider=asset.provider,
            file_id=asset.provider_file_id,
            source=source_for(asset.provider),
        )
    except AssetSourceError as exc:
        raise AttachmentHold([AttachmentProblem(
            requirement,
            HoldCode.INACCESSIBLE,
            f"{requirement.label}: asset {asset.key!r} could not be read ({exc.code}: {exc.message})",
            asset.key,
        )]) from None
    if snapshot.sha256 != asset.approved_sha256:
        raise AttachmentHold([AttachmentProblem(
            requirement,
            HoldCode.CHANGED,
            f"{requirement.label}: asset {asset.key!r} file changed since approval "
            f"(approved {asset.approved_sha256[:12]}, now {snapshot.sha256[:12]})",
            asset.key,
        )])
    try:
        return AttachmentFile(
            filename=asset.filename,
            mime_type=asset.mime_type,
            content=snapshot.content,
            sha256=snapshot.sha256,
        )
    except AttachmentFileError as exc:
        raise AttachmentHold([AttachmentProblem(
            requirement,
            HoldCode.INVALID_FILE,
            f"{requirement.label}: asset {asset.key!r} is not sendable ({exc})",
            asset.key,
        )]) from None


def combination_problem(planned: Iterable[PlannedAttachment]) -> AttachmentProblem | None:
    """DB-only check that the planned files can be sent together.

    Uses each asset's registered filename and size, so it runs before any
    download (and in the pre-send review). Sending re-checks the downloaded
    bytes with the same rule.
    """
    ready = [item for item in planned if item.ready]
    message = attachment_set_problem(
        (item.asset.filename, item.asset.size_bytes) for item in ready
    )
    if not message:
        return None
    return AttachmentProblem(
        ready[0].requirement,
        HoldCode.INVALID_COMBINATION,
        f"attachments cannot be sent together: {message}",
    )


def resolve_attachments(
    *,
    lead_id: int,
    requirements: Iterable[AttachmentRequirement],
    source_for: SourceFactory = source_for_provider,
) -> tuple[ResolvedAttachment, ...]:
    """Return exact approved files for one recipient, or raise ``AttachmentHold``."""
    planned = plan_attachments(lead_id=lead_id, requirements=requirements)
    problems = [item.problem for item in planned if item.problem is not None]
    if problems:
        raise AttachmentHold(problems)
    combination = combination_problem(planned)
    if combination is not None:
        raise AttachmentHold([combination])

    resolved: list[ResolvedAttachment] = []
    for item in planned:
        if not item.ready:
            continue
        try:
            attachment = fetch_approved_file(
                item.asset,
                requirement=item.requirement,
                source_for=source_for,
            )
        except AttachmentHold as hold:
            problems.extend(hold.problems)
            continue
        resolved.append(ResolvedAttachment(
            requirement=item.requirement,
            asset_id=item.asset.pk,
            asset_key=item.asset.key,
            asset_name=item.asset.name,
            provider=item.asset.provider,
            provider_file_id=item.asset.provider_file_id,
            file=attachment,
        ))
    if problems:
        raise AttachmentHold(problems)
    try:
        validate_attachment_set([item.file for item in resolved])
    except AttachmentFileError as exc:
        raise AttachmentHold([AttachmentProblem(
            resolved[0].requirement,
            HoldCode.INVALID_COMBINATION,
            f"attachments cannot be sent together: {exc}",
        )]) from None
    return tuple(resolved)


def recheck_resolved_attachments(
    *,
    lead_id: int,
    requirements: Iterable[AttachmentRequirement],
    resolved: Iterable[ResolvedAttachment],
) -> None:
    """Final DB check that downloaded files are still exactly what may be sent.

    Call inside the transaction that crosses the submission boundary. It
    locks the recipient's Lead, slot assignments and the assets involved (in
    that order, the order every asset/assignment writer uses), re-plans
    from that current state, and raises ``AttachmentHold`` when any asset was
    retired, unapproved, re-approved with other bytes, unassigned or
    reassigned after the download.
    """
    from crm.models import Lead

    requirements = tuple(requirements)
    resolved = tuple(resolved)
    slots = [requirement.slot for requirement in requirements if requirement.slot]
    keys = [requirement.asset for requirement in requirements if requirement.asset]
    # Shared lock order: Lead -> assignment -> asset (see services._lock_lead_slots).
    # Inside the submission transaction the Lead is already locked; this is
    # then a no-op, and it keeps standalone (one-off) rechecks in order too.
    list(Lead.objects.select_for_update().filter(pk=lead_id).values_list("pk", flat=True))
    assignment_asset_ids = {
        slot: asset_id
        for slot, asset_id in LeadEmailAsset.objects.select_for_update()
        .filter(lead_id=lead_id, slot__in=slots)
        .order_by("pk")
        .values_list("slot", "asset_id")
    } if slots else {}
    asset_filter = Q(pk__in=[*assignment_asset_ids.values(), *(item.asset_id for item in resolved)])
    if keys:
        asset_filter |= Q(key__in=keys)
    locked_assets = {
        asset.pk: asset
        for asset in EmailAsset.objects.select_for_update().filter(asset_filter).order_by("pk")
    }
    planned = plan_attachments(
        lead_id=lead_id,
        requirements=requirements,
        assignments={
            slot: locked_assets[asset_id] for slot, asset_id in assignment_asset_ids.items()
        },
        fixed_assets={asset.key: asset for asset in locked_assets.values() if asset.key in keys},
    )
    problems = [item.problem for item in planned if item.problem is not None]
    if problems:
        raise AttachmentHold(problems)
    current = [(item.requirement, item.asset.pk) for item in planned if item.ready]
    prepared = [(item.requirement, item.asset_id) for item in resolved]
    if current != prepared:
        changed = next(
            (requirement for requirement, _asset in current if (requirement, _asset) not in prepared),
            prepared[0][0] if prepared else requirements[0],
        )
        raise AttachmentHold([AttachmentProblem(
            changed,
            HoldCode.ASSIGNMENT_CHANGED,
            f"{changed.label}: the assigned file changed while this email was being prepared",
        )])
    for item in resolved:
        asset = locked_assets[item.asset_id]
        if asset.approved_sha256 != item.file.sha256:
            raise AttachmentHold([AttachmentProblem(
                item.requirement,
                HoldCode.CHANGED,
                f"{item.requirement.label}: asset {asset.key!r} file changed since approval "
                "while this email was being prepared",
                asset.key,
            )])


def check_asset_source(
    asset: EmailAsset,
    *,
    source_for: SourceFactory = source_for_provider,
) -> AttachmentProblem | None:
    """Review-time proof that an approved file can be sent right now.

    Does exactly what a send does: reads metadata (including whether this
    reader may download it), downloads the bytes and requires the approved
    SHA-256. A matching provider checksum alone is not trusted, because a
    file can be readable but not downloadable. Each call downloads once.
    """
    requirement = AttachmentRequirement(asset=asset.key)
    problem = _asset_problem(requirement, asset)
    if problem is not None:
        return problem
    try:
        fetch_approved_file(asset, requirement=requirement, source_for=source_for)
    except AttachmentHold as hold:
        return hold.problems[0]
    return None
