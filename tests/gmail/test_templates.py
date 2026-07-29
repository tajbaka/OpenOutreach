from types import SimpleNamespace
import json

import pytest

from gmail.templates import TEMPLATES_PATH
from gmail.templates import render_for_icp, render_for_lead, steps_for_lead, validate_all_templates
from linkedin.exceptions import SheetsError


def test_render_for_lead_uses_gmail_templates():
    lead = SimpleNamespace(
        id=7,
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
    )

    rendered = render_for_lead(
        sender="Arian",
        role="CSP",
        sequence_name="gmail_fallback",
        lead=lead,
        step_index=0,
    )

    assert rendered.subject == "FedRAMP 20x evidence question at Analytical Engines"
    assert "Hi Ada" in rendered.body
    assert "Analytical Engines" in rendered.body
    assert "June 24 consolidated rules" in rendered.body


def test_render_for_lead_uses_safe_company_fallback():
    lead = SimpleNamespace(
        id=7,
        first_name="Ada",
        last_name="Lovelace",
        company_name="Unknown Company",
    )

    rendered = render_for_lead(
        sender="Arian",
        role="CSP",
        sequence_name="gmail_fallback",
        lead=lead,
        step_index=0,
    )

    assert "Unknown Company" not in rendered.subject
    assert "Unknown Company" not in rendered.body
    assert "your team" in rendered.subject


def test_render_for_icp_uses_direct_icp_bucket():
    lead = SimpleNamespace(
        id=8,
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
    )

    rendered = render_for_icp(
        sender="Arian",
        icp="Channel",
        sequence_name="gmail_fallback",
        lead=lead,
        step_index=0,
    )

    assert rendered.subject == "FedRAMP 20x vendor readiness"
    assert "vendors around FedRAMP 20x" in rendered.body


def test_render_for_icp_uses_white_label_champion_routing_copy():
    lead = SimpleNamespace(
        id=8,
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
    )

    rendered = render_for_icp(
        sender="Arian",
        icp="White Label Champions",
        sequence_name="gmail_fallback",
        lead=lead,
        step_index=0,
    )

    assert rendered.subject == "Who is closest to 20x at Analytical Engines?"
    assert "validation evidence and keeping certification data current" in rendered.body


def test_render_for_icp_uses_rev5_ready_transition_copy():
    lead = SimpleNamespace(
        id=8,
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
    )

    rendered = render_for_icp(
        sender="Arian",
        icp="Rev5 Ready",
        sequence_name="gmail_fallback",
        lead=lead,
        step_index=0,
    )

    assert rendered.subject == "Carrying ready work into 20x"
    assert "status became legacy" in rendered.body
    assert "what can be reused" in rendered.body


def test_render_for_icp_uses_stage_specific_csp_copy():
    lead = SimpleNamespace(
        id=8,
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
    )

    rendered = render_for_icp(
        sender="Arian",
        icp="20x Initial Implementation",
        sequence_name="gmail_fallback",
        lead=lead,
        step_index=0,
    )

    assert rendered.subject == "20x implementation at Analytical Engines"
    assert "20x initial implementation" in rendered.body
    assert "validation evidence" in rendered.body


def test_render_for_icp_uses_gmail_sender_display_name_override():
    lead = SimpleNamespace(
        id=8,
        first_name="Ada",
        last_name="Lovelace",
        company_name="Analytical Engines",
    )

    rendered = render_for_icp(
        sender="Chuka",
        icp="3PAOs/Assessors",
        sequence_name="gmail_fallback",
        lead=lead,
        step_index=0,
    )

    assert rendered.body.endswith("Best,\nEddy")
    assert "Best,\nChuka" not in rendered.body


def test_icp_emails_uses_direct_icp_step_arrays():
    data = json.loads(TEMPLATES_PATH.read_text())

    assert isinstance(data["Arian"]["CSPs"], list)
    assert "gmail_fallback" not in data["Arian"]["CSPs"]


def test_steps_for_lead_accepts_decimal_delay_hours(tmp_path, monkeypatch):
    path = tmp_path / "icp_emails.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": [
                {
                    "delay_hours": 0.33,
                    "subject_variants": ["Hello {first_name}"],
                    "body_variants": ["Hi {first_name}"],
                },
            ],
        },
    }))
    monkeypatch.setattr("gmail.templates.TEMPLATES_PATH", path)

    parsed_steps = steps_for_lead(sender="Arian", role="CSP")

    assert parsed_steps[0].delay_hours == 0.33


def test_steps_for_lead_rejects_missing_sender():
    with pytest.raises(SheetsError, match="sender 'Missing' has no block"):
        steps_for_lead(sender="Missing", role="CSP")


def test_validate_all_templates_accepts_checked_in_json():
    result = validate_all_templates()

    assert result.enabled_steps > 0
    assert result.disabled_blocks == 0


def test_steps_rejects_unknown_placeholder(tmp_path, monkeypatch):
    path = tmp_path / "icp_emails.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": [
                {
                    "delay_hours": 24,
                    "subject_variants": ["Hello {first_name}"],
                    "body_variants": ["Hi {first_name}, bad token {account_name}"],
                },
            ],
        },
    }))
    monkeypatch.setattr("gmail.templates.TEMPLATES_PATH", path)

    with pytest.raises(SheetsError, match="unknown placeholder"):
        steps_for_lead(sender="Arian", role="CSP")


def test_validate_all_templates_rejects_escaped_leftover_braces(tmp_path, monkeypatch):
    path = tmp_path / "icp_emails.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": [
                {
                    "delay_hours": 24,
                    "subject_variants": ["Hello {{first_name}}"],
                    "body_variants": ["Hi {first_name}"],
                },
            ],
        },
    }))
    monkeypatch.setattr("gmail.templates.TEMPLATES_PATH", path)

    with pytest.raises(SheetsError, match="leftover braces"):
        validate_all_templates()
