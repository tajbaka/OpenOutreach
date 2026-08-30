"""Gmail OAuth/token helpers for the browserless Gmail worker."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from linkedin.conf import ROOT_DIR

GMAIL_DATA_DIR = ROOT_DIR / "data" / "gmail"
DEFAULT_CLIENT_SECRET_PATH = GMAIL_DATA_DIR / "oauth-client.json"

SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.readonly",
)


@dataclass(frozen=True)
class GmailAccount:
    key: str
    send_as_aliases: tuple[str, ...]
    reply_to_aliases: tuple[str, ...]

    @property
    def delivery_aliases(self) -> tuple[str, ...]:
        """Every verified mailbox identity required for outbound delivery."""
        return tuple(dict.fromkeys((*self.send_as_aliases, *self.reply_to_aliases)))


GMAIL_ACCOUNTS: dict[str, GmailAccount] = {
    "arian_boundera": GmailAccount(
        key="arian_boundera",
        send_as_aliases=(
            "ariant@getboundera.com",
            "leili@getboundera.com",
        ),
        reply_to_aliases=(
            "ariant@boundera.io",
            "leili@boundera.io",
        ),
    ),
    "eddy_boundera": GmailAccount(
        key="eddy_boundera",
        send_as_aliases=(
            "athena@getboundera.com",
            "eddy@getboundera.com",
        ),
        reply_to_aliases=(
            "athena@boundera.io",
            "eddy@boundera.io",
        ),
    ),
}

GMAIL_OPERATOR_MAPPING: dict[str, dict[str, str]] = {
    "Arian": {
        "gmail_account": "arian_boundera",
        "send_as": "ariant@getboundera.com",
        "reply_to": "ariant@boundera.io",
    },
    "Athena": {
        "gmail_account": "eddy_boundera",
        "send_as": "athena@getboundera.com",
        "reply_to": "athena@boundera.io",
    },
    "Leili": {
        "gmail_account": "arian_boundera",
        "send_as": "leili@getboundera.com",
        "reply_to": "leili@boundera.io",
    },
    "Chuka": {
        "gmail_account": "eddy_boundera",
        "send_as": "eddy@getboundera.com",
        "reply_to": "eddy@boundera.io",
        "display_name": "Eddy",
    },
    "Eddy": {
        "gmail_account": "eddy_boundera",
        "send_as": "eddy@getboundera.com",
        "reply_to": "eddy@boundera.io",
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


def operators_for_account(account_key: str) -> tuple[str, ...]:
    """Return every configured operator routed through one Gmail mailbox."""
    account_for_key(account_key)
    return tuple(
        operator
        for operator, mapping in GMAIL_OPERATOR_MAPPING.items()
        if mapping["gmail_account"] == account_key
    )
