from io import StringIO

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
