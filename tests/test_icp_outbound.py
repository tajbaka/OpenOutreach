"""Rigid ICP-keyed outbound template tests.

`linkedin.icp_outbound` is the no-LLM path for the high-volume
no-reply cohort. Substitution covers `{first_name}` plus the env-driven
`{our_company_name}` / `{our_website_url}` so a rebrand only needs an
.env edit (no JSON / code change).
"""
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


def test_fill_message_substitutes_first_name_and_brand():
    """{first_name}, {our_company_name}, and {our_website_url} all fill.
    The brand fields come from `.env` via `linkedin.conf`, pinned to
    `BrandCo` / `https://brand.co/` by the autouse `_stub_brand` fixture
    so the assertion doesn't drift with operator-side env edits."""
    out = icp_outbound.fill_message(
        icp="CSPs",
        channel="linkedin",
        first_name="Jane",
        variant_index=0,
    )
    assert "Hey Jane," in out or "Hi Jane" in out or "Jane," in out
    assert "BrandCo" in out
    assert "https://brand.co/" in out
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
    # Variant 0 starts with "Hi {first_name}, ..." → "Hi , ..."
    assert "BrandCo" in out  # still well-formed message (brand substituted)
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
        # Each substring must appear in ONLY one ICP's body so the test
        # actually proves ROLE→ICP routing landed in the right bucket.
        # FU_ROLE_TO_ICP: CSP→CSPs, 3PAO/Assessor→3PAOs/Assessors,
        # Advisor→Advisors, Channel→Channel (own bucket since 2026-05-13).
        "CSP":      "billable hours to advisors",           # CSPs-only copy
        "3PAO":     "accessor portal",                      # 3PAOs/Assessors-specific
        "Advisor":  "referral program that gives advisors", # Advisors-specific copy
        "Channel":  "channel partner program with commission",  # Channel-specific
        "Assessor": "accessor portal",                      # rolls into 3PAOs/Assessors
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
