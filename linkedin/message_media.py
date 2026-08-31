"""Resolve and validate media attached to LinkedIn direct messages.

Only reviewed repository assets under ``assets/follow_up`` are accepted.  The
returned value is immutable so callers can carry the exact validated evidence
through their existing message-delivery lifecycle without trusting a filename
alone.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from linkedin.conf import ROOT_DIR
from linkedin.exceptions import (
    LinkedInMediaMismatchError,
    LinkedInMediaValidationError,
)


MAX_LINKEDIN_MEDIA_BYTES = 20 * 1024 * 1024
_APPROVED_ASSET_ROOT = Path("assets/follow_up")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class LinkedInMediaKind(StrEnum):
    """LinkedIn direct-message attachment types supported by this project."""

    GIF = "gif"
    VIDEO = "video"


@dataclass(frozen=True, slots=True)
class LinkedInMediaAsset:
    """Validated identity and local path for one LinkedIn attachment."""

    reference: str
    path: Path
    kind: LinkedInMediaKind
    mime_type: str
    size_bytes: int
    sha256: str

    def evidence(self) -> dict[str, str | int]:
        """Return the stable evidence persisted with a confirmed message."""

        return {
            "type": self.kind.value,
            "reference": self.reference,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


def resolve_linkedin_media(
    reference: str,
    *,
    expected_kind: LinkedInMediaKind | str | None = None,
    expected_mime_type: str | None = None,
    expected_size_bytes: int | None = None,
    expected_sha256: str | None = None,
    root_dir: Path | None = None,
) -> LinkedInMediaAsset:
    """Resolve and validate one GIF or MP4 from the approved repository root.

    ``reference`` may be relative to ``assets/follow_up`` (the normal campaign
    form) or explicitly start with ``assets/follow_up/``.  Absolute paths,
    traversal, directories, and symlinks escaping the approved root fail
    closed.  Expected values are optional and let drip execution prove that
    the local bytes still match the published campaign snapshot.
    """

    normalized_reference = _normalize_reference(reference)
    repository_root = _resolved_repository_root(root_dir)
    approved_root = _resolved_approved_root(repository_root)
    candidate = _candidate_path(
        normalized_reference,
        repository_root=repository_root,
        approved_root=approved_root,
    )
    kind, mime_type = _media_type(candidate)
    size_bytes, sha256 = _read_identity(candidate, kind=kind)

    asset = LinkedInMediaAsset(
        reference=normalized_reference,
        path=candidate,
        kind=kind,
        mime_type=mime_type,
        size_bytes=size_bytes,
        sha256=sha256,
    )
    _verify_expected_metadata(
        asset,
        expected_kind=expected_kind,
        expected_mime_type=expected_mime_type,
        expected_size_bytes=expected_size_bytes,
        expected_sha256=expected_sha256,
    )
    return asset


def _normalize_reference(reference: str) -> str:
    if not isinstance(reference, str):
        raise LinkedInMediaValidationError("media reference must be a string")

    value = reference.strip().replace("\\", "/")
    if not value:
        raise LinkedInMediaValidationError("media reference cannot be blank")

    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        raise LinkedInMediaValidationError(
            f"media reference must stay inside {_APPROVED_ASSET_ROOT.as_posix()}: {reference!r}"
        )
    if "." in pure.parts:
        pure = PurePosixPath(*(part for part in pure.parts if part != "."))
    if not pure.parts:
        raise LinkedInMediaValidationError("media reference cannot be blank")
    return pure.as_posix()


def _resolved_repository_root(root_dir: Path | None) -> Path:
    configured_root = Path(root_dir) if root_dir is not None else ROOT_DIR
    try:
        repository_root = configured_root.resolve(strict=True)
    except OSError as exc:
        raise LinkedInMediaValidationError(
            f"repository root cannot be resolved: {configured_root}"
        ) from exc
    if not repository_root.is_dir():
        raise LinkedInMediaValidationError(
            f"repository root is not a directory: {configured_root}"
        )
    return repository_root


def _resolved_approved_root(repository_root: Path) -> Path:
    configured_root = repository_root / _APPROVED_ASSET_ROOT
    current = repository_root
    for part in _APPROVED_ASSET_ROOT.parts:
        current /= part
        if current.is_symlink():
            raise LinkedInMediaValidationError(
                f"approved media root is unsafe: {_APPROVED_ASSET_ROOT.as_posix()}"
            )
    try:
        approved_root = configured_root.resolve(strict=True)
    except OSError as exc:
        raise LinkedInMediaValidationError(
            f"approved media root does not exist: {_APPROVED_ASSET_ROOT.as_posix()}"
        ) from exc

    if not approved_root.is_dir() or not _is_within(approved_root, repository_root):
        raise LinkedInMediaValidationError(
            f"approved media root is unsafe: {_APPROVED_ASSET_ROOT.as_posix()}"
        )
    return approved_root


def _candidate_path(
    reference: str,
    *,
    repository_root: Path,
    approved_root: Path,
) -> Path:
    reference_path = Path(reference)
    approved_parts = _APPROVED_ASSET_ROOT.parts
    if reference_path.parts[: len(approved_parts)] == approved_parts:
        unresolved = repository_root / reference_path
    else:
        unresolved = approved_root / reference_path

    try:
        candidate = unresolved.resolve(strict=True)
    except FileNotFoundError as exc:
        raise LinkedInMediaValidationError(
            f"LinkedIn media asset does not exist: {reference!r}"
        ) from exc
    except OSError as exc:
        raise LinkedInMediaValidationError(
            f"LinkedIn media asset cannot be resolved: {reference!r}"
        ) from exc

    if not _is_within(candidate, approved_root):
        raise LinkedInMediaValidationError(
            f"LinkedIn media asset escapes the approved root: {reference!r}"
        )
    if not candidate.is_file():
        raise LinkedInMediaValidationError(
            f"LinkedIn media asset is not a regular file: {reference!r}"
        )
    return candidate


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _media_type(path: Path) -> tuple[LinkedInMediaKind, str]:
    suffix = path.suffix.lower()
    if suffix == ".gif":
        return LinkedInMediaKind.GIF, "image/gif"
    if suffix == ".mp4":
        return LinkedInMediaKind.VIDEO, "video/mp4"
    raise LinkedInMediaValidationError(
        f"unsupported LinkedIn media extension {path.suffix!r}; expected .gif or .mp4"
    )


def _read_identity(path: Path, *, kind: LinkedInMediaKind) -> tuple[int, str]:
    digest = hashlib.sha256()
    first_bytes = b""
    size_bytes = 0

    try:
        with path.open("rb") as media_file:
            while chunk := media_file.read(1024 * 1024):
                if not first_bytes:
                    first_bytes = chunk[:32]
                size_bytes += len(chunk)
                if size_bytes > MAX_LINKEDIN_MEDIA_BYTES:
                    raise LinkedInMediaValidationError(
                        "LinkedIn media asset exceeds the 20 MiB limit: "
                        f"{path.name!r}"
                    )
                digest.update(chunk)
    except LinkedInMediaValidationError:
        raise
    except OSError as exc:
        raise LinkedInMediaValidationError(
            f"LinkedIn media asset cannot be read: {path.name!r}"
        ) from exc

    if size_bytes == 0:
        raise LinkedInMediaValidationError(
            f"LinkedIn media asset is empty: {path.name!r}"
        )
    _validate_signature(first_bytes, kind=kind, filename=path.name)
    return size_bytes, digest.hexdigest()


def _validate_signature(
    first_bytes: bytes,
    *,
    kind: LinkedInMediaKind,
    filename: str,
) -> None:
    if kind is LinkedInMediaKind.GIF:
        if first_bytes[:6] not in {b"GIF87a", b"GIF89a"}:
            raise LinkedInMediaValidationError(
                f"LinkedIn GIF has an invalid file signature: {filename!r}"
            )
        return

    if len(first_bytes) < 12 or first_bytes[4:8] != b"ftyp":
        raise LinkedInMediaValidationError(
            f"LinkedIn MP4 has an invalid ftyp signature: {filename!r}"
        )
    box_size = int.from_bytes(first_bytes[:4], byteorder="big")
    if box_size < 12:
        raise LinkedInMediaValidationError(
            f"LinkedIn MP4 has an invalid ftyp box: {filename!r}"
        )


def _verify_expected_metadata(
    asset: LinkedInMediaAsset,
    *,
    expected_kind: LinkedInMediaKind | str | None,
    expected_mime_type: str | None,
    expected_size_bytes: int | None,
    expected_sha256: str | None,
) -> None:
    normalized_kind = _normalize_expected_kind(expected_kind)
    if normalized_kind is not None and asset.kind is not normalized_kind:
        _raise_mismatch("kind", normalized_kind.value, asset.kind.value)

    if expected_mime_type is not None:
        if not isinstance(expected_mime_type, str) or not expected_mime_type.strip():
            raise LinkedInMediaValidationError(
                "expected media MIME type must be a nonempty string"
            )
        normalized_mime_type = expected_mime_type.strip().lower()
        if asset.mime_type != normalized_mime_type:
            _raise_mismatch("MIME type", normalized_mime_type, asset.mime_type)

    if expected_size_bytes is not None:
        if (
            isinstance(expected_size_bytes, bool)
            or not isinstance(expected_size_bytes, int)
            or expected_size_bytes <= 0
        ):
            raise LinkedInMediaValidationError(
                "expected media size must be a positive integer"
            )
        if asset.size_bytes != expected_size_bytes:
            _raise_mismatch("size", expected_size_bytes, asset.size_bytes)

    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str):
            raise LinkedInMediaValidationError(
                "expected media SHA-256 must be a 64-character hexadecimal string"
            )
        normalized_sha256 = expected_sha256.strip().lower()
        if not _SHA256_RE.fullmatch(normalized_sha256):
            raise LinkedInMediaValidationError(
                "expected media SHA-256 must be a 64-character hexadecimal string"
            )
        if asset.sha256 != normalized_sha256:
            _raise_mismatch("SHA-256", normalized_sha256, asset.sha256)


def _normalize_expected_kind(
    value: LinkedInMediaKind | str | None,
) -> LinkedInMediaKind | None:
    if value is None:
        return None
    try:
        return LinkedInMediaKind(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(kind.value for kind in LinkedInMediaKind)
        raise LinkedInMediaValidationError(
            f"expected media kind must be one of: {allowed}"
        ) from exc


def _raise_mismatch(field: str, expected: object, actual: object) -> None:
    raise LinkedInMediaMismatchError(
        f"LinkedIn media {field} mismatch: expected {expected!r}, got {actual!r}"
    )
