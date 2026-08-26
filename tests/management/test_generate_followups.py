from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from linkedin import canonical_followup_command


def _queue():
    return {
        "instructions": "draft safely",
        "schema": {},
        "candidate_count": 2,
        "counts_by_owner": {"Arian": 1, "Chuka": 1},
        "unowned_daily_count": 0,
        "candidates": [
            {"action_id": "a", "owner": {"handle": "Arian"}},
            {"action_id": "c", "owner": {"handle": "Chuka"}},
        ],
    }


def test_generate_followups_defaults_to_canonical_queue(monkeypatch):
    monkeypatch.setattr(canonical_followup_command, "_canonical_queue", _queue)
    stdout = StringIO()

    call_command("generate_followups", operator=["Arian"], stdout=stdout)

    output = stdout.getvalue()
    assert '"candidate_count": 1' in output
    assert '"action_id": "a"' in output
    assert '"action_id": "c"' not in output


def test_legacy_only_filters_fail_closed_without_explicit_legacy(monkeypatch):
    monkeypatch.setattr(canonical_followup_command, "_canonical_queue", _queue)

    with pytest.raises(CommandError, match="legacy-only"):
        call_command("generate_followups", no_active=True)


def test_filter_queue_limit_recomputes_owner_counts():
    filtered = canonical_followup_command._filter_queue(
        _queue(),
        operators=(),
        limit=1,
    )

    assert filtered["candidate_count"] == 1
    assert filtered["counts_by_owner"] == {"Arian": 1}


def test_refresh_convenience_uses_context_then_routine_crm_v2(monkeypatch):
    calls = []
    monkeypatch.setattr(canonical_followup_command, "_canonical_queue", _queue)
    monkeypatch.setattr(
        canonical_followup_command,
        "call_command",
        lambda name, **options: calls.append((name, options)),
    )

    call_command("generate_followups", refresh_crm=True, stdout=StringIO())

    assert calls == [
        ("sync_crm_v2_context", {"apply": True}),
        ("refresh_crm_v2", {"apply": True, "routine": True}),
    ]
    assert all(name != "refresh_crm" for name, _options in calls)


def test_sync_sheets_compatibility_flag_publishes_v2_without_context(monkeypatch):
    calls = []
    monkeypatch.setattr(canonical_followup_command, "_canonical_queue", _queue)
    monkeypatch.setattr(
        canonical_followup_command,
        "call_command",
        lambda name, **options: calls.append((name, options)),
    )

    call_command("generate_followups", sync_sheets=True, stdout=StringIO())

    assert calls == [("refresh_crm_v2", {"apply": True, "routine": True})]


def test_applied_drafts_republish_through_routine_crm_v2(monkeypatch):
    from linkedin import crm_followup_decisions

    publications = []
    monkeypatch.setattr(canonical_followup_command, "_canonical_queue", _queue)
    monkeypatch.setattr(
        canonical_followup_command,
        "_publish_crm_v2",
        lambda: publications.append("v2"),
    )
    monkeypatch.setattr(
        crm_followup_decisions,
        "load_crm_followup_decisions",
        lambda _path: [object()],
    )
    monkeypatch.setattr(
        crm_followup_decisions,
        "apply_crm_followup_decisions",
        lambda *_args, **_kwargs: SimpleNamespace(
            drafts_applied=1,
            counts=lambda: {"drafts_applied": 1},
        ),
    )

    call_command(
        "generate_followups",
        apply_json="decisions.json",
        stdout=StringIO(),
    )

    assert publications == ["v2"]
