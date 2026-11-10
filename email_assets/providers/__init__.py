"""Storage-provider boundary for email assets.

Every provider resolves a pasted link to a stable file ID, reads metadata and
downloads exact bytes through authorized access. Nothing above this module
knows which provider holds a file. Add a provider by implementing
``AssetSource`` and registering it in ``_SOURCE_FACTORIES``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class AssetSourceError(Exception):
    """A provider could not return the requested file.

    ``code`` is one of ``not_found``, ``permission``, ``unsupported``,
    ``too_large`` or ``unavailable`` so callers can show a stable reason.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SourceFileMetadata:
    file_id: str
    name: str
    mime_type: str
    size_bytes: int | None
    md5_checksum: str = ""
    sha256_checksum: str = ""
    revision: str = ""
    modified_at: datetime | None = None
    # Whether this reader may download the bytes (Drive capabilities.canDownload).
    # None means the provider does not report it; the download itself decides.
    can_download: bool | None = None


class AssetSource(Protocol):
    provider: str

    def parse_link(self, url: str) -> str:
        """Return the provider file ID named by a pasted link."""

    def metadata(self, file_id: str) -> SourceFileMetadata:
        """Read current file metadata, raising ``AssetSourceError``."""

    def download(self, file_id: str, *, max_bytes: int) -> bytes:
        """Download the exact current bytes, raising ``AssetSourceError``."""

    def access_hint(self) -> str:
        """Explain how to grant OpenOutreach access to a private file."""

    def view_link(self, file_id: str) -> str:
        """Return a stable link an operator can open to inspect the file."""


def _google_drive_source() -> AssetSource:
    from email_assets.providers.google_drive import GoogleDriveSource

    return GoogleDriveSource()


_SOURCE_FACTORIES = {
    "google_drive": _google_drive_source,
}


def source_for_provider(provider: str) -> AssetSource:
    try:
        factory = _SOURCE_FACTORIES[provider]
    except KeyError:
        raise AssetSourceError(
            "unsupported",
            f"email asset provider {provider!r} is not supported",
        ) from None
    return factory()


def provider_for_link(url: str) -> str:
    """Detect which supported provider a pasted link belongs to."""
    from email_assets.providers.google_drive import is_google_drive_link

    if is_google_drive_link(url):
        return "google_drive"
    raise AssetSourceError(
        "unsupported",
        "only Google Drive file links are supported for email assets",
    )
