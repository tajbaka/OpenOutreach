"""Small helpers for using profile names in outbound copy."""
from __future__ import annotations

import re


_NICKNAME_RE = re.compile(r"\s+(?:[\"'“‘].*?[\"'”’]|\(.*?\))")
_LEADING_TITLE_RE = re.compile(
    r"^(?:dr|mr|mrs|ms|miss|prof|sir|dame)\.?\s+",
    re.IGNORECASE,
)


def greeting_first_name(value: str | None) -> str:
    """Return a conservative first-name token for message greetings.

    LinkedIn sometimes parses display names like ``Allen "Al" Mayfield`` as
    first_name=``Allen "Al"``. That is useful provenance, but it reads badly
    in outbound copy. Keep the stored value intact and only sanitize at render
    time.
    """
    name = (value or "").strip()
    if not name:
        return ""

    name = _LEADING_TITLE_RE.sub("", name)
    name = _NICKNAME_RE.sub("", name)
    name = name.split(",", 1)[0]
    name = " ".join(name.split())
    if not name:
        return ""

    return name.split()[0].strip(" .,:;\"'“”‘’()[]{}")
