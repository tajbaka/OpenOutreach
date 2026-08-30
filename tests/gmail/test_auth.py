from gmail.auth import GMAIL_ACCOUNTS, GMAIL_OPERATOR_MAPPING
from linkedin.granola_sync import _default_internal_emails
from linkedin.management.commands.sync_gmail_context import Command


def test_accounts_require_every_mapped_from_and_reply_to_identity():
    for account_key, account in GMAIL_ACCOUNTS.items():
        expected = {
            identity.lower()
            for mapping in GMAIL_OPERATOR_MAPPING.values()
            if mapping["gmail_account"] == account_key
            for identity in (mapping["send_as"], mapping["reply_to"])
        }

        assert set(account.delivery_aliases) == expected


def test_reply_to_identities_are_internal_and_operator_attributed():
    internal = _default_internal_emails()
    arian = Command._operator_by_self_email("arian_boundera")
    eddy = Command._operator_by_self_email("eddy_boundera")

    assert {
        "ariant@boundera.io",
        "leili@boundera.io",
        "athena@boundera.io",
        "eddy@boundera.io",
    } <= internal
    assert arian["ariant@boundera.io"] == "Arian"
    assert arian["leili@boundera.io"] == "Leili"
    assert eddy["athena@boundera.io"] == "Athena"
    assert eddy["eddy@boundera.io"] == "Chuka"
