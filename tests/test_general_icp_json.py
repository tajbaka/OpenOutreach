import json

from linkedin.general_icp_json import (
    load_general_message_programs,
    render_general_icp_stores,
    write_general_icp_stores,
)
from linkedin.general_icp_messages import GENERAL_ICP_MESSAGES_HEADERS, parse_general_icp_message_rows


def _drafts():
    values = {
        "ICP": "CRO | Enterprise | Rev5 Authorized → 20x | Direct agency",
        "Connect Message": "Hi {first_name}, open to comparing notes?",
        "Followup Message 1": "I work with {role}. Would a quick discussion help?",
        "Email Subject 1": "Federal revenue question",
        "Email Body 1": "Hi {first_name},\n\nI work with {role}. Would comparing notes help?",
        "Followup Message 2": "Is there someone better to speak with?",
        "Email Subject 2": "Right owner",
        "Email Body 2": "Hi {first_name},\n\nWho owns this at {company_name}?",
    }
    return parse_general_icp_message_rows([
        list(GENERAL_ICP_MESSAGES_HEADERS),
        [values[header] for header in GENERAL_ICP_MESSAGES_HEADERS],
    ])


def test_split_json_stores_round_trip_without_sheet_access(tmp_path):
    linkedin_path = tmp_path / "linkedin.json"
    gmail_path = tmp_path / "gmail.json"
    base = {"schema_version": 2, "shared_programs": {}, "sender_icps": {"Arian": {}}}
    linkedin_path.write_text(json.dumps(base), encoding="utf-8")
    gmail_path.write_text(json.dumps(base), encoding="utf-8")

    linkedin_store, gmail_store = render_general_icp_stores(
        _drafts(),
        linkedin_path=linkedin_path,
        gmail_path=gmail_path,
    )
    assert len(linkedin_store["shared_programs"]["fedramp-marketplace-csp"]["messages"]) == 3
    assert len(gmail_store["shared_programs"]["fedramp-marketplace-csp"]["messages"]) == 2
    assert linkedin_store["sender_icps"] == {"Arian": {}}

    write_general_icp_stores(
        linkedin_store,
        gmail_store,
        linkedin_path=linkedin_path,
        gmail_path=gmail_path,
    )
    loaded = load_general_message_programs(
        linkedin_path=linkedin_path,
        gmail_path=gmail_path,
    )
    assert loaded[0].payload() == _drafts()[0].payload()
