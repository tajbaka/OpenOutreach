from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from django.core.management import call_command as django_call_command
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError

from gmail.client import GmailClient
from linkedin.exceptions import EnrichmentError
from linkedin.management.commands import refresh_crm, sync_gmail_context


SAFE_ERROR = "Gmail context sync is temporarily unavailable."


def _http_error_with_sensitive_detail() -> HttpError:
    response = SimpleNamespace(
        status=503,
        reason="Service Unavailable",
        get=lambda _name, _default=None: None,
    )
    return HttpError(
        response,
        b'{"error":{"message":"mailbox@example.invalid secret-token"}}',
        uri="https://gmail.googleapis.test/private-thread",
    )


def test_sync_command_sanitizes_gmail_api_http_error(monkeypatch):
    monkeypatch.setattr(
        sync_gmail_context,
        "GmailClient",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        sync_gmail_context,
        "self_emails_for_client",
        lambda _client: (_ for _ in ()).throw(
            _http_error_with_sensitive_detail()
        ),
    )

    with pytest.raises(EnrichmentError) as caught:
        django_call_command(
            "sync_gmail_context",
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

    assert str(caught.value) == SAFE_ERROR
    assert "secret-token" not in str(caught.value)
    assert "mailbox@" not in str(caught.value)


def test_sync_command_does_not_hide_programmer_or_payload_errors(monkeypatch):
    monkeypatch.setattr(
        sync_gmail_context,
        "GmailClient",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        sync_gmail_context,
        "self_emails_for_client",
        lambda _client: (_ for _ in ()).throw(
            ValueError("malformed Gmail response shape")
        ),
    )

    with pytest.raises(ValueError, match="malformed Gmail response shape"):
        django_call_command(
            "sync_gmail_context",
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )


def test_gmail_client_sanitizes_auth_refresh_transport_error(
    monkeypatch,
    tmp_path,
):
    token = tmp_path / "gmail-token.json"
    token.write_text("{}", encoding="utf-8")

    class ExpiredCredentials:
        expired = True
        refresh_token = "configured"
        valid = False

        def refresh(self, _request):
            raise TransportError("secret-token mailbox@example.invalid")

    monkeypatch.setattr("gmail.client.token_path", lambda _account: token)
    monkeypatch.setattr(
        "gmail.client.Credentials.from_authorized_user_file",
        lambda *_args, **_kwargs: ExpiredCredentials(),
    )

    with pytest.raises(EnrichmentError) as caught:
        GmailClient(operator="Arian")

    assert str(caught.value) == (
        "Gmail authentication refresh is temporarily unavailable."
    )
    assert "secret-token" not in str(caught.value)
    assert "mailbox@" not in str(caught.value)


class _ReachedStoredContextFallback(RuntimeError):
    pass


@pytest.mark.parametrize("failure_kind", ["http", "auth"])
def test_refresh_continues_with_stored_context_after_recoverable_gmail_failure(
    monkeypatch,
    failure_kind,
):
    spreadsheet = SimpleNamespace(id="crm-workbook")
    monkeypatch.setattr("linkedin.conf.GOOGLE_SHEETS_ID", "crm-workbook")
    monkeypatch.setattr(
        "linkedin.notifications.sheets._gspread_client",
        lambda: spreadsheet,
    )
    monkeypatch.setattr(
        refresh_crm,
        "_inventory_with_stable_keys",
        lambda *_args, **_kwargs: {
            "title": "CRM",
            "tab_count": 1,
            "tabs": [],
        },
    )
    monkeypatch.setattr(
        refresh_crm,
        "_capture_people_preservation_snapshot",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        refresh_crm,
        "_people_explicit_stage_lead_ids",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            _ReachedStoredContextFallback()
        ),
    )

    if failure_kind == "http":
        monkeypatch.setattr(
            sync_gmail_context,
            "GmailClient",
            lambda **_kwargs: object(),
        )
        monkeypatch.setattr(
            sync_gmail_context,
            "self_emails_for_client",
            lambda _client: (_ for _ in ()).throw(
                _http_error_with_sensitive_detail()
            ),
        )
    else:
        monkeypatch.setattr(
            sync_gmail_context,
            "GmailClient",
            lambda **_kwargs: (_ for _ in ()).throw(
                TransportError("secret-token mailbox@example.invalid")
            ),
        )

    def run_real_gmail_command(name, *args, **kwargs):
        assert name == "sync_gmail_context"
        return django_call_command(name, *args, **kwargs)

    monkeypatch.setattr(refresh_crm, "call_command", run_real_gmail_command)

    options = {
        "gmail_since_days": 365,
        "granola_max_notes": None,
        "skip_gmail_context": False,
        "skip_granola": True,
        "skip_people": True,
        "backup_dir": "artifacts/crm-backups",
    }
    with pytest.raises(_ReachedStoredContextFallback):
        refresh_crm.Command()._refresh(options, dry_run=True)
