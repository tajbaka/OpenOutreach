"""Canonical operator handles (Chuka / Arian / etc).

Different surfaces refer to the same human by different strings — LinkedIn
display names ("Chukwuka Agu", "Arian Taj"), Gmail addresses
("eddy@tryfedrampgpt.com", "ariantajbakh@gmail.com"), or env-var slot
labels. Anywhere we want to record "which operator did this" — Slack
notifications, WorkflowRun rows, followup sheet operator-routing — needs
a single canonical short handle so cross-table lookups are consistent.

The map is hard-coded for now. If/when a third operator joins, add their
known aliases here. Keep keys lowercased and stripped; the resolver does
a case-insensitive lookup.
"""
from __future__ import annotations


# Lowercase any-form → canonical handle.
_OPERATOR_ALIASES: dict[str, str] = {
    # Chuka
    "chuka": "Chuka",
    "chuka agu": "Chuka",
    "chukwuka": "Chuka",
    "chukwuka agu": "Chuka",
    "chuka eddy jack": "Chuka",
    "chuky eddy jack": "Chuka",
    "chukyjack": "Chuka",
    "chukyjack@gmail.com": "Chuka",
    "eddy": "Chuka",
    "eddy agu": "Chuka",
    "eddy@tryfedrampgpt.com": "Chuka",
    "eddy@boundera.io": "Chuka",

    # Arian
    "arian": "Arian",
    "arian taj": "Arian",
    "arian tajbakhsh": "Arian",
    "ariantajbakh": "Arian",
    "ariantajbakh@gmail.com": "Arian",
    "ariantajbaka@gmail.com": "Arian",
    "ariant2013@gmail.com": "Arian",
    "ariant@tryfedrampgpt.com": "Arian",
    "ariant@boundera.io": "Arian",
    "arian@boundera.io": "Arian",

    # Leili
    "leili": "Leili",
    "leili amirshahi": "Leili",
    "leiliash2011": "Leili",
    "leili.ash2011@yahoo.com": "Leili",

    # Athena
    "athena": "Athena",
    "athenaaghdami": "Athena",
    "athena aghdami": "Athena",
    "athenaaghdami@gmail.com": "Athena",
    "athena@getboundera.com": "Athena",
}

CANONICAL_OPERATOR_HANDLES = frozenset({"Arian", "Athena", "Chuka", "Leili"})


def resolve_operator(value: str | None) -> str:
    """Map any known representation of an operator to their canonical handle.

    Returns the canonical handle ("Chuka", "Arian", ...) when the input
    matches a known alias. Falls through to the trimmed input itself when
    no alias matches — useful for telemetry that wants to capture the
    raw string even for unknown operators, rather than losing it.

    Empty / None → empty string.
    """
    if not value:
        return ""
    key = value.strip().lower()
    if not key:
        return ""
    return _OPERATOR_ALIASES.get(key, value.strip())


def resolve_sales_owner_handle(value: str | None) -> str:
    """Resolve only known active CRM-owner handles.

    ``resolve_operator`` intentionally preserves unknown strings for telemetry.
    A Sheet owner cell must fail closed instead: a typo cannot silently become
    a new owner or route an action to the wrong sender.
    """
    resolved = resolve_operator(value)
    return resolved if resolved in CANONICAL_OPERATOR_HANDLES else ""
