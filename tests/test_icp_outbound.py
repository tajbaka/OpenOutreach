"""Rigid ICP-keyed outbound template tests.

`linkedin.icp_outbound` is the no-LLM path for the high-volume
no-reply cohort. Substitution covers `{first_name}` plus the env-driven
`{our_company_name}` / `{our_website_url}` so a rebrand only needs an
.env edit (no JSON / code change).
"""
import json

import pytest

from linkedin import icp_outbound
from linkedin.exceptions import SheetsError


@pytest.fixture(autouse=True)
def _stub_brand(monkeypatch):
    """Pin {our_company_name} and {our_website_url} to known values so
    assertions don't depend on whatever .env currently holds. `fill_message`
    does a fresh `from linkedin.conf import OUR_COMPANY_NAME` inside its
    body each call, so patching `linkedin.conf.*` is what reaches the
    substitution."""
    monkeypatch.setattr("linkedin.conf.OUR_COMPANY_NAME", "BrandCo")
    monkeypatch.setattr("linkedin.conf.OUR_WEBSITE_URL", "https://brand.co/")


def test_load_icp_messages_returns_known_buckets():
    """The buckets the workflow's FU_ROLE_TO_ICP maps to must exist
    under each sender's block."""
    for sender in ("Arian", "Chuka"):
        messages = icp_outbound.load_icp_messages(sender)
        for icp in ("CSPs", "3PAOs/Assessors", "Advisors"):
            assert icp in messages, f"{sender} missing {icp}"


def test_load_icp_messages_channels_normalize_to_steps():
    """Each channel must normalize to at least one step with at least one
    string variant. This accepts both legacy [variants] and sequence
    [{delay_days, variants}] shapes."""
    for sender in ("Arian", "Chuka"):
        messages = icp_outbound.load_icp_messages(sender)
        for icp, channels in messages.items():
            for channel in channels:
                steps = icp_outbound.channel_steps(sender=sender, icp=icp, channel=channel)
                assert len(steps) >= 1, f"{sender}.{icp}.{channel} needs >=1 step"
                for step in steps:
                    assert step.delay_days >= 0
                    assert len(step.variants) >= 1
                    assert all(isinstance(v, str) for v in step.variants)


def test_load_icp_messages_unknown_sender_raises():
    """An operator handle absent from the JSON must crash loud — no
    shared default, per the project's no-silent-fallback rule. Sending
    under another operator's copy is worse than crashing the run."""
    with pytest.raises(SheetsError, match="sender 'NotAnOperator'"):
        icp_outbound.load_icp_messages("NotAnOperator")


def test_fill_message_substitutes_first_name_and_brand(tmp_path, monkeypatch):
    """{first_name}, {our_company_name}, and {our_website_url} all fill.
    The brand fields come from `.env` via `linkedin.conf`, pinned to
    `BrandCo` / `https://brand.co/` by the autouse `_stub_brand` fixture
    so the assertion doesn't drift with operator-side env edits."""
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "linkedin_connect_followup": [
                    "Hi {first_name}, {our_company_name} lives at {our_website_url}"
                ],
            },
        },
    }))
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    out = icp_outbound.fill_message(
        sender="Arian",
        icp="CSPs",
        channel="linkedin_connect_followup",
        first_name="Jane",
        variant_index=0,
    )
    assert "Hi Jane," in out
    assert "BrandCo" in out
    assert "https://brand.co/" in out
    assert "{" not in out  # no leftover placeholders


def test_fill_message_replaces_unknown_company_sentinel(tmp_path, monkeypatch):
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "linkedin_connect_followup": [
                    "Hi {first_name}, noticed {company_name} in the FedRAMP space"
                ],
            },
        },
    }))
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    out = icp_outbound.fill_message(
        sender="Arian",
        icp="CSPs",
        channel="linkedin_connect_followup",
        first_name="Jamil",
        company_name="Unknown Company",
    )

    assert "Unknown Company" not in out
    assert "noticed your team in the FedRAMP space" in out


def test_fill_message_variant_index_picks_explicit_variant(tmp_path, monkeypatch):
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "linkedin_connect_followup": [
                    "variant zero for {first_name}",
                    "variant one for {first_name}",
                ],
            },
        },
    }))
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    a = icp_outbound.fill_message(
        sender="Arian", icp="CSPs", channel="linkedin_connect_followup", first_name="X", variant_index=0,
    )
    b = icp_outbound.fill_message(
        sender="Arian", icp="CSPs", channel="linkedin_connect_followup", first_name="X", variant_index=1,
    )
    assert a != b  # different variants → different output


def test_fill_message_supports_step_object_templates(tmp_path, monkeypatch):
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "linkedin_connect_followup": [
                    {"delay_days": 0, "variants": ["Step 0 for {first_name}"]},
                    {"delay_days": 4, "variants": ["Step 1 for {company_name}"]},
                ],
            },
        },
    }))
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    first = icp_outbound.fill_message(
        sender="Arian",
        icp="CSPs",
        channel="linkedin_connect_followup",
        first_name="Jane",
        company_name="Acme",
        step_index=0,
    )
    second = icp_outbound.fill_message(
        sender="Arian",
        icp="CSPs",
        channel="linkedin_connect_followup",
        first_name="Jane",
        company_name="Acme",
        step_index=1,
    )

    assert first.body == "Step 0 for Jane"
    assert second.body == "Step 1 for Acme"
    steps = icp_outbound.channel_steps(
        sender="Arian", icp="CSPs", channel="linkedin_connect_followup",
    )
    assert [s.delay_days for s in steps] == [0, 4]


def test_fill_message_rejects_unknown_step_index():
    with pytest.raises(SheetsError, match="has no step 1"):
        icp_outbound.fill_message(
            sender="Arian",
            icp="CSPs",
            channel="linkedin_connect_followup",
            first_name="Jane",
            step_index=1,
        )


def test_fill_message_lead_id_stable_across_calls():
    """Same lead_id → same variant. Operator can re-render the sheet
    and lead 42 always gets the same opener."""
    a = icp_outbound.fill_message(
        sender="Arian", icp="CSPs", channel="linkedin_connect_followup", first_name="Jane", lead_id=42,
    )
    b = icp_outbound.fill_message(
        sender="Arian", icp="CSPs", channel="linkedin_connect_followup", first_name="Jane", lead_id=42,
    )
    assert a == b


def test_fill_message_lead_id_modulo_wraps(tmp_path, monkeypatch):
    """lead_id 5 with 3 variants → variant index 2. Confirms the modulo
    math doesn't crash on a lead_id larger than variant count."""
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "linkedin_connect_followup": [
                    "variant zero for {first_name}",
                    "variant one for {first_name}",
                    "variant two for {first_name}",
                ],
            },
        },
    }))
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    variants_count = 3
    out_a = icp_outbound.fill_message(
        sender="Arian", icp="CSPs", channel="linkedin_connect_followup", first_name="X", lead_id=variants_count,
    )
    out_b = icp_outbound.fill_message(
        sender="Arian", icp="CSPs", channel="linkedin_connect_followup", first_name="X", lead_id=0,
    )
    assert out_a == out_b  # lead_id N and 0 both land on variant 0


def test_fill_message_unknown_icp_raises():
    """Unknown ICP must crash loud — silently falling back would mean
    a lead gets a wrong-bucket message in production. Per project
    error-handling rule."""
    with pytest.raises(SheetsError, match="ICP 'NotAnICP'"):
        icp_outbound.fill_message(
            sender="Arian",
            icp="NotAnICP",
            channel="linkedin_connect_followup",
            first_name="Jane",
        )


def test_fill_message_unknown_channel_raises():
    with pytest.raises(SheetsError, match="'sms' channel"):
        icp_outbound.fill_message(
            sender="Arian",
            icp="CSPs",
            channel="sms",
            first_name="Jane",
        )


def test_fill_message_unknown_sender_raises():
    """An unknown sender propagates the SheetsError out of fill_message."""
    with pytest.raises(SheetsError, match="sender 'Nobody'"):
        icp_outbound.fill_message(
            sender="Nobody",
            icp="CSPs",
            channel="linkedin_connect_followup",
            first_name="Jane",
        )


def test_known_senders_lists_operator_blocks():
    """known_senders() exposes the JSON's top-level operator keys — the
    daemon's startup check verifies an account against this set."""
    senders = icp_outbound.known_senders()
    assert "Arian" in senders
    assert "Chuka" in senders


def test_missing_sender_block_none_for_known_operator():
    """A LinkedIn username whose resolved handle has a block → covered,
    so missing_sender_block returns None (account safe to run outbound)."""
    # `ariant@tryfedrampgpt.com` resolves (via operators.py) to "Arian".
    assert icp_outbound.missing_sender_block("ariant@tryfedrampgpt.com") is None


def test_missing_sender_block_returns_handle_for_unknown():
    """An un-onboarded account → resolve_operator falls through to the raw
    string, which has no block → returned as the handle to add."""
    assert (
        icp_outbound.missing_sender_block("nobody@example.com")
        == "nobody@example.com"
    )


def test_icp_messages_rows_round_trip_for_sender(tmp_path, monkeypatch):
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps({
        "Leili": {
            "CSPs": {
                "linkedin_connect_note": ["csp connect"],
                "linkedin_connect_followup": ["csp followup"],
            },
            "3PAOs/Assessors": {
                "linkedin_connect_note": ["assessor connect"],
                "linkedin_connect_followup": ["assessor followup"],
            },
            "Advisors": {
                "linkedin_connect_note": ["advisor connect"],
                "linkedin_connect_followup": ["advisor followup"],
            },
            "Channel": {
                "linkedin_connect_note": ["channel preserved"],
                "linkedin_connect_followup": ["channel preserved"],
            },
        },
    }))
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    rows = icp_outbound.icp_messages_rows("Leili")
    parsed = icp_outbound.parse_icp_messages_rows(rows)
    assert parsed == {
        icp: icp_outbound.load_icp_messages("Leili")[icp]
        for icp in icp_outbound.ICP_MESSAGES_SHEET_BUCKETS
    }


def test_icp_messages_rows_rejects_sequenced_followup(tmp_path, monkeypatch):
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "linkedin_connect_note": ["connect"],
                "linkedin_connect_followup": [
                    {"delay_days": 0, "variants": ["step zero"]},
                    {"delay_days": 4, "variants": ["step one"]},
                ],
            },
        },
    }))
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    with pytest.raises(SheetsError, match="cannot push sequenced follow-up templates"):
        icp_outbound.icp_messages_rows("Arian")


def test_parse_icp_messages_rows_rejects_duplicate_icp():
    rows = [
        list(icp_outbound.ICP_MESSAGES_HEADERS),
        ["CSPs", "first connect", "first followup"],
        ["CSPs", "dupe connect", "dupe followup"],
    ]
    with pytest.raises(SheetsError, match="duplicates ICP 'CSPs'"):
        icp_outbound.parse_icp_messages_rows(rows)


def test_save_icp_messages_replaces_one_sender_block(tmp_path, monkeypatch):
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps({
        "Arian": {"CSPs": {"linkedin_connect_note": ["a"]}},
        "Leili": {
            "CSPs": {"linkedin_connect_note": ["old"], "linkedin_connect_followup": ["oldf"]},
            "Channel": {"linkedin_connect_note": ["keep"], "linkedin_connect_followup": ["keepf"]},
        },
    }, indent=2) + "\n")
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    icp_outbound.save_icp_messages(
        "Leili",
        {"CSPs": {"linkedin_connect_note": ["new"], "linkedin_connect_followup": ["newf"]}},
    )

    saved = json.loads(path.read_text())
    assert saved["Arian"]["CSPs"]["linkedin_connect_note"] == ["a"]
    assert saved["Leili"]["CSPs"]["linkedin_connect_note"] == ["new"]
    assert saved["Leili"]["Channel"]["linkedin_connect_note"] == ["keep"]


def test_save_icp_messages_preserves_sequenced_followup_on_pull(tmp_path, monkeypatch):
    path = tmp_path / "icp_messages.json"
    sequence = [
        {"delay_days": 0, "variants": ["step zero"]},
        {"delay_days": 4, "variants": ["step one"]},
    ]
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "linkedin_connect_note": ["old connect"],
                "linkedin_connect_followup": sequence,
            },
        },
    }, indent=2) + "\n")
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    icp_outbound.save_icp_messages(
        "Arian",
        {"CSPs": {
            "linkedin_connect_note": ["new connect"],
            "linkedin_connect_followup": ["flattened from sheet"],
        }},
    )

    saved = json.loads(path.read_text())
    assert saved["Arian"]["CSPs"]["linkedin_connect_note"] == ["new connect"]
    assert saved["Arian"]["CSPs"]["linkedin_connect_followup"] == sequence


def test_fill_message_missing_first_name_renders_empty():
    """Empty first_name doesn't crash — renders awkward but recoverable.
    Better than a crash mid-batch that wipes the rest of the run."""
    out = icp_outbound.fill_message(
        sender="Arian",
        icp="CSPs",
        channel="linkedin_connect_followup",
        first_name="",
        variant_index=0,
    )
    # Variant 0 starts with "Hi {first_name}, ..." → "Hi , ..."
    assert "BrandCo" in out  # still well-formed message (brand substituted)
    assert "{" not in out


def test_fill_message_sanitizes_greeting_first_name():
    out = icp_outbound.fill_message(
        sender="Arian",
        icp="CSPs",
        channel="linkedin_connect_note",
        first_name='Allen "Al"',
        company_name="Global Defense, Inc.",
        variant_index=0,
    )
    assert out.body.startswith("Hi Allen,")
    assert 'Allen "Al"' not in out.body


def test_fill_for_lead_resolves_role_to_icp(tmp_path, monkeypatch):
    """ROLE→ICP routing matches FU_ROLE_TO_ICP. The followup drafter
    holds ROLE on each row, not ICP — this helper closes the gap."""
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "linkedin_connect_followup": ["csp-only phrase for {first_name}"],
            },
            "3PAOs/Assessors": {
                "linkedin_connect_followup": ["assessor-only phrase for {first_name}"],
            },
            "Advisors": {
                "linkedin_connect_followup": ["advisor-only phrase for {first_name}"],
            },
            "Channel": {
                "linkedin_connect_followup": ["channel-only phrase for {first_name}"],
            },
        },
    }))
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    class StubLead:
        def __init__(self, lid):
            self.first_name = "Jane"
            self.last_name = "Doe"
            self.company_name = "Acme"
            self.id = lid

    role_to_expected_substring = {
        "CSP": "csp-only phrase",
        "3PAO": "assessor-only phrase",
        "Assessor": "assessor-only phrase",
        "Advisor": "advisor-only phrase",
        "Channel": "channel-only phrase",
    }
    for role, expected in role_to_expected_substring.items():
        out = icp_outbound.fill_for_lead(
            sender="Arian",
            role=role,
            channel="linkedin_connect_followup",
            lead=StubLead(lid=1),
        )
        assert expected in out.body, (
            f"ROLE={role}: missing {expected!r} substring in:\n{out.body}"
        )


def test_fill_for_lead_unknown_role_raises():

    class StubLead:
        first_name = "Jane"
        last_name = "Doe"
        company_name = "Acme"
        id = 1

    with pytest.raises(SheetsError, match="ROLE 'CTO'"):
        icp_outbound.fill_for_lead(
            sender="Arian",
            role="CTO",
            channel="linkedin_connect_followup",
            lead=StubLead(),
        )
