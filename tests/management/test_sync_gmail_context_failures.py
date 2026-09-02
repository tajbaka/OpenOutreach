from __future__ import annotations

import io
import logging
from types import SimpleNamespace

import pytest
from django.core.management import call_command as django_call_command
from django.core.management.base import CommandError
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError

from gmail.client import GmailClient
from gmail.data_sync import GmailContextSyncResult
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


def test_sync_command_output_is_aggregate_only(monkeypatch):
    private_mailbox = "arian-private@example.invalid"
    private_prospect = "buyer-private@example.invalid"
    private_subject = "Secret acquisition meeting"
    monkeypatch.setattr(
        sync_gmail_context,
        "GmailClient",
        lambda operator: SimpleNamespace(operator=operator),
    )
    monkeypatch.setattr(
        sync_gmail_context,
        "self_emails_for_client",
        lambda _client: {private_mailbox},
    )
    monkeypatch.setattr(
        sync_gmail_context.Command,
        "_lead_queryset",
        lambda _self, _options: [],
    )
    monkeypatch.setattr(
        sync_gmail_context,
        "sync_gmail_threads",
        lambda **_kwargs: GmailContextSyncResult(
            discovery_messages_scanned=2,
            discovery_threads_selected=1,
            unmapped_external_participants=[{
                "email": private_prospect,
                "display_name": "Private Buyer",
                "domain": "example.invalid",
                "last_inbound_at": "2026-08-20T15:00:00+00:00",
                "latest_thread_id": "private-thread-id",
                "thread_count": 1,
            }],
        ),
    )
    monkeypatch.setattr(
        sync_gmail_context,
        "sync_gmail_note_emails",
        lambda **_kwargs: GmailContextSyncResult(
            note_emails_seen=1,
            note_emails_unmatched=1,
            unmatched_notes=[{
                "date": "2026-08-20T15:00:00+00:00",
                "subject": private_subject,
                "reason": "no unique CRM lead/meeting match",
            }],
        ),
    )
    stdout = io.StringIO()

    django_call_command(
        "sync_gmail_context",
        operator="Arian",
        dry_run=True,
        stdout=stdout,
        stderr=io.StringIO(),
    )

    output = stdout.getvalue()
    assert "mailbox identities resolved: 1" in output
    assert "bidirectional_unmapped_participants=1" in output
    assert "no unique CRM lead/meeting match" not in output
    assert private_mailbox not in output
    assert private_prospect not in output
    assert private_subject not in output


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("since_days", 0, "--since-days must be positive"),
        ("discovery_since_days", -1, "--discovery-since-days must be positive"),
        ("discovery_max_messages", 0, "--discovery-max-messages must be positive"),
        ("discovery_max_threads", 0, "--discovery-max-threads must be positive"),
        ("limit", 0, "--limit must be positive"),
        ("show_unmatched", -1, "--show-unmatched cannot be negative"),
    ],
)
def test_sync_command_rejects_unsafe_numeric_bounds(option, value, message):
    with pytest.raises(CommandError, match=message):
        django_call_command(
            "sync_gmail_context",
            **{option: value},
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )


def test_private_gmail_discovery_state_round_trips_with_restricted_mode(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(sync_gmail_context, "GMAIL_DATA_DIR", tmp_path)
    monkeypatch.delattr(sync_gmail_context.os, "fchmod", raising=False)
    result = GmailContextSyncResult(
        gmail_processed_thread_versions={"a" * 64: "b" * 64},
        unmapped_external_participants=[{
            "account_key": "arian_boundera",
            "email": "buyer@example.invalid",
            "display_name": "Buyer",
            "domain": "example.invalid",
            "last_inbound_at": "2026-08-20T15:00:00+00:00",
            "latest_thread_id": "arian_boundera:thread-id",
            "thread_count": 1,
        }],
    )

    sync_gmail_context.Command._write_gmail_sync_state(
        "arian_boundera",
        result,
    )
    state = sync_gmail_context.Command._gmail_sync_state("arian_boundera")
    path = tmp_path / "arian_boundera-context-state.json"

    assert state["gmail_processed_thread_versions"] == {"a" * 64: "b" * 64}
    assert state["gmail_unmapped_external_participants"][0]["email"] == (
        "buyer@example.invalid"
    )
    if sync_gmail_context.os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_sync_command_suppresses_google_api_request_logs(monkeypatch, caplog):
    private_query = "from:private-buyer@example.invalid"
    monkeypatch.setattr(
        sync_gmail_context,
        "GmailClient",
        lambda operator: SimpleNamespace(operator=operator),
    )

    def resolve_self_emails(_client):
        logging.getLogger("googleapiclient.discovery").debug(private_query)
        logging.getLogger("googleapiclient.http").warning(private_query)
        return {"arian@getboundera.com"}

    monkeypatch.setattr(
        sync_gmail_context,
        "self_emails_for_client",
        resolve_self_emails,
    )
    monkeypatch.setattr(
        sync_gmail_context.Command,
        "_lead_queryset",
        lambda _self, _options: [],
    )
    monkeypatch.setattr(
        sync_gmail_context,
        "sync_gmail_threads",
        lambda **_kwargs: GmailContextSyncResult(),
    )
    monkeypatch.setattr(
        sync_gmail_context,
        "sync_gmail_note_emails",
        lambda **_kwargs: GmailContextSyncResult(),
    )

    with caplog.at_level(logging.DEBUG):
        django_call_command(
            "sync_gmail_context",
            operator="Arian",
            dry_run=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

    assert private_query not in caplog.text


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
