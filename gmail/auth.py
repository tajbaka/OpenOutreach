"""Gmail OAuth/token helpers for the browserless Gmail worker."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from linkedin.conf import ROOT_DIR

GMAIL_DATA_DIR = ROOT_DIR / "data" / "gmail"
DEFAULT_CLIENT_SECRET_PATH = GMAIL_DATA_DIR / "oauth-client.json"

SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.readonly",
)


@dataclass(frozen=True)
class GmailAccount:
    key: str
    send_as_aliases: tuple[str, ...]


GMAIL_ACCOUNTS: dict[str, GmailAccount] = {
    "arian_boundera": GmailAccount(
        key="arian_boundera",
        send_as_aliases=(
            "ariant@boundera.io",
            "leili@boundera.io",
        ),
    ),
    "eddy_boundera": GmailAccount(
        key="eddy_boundera",
        send_as_aliases=(
            "athena@boundera.io",
            "eddy@boundera.io",
        ),
    ),
}

GMAIL_OPERATOR_MAPPING: dict[str, dict[str, str]] = {
    "Arian": {
        "gmail_account": "arian_boundera",
        "send_as": "ariant@boundera.io",
    },
    "Athena": {
        "gmail_account": "eddy_boundera",
        "send_as": "athena@boundera.io",
    },
    "Leili": {
        "gmail_account": "arian_boundera",
        "send_as": "leili@boundera.io",
    },
    "Eddy": {
        "gmail_account": "eddy_boundera",
        "send_as": "eddy@boundera.io",
    },
}


def token_path(account_key: str) -> Path:
    return GMAIL_DATA_DIR / f"{account_key}-token.json"


def account_for_key(account_key: str) -> GmailAccount:
    try:
        return GMAIL_ACCOUNTS[account_key]
    except KeyError as exc:
        known = ", ".join(sorted(GMAIL_ACCOUNTS))
        raise ValueError(f"Unknown Gmail account {account_key!r}; known: {known}") from exc
