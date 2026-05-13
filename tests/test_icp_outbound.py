"""Rigid ICP-keyed outbound template tests.

`linkedin.icp_outbound` is the no-LLM path for the high-volume
no-reply cohort. Substitution is just `{first_name}`; everything else
in the message body is hardcoded literally in the JSON.
"""
import pytest

from linkedin import icp_outbound
from linkedin.exceptions import SheetsError


def test_load_icp_messages_returns_known_buckets():
    """The buckets the workflow's FU_ROLE_TO_ICP maps to must exist."""
    messages = icp_outbound.load_icp_messages()
    for icp in ("CSPs", "3PAOs/Assessors", "Advisors"):
        assert icp in messages


def test_load_icp_messages_returns_lists_of_variants():
    """Each channel must be a list of variant strings — the random-but-
    stable variant selection in fill_message assumes list shape."""
    messages = icp_outbound.load_icp_messages()
    for icp, channels in messages.items():
        for channel, variants in channels.items():
            assert isinstance(variants, list), f"{icp}.{channel} should be list"
            assert len(variants) >= 1, f"{icp}.{channel} needs ≥1 variant"
            assert all(isinstance(v, str) for v in variants)


def test_fill_message_substitutes_first_name_only():
    """{first_name} fills, everything else (product name, URL, etc.) is
    hardcoded in the JSON. No env-var indirection."""
    out = icp_outbound.fill_message(
        icp="CSPs",
        channel="linkedin",
        first_name="Jane",
        variant_index=0,
    )
    assert "Hey Jane," in out or "Hi Jane" in out or "Jane," in out
    assert "FedrampGPT" in out  # hardcoded in body
    assert "{" not in out  # no leftover placeholders


def test_fill_message_variant_index_picks_explicit_variant():
    """Skipped when the JSON only has one variant per channel — current
    state since we mirror the single Sheets template per ICP. The
    variant-rotation feature still works (try `variant_index=1` against a
    multi-variant entry), but there's nothing to assert against here."""
    variants = icp_outbound.load_icp_messages()["CSPs"]["linkedin"]
    if len(variants) < 2:
        pytest.skip("Only one variant per channel — rotation not testable.")
    a = icp_outbound.fill_message(
        icp="CSPs", channel="linkedin", first_name="X", variant_index=0,
    )
    b = icp_outbound.fill_message(
        icp="CSPs", channel="linkedin", first_name="X", variant_index=1,
    )
    assert a != b  # different variants → different output


def test_fill_message_lead_id_stable_across_calls():
    """Same lead_id → same variant. Operator can re-render the sheet
    and lead 42 always gets the same opener."""
    a = icp_outbound.fill_message(
        icp="CSPs", channel="linkedin", first_name="Jane", lead_id=42,
    )
    b = icp_outbound.fill_message(
        icp="CSPs", channel="linkedin", first_name="Jane", lead_id=42,
    )
    assert a == b


def test_fill_message_lead_id_modulo_wraps():
    """lead_id 5 with 3 variants → variant index 2. Confirms the modulo
    math doesn't crash on a lead_id larger than variant count."""
    variants_count = len(icp_outbound.load_icp_messages()["CSPs"]["linkedin"])
    out_a = icp_outbound.fill_message(
        icp="CSPs", channel="linkedin", first_name="X", lead_id=variants_count,
    )
    out_b = icp_outbound.fill_message(
        icp="CSPs", channel="linkedin", first_name="X", lead_id=0,
    )
    assert out_a == out_b  # lead_id N and 0 both land on variant 0


def test_fill_message_unknown_icp_raises():
    """Unknown ICP must crash loud — silently falling back would mean
    a lead gets a wrong-bucket message in production. Per project
    error-handling rule."""
    with pytest.raises(SheetsError, match="ICP 'NotAnICP'"):
        icp_outbound.fill_message(
            icp="NotAnICP",
            channel="linkedin",
            first_name="Jane",
        )


def test_fill_message_unknown_channel_raises():
    with pytest.raises(SheetsError, match="'sms' channel"):
        icp_outbound.fill_message(
            icp="CSPs",
            channel="sms",
            first_name="Jane",
        )


def test_fill_message_missing_first_name_renders_empty():
    """Empty first_name doesn't crash — renders awkward but recoverable.
    Better than a crash mid-batch that wipes the rest of the run."""
    out = icp_outbound.fill_message(
        icp="CSPs",
        channel="linkedin",
        first_name="",
        variant_index=0,
    )
    # Variant 0 starts with "Hey {first_name}, ..." → "Hey , ..."
    assert "FedrampGPT" in out  # still well-formed message
    assert "{" not in out


def test_fill_for_lead_resolves_role_to_icp():
    """ROLE→ICP routing matches FU_ROLE_TO_ICP. The followup drafter
    holds ROLE on each row, not ICP — this helper closes the gap."""

    class StubLead:
        def __init__(self, lid):
            self.first_name = "Jane"
            self.last_name = "Doe"
            self.company_name = "Acme"
            self.id = lid

    role_to_expected_substring = {
        "CSP":      "FedRamp 20x at Acme",                 # CSPs uses {company_name}
        "3PAO":     "accessor portal",                     # 3PAOs/Assessors-specific
        "Advisor":  "referral program that gives advisors",  # Advisors-specific copy
        "Channel":  "referral program that gives advisors",  # rolls into Advisors
        "Assessor": "accessor portal",                     # rolls into 3PAOs/Assessors
    }
    for role, expected in role_to_expected_substring.items():
        out = icp_outbound.fill_for_lead(
            role=role,
            channel="linkedin",
            lead=StubLead(lid=1),
        )
        assert expected.lower() in out.body.lower(), (
            f"ROLE={role}: missing {expected!r} substring in:\n{out.body}"
        )


def test_fill_for_lead_email_inserts_my_name_signature():
    """Email channel templates end in `Best Regards,\\n\\n{my_name}` —
    confirm the operator handle lands in the signature."""
    class StubLead:
        first_name = "Jane"
        last_name = "Doe"
        company_name = "Acme"
        id = 1

    out = icp_outbound.fill_for_lead(
        role="CSP",  # CSP email template doesn't have signature block actually
        channel="email",
        lead=StubLead(),
        my_name="Arian",
    )
    # CSP email doesn't have signature, just confirm no leftover braces
    assert "{" not in out

    # 3PAO email DOES have signature
    out = icp_outbound.fill_for_lead(
        role="3PAO",
        channel="email",
        lead=StubLead(),
        my_name="Arian",
    )
    assert "Best Regards" in out
    assert "Arian" in out
    assert "{" not in out


def test_fill_for_lead_unknown_role_raises():

    class StubLead:
        first_name = "Jane"
        last_name = "Doe"
        company_name = "Acme"
        id = 1

    with pytest.raises(SheetsError, match="ROLE 'CTO'"):
        icp_outbound.fill_for_lead(
            role="CTO",
            channel="linkedin",
            lead=StubLead(),
        )
