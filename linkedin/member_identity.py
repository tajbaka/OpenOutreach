"""Validation helpers for exact LinkedIn messaging member URNs."""
from __future__ import annotations

import re


_MEMBER_URN_RE = re.compile(r"^urn:li:fsd_profile:[A-Za-z0-9_-]+$")


def normalize_member_urn(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def valid_member_urn(value: object) -> bool:
    return bool(_MEMBER_URN_RE.fullmatch(normalize_member_urn(value)))
