"""Attachment requirements declared on a published email step.

A step names *what* to attach, never a file: either a lead slot resolved per
recipient (``{"slot": "marketplace_pdf"}``) or one fixed asset for everyone
(``{"asset": "boundera-overview-pdf"}``). ``required`` defaults to true; an
optional slot with no assignment for a recipient is skipped, while every other
problem still holds the email. This module is pure so the message-program
parsers can share it without importing models.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from email_assets.files import MAX_ATTACHMENTS_PER_EMAIL


ASSET_KEY_MAX_LENGTH = 160
SLOT_KEY_MAX_LENGTH = 80
_ASSET_KEY_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9_-]{0,159})\Z")
_SLOT_KEY_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9_-]{0,79})\Z")
_ALLOWED_FIELDS = frozenset({"slot", "asset", "required"})


class AttachmentRequirementError(ValueError):
    """A declared step attachment is malformed."""


def validate_asset_key(value: object, context: str = "asset key") -> str:
    if not isinstance(value, str) or not _ASSET_KEY_RE.fullmatch(value):
        raise AttachmentRequirementError(
            f"{context} must be a lowercase key of letters, digits, '-' or '_' "
            f"(at most {ASSET_KEY_MAX_LENGTH} characters)"
        )
    return value


def validate_slot_key(value: object, context: str = "slot") -> str:
    if not isinstance(value, str) or not _SLOT_KEY_RE.fullmatch(value):
        raise AttachmentRequirementError(
            f"{context} must be a lowercase key of letters, digits, '-' or '_' "
            f"(at most {SLOT_KEY_MAX_LENGTH} characters)"
        )
    return value


@dataclass(frozen=True)
class AttachmentRequirement:
    slot: str = ""
    asset: str = ""
    required: bool = True

    def __post_init__(self) -> None:
        if bool(self.slot) == bool(self.asset):
            raise AttachmentRequirementError(
                "an attachment requirement names exactly one of slot or asset"
            )
        if self.slot:
            validate_slot_key(self.slot)
        else:
            validate_asset_key(self.asset)
        if not isinstance(self.required, bool):
            raise AttachmentRequirementError("attachment 'required' must be true or false")

    @property
    def kind(self) -> str:
        return "slot" if self.slot else "asset"

    @property
    def reference(self) -> str:
        return self.slot or self.asset

    @property
    def label(self) -> str:
        suffix = "" if self.required else " (optional)"
        return f"{self.kind} {self.reference}{suffix}"

    def payload(self) -> dict[str, object]:
        return {self.kind: self.reference, "required": self.required}


def parse_attachment_requirements(
    value: object,
    context: str = "attachments",
) -> tuple[AttachmentRequirement, ...]:
    """Strictly parse a JSON attachment declaration list."""
    if not isinstance(value, list):
        raise AttachmentRequirementError(f"{context} must be a list")
    if len(value) > MAX_ATTACHMENTS_PER_EMAIL:
        raise AttachmentRequirementError(
            f"{context} lists more than {MAX_ATTACHMENTS_PER_EMAIL} attachments"
        )
    parsed: list[AttachmentRequirement] = []
    seen: set[tuple[str, str]] = set()
    for position, item in enumerate(value, start=1):
        item_context = f"{context} entry {position}"
        if not isinstance(item, dict):
            raise AttachmentRequirementError(f"{item_context} must be an object")
        extra = sorted(set(item) - _ALLOWED_FIELDS)
        if extra:
            raise AttachmentRequirementError(f"{item_context} has unknown fields {extra!r}")
        if ("slot" in item) == ("asset" in item):
            raise AttachmentRequirementError(
                f"{item_context} must name exactly one of 'slot' or 'asset'"
            )
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise AttachmentRequirementError(f"{item_context} 'required' must be true or false")
        try:
            if "slot" in item:
                requirement = AttachmentRequirement(
                    slot=validate_slot_key(item["slot"], f"{item_context} slot"),
                    required=required,
                )
            else:
                requirement = AttachmentRequirement(
                    asset=validate_asset_key(item["asset"], f"{item_context} asset"),
                    required=required,
                )
        except AttachmentRequirementError as exc:
            raise AttachmentRequirementError(str(exc)) from None
        identity = (requirement.kind, requirement.reference)
        if identity in seen:
            raise AttachmentRequirementError(
                f"{item_context} repeats {requirement.kind} {requirement.reference!r}"
            )
        seen.add(identity)
        parsed.append(requirement)
    return tuple(parsed)


def requirements_payload(
    requirements: tuple[AttachmentRequirement, ...],
) -> list[dict[str, object]]:
    return [requirement.payload() for requirement in requirements]
