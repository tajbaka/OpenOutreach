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
        for icp in icp_outbound.ICP_MESSAGES_SHEET_BUCKETS:
            assert icp in messages, f"{sender} missing {icp}"


def test_load_icp_messages_channels_normalize_to_steps():
    """Each channel must normalize to at least one step with at least one
    string variant. This accepts both legacy [variants] and sequence
    [{delay_hours, variants}] shapes."""
    for sender in ("Arian", "Chuka"):
        messages = icp_outbound.load_icp_messages(sender)
        for icp, channels in messages.items():
            for channel in channels:
                if channel == "media":
                    continue
                steps = icp_outbound.channel_steps(sender=sender, icp=icp, channel=channel)
                assert len(steps) >= 1, f"{sender}.{icp}.{channel} needs >=1 step"
                for step in steps:
                    assert step.delay_hours >= 0
                    assert len(step.variants) >= 1
                    assert all(isinstance(v, str) for v in step.variants)


def test_white_label_connect_notes_are_short_two_variant_experiments():
    white_label_icps = (
        "White Label Product/Executive",
        "White Label Partnerships",
        "White Label Delivery",
        "White Label Champions",
    )
    for sender in ("Arian", "Chuka"):
        messages = icp_outbound.load_icp_messages(sender)
        for icp in white_label_icps:
            variants = messages[icp]["linkedin_connect_note"]
            assert len(variants) == 2, f"{sender}.{icp} must keep two test variants"
            for message in variants:
                assert 21 <= len(message.split()) <= 40
                assert len(message) < 300
                assert message.count("?") == 1
                assert "http" not in message.lower()


def test_white_label_copy_avoids_capitalized_terms_and_internal_strategy_questions():
    forbidden_exact = ("Certification Data", "Security Decision Record")
    forbidden_lower = ("becomes part of the product", "stays a services workflow")
    for sender in ("Arian", "Chuka"):
        linkedin_messages = icp_outbound.load_icp_messages(sender)
        gmail_messages = icp_outbound.load_gmail_messages(sender)
        for icp, channels in linkedin_messages.items():
            if not icp.startswith("White Label"):
                continue
            copy = json.dumps(channels)
            assert not any(phrase in copy for phrase in forbidden_exact)
            assert not any(phrase in copy.lower() for phrase in forbidden_lower)
        for icp, steps in gmail_messages.items():
            if not icp.startswith("White Label"):
                continue
            copy = json.dumps(steps)
            assert not any(phrase in copy for phrase in forbidden_exact)
            assert not any(phrase in copy.lower() for phrase in forbidden_lower)


def test_rev5_ready_copy_is_a_short_20x_program_path_experiment():
    for sender in ("Arian", "Chuka"):
        messages = icp_outbound.load_icp_messages(sender)["Rev5 Ready"]
        assert len(messages["linkedin_connect_note"]) == 2
        for message in messages["linkedin_connect_note"]:
            assert 21 <= len(message.split()) <= 40
            assert len(message) < 300
            assert message.count("?") == 1
            assert "20x program path" in message
            assert "http" not in message.lower()

        copy = json.dumps(messages)
        assert "agency sponsor" in copy
        assert "validation evidence" in copy
        assert not any(mark in copy for mark in ("—", "–", "--"))


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


def test_fill_message_resolves_registered_media_placeholder(tmp_path, monkeypatch):
    root = tmp_path / "root"
    asset_dir = root / "assets" / "follow_up"
    asset_dir.mkdir(parents=True)
    asset = asset_dir / "demo.gif"
    asset.write_bytes(b"GIF89a")
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "media": ["demo.gif"],
                "linkedin_connect_followup": [
                    "Hi {first_name}, quick visual attached.\n\n{demo.gif}"
                ],
            },
        },
    }))
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)
    monkeypatch.setattr(icp_outbound, "ROOT_DIR", root)

    out = icp_outbound.fill_message(
        sender="Arian",
        icp="CSPs",
        channel="linkedin_connect_followup",
        first_name="Jane",
    )

    assert out.body == "Hi Jane, quick visual attached."
    assert out.attachments == [asset]


def test_fill_message_legacy_add_placeholder_still_resolves(tmp_path, monkeypatch):
    root = tmp_path / "root"
    asset_dir = root / "assets" / "follow_up"
    asset_dir.mkdir(parents=True)
    asset = asset_dir / "demo.gif"
    asset.write_bytes(b"GIF89a")
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "linkedin_connect_followup": [
                    "Hi {first_name}, quick visual attached.\n\n{add demo.gif}"
                ],
            },
        },
    }))
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)
    monkeypatch.setattr(icp_outbound, "ROOT_DIR", root)

    out = icp_outbound.fill_message(
        sender="Arian",
        icp="CSPs",
        channel="linkedin_connect_followup",
        first_name="Jane",
    )

    assert out.body == "Hi Jane, quick visual attached."
    assert out.attachments == [asset]


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
                    {"delay_hours": 0, "variants": ["Step 0 for {first_name}"]},
                    {"delay_hours": 120, "variants": ["Step 1 for {company_name}"]},
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
    assert [s.delay_hours for s in steps] == [0, 120]


def test_channel_steps_accepts_decimal_delay_hours(tmp_path, monkeypatch):
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "linkedin_connect_followup": [
                    {"delay_hours": 0.33, "variants": ["Quick note for {first_name}"]},
                ],
            },
        },
    }))
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    steps = icp_outbound.channel_steps(
        sender="Arian", icp="CSPs", channel="linkedin_connect_followup",
    )

    assert steps[0].delay_hours == 0.33


def test_fill_message_rejects_unknown_step_index():
    with pytest.raises(SheetsError, match="has no step 99"):
        icp_outbound.fill_message(
            sender="Arian",
            icp="CSPs",
            channel="linkedin_connect_followup",
            first_name="Jane",
            step_index=99,
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
    gmail_path = tmp_path / "icp_emails.json"
    gmail_path.write_text("{}")
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
            "CMMC Buyers": {
                "linkedin_connect_note": ["cmmc buyer connect"],
                "linkedin_connect_followup": ["cmmc buyer followup"],
            },
            "CMMC Advisor/Channel": {
                "linkedin_connect_note": ["cmmc advisor connect"],
                "linkedin_connect_followup": ["cmmc advisor followup"],
            },
        },
    }))
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)
    monkeypatch.setattr(icp_outbound, "_GMAIL_MESSAGES_PATH", gmail_path)

    rows = icp_outbound.icp_messages_rows("Leili")
    expected_icps = [
        icp for icp in icp_outbound.ICP_MESSAGES_SHEET_BUCKETS
        if icp in icp_outbound.load_icp_messages("Leili")
    ]
    assert [row[0] for row in rows[1:]] == expected_icps
    parsed = icp_outbound.parse_icp_messages_rows(rows)
    assert parsed == {
        icp: icp_outbound.load_icp_messages("Leili")[icp]
        for icp in expected_icps
    }


def test_icp_messages_rows_renders_sequenced_followup(tmp_path, monkeypatch):
    path = tmp_path / "icp_messages.json"
    gmail_path = tmp_path / "icp_emails.json"
    gmail_path.write_text("{}")
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "linkedin_connect_note": ["connect"],
                "linkedin_connect_followup": [
                    {"delay_hours": 0, "variants": ["step zero", "alt zero"]},
                    {"delay_hours": 120, "variants": ["step one"]},
                ],
            },
        },
    }))
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)
    monkeypatch.setattr(icp_outbound, "_GMAIL_MESSAGES_PATH", gmail_path)

    rows = icp_outbound.icp_messages_rows("Arian")
    # Two-step sequence → two follow-up columns, sized dynamically.
    assert rows[0] == icp_outbound.icp_messages_headers(2)
    # First variant of step 0 → "Followup Message 1"; step 1 → "Followup Message 2".
    csp_row = next(r for r in rows[1:] if r[0] == "CSPs")
    assert csp_row == ["CSPs", "connect", "step zero", "", "", "step one", "", ""]


def test_icp_messages_rows_grows_columns_for_long_sequences(tmp_path, monkeypatch):
    path = tmp_path / "icp_messages.json"
    gmail_path = tmp_path / "icp_emails.json"
    gmail_path.write_text("{}")
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "linkedin_connect_note": ["connect"],
                "linkedin_connect_followup": [
                    {"delay_hours": 0, "variants": ["s0"]},
                    {"delay_hours": 120, "variants": ["s1"]},
                    {"delay_hours": 240, "variants": ["s2"]},
                ],
            },
        },
    }))
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)
    monkeypatch.setattr(icp_outbound, "_GMAIL_MESSAGES_PATH", gmail_path)

    rows = icp_outbound.icp_messages_rows("Arian")
    # A three-step sequence grows the tab to three follow-up columns.
    assert rows[0] == icp_outbound.icp_messages_headers(3)
    csp_row = next(r for r in rows[1:] if r[0] == "CSPs")
    assert csp_row == ["CSPs", "connect", "s0", "", "", "s1", "", "", "s2", "", ""]


def test_parse_icp_messages_rows_rebuilds_sequence_from_extra_columns():
    rows = [
        icp_outbound.icp_messages_headers(3),
        [
            "CSPs", "connect",
            "first followup", "", "",
            "second followup", "", "",
            "third followup", "", "",
        ],
    ]
    parsed = icp_outbound.parse_icp_messages_rows(rows)
    assert parsed["CSPs"]["linkedin_connect_followup"] == [
        {"delay_hours": 0, "variants": ["first followup"]},
        {"delay_hours": 96, "variants": ["second followup"]},
        {"delay_hours": 96, "variants": ["third followup"]},
    ]


def test_parse_icp_messages_sheet_rows_rebuilds_gmail_steps():
    rows = [
        icp_outbound.icp_messages_headers(2, email_steps=2),
        [
            "CSPs", "connect",
            "linkedin one", "Subject one", "Body one",
            "linkedin two", "Subject two", "Body two",
        ],
    ]

    linkedin_block, gmail_block = icp_outbound.parse_icp_messages_sheet_rows(rows)

    assert linkedin_block["CSPs"]["linkedin_connect_followup"] == [
        {"delay_hours": 0, "variants": ["linkedin one"]},
        {"delay_hours": 96, "variants": ["linkedin two"]},
    ]
    assert gmail_block["CSPs"] == [
        {"delay_hours": 0.33, "subject_variants": ["Subject one"], "body_variants": ["Body one"]},
        {"delay_hours": 192, "subject_variants": ["Subject two"], "body_variants": ["Body two"]},
    ]


def test_parse_blank_gmail_cells_explicitly_disable_icp():
    rows = [
        icp_outbound.icp_messages_headers(1, email_steps=1),
        ["CSPs", "connect", "linkedin followup", "", ""],
    ]

    _linkedin_block, gmail_block = icp_outbound.parse_icp_messages_sheet_rows(rows)

    assert gmail_block["CSPs"] == []


def test_parse_icp_messages_rows_single_cell_stays_legacy():
    # Blank trailing follow-up columns → legacy single-string shape.
    rows = [
        icp_outbound.icp_messages_headers(2),
        ["CSPs", "connect", "only followup", "", "", "", "", ""],
    ]
    parsed = icp_outbound.parse_icp_messages_rows(rows)
    assert parsed["CSPs"]["linkedin_connect_followup"] == ["only followup"]


def test_save_icp_messages_sequence_edit_preserves_existing_delays(tmp_path, monkeypatch):
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "linkedin_connect_note": ["old connect"],
                "linkedin_connect_followup": [
                    {"delay_hours": 0, "variants": ["old step zero"]},
                    {"delay_hours": 168, "variants": ["old step one"]},
                ],
            },
        },
    }, indent=2) + "\n")
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    # Pull-shaped payload: edited text, parser-default delays (0, 96).
    icp_outbound.save_icp_messages(
        "Arian",
        {"CSPs": {
            "linkedin_connect_note": ["new connect"],
            "linkedin_connect_followup": [
                {"delay_hours": 0, "variants": ["edited step zero"]},
                {"delay_hours": 96, "variants": ["edited step one"]},
            ],
        }},
    )

    saved = json.loads(path.read_text())
    # Text updated from the sheet; the existing 7-day cadence is kept.
    assert saved["Arian"]["CSPs"]["linkedin_connect_followup"] == [
        {"delay_hours": 0, "variants": ["edited step zero"]},
        {"delay_hours": 168, "variants": ["edited step one"]},
    ]


def test_save_gmail_messages_preserves_existing_delays(tmp_path, monkeypatch):
    path = tmp_path / "icp_emails.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": [
                {"delay_hours": 48, "subject_variants": ["old subject"], "body_variants": ["old body"]},
                {"delay_hours": 240, "subject_variants": ["old two"], "body_variants": ["old body two"]},
            ],
        },
    }, indent=2) + "\n")
    monkeypatch.setattr(icp_outbound, "_GMAIL_MESSAGES_PATH", path)

    icp_outbound.save_gmail_messages(
        "Arian",
        {"CSPs": [
            {"delay_hours": 24, "subject_variants": ["new subject"], "body_variants": ["new body"]},
            {"delay_hours": 216, "subject_variants": ["new two"], "body_variants": ["new body two"]},
        ]},
    )

    saved = json.loads(path.read_text())
    assert saved["Arian"]["CSPs"] == [
        {"delay_hours": 48, "subject_variants": ["new subject"], "body_variants": ["new body"]},
        {"delay_hours": 240, "subject_variants": ["new two"], "body_variants": ["new body two"]},
    ]


def test_save_gmail_messages_empty_list_disables_stale_copy(tmp_path, monkeypatch):
    path = tmp_path / "icp_emails.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": [
                {"delay_hours": 48, "subject_variants": ["old subject"], "body_variants": ["old body"]},
            ],
            "Advisors": [
                {"delay_hours": 24, "subject_variants": ["keep"], "body_variants": ["keep body"]},
            ],
        },
    }, indent=2) + "\n")
    monkeypatch.setattr(icp_outbound, "_GMAIL_MESSAGES_PATH", path)

    icp_outbound.save_gmail_messages("Arian", {"CSPs": []})

    saved = json.loads(path.read_text())
    assert saved["Arian"]["CSPs"] == []
    assert saved["Arian"]["Advisors"] == [
        {"delay_hours": 24, "subject_variants": ["keep"], "body_variants": ["keep body"]},
    ]


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
        {"delay_hours": 0, "variants": ["step zero"]},
        {"delay_hours": 120, "variants": ["step one"]},
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


def test_save_icp_messages_preserves_media_registry_on_pull(tmp_path, monkeypatch):
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps({
        "Arian": {
            "CSPs": {
                "media": ["demo.gif"],
                "linkedin_connect_note": ["old connect"],
                "linkedin_connect_followup": ["old followup {demo.gif}"],
            },
        },
    }, indent=2) + "\n")
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    icp_outbound.save_icp_messages(
        "Arian",
        {"CSPs": {
            "linkedin_connect_note": ["new connect"],
            "linkedin_connect_followup": ["new followup {demo.gif}"],
        }},
    )

    saved = json.loads(path.read_text())
    assert saved["Arian"]["CSPs"]["media"] == ["demo.gif"]
    assert saved["Arian"]["CSPs"]["linkedin_connect_followup"] == ["new followup {demo.gif}"]


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
    assert "Appreciate the connection" in out.body
    assert "20x" in out.body
    assert "{" not in out.body


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
