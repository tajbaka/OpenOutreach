"""Google Drive source for email assets.

Files stay private: OpenOutreach reads them with the existing Google service
account (``GOOGLE_SHEETS_CREDENTIALS_PATH``) under the read-only Drive scope.
An operator grants access by sharing the file, or its folder, with the
service account's email as a Viewer. No mailbox OAuth scope changes.

Native Google Docs/Sheets/Slides are rejected: their exports are regenerated
on every download, so an approved hash could never be verified again. Export
the document to PDF and register the PDF instead.
"""
from __future__ import annotations

import io
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from email_assets.providers import AssetSourceError, SourceFileMetadata


DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
REQUEST_TIMEOUT_SECONDS = 60
_DOWNLOAD_CHUNK_BYTES = 4 * 1024 * 1024
_FILE_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{10,200}\Z")
_PATH_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]{10,200})(?:/|$)")
_DRIVE_HOSTS = frozenset({"drive.google.com", "docs.google.com"})
# capabilities.canDownload is Drive's own answer to "may this reader download
# the bytes"; a file can be readable (metadata, checksum) but not downloadable.
_METADATA_FIELDS = (
    "id,name,mimeType,size,md5Checksum,sha256Checksum,"
    "headRevisionId,modifiedTime,trashed,capabilities(canDownload)"
)


def is_google_drive_link(url: str) -> bool:
    try:
        host = (urlsplit(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in _DRIVE_HOSTS


def parse_google_drive_link(url: str) -> str:
    """Return the Drive file ID from a pasted share, open or download link."""
    text = str(url or "").strip()
    if _FILE_ID_RE.fullmatch(text):
        return text
    try:
        parts = urlsplit(text)
    except ValueError:
        raise AssetSourceError("unsupported", "the Drive link is not a valid URL") from None
    if parts.scheme != "https" or (parts.hostname or "").lower() not in _DRIVE_HOSTS:
        raise AssetSourceError(
            "unsupported",
            "paste an https://drive.google.com or https://docs.google.com file link",
        )
    path_match = _PATH_ID_RE.search(parts.path)
    if path_match:
        return path_match.group(1)
    query_ids = parse_qs(parts.query).get("id", [])
    if len(query_ids) == 1 and _FILE_ID_RE.fullmatch(query_ids[0]):
        return query_ids[0]
    if "/folders/" in parts.path:
        raise AssetSourceError(
            "unsupported",
            "the link is a Drive folder; paste the link to one file",
        )
    raise AssetSourceError("unsupported", "the Drive link does not name a file ID")


def _parse_rfc3339(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


class GoogleDriveSource:
    provider = "google_drive"

    def __init__(self, credentials_path: str | Path | None = None):
        self._credentials_path = credentials_path
        self._service = None

    # -- credentials -----------------------------------------------------

    def _resolved_credentials_path(self) -> Path:
        from linkedin.conf import GOOGLE_SHEETS_CREDENTIALS_PATH, ROOT_DIR

        raw = self._credentials_path or GOOGLE_SHEETS_CREDENTIALS_PATH
        if not raw:
            raise AssetSourceError(
                "permission",
                "Google Drive access needs GOOGLE_SHEETS_CREDENTIALS_PATH on this machine",
            )
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = ROOT_DIR / path
        if not path.is_file():
            raise AssetSourceError(
                "permission",
                f"Google service-account credentials are missing at {path}",
            )
        return path

    def service_account_email(self) -> str:
        try:
            data = json.loads(self._resolved_credentials_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, AssetSourceError):
            return ""
        return str(data.get("client_email") or "")

    def access_hint(self) -> str:
        email = self.service_account_email()
        if email:
            return f"Share the file (or its folder) with {email} as a Viewer."
        return "Share the file with the OpenOutreach Google service account as a Viewer."

    def _drive(self):
        if self._service is not None:
            return self._service
        import google_auth_httplib2
        import httplib2
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        try:
            credentials = Credentials.from_service_account_file(
                str(self._resolved_credentials_path()),
                scopes=[DRIVE_READONLY_SCOPE],
            )
        except (OSError, ValueError) as exc:
            raise AssetSourceError(
                "permission",
                f"Google service-account credentials are unreadable ({type(exc).__name__})",
            ) from None
        http = google_auth_httplib2.AuthorizedHttp(
            credentials,
            http=httplib2.Http(timeout=REQUEST_TIMEOUT_SECONDS),
        )
        self._service = build("drive", "v3", http=http, cache_discovery=False)
        return self._service

    # -- errors ----------------------------------------------------------

    @staticmethod
    def _expected_errors() -> tuple[type[BaseException], ...]:
        """Provider and transport failures; anything else is a bug and propagates."""
        import httplib2
        from google.auth.exceptions import GoogleAuthError
        from googleapiclient.errors import HttpError

        return (HttpError, httplib2.HttpLib2Error, GoogleAuthError, OSError)

    def _provider_error(self, exc: Exception) -> AssetSourceError:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            status = int(getattr(exc.resp, "status", 0) or 0)
            detail = ""
            try:
                detail = exc.content.decode("utf-8", "replace") if exc.content else ""
            except AttributeError:
                detail = ""
            if status == 404:
                return AssetSourceError(
                    "not_found",
                    "Drive file was not found or is not shared with OpenOutreach. "
                    + self.access_hint(),
                )
            if status in {401, 403} and "rateLimitExceeded" not in detail:
                return AssetSourceError(
                    "permission",
                    "OpenOutreach is not allowed to read this Drive file. "
                    + self.access_hint(),
                )
            return AssetSourceError(
                "unavailable",
                f"Google Drive is temporarily unavailable (HTTP {status or 'error'})",
            )
        return AssetSourceError(
            "unavailable",
            f"Google Drive could not be reached ({type(exc).__name__})",
        )

    # -- AssetSource -----------------------------------------------------

    def parse_link(self, url: str) -> str:
        return parse_google_drive_link(url)

    def view_link(self, file_id: str) -> str:
        return f"https://drive.google.com/file/d/{file_id}/view"

    def metadata(self, file_id: str) -> SourceFileMetadata:
        if not _FILE_ID_RE.fullmatch(str(file_id or "")):
            raise AssetSourceError("unsupported", "Drive file ID is malformed")
        try:
            raw = self._drive().files().get(
                fileId=file_id,
                fields=_METADATA_FIELDS,
                supportsAllDrives=True,
            ).execute(num_retries=2)
        except self._expected_errors() as exc:
            raise self._provider_error(exc) from None
        if raw.get("trashed"):
            raise AssetSourceError("not_found", "Drive file is in the trash")
        mime_type = str(raw.get("mimeType") or "")
        if mime_type.startswith("application/vnd.google-apps."):
            raise AssetSourceError(
                "unsupported",
                "native Google Docs/Sheets/Slides cannot be approved by hash; "
                "export it to PDF, upload the PDF to Drive and register that file",
            )
        size = raw.get("size")
        can_download = (raw.get("capabilities") or {}).get("canDownload")
        return SourceFileMetadata(
            file_id=str(raw.get("id") or file_id),
            name=str(raw.get("name") or ""),
            mime_type=mime_type,
            size_bytes=int(size) if size not in (None, "") else None,
            md5_checksum=str(raw.get("md5Checksum") or "").lower(),
            sha256_checksum=str(raw.get("sha256Checksum") or "").lower(),
            revision=str(raw.get("headRevisionId") or ""),
            modified_at=_parse_rfc3339(raw.get("modifiedTime") or ""),
            can_download=can_download if isinstance(can_download, bool) else None,
        )

    def download(self, file_id: str, *, max_bytes: int) -> bytes:
        from googleapiclient.http import MediaIoBaseDownload

        if not _FILE_ID_RE.fullmatch(str(file_id or "")):
            raise AssetSourceError("unsupported", "Drive file ID is malformed")
        buffer = io.BytesIO()
        try:
            request = self._drive().files().get_media(
                fileId=file_id,
                supportsAllDrives=True,
            )
            downloader = MediaIoBaseDownload(
                buffer,
                request,
                chunksize=_DOWNLOAD_CHUNK_BYTES,
            )
            done = False
            while not done:
                _progress, done = downloader.next_chunk(num_retries=2)
                if buffer.tell() > max_bytes:
                    raise AssetSourceError(
                        "too_large",
                        f"Drive file exceeds the {max_bytes}-byte attachment limit",
                    )
        except self._expected_errors() as exc:
            raise self._provider_error(exc) from None
        return buffer.getvalue()
