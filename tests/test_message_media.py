from __future__ import annotations

import hashlib
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from linkedin.exceptions import (
    LinkedInMediaMismatchError,
    LinkedInMediaValidationError,
)
from linkedin.message_media import (
    MAX_LINKEDIN_MEDIA_BYTES,
    LinkedInMediaKind,
    resolve_linkedin_media,
)


def _asset_root(repository_root: Path) -> Path:
    root = repository_root / "assets" / "follow_up"
    root.mkdir(parents=True)
    return root


def _valid_gif(version: bytes = b"GIF89a") -> bytes:
    return version + b"minimal-test-payload"


def _valid_mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


@pytest.mark.parametrize("signature", (b"GIF87a", b"GIF89a"))
def test_resolve_linkedin_media_returns_immutable_gif_evidence(
    tmp_path: Path,
    signature: bytes,
) -> None:
    payload = _valid_gif(signature)
    path = _asset_root(tmp_path) / "demo.gif"
    path.write_bytes(payload)

    asset = resolve_linkedin_media("demo.gif", root_dir=tmp_path)

    assert asset.reference == "demo.gif"
    assert asset.path == path.resolve()
    assert asset.kind is LinkedInMediaKind.GIF
    assert asset.mime_type == "image/gif"
    assert asset.size_bytes == len(payload)
    assert asset.sha256 == hashlib.sha256(payload).hexdigest()
    assert asset.evidence() == {
        "type": "gif",
        "reference": "demo.gif",
        "mime_type": "image/gif",
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    with pytest.raises(FrozenInstanceError):
        asset.reference = "replacement.gif"  # type: ignore[misc]


def test_resolve_linkedin_media_accepts_explicit_approved_root_mp4(
    tmp_path: Path,
) -> None:
    payload = _valid_mp4()
    path = _asset_root(tmp_path) / "overview.mp4"
    path.write_bytes(payload)

    asset = resolve_linkedin_media(
        "assets/follow_up/overview.mp4",
        expected_kind="video",
        expected_mime_type="video/mp4",
        expected_size_bytes=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest().upper(),
        root_dir=tmp_path,
    )

    assert asset.reference == "assets/follow_up/overview.mp4"
    assert asset.kind is LinkedInMediaKind.VIDEO
    assert asset.mime_type == "video/mp4"


@pytest.mark.parametrize(
    ("filename", "payload", "message"),
    (
        ("bad.gif", b"not-a-gif", "invalid file signature"),
        ("bad.mp4", b"not-an-mp4", "invalid ftyp signature"),
        ("bad.jpg", b"GIF89a-payload", "unsupported LinkedIn media extension"),
        ("bad.gif", _valid_mp4(), "invalid file signature"),
        ("bad.mp4", _valid_gif(), "invalid ftyp signature"),
    ),
)
def test_resolve_linkedin_media_rejects_unsupported_or_spoofed_files(
    tmp_path: Path,
    filename: str,
    payload: bytes,
    message: str,
) -> None:
    (_asset_root(tmp_path) / filename).write_bytes(payload)

    with pytest.raises(LinkedInMediaValidationError, match=message):
        resolve_linkedin_media(filename, root_dir=tmp_path)


def test_resolve_linkedin_media_rejects_missing_empty_and_directory(
    tmp_path: Path,
) -> None:
    root = _asset_root(tmp_path)
    (root / "empty.gif").touch()
    (root / "folder.gif").mkdir()

    with pytest.raises(LinkedInMediaValidationError, match="does not exist"):
        resolve_linkedin_media("missing.gif", root_dir=tmp_path)
    with pytest.raises(LinkedInMediaValidationError, match="is empty"):
        resolve_linkedin_media("empty.gif", root_dir=tmp_path)
    with pytest.raises(LinkedInMediaValidationError, match="not a regular file"):
        resolve_linkedin_media("folder.gif", root_dir=tmp_path)


def test_resolve_linkedin_media_rejects_asset_larger_than_limit(
    tmp_path: Path,
) -> None:
    path = _asset_root(tmp_path) / "oversized.gif"
    with path.open("wb") as media_file:
        media_file.write(b"GIF89a")
        media_file.seek(MAX_LINKEDIN_MEDIA_BYTES)
        media_file.write(b"x")

    with pytest.raises(LinkedInMediaValidationError, match="20 MiB"):
        resolve_linkedin_media("oversized.gif", root_dir=tmp_path)


@pytest.mark.parametrize(
    "reference",
    (
        "../outside.gif",
        "assets/follow_up/../../outside.gif",
        "/tmp/outside.gif",
        "..\\outside.gif",
    ),
)
def test_resolve_linkedin_media_rejects_traversal_and_absolute_paths(
    tmp_path: Path,
    reference: str,
) -> None:
    _asset_root(tmp_path)

    with pytest.raises(LinkedInMediaValidationError, match="must stay inside"):
        resolve_linkedin_media(reference, root_dir=tmp_path)


def test_resolve_linkedin_media_rejects_symlink_escape(tmp_path: Path) -> None:
    root = _asset_root(tmp_path)
    outside = tmp_path / "outside.gif"
    outside.write_bytes(_valid_gif())
    (root / "escape.gif").symlink_to(outside)

    with pytest.raises(LinkedInMediaValidationError, match="escapes the approved root"):
        resolve_linkedin_media("escape.gif", root_dir=tmp_path)


def test_resolve_linkedin_media_rejects_symlinked_approved_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "demo.gif").write_bytes(_valid_gif())
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "follow_up").symlink_to(outside)

    with pytest.raises(LinkedInMediaValidationError, match="approved media root is unsafe"):
        resolve_linkedin_media("demo.gif", root_dir=tmp_path)


@pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="mode-000 readability is platform/user dependent",
)
def test_resolve_linkedin_media_rejects_unreadable_file(tmp_path: Path) -> None:
    path = _asset_root(tmp_path) / "unreadable.gif"
    path.write_bytes(_valid_gif())
    path.chmod(0)
    try:
        with pytest.raises(LinkedInMediaValidationError, match="cannot be read"):
            resolve_linkedin_media("unreadable.gif", root_dir=tmp_path)
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize(
    ("kwargs", "field"),
    (
        ({"expected_kind": "video"}, "kind"),
        ({"expected_mime_type": "video/mp4"}, "MIME type"),
        ({"expected_size_bytes": 999}, "size"),
        ({"expected_sha256": "f" * 64}, "SHA-256"),
    ),
)
def test_resolve_linkedin_media_detects_frozen_metadata_mismatch(
    tmp_path: Path,
    kwargs: dict[str, object],
    field: str,
) -> None:
    (_asset_root(tmp_path) / "demo.gif").write_bytes(_valid_gif())

    with pytest.raises(LinkedInMediaMismatchError, match=field):
        resolve_linkedin_media("demo.gif", root_dir=tmp_path, **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    (
        {"expected_kind": "document"},
        {"expected_mime_type": ""},
        {"expected_size_bytes": 0},
        {"expected_size_bytes": True},
        {"expected_sha256": "not-a-digest"},
    ),
)
def test_resolve_linkedin_media_rejects_invalid_expected_metadata(
    tmp_path: Path,
    kwargs: dict[str, object],
) -> None:
    (_asset_root(tmp_path) / "demo.gif").write_bytes(_valid_gif())

    with pytest.raises(LinkedInMediaValidationError):
        resolve_linkedin_media("demo.gif", root_dir=tmp_path, **kwargs)
