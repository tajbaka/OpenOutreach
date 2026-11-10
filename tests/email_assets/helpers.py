"""In-memory storage provider and fixtures for email-asset tests."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from email_assets.models import EmailAsset
from email_assets.providers import AssetSourceError, SourceFileMetadata


def pdf_bytes(label: str) -> bytes:
    return b"%PDF-1.7\n% " + label.encode("utf-8") + b"\n1 0 obj << >> endobj\n%%EOF\n"


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass
class _File:
    name: str
    mime_type: str
    content: bytes
    error: AssetSourceError | None = None
    download_error: AssetSourceError | None = None
    report_sha256: bool = True
    can_download: bool | None = True


class FakeSource:
    """Drive-like source whose files tests can edit, break or delete."""

    provider = "google_drive"

    def __init__(self):
        self.files: dict[str, _File] = {}
        self.downloads: list[str] = []
        self.metadata_reads: list[str] = []
        # Called after a download completes, before the bytes are returned:
        # lets a test change approvals/assignments while a send is preparing.
        self.on_download = None

    def add(self, file_id: str, content: bytes, *, name: str = "", mime_type: str = "application/pdf"):
        self.files[file_id] = _File(name=name or f"{file_id}.pdf", mime_type=mime_type, content=content)
        return file_id

    def change(self, file_id: str, content: bytes) -> None:
        self.files[file_id].content = content

    def fail(self, file_id: str, code: str = "permission", message: str = "access revoked") -> None:
        self.files[file_id].error = AssetSourceError(code, message)

    def parse_link(self, url: str) -> str:
        file_id = url.rstrip("/").split("/d/")[-1].split("/")[0] if "/d/" in url else url
        if file_id not in self.files:
            raise AssetSourceError("not_found", f"no file {file_id}")
        return file_id

    def view_link(self, file_id: str) -> str:
        return f"https://drive.google.com/file/d/{file_id}/view"

    def access_hint(self) -> str:
        return "Share it with the test service account."

    def _file(self, file_id: str) -> _File:
        item = self.files.get(file_id)
        if item is None:
            raise AssetSourceError("not_found", f"no file {file_id}")
        if item.error is not None:
            raise item.error
        return item

    def metadata(self, file_id: str) -> SourceFileMetadata:
        self.metadata_reads.append(file_id)
        item = self._file(file_id)
        return SourceFileMetadata(
            file_id=file_id,
            name=item.name,
            mime_type=item.mime_type,
            size_bytes=len(item.content),
            md5_checksum=hashlib.md5(item.content).hexdigest(),
            sha256_checksum=sha(item.content) if item.report_sha256 else "",
            revision=f"rev-{sha(item.content)[:8]}",
            can_download=item.can_download,
        )

    def download(self, file_id: str, *, max_bytes: int) -> bytes:
        self.downloads.append(file_id)
        item = self._file(file_id)
        if item.download_error is not None:
            raise item.download_error
        if len(item.content) > max_bytes:
            raise AssetSourceError("too_large", "too large")
        content = item.content
        if self.on_download is not None:
            hook, self.on_download = self.on_download, None
            hook(file_id)
        return content


def approved_asset(source: FakeSource, *, key: str, content: bytes, filename: str = "") -> EmailAsset:
    """Register and approve one asset through the real services."""
    from email_assets.services import approve_asset, register_asset

    file_id = source.add(f"file{key.replace('-', '')}0000", content, name=filename or f"{key}.pdf")
    asset, _snapshot = register_asset(
        url=f"https://drive.google.com/file/d/{file_id}/view",
        key=key,
        name=key.replace("-", " ").title(),
        registered_by="Arian",
        apply=True,
        source=source,
    )
    approve_asset(
        key=key,
        approved_by="Arian",
        sha256=asset.sha256,
        apply=True,
        source=source,
    )
    asset.refresh_from_db()
    return asset
