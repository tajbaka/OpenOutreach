"""Approved attachments for individual, operator-approved emails.

One-off emails use the Gmail drafts API (see ``skills/gmail-oauth``), never
the campaign worker. These helpers resolve an approved asset (by key, or a
Lead's slot assignment), attach its verified bytes to a draft's MIME message,
and read the saved draft or sent message back to prove the exact file is on
it. They never create Tasks, send, or touch campaign state.

Typical use::

    attachments = resolve_one_off_attachments(asset_keys=["acme-marketplace-pdf"])
    attach_to_message(mime_message, attachments)
    ...  # users.drafts.create(...)
    verify_gmail_attachments(service, draft_id=draft_id, expected=attachments)
    ...  # immediately before users.drafts.send (after explicit approval):
    recheck_one_off_attachments(attachments)
"""
from __future__ import annotations

import base64
from email import message_from_bytes, policy
from email.message import EmailMessage
from typing import Iterable, Sequence

from email_assets.files import add_attachments_to_message, sha256_hex
from email_assets.providers import source_for_provider
from email_assets.requirements import AttachmentRequirement
from email_assets.resolution import (
    ResolvedAttachment,
    SourceFactory,
    recheck_resolved_attachments,
    resolve_attachments,
)


def resolve_one_off_attachments(
    *,
    asset_keys: Iterable[str] = (),
    lead_id: int | None = None,
    slots: Iterable[str] = (),
    source_for: SourceFactory = source_for_provider,
) -> tuple[ResolvedAttachment, ...]:
    """Fetch and verify approved files, raising ``AttachmentHold`` on any problem."""
    requirements = [AttachmentRequirement(asset=key) for key in asset_keys]
    slot_list = list(slots)
    if slot_list and lead_id is None:
        raise ValueError("slot attachments need the recipient's lead_id")
    requirements.extend(AttachmentRequirement(slot=slot) for slot in slot_list)
    if not requirements:
        raise ValueError("name at least one asset key or slot to attach")
    return resolve_attachments(
        lead_id=lead_id or 0,
        requirements=requirements,
        source_for=source_for,
    )


def recheck_one_off_attachments(
    attachments: Sequence[ResolvedAttachment],
    *,
    lead_id: int | None = None,
) -> None:
    """Prove right before sending that each file is still approved and assigned.

    Raises ``AttachmentHold`` if an asset was retired, re-approved with other
    bytes, or (for slot files) unassigned or reassigned since it was attached.
    """
    from django.db import transaction

    requirements = [attachment.requirement for attachment in attachments]
    if any(requirement.slot for requirement in requirements) and lead_id is None:
        raise ValueError("slot attachments need the recipient's lead_id")
    with transaction.atomic():
        recheck_resolved_attachments(
            lead_id=lead_id or 0,
            requirements=requirements,
            resolved=attachments,
        )


def attach_to_message(
    message: EmailMessage,
    attachments: Sequence[ResolvedAttachment],
) -> None:
    """Append the verified bytes to a message built with ``set_content``."""
    add_attachments_to_message(message, [attachment.file for attachment in attachments])


def _attachments_in_raw(raw_message: bytes) -> list[dict[str, object]]:
    parsed = message_from_bytes(raw_message, policy=policy.default)
    found = []
    for part in parsed.iter_attachments():
        content = part.get_payload(decode=True) or b""
        found.append({
            "filename": part.get_filename() or "",
            "mime_type": part.get_content_type(),
            "sha256": sha256_hex(content),
            "size_bytes": len(content),
        })
    return found


def verify_gmail_attachments(
    service,
    *,
    expected: Sequence[ResolvedAttachment],
    draft_id: str = "",
    message_id: str = "",
) -> list[dict[str, object]]:
    """Read a saved draft or sent message back and require exactly ``expected``.

    Returns the attachments found. Raises ``ValueError`` when any file is
    missing, extra, renamed or has different bytes.
    """
    if bool(draft_id) == bool(message_id):
        raise ValueError("pass exactly one of draft_id or message_id")
    if draft_id:
        draft = service.users().drafts().get(userId="me", id=draft_id, format="raw").execute()
        raw = (draft.get("message") or {}).get("raw") or ""
    else:
        message = service.users().messages().get(
            userId="me",
            id=message_id,
            format="raw",
        ).execute()
        raw = message.get("raw") or ""
    if not raw:
        raise ValueError("Gmail returned no raw message to verify")
    found = _attachments_in_raw(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    want = sorted(
        (attachment.file.filename, attachment.file.sha256) for attachment in expected
    )
    have = sorted((str(item["filename"]), str(item["sha256"])) for item in found)
    if want != have:
        raise ValueError(
            "Gmail attachments do not match the approved files: "
            f"expected {want}, found {have}"
        )
    return found
