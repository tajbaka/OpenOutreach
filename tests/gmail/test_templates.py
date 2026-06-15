from types import SimpleNamespace
import json

import pytest

from gmail.templates import TEMPLATES_PATH
from gmail.templates import render_for_lead, steps_for_lead
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

    assert rendered.subject == "FedRAMP 20x readiness at Analytical Engines"
    assert "Hi Ada" in rendered.body
    assert "Analytical Engines" in rendered.body


def test_icp_emails_uses_direct_icp_step_arrays():
    data = json.loads(TEMPLATES_PATH.read_text())

    assert isinstance(data["Arian"]["CSPs"], list)
    assert "gmail_fallback" not in data["Arian"]["CSPs"]


def test_steps_for_lead_rejects_missing_sender():
    with pytest.raises(SheetsError, match="sender 'Missing' has no block"):
        steps_for_lead(sender="Missing", role="CSP")
