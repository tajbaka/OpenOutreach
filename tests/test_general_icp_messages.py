import copy
from types import SimpleNamespace

import pytest

from linkedin.exceptions import GeneralICPMessageError
from linkedin.general_icp_message_migration import render_role_persona_migration_rows
from linkedin.general_icp_messages import (
    GENERAL_ICP_MESSAGES_HEADERS,
    general_drafts_to_wide_rows,
    parse_general_icp_message_rows,
)
from linkedin.general_icp_messages_sheet import _general_tab_layout_requests
from linkedin.icp_outbound import CSP_ROLE_PERSONA_AUTHORING_BUCKETS


def _row(**overrides):
    values = {
        "ICP": "Founder/CEO | Small | Rev5 Ready → 20x | Direct agency",
        "Connect Message": "Hi {first_name}, is {company_name} evaluating 20x?",
        "Followup Message 1": "Would a quick comparison be useful?",
        "Email Subject 1": "20x question",
        "Email Body 1": "Hi {first_name},\n\nCould we compare notes?",
        "Followup Message 2": "One last question for {first_name}.",
        "Email Subject 2": "One last 20x question",
        "Email Body 2": "Hi {first_name},\n\nIs this owned by your team?",
    }
    values.update(overrides)
    return [values[header] for header in GENERAL_ICP_MESSAGES_HEADERS]


def _rows(*message_rows):
    return [list(GENERAL_ICP_MESSAGES_HEADERS), *message_rows]


def test_wide_row_expands_to_complete_canonical_sequence():
    (draft,) = parse_general_icp_message_rows(_rows(_row()))
    assert draft.key == "fedramp-marketplace-csp"
    assert draft.name == "FedRAMP Marketplace CSP Outreach"
    assert len(draft.messages) == 5
    assert {message.channel for message in draft.messages} == {
        "linkedin_connect", "linkedin_followup", "gmail",
    }
    assert draft.messages[0].icp_label == (
        "Founder/CEO | Small | Rev5 Ready → 20x | Direct agency"
    )
    assert draft.messages[0].audience_key == (
        "csp-small-founder-ceo-rev5-ready-to-20x-direct-agency"
    )
    assert draft.messages[0].role_persona == "CSP Small | Founder/CEO"
    assert draft.messages[0].fedramp_segment == "Rev5 Ready → 20x"
    assert [m.delay_hours for m in draft.messages if m.channel == "linkedin_followup"] == [0, 72]
    assert [m.delay_hours for m in draft.messages if m.channel == "gmail"] == [0.33, 192]
    assert general_drafts_to_wide_rows((draft,)) == _rows(_row())


def test_hash_is_stable_when_icp_rows_are_reordered():
    second = _row(**{
        "ICP": "CFO | Small | Rev5 Ready → 20x | Both",
    })
    first = _row()
    draft_a = parse_general_icp_message_rows(_rows(first, second))[0]
    draft_b = parse_general_icp_message_rows(_rows(second, first))[0]
    assert draft_a.payload() == draft_b.payload()
    assert draft_a.content_hash == draft_b.content_hash


def test_exact_header_schema_is_required():
    rows = _rows(_row())
    rows[0][0] = "Campaign"
    with pytest.raises(GeneralICPMessageError, match="headers must exactly equal"):
        parse_general_icp_message_rows(rows)


def test_icp_label_carries_role_size_stage_and_revenue_intent():
    with pytest.raises(
        GeneralICPMessageError,
        match="Role \\| Company Size \\| FedRAMP Stage \\| Revenue Intent",
    ):
        parse_general_icp_message_rows(_rows(_row(**{"ICP": "Founder/CEO | Small"})))


def test_icp_label_rejects_unknown_revenue_intent():
    with pytest.raises(GeneralICPMessageError, match="Revenue Intent must be one of"):
        parse_general_icp_message_rows(_rows(_row(**{
            "ICP": "Founder/CEO | Small | Rev5 Ready → 20x | High confidence",
        })))


def test_connect_and_email_pairs_are_required():
    with pytest.raises(GeneralICPMessageError, match="Connect Message is required"):
        parse_general_icp_message_rows(_rows(_row(**{"Connect Message": ""})))
    with pytest.raises(GeneralICPMessageError, match="must both be filled"):
        parse_general_icp_message_rows(_rows(_row(**{"Email Subject 1": ""})))


@pytest.mark.parametrize(
    "body,match",
    [
        ("Hi {unknown}", "unsupported placeholder"),
        ("Hi {lead.first_name}", "nested placeholder"),
        ("Hi {first_name!r}", "unsupported formatting"),
        ("Hi {first_name", "invalid braces"),
    ],
)
def test_placeholders_are_strict(body, match):
    with pytest.raises(GeneralICPMessageError, match=match):
        parse_general_icp_message_rows(_rows(_row(**{"Connect Message": body})))


def test_role_placeholder_preserves_manually_authored_audience_identity():
    original = _row()
    revised = _row(**{
        "Followup Message 1": "I work with {role} at companies like {company_name}.",
        "Email Subject 1": "For {role}",
        "Email Body 1": "Hi {first_name}, I work with {role}.",
    })
    before = parse_general_icp_message_rows(_rows(original))[0]
    after = parse_general_icp_message_rows(_rows(revised))[0]
    assert {m.audience_key for m in before.messages} == {m.audience_key for m in after.messages}
    assert general_drafts_to_wide_rows((after,)) == _rows(revised)


def _legacy_rows(connect="Connect"):
    persona = CSP_ROLE_PERSONA_AUTHORING_BUCKETS[0]
    return [
        ["ICP", "Connect Message", "Followup Message 1", "Email Subject 1", "Email Body 1"],
        [persona, connect, "Follow up", "Subject", "Email body"],
    ]


def test_role_persona_migration_collapses_identical_sender_sequences():
    legacy = _legacy_rows()
    rendered = render_role_persona_migration_rows(
        {"Arian": legacy, "Chuka": copy.deepcopy(legacy)},
        program_key="fedramp-marketplace-csp",
        program_name="FedRAMP Marketplace CSP Outreach",
    )
    assert len(rendered) == 2
    assert len(rendered[1]) == 8
    assert rendered[1][0].endswith("| Stage needs review | Unclear")
    assert rendered[1][GENERAL_ICP_MESSAGES_HEADERS.index("Connect Message")] == "Connect"


def test_role_persona_migration_rejects_sender_specific_differences():
    with pytest.raises(GeneralICPMessageError, match="cannot represent sender-specific"):
        render_role_persona_migration_rows(
            {"Arian": _legacy_rows("Arian"), "Chuka": _legacy_rows("Chuka")},
            program_key="fedramp-marketplace-csp",
            program_name="FedRAMP Marketplace CSP Outreach",
        )


def test_general_layout_freezes_icp_and_has_only_eight_visible_columns():
    requests = _general_tab_layout_requests(SimpleNamespace(id=7123, row_count=100), used_rows=82)
    properties = requests[0]["updateSheetProperties"]["properties"]
    assert properties["gridProperties"] == {
        "frozenRowCount": 1,
        "frozenColumnCount": 1,
    }
    column_requests = [
        request["updateDimensionProperties"]
        for request in requests
        if request.get("updateDimensionProperties", {}).get("range", {}).get("dimension")
        == "COLUMNS"
    ]
    assert len(GENERAL_ICP_MESSAGES_HEADERS) == 8
    assert column_requests[-1]["range"] == {
        "sheetId": 7123,
        "dimension": "COLUMNS",
        "startIndex": 0,
        "endIndex": 8,
    }
    assert column_requests[-1]["properties"] == {"hiddenByUser": False}
