import json

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from email_assets.providers import AssetSourceError, provider_for_link
from email_assets.providers.google_drive import GoogleDriveSource, parse_google_drive_link


FILE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


@pytest.mark.parametrize("link", [
    f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing",
    f"https://drive.google.com/file/u/0/d/{FILE_ID}/view",
    f"https://drive.google.com/open?id={FILE_ID}",
    f"https://drive.google.com/uc?id={FILE_ID}&export=download",
    f"https://docs.google.com/document/d/{FILE_ID}/edit",
    FILE_ID,
])
def test_drive_links_resolve_to_the_file_id(link):
    assert parse_google_drive_link(link) == FILE_ID


@pytest.mark.parametrize("link, message", [
    ("https://example.com/file/d/x/view", "drive.google.com"),
    ("http://drive.google.com/file/d/" + FILE_ID, "https://"),
    ("https://drive.google.com/drive/folders/abc", "folder"),
])
def test_drive_links_reject_non_files(link, message):
    with pytest.raises(AssetSourceError, match=message):
        parse_google_drive_link(link)


def test_provider_detection_supports_only_drive():
    assert provider_for_link(f"https://drive.google.com/file/d/{FILE_ID}/view") == "google_drive"
    with pytest.raises(AssetSourceError, match="only Google Drive"):
        provider_for_link("https://dropbox.com/s/abc/file.pdf")


class _Request:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def execute(self, num_retries=0):
        if self.error is not None:
            raise self.error
        return self.response


class _Files:
    def __init__(self, request):
        self.request = request
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        return self.request


class _Drive:
    def __init__(self, request):
        self.files_api = _Files(request)

    def files(self):
        return self.files_api


def _source(request, tmp_path):
    credentials = tmp_path / "sa.json"
    credentials.write_text(json.dumps({"client_email": "reader@example.iam.gserviceaccount.com"}))
    source = GoogleDriveSource(credentials_path=credentials)
    source._service = _Drive(request)
    return source


def _http_error(status):
    return HttpError(Response({"status": status}), b'{"error": {"message": "nope"}}')


def test_metadata_reads_private_file_fields(tmp_path):
    source = _source(_Request({
        "id": FILE_ID,
        "name": "Acme Marketplace.pdf",
        "mimeType": "application/pdf",
        "size": "2048",
        "md5Checksum": "ABC",
        "sha256Checksum": "F" * 64,
        "headRevisionId": "rev-1",
        "modifiedTime": "2026-11-10T15:00:00.000Z",
        "capabilities": {"canDownload": True},
    }), tmp_path)

    metadata = source.metadata(FILE_ID)

    assert metadata.can_download is True
    assert "capabilities(canDownload)" in source._service.files_api.calls[0]["fields"]
    assert metadata.size_bytes == 2048
    assert metadata.sha256_checksum == "f" * 64
    assert metadata.modified_at.isoformat() == "2026-11-10T15:00:00+00:00"
    call = source._service.files_api.calls[0]
    assert call["fileId"] == FILE_ID
    assert call["supportsAllDrives"] is True


@pytest.mark.parametrize("status, code", [(404, "not_found"), (403, "permission"), (500, "unavailable")])
def test_metadata_maps_provider_errors_with_a_sharing_hint(tmp_path, status, code):
    source = _source(_Request(error=_http_error(status)), tmp_path)
    with pytest.raises(AssetSourceError) as raised:
        source.metadata(FILE_ID)
    assert raised.value.code == code
    if code != "unavailable":
        assert "reader@example.iam.gserviceaccount.com" in raised.value.message


def test_native_google_docs_and_trashed_files_are_refused(tmp_path):
    native = _source(_Request({"id": FILE_ID, "mimeType": "application/vnd.google-apps.document"}), tmp_path)
    with pytest.raises(AssetSourceError, match="export it to PDF"):
        native.metadata(FILE_ID)
    trashed = _source(_Request({"id": FILE_ID, "mimeType": "application/pdf", "trashed": True}), tmp_path)
    with pytest.raises(AssetSourceError, match="trash"):
        trashed.metadata(FILE_ID)


def test_missing_credentials_is_a_permission_problem(tmp_path):
    source = GoogleDriveSource(credentials_path=tmp_path / "absent.json")
    with pytest.raises(AssetSourceError) as raised:
        source.metadata(FILE_ID)
    assert raised.value.code == "permission"


def test_download_restriction_is_reported_from_capabilities(tmp_path):
    restricted = _source(_Request({
        "id": FILE_ID, "mimeType": "application/pdf", "size": "10",
        "capabilities": {"canDownload": False},
    }), tmp_path)
    assert restricted.metadata(FILE_ID).can_download is False
    unreported = _source(_Request({"id": FILE_ID, "mimeType": "application/pdf"}), tmp_path)
    assert unreported.metadata(FILE_ID).can_download is None
