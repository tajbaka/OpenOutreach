from __future__ import annotations

import re
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from drip.exceptions import LinkAttributionError


REFERENCE_PREFIX = "oo_"
REFERENCE_TOKEN_LENGTH = 22
REFERENCE_LENGTH = len(REFERENCE_PREFIX) + REFERENCE_TOKEN_LENGTH
ALLOWED_DESTINATION_HOSTS = frozenset({"boundera.io", "www.boundera.io"})
MAX_ATTRIBUTED_URL_LENGTH = 2_048
MAX_DESTINATION_URL_LENGTH = MAX_ATTRIBUTED_URL_LENGTH - len("?ref=") - REFERENCE_LENGTH
MAX_REFERENCE_GENERATION_ATTEMPTS = 5

# A 16-byte value encodes to 22 unpadded base64url characters. The final
# character can carry only the remaining two data bits, so accepting any
# base64url character there would permit non-canonical aliases for one token.
_REFERENCE_RE = re.compile(rf"^{REFERENCE_PREFIX}[A-Za-z0-9_-]{{21}}[AQgw]$")
_MALFORMED_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_RESERVED_REFERENCE_IN_TEXT_RE = re.compile(r"ref=(oo_[A-Za-z0-9_-]*)")


def validate_reference(value: str) -> str:
    if not isinstance(value, str) or not _REFERENCE_RE.fullmatch(value):
        raise LinkAttributionError(
            "OpenOutreach references must be a canonical 128-bit oo_ token.",
        )
    return value


def generate_reference() -> str:
    reference = f"{REFERENCE_PREFIX}{secrets.token_urlsafe(16)}"
    return validate_reference(reference)


def canonical_destination_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LinkAttributionError("Tracked-link destination must be a non-empty URL.")
    destination = value.strip()
    if len(destination) > MAX_DESTINATION_URL_LENGTH:
        raise LinkAttributionError("Tracked-link destination is too long.")
    if any(ord(character) < 32 or ord(character) == 127 for character in destination):
        raise LinkAttributionError("Tracked-link destination contains a control character.")
    if "\\" in destination or _MALFORMED_PERCENT_RE.search(destination):
        raise LinkAttributionError("Tracked-link destination contains ambiguous URL syntax.")

    try:
        parsed = urlsplit(destination)
        port = parsed.port
    except ValueError as exc:
        raise LinkAttributionError("Tracked-link destination is malformed.") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https":
        raise LinkAttributionError("Tracked-link destination must use https.")
    if parsed.username is not None or parsed.password is not None:
        raise LinkAttributionError("Tracked-link destination cannot contain credentials.")
    if port is not None:
        raise LinkAttributionError("Tracked-link destination cannot contain an explicit port.")
    if host not in ALLOWED_DESTINATION_HOSTS:
        raise LinkAttributionError("Tracked-link destination must use an allowed Boundera host.")

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key == "ref" for key, _value in query_pairs):
        raise LinkAttributionError("Tracked-link destination already contains ref.")
    canonical_query = urlencode(query_pairs, doseq=True)
    canonical = urlunsplit(("https", host, parsed.path, canonical_query, parsed.fragment))
    if len(canonical) > MAX_DESTINATION_URL_LENGTH:
        raise LinkAttributionError("Tracked-link destination is too long.")
    return canonical


def build_attributed_url(destination_url: str, reference: str) -> str:
    destination = canonical_destination_url(destination_url)
    public_reference = validate_reference(reference)
    parsed = urlsplit(destination)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query_pairs.append(("ref", public_reference))
    attributed = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query_pairs, doseq=True),
            parsed.fragment,
        ),
    )
    if len(attributed) > MAX_ATTRIBUTED_URL_LENGTH:
        raise LinkAttributionError("Attributed tracked-link URL is too long.")
    return attributed


def reserved_references_in_text(value: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise LinkAttributionError("Tracked-link body must be text.")
    return tuple(match.group(1) for match in _RESERVED_REFERENCE_IN_TEXT_RE.finditer(value))
