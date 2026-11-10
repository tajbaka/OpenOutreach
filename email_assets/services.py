"""Register, approve, refresh and assign reusable email assets.

Every mutating function previews by default and writes only with
``apply=True``. Approval binds to the exact SHA-256 an operator reviewed: the
file is fetched again and must still match before it becomes sendable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from email_assets.files import (
    MAX_EMAIL_ATTACHMENT_BYTES,
    AttachmentFileError,
    clean_filename,
    is_sha256,
    sha256_hex,
    validate_attachment_bytes,
    validate_mime_type,
)
from email_assets.models import EmailAsset, LeadEmailAsset
from email_assets.providers import (
    AssetSource,
    AssetSourceError,
    SourceFileMetadata,
    provider_for_link,
    source_for_provider,
)
from email_assets.requirements import (
    AttachmentRequirementError,
    validate_asset_key,
    validate_slot_key,
)


class EmailAssetError(Exception):
    """An operator request cannot be carried out; nothing was written."""


@dataclass(frozen=True)
class SourceSnapshot:
    provider: str
    file_id: str
    metadata: SourceFileMetadata
    content: bytes = field(repr=False)
    sha256: str

    @property
    def size_bytes(self) -> int:
        return len(self.content)


def fetch_source_snapshot(
    *,
    provider: str,
    file_id: str,
    source: AssetSource | None = None,
) -> SourceSnapshot:
    """Download and validate the current provider bytes for one file."""
    source = source or source_for_provider(provider)
    metadata = source.metadata(file_id)
    if metadata.can_download is False:
        raise AssetSourceError(
            "permission",
            "the provider does not allow OpenOutreach to download this file (its "
            "download restriction blocks viewers); allow viewers to download it, "
            "then check again",
        )
    try:
        mime_type = validate_mime_type(metadata.mime_type)
    except AttachmentFileError as exc:
        raise AssetSourceError("unsupported", str(exc)) from None
    if metadata.size_bytes is not None and metadata.size_bytes > MAX_EMAIL_ATTACHMENT_BYTES:
        raise AssetSourceError(
            "too_large",
            f"file is {metadata.size_bytes} bytes; the attachment limit is "
            f"{MAX_EMAIL_ATTACHMENT_BYTES} bytes",
        )
    content = source.download(file_id, max_bytes=MAX_EMAIL_ATTACHMENT_BYTES)
    try:
        validate_attachment_bytes(content, mime_type=mime_type)
    except AttachmentFileError as exc:
        raise AssetSourceError("unsupported", str(exc)) from None
    if metadata.size_bytes is not None and len(content) != metadata.size_bytes:
        raise AssetSourceError(
            "unavailable",
            "downloaded file size does not match the provider's metadata",
        )
    digest = sha256_hex(content)
    if metadata.sha256_checksum and metadata.sha256_checksum != digest:
        raise AssetSourceError(
            "unavailable",
            "downloaded bytes do not match the provider's SHA-256",
        )
    return SourceSnapshot(
        provider=provider,
        file_id=file_id,
        metadata=metadata,
        content=content,
        sha256=digest,
    )


def _source_error(exc: AssetSourceError) -> EmailAssetError:
    return EmailAssetError(f"{exc.code}: {exc.message}")


def _required_text(value: str, label: str, *, max_length: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise EmailAssetError(f"{label} is required")
    if len(text) > max_length:
        raise EmailAssetError(f"{label} exceeds {max_length} characters")
    return text


def get_asset(key: str) -> EmailAsset:
    asset = EmailAsset.objects.filter(key=key).first()
    if asset is None:
        raise EmailAssetError(f"no email asset has key {key!r}")
    return asset


def register_asset(
    *,
    url: str,
    key: str,
    name: str,
    registered_by: str,
    filename: str = "",
    notes: str = "",
    apply: bool = False,
    source: AssetSource | None = None,
) -> tuple[EmailAsset, SourceSnapshot]:
    """Fetch a pasted provider link and register it awaiting approval."""
    try:
        validate_asset_key(key, "asset key")
    except AttachmentRequirementError as exc:
        raise EmailAssetError(str(exc)) from None
    name = _required_text(name, "asset name", max_length=200)
    registered_by = _required_text(registered_by, "registered by", max_length=150)
    try:
        provider = source.provider if source is not None else provider_for_link(url)
        source = source or source_for_provider(provider)
        file_id = source.parse_link(url)
    except AssetSourceError as exc:
        raise _source_error(exc) from None
    if EmailAsset.objects.filter(key=key).exists():
        raise EmailAssetError(f"email asset key {key!r} already exists")
    duplicate = EmailAsset.objects.filter(
        provider=provider,
        provider_file_id=file_id,
    ).first()
    if duplicate is not None:
        raise EmailAssetError(
            f"this file is already registered as {duplicate.key!r}; assign that asset instead"
        )
    try:
        snapshot = fetch_source_snapshot(provider=provider, file_id=file_id, source=source)
    except AssetSourceError as exc:
        raise _source_error(exc) from None
    try:
        attachment_name = clean_filename(
            filename or snapshot.metadata.name,
            mime_type=snapshot.metadata.mime_type,
        )
    except AttachmentFileError as exc:
        raise EmailAssetError(f"{exc}; pass --filename to choose the attachment name") from None
    asset = EmailAsset(
        key=key,
        name=name,
        provider=provider,
        provider_file_id=file_id,
        source_url=source.view_link(file_id),
        filename=attachment_name,
        mime_type=snapshot.metadata.mime_type.lower(),
        size_bytes=snapshot.size_bytes,
        sha256=snapshot.sha256,
        provider_checksum=snapshot.metadata.md5_checksum,
        provider_revision=snapshot.metadata.revision,
        provider_modified_at=snapshot.metadata.modified_at,
        status=EmailAsset.Status.PENDING,
        registered_by=registered_by,
        notes=str(notes or "").strip(),
    )
    asset.full_clean()
    if apply:
        asset.save()
    return asset, snapshot


_SNAPSHOT_FIELDS = (
    "size_bytes",
    "provider_checksum",
    "provider_revision",
    "provider_modified_at",
)


def _apply_snapshot_metadata(asset: EmailAsset, snapshot: SourceSnapshot) -> None:
    asset.size_bytes = snapshot.size_bytes
    asset.provider_checksum = snapshot.metadata.md5_checksum
    asset.provider_revision = snapshot.metadata.revision
    asset.provider_modified_at = snapshot.metadata.modified_at


def _decision_state(asset: EmailAsset) -> tuple:
    """Fields an operator decision depends on; a change means "re-review"."""
    return (asset.status, asset.sha256, asset.approved_sha256, asset.mime_type, asset.filename)


def approve_asset(
    *,
    key: str,
    approved_by: str,
    sha256: str,
    apply: bool = False,
    source: AssetSource | None = None,
) -> tuple[EmailAsset, bool]:
    """Approve the exact registered bytes. Returns (asset, already_approved).

    The provider download happens outside any lock; the approval is then
    written to the locked current row only if nothing an operator decided
    (status, hashes, type, filename) changed meanwhile, so a concurrent
    retirement or refresh is never overwritten.
    """
    approved_by = _required_text(approved_by, "approved by", max_length=150)
    if not is_sha256(sha256):
        raise EmailAssetError("--sha256 must be the full 64-character hash being approved")
    asset = get_asset(key)
    if sha256 != asset.sha256:
        raise EmailAssetError(
            f"{sha256} is not the registered file hash {asset.sha256}; "
            "review the current file before approving"
        )
    if asset.is_sendable:
        return asset, True
    observed = _decision_state(asset)
    try:
        snapshot = fetch_source_snapshot(
            provider=asset.provider,
            file_id=asset.provider_file_id,
            source=source,
        )
    except AssetSourceError as exc:
        raise _source_error(exc) from None
    if snapshot.sha256 != asset.sha256:
        raise EmailAssetError(
            "the provider file changed since it was registered; run "
            "refresh_email_asset, review the new file, then approve its new hash"
        )
    if snapshot.metadata.mime_type.lower() != asset.mime_type:
        raise EmailAssetError("the provider file type changed since it was registered")

    def approve(target: EmailAsset) -> None:
        _apply_snapshot_metadata(target, snapshot)
        target.status = EmailAsset.Status.APPROVED
        target.approved_sha256 = snapshot.sha256
        target.approved_by = approved_by
        target.approved_at = timezone.now()
        target.full_clean()

    if not apply:
        approve(asset)
        return asset, False
    with transaction.atomic():
        locked = EmailAsset.objects.select_for_update().get(pk=asset.pk)
        if _decision_state(locked) != observed:
            raise EmailAssetError(
                f"the asset changed while it was being approved (now "
                f"{locked.get_status_display().lower()}, SHA-256 {locked.sha256[:12]}); "
                "review it again before approving"
            )
        approve(locked)
        locked.save(update_fields={
            *_SNAPSHOT_FIELDS,
            "status",
            "approved_sha256",
            "approved_by",
            "approved_at",
            "updated_at",
        })
    return locked, False


def _apply_refresh(asset: EmailAsset, snapshot: SourceSnapshot) -> tuple[bool, set[str]]:
    """Apply provider facts to ``asset``; return (bytes_changed, fields)."""
    fields = {*_SNAPSHOT_FIELDS}
    mime_type = snapshot.metadata.mime_type.lower()
    if mime_type != asset.mime_type:
        try:
            clean_filename(asset.filename, mime_type=mime_type)
        except AttachmentFileError as exc:
            raise EmailAssetError(
                f"the provider file is now {mime_type}; {exc}. Register it as a new asset"
            ) from None
        asset.mime_type = mime_type
        fields.add("mime_type")
    changed = snapshot.sha256 != asset.sha256
    _apply_snapshot_metadata(asset, snapshot)
    if changed:
        asset.sha256 = snapshot.sha256
        fields.add("sha256")
        if asset.status == EmailAsset.Status.APPROVED:
            # New bytes need a new approval. Retired assets stay retired.
            asset.status = EmailAsset.Status.PENDING
            fields.add("status")
    asset.full_clean()
    return changed, fields


def refresh_asset(
    *,
    key: str,
    apply: bool = False,
    source: AssetSource | None = None,
) -> tuple[EmailAsset, bool, SourceSnapshot]:
    """Re-read the provider file. A content change returns it to review.

    Refresh owns only provider facts (bytes hash, size, checksums, type). It
    is applied to the locked current row and saves only those fields, so a
    retirement or approval made during the download is kept.
    """
    asset = get_asset(key)
    try:
        snapshot = fetch_source_snapshot(
            provider=asset.provider,
            file_id=asset.provider_file_id,
            source=source,
        )
    except AssetSourceError as exc:
        raise _source_error(exc) from None
    if not apply:
        changed, _fields = _apply_refresh(asset, snapshot)
        return asset, changed, snapshot
    with transaction.atomic():
        locked = EmailAsset.objects.select_for_update().get(pk=asset.pk)
        changed, fields = _apply_refresh(locked, snapshot)
        locked.save(update_fields={*fields, "updated_at"})
    return locked, changed, snapshot


def retire_asset(*, key: str, apply: bool = False) -> EmailAsset:
    asset = get_asset(key)
    if not apply:
        asset.status = EmailAsset.Status.RETIRED
        return asset
    with transaction.atomic():
        locked = EmailAsset.objects.select_for_update().get(pk=asset.pk)
        locked.status = EmailAsset.Status.RETIRED
        locked.save(update_fields={"status", "updated_at"})
    return locked


@dataclass(frozen=True)
class AssignmentChange:
    lead_id: int
    lead_label: str
    slot: str
    action: str  # create | replace | unchanged | remove | absent
    previous_asset_key: str = ""
    asset_key: str = ""


def _leads(lead_ids) -> list:
    from crm.models import Lead

    ids = sorted({int(value) for value in lead_ids})
    if not ids:
        raise EmailAssetError("at least one Lead is required")
    leads = list(Lead.objects.filter(pk__in=ids).order_by("pk"))
    missing = sorted(set(ids) - {lead.pk for lead in leads})
    if missing:
        raise EmailAssetError(f"Lead IDs do not exist: {missing}")
    return leads


def leads_for_company(*, campaign_id: int, company: str) -> list:
    """Return Leads in one Campaign whose company name matches exactly."""
    from crm.models import Lead

    target = " ".join(str(company or "").split()).casefold()
    if not target:
        raise EmailAssetError("--company must be nonblank")
    candidates = Lead.objects.filter(deal__campaign_id=campaign_id).distinct().order_by("pk")
    return [
        lead
        for lead in candidates
        if " ".join(str(lead.company_name or "").split()).casefold() == target
    ]


def _lead_label(lead) -> str:
    name = " ".join(f"{lead.first_name or ''} {lead.last_name or ''}".split())
    company = f" ({lead.company_name})" if lead.company_name else ""
    return f"{name or 'Lead'}{company}"


def _lock_lead_slots(lead_ids, slot: str) -> dict[int, LeadEmailAsset]:
    """Take the first two steps of the shared lock order: Leads, then slot rows.

    Every transaction that locks more than one of Lead, LeadEmailAsset and
    EmailAsset uses this order, Lead -> assignment -> asset, matching the
    Gmail submission transaction (Lead -> Task -> delivery, then the final
    attachment recheck). Rows are locked in primary-key order.
    """
    from crm.models import Lead

    ids = sorted(set(lead_ids))
    list(Lead.objects.select_for_update().filter(pk__in=ids).order_by("pk").values_list("pk", flat=True))
    return {
        row.lead_id: row
        for row in LeadEmailAsset.objects.select_for_update(of=("self",))
        .select_related("asset")
        .filter(lead_id__in=ids, slot=slot)
        .order_by("pk")
    }


def _unlocked_lead_slots(lead_ids, slot: str) -> dict[int, LeadEmailAsset]:
    return {
        row.lead_id: row
        for row in LeadEmailAsset.objects.select_related("asset").filter(
            lead_id__in=list(lead_ids),
            slot=slot,
        )
    }


def _assignment_changes(leads, existing, *, slot: str, asset: EmailAsset) -> list[AssignmentChange]:
    changes = []
    for lead in leads:
        current = existing.get(lead.pk)
        if current is None:
            action = "create"
        elif current.asset_id == asset.pk:
            action = "unchanged"
        else:
            action = "replace"
        changes.append(AssignmentChange(
            lead_id=lead.pk,
            lead_label=_lead_label(lead),
            slot=slot,
            action=action,
            previous_asset_key=current.asset.key if current is not None else "",
            asset_key=asset.key,
        ))
    return changes


def assign_asset(
    *,
    key: str,
    slot: str,
    lead_ids,
    assigned_by: str,
    apply: bool = False,
) -> list[AssignmentChange]:
    """Assign one asset to each Lead's slot, replacing any earlier asset."""
    try:
        validate_slot_key(slot, "slot")
    except AttachmentRequirementError as exc:
        raise EmailAssetError(str(exc)) from None
    assigned_by = _required_text(assigned_by, "assigned by", max_length=150)
    asset = get_asset(key)
    if asset.status == EmailAsset.Status.RETIRED:
        raise EmailAssetError(f"email asset {key!r} is retired")
    leads = _leads(lead_ids)
    lead_pks = [lead.pk for lead in leads]
    if not apply:
        return _assignment_changes(
            leads, _unlocked_lead_slots(lead_pks, slot), slot=slot, asset=asset,
        )
    now = timezone.now()
    with transaction.atomic():
        existing = _lock_lead_slots(lead_pks, slot)
        locked = EmailAsset.objects.select_for_update().get(pk=asset.pk)
        if locked.status == EmailAsset.Status.RETIRED:
            raise EmailAssetError(f"email asset {key!r} was retired; nothing was assigned")
        changes = _assignment_changes(leads, existing, slot=slot, asset=locked)
        for change in changes:
            if change.action == "replace":
                row = existing[change.lead_id]
                row.asset = locked
                row.assigned_by = assigned_by
                row.assigned_at = now
                row.save(update_fields={"asset", "assigned_by", "assigned_at"})
            elif change.action == "create":
                LeadEmailAsset.objects.create(
                    lead_id=change.lead_id,
                    slot=slot,
                    asset=locked,
                    assigned_by=assigned_by,
                    assigned_at=now,
                )
    return changes


def remove_assignments(
    *,
    slot: str,
    lead_ids,
    apply: bool = False,
) -> list[AssignmentChange]:
    try:
        validate_slot_key(slot, "slot")
    except AttachmentRequirementError as exc:
        raise EmailAssetError(str(exc)) from None
    leads = _leads(lead_ids)
    lead_pks = [lead.pk for lead in leads]

    def changes_for(existing):
        return [
            AssignmentChange(
                lead_id=lead.pk,
                lead_label=_lead_label(lead),
                slot=slot,
                action="remove" if lead.pk in existing else "absent",
                previous_asset_key=existing[lead.pk].asset.key if lead.pk in existing else "",
            )
            for lead in leads
        ]

    if not apply:
        return changes_for(_unlocked_lead_slots(lead_pks, slot))
    with transaction.atomic():
        existing = _lock_lead_slots(lead_pks, slot)
        changes = changes_for(existing)
        LeadEmailAsset.objects.filter(pk__in=[row.pk for row in existing.values()]).delete()
    return changes
