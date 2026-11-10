"""Validated attachment bytes and MIME assembly for outbound email.

This module is pure: it never touches the database or a storage provider.
Every ``AttachmentFile`` proves on construction that its bytes match the
declared SHA-256, type and filename rules, so the Gmail permit and client can
trust its identity without re-deriving it.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Iterable, Sequence


# Gmail rejects messages above 25 MB once base64-encoded. 18 MiB of raw
# attachment bytes stays below that after encoding and headers.
MAX_EMAIL_ATTACHMENT_BYTES = 18 * 1024 * 1024
MAX_ATTACHMENTS_PER_EMAIL = 5
MAX_FILENAME_LENGTH = 150

ALLOWED_ATTACHMENT_TYPES: dict[str, tuple[str, ...]] = {
    "application/pdf": (".pdf",),
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/gif": (".gif",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (".docx",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (".xlsx",),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (".pptx",),
}
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (b"PK\x03\x04",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (b"PK\x03\x04",),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (b"PK\x03\x04",),
}
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_UNSAFE_FILENAME_RE = re.compile(r"[\x00-\x1f\x7f/\\]")


class AttachmentFileError(ValueError):
    """Attachment bytes, type or filename cannot be sent safely."""


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def validate_mime_type(mime_type: str) -> str:
    clean = (mime_type or "").strip().lower()
    if clean not in ALLOWED_ATTACHMENT_TYPES:
        allowed = ", ".join(sorted(ALLOWED_ATTACHMENT_TYPES))
        raise AttachmentFileError(
            f"file type {mime_type!r} is not an allowed email attachment ({allowed})"
        )
    return clean


def clean_filename(name: str, *, mime_type: str) -> str:
    """Return a safe attachment filename whose extension matches its type."""
    mime = validate_mime_type(mime_type)
    filename = " ".join(str(name or "").split())
    if not filename:
        raise AttachmentFileError("attachment filename is blank")
    if _UNSAFE_FILENAME_RE.search(filename) or filename.startswith("."):
        raise AttachmentFileError(
            f"attachment filename {filename!r} contains path or control characters"
        )
    if len(filename) > MAX_FILENAME_LENGTH:
        raise AttachmentFileError(
            f"attachment filename exceeds {MAX_FILENAME_LENGTH} characters"
        )
    extensions = ALLOWED_ATTACHMENT_TYPES[mime]
    if not filename.lower().endswith(extensions):
        raise AttachmentFileError(
            f"attachment filename {filename!r} must end with {' or '.join(extensions)} "
            f"for {mime}"
        )
    return filename


def validate_attachment_bytes(content: bytes, *, mime_type: str) -> None:
    mime = validate_mime_type(mime_type)
    if not isinstance(content, (bytes, bytearray)) or not content:
        raise AttachmentFileError("attachment file is empty")
    if len(content) > MAX_EMAIL_ATTACHMENT_BYTES:
        raise AttachmentFileError(
            f"attachment is {len(content)} bytes; the limit is "
            f"{MAX_EMAIL_ATTACHMENT_BYTES} bytes"
        )
    if not bytes(content).startswith(_SIGNATURES[mime]):
        raise AttachmentFileError(f"attachment bytes are not a valid {mime} file")


@dataclass(frozen=True)
class AttachmentFile:
    """Exact bytes and identity for one email attachment."""

    filename: str
    mime_type: str
    content: bytes = field(repr=False)
    sha256: str

    def __post_init__(self) -> None:
        mime = validate_mime_type(self.mime_type)
        if mime != self.mime_type:
            raise AttachmentFileError("attachment MIME type must be normalized")
        if clean_filename(self.filename, mime_type=mime) != self.filename:
            raise AttachmentFileError("attachment filename must be normalized")
        validate_attachment_bytes(self.content, mime_type=mime)
        if not is_sha256(self.sha256):
            raise AttachmentFileError("attachment SHA-256 must be lowercase hex")
        if sha256_hex(bytes(self.content)) != self.sha256:
            raise AttachmentFileError(
                f"attachment {self.filename!r} bytes do not match their SHA-256"
            )

    @property
    def size_bytes(self) -> int:
        return len(self.content)

    def identity(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


def attachment_identities(attachments: Iterable[AttachmentFile]) -> list[dict[str, object]]:
    return [attachment.identity() for attachment in attachments]


def attachment_set_problem(entries: Iterable[tuple[str, int]]) -> str:
    """Return why (filename, size) entries cannot share one email, or "".

    The single rule used by sending (on downloaded bytes) and by planning and
    review (on registered sizes), so a preflight "ready" matches the send.
    """
    items = list(entries)
    if len(items) > MAX_ATTACHMENTS_PER_EMAIL:
        return f"an email may carry at most {MAX_ATTACHMENTS_PER_EMAIL} attachments"
    names = [filename.casefold() for filename, _size in items]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        return (
            "an email cannot carry two attachments with one filename "
            f"({', '.join(duplicates)})"
        )
    total = sum(int(size or 0) for _filename, size in items)
    if total > MAX_EMAIL_ATTACHMENT_BYTES:
        return (
            f"attachments total {total} bytes; the limit is "
            f"{MAX_EMAIL_ATTACHMENT_BYTES} bytes"
        )
    return ""


def validate_attachment_set(attachments: Sequence[AttachmentFile]) -> tuple[AttachmentFile, ...]:
    """Validate one message's attachment list as a whole."""
    items = tuple(attachments)
    if not all(isinstance(item, AttachmentFile) for item in items):
        raise AttachmentFileError("attachments must be validated AttachmentFile values")
    problem = attachment_set_problem((item.filename, item.size_bytes) for item in items)
    if problem:
        raise AttachmentFileError(problem)
    return items


def add_attachments_to_message(
    message: EmailMessage,
    attachments: Sequence[AttachmentFile],
) -> None:
    """Append exact attachment bytes to an already-built plain-text message."""
    for attachment in validate_attachment_set(attachments):
        maintype, subtype = attachment.mime_type.split("/", 1)
        message.add_attachment(
            bytes(attachment.content),
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )
