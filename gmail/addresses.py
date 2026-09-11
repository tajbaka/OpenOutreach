"""Syntactic address checks for current automated Gmail scheduling."""
from django.core.exceptions import ValidationError
from django.core.validators import validate_email


def usable_email(value: str | None) -> str:
    """Return a normalized address, never a deliverability assertion."""
    if not isinstance(value, str) or any(char in value for char in "\r\n\x00"):
        return ""
    # The outbound client receives one mailbox, not an RFC recipient list or
    # display-name header. Never validate only a parsed subset of raw input.
    address = value.strip().lower()
    try:
        validate_email(address)
    except ValidationError:
        return ""
    return address
