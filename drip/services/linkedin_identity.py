"""Fail-closed LinkedIn recipient identity inspection for drip enrollment.

LinkedIn's public profile URL is useful for human review, but the messaging
composer is addressed by an opaque ``fsd_profile`` URN.  A drip lane therefore
freezes both identities only after the Lead row and its stored Voyager profile
agree.  No live provider lookup happens here: planning, reconciliation, and
the send boundary all validate the same persisted evidence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from linkedin.member_identity import normalize_member_urn, valid_member_urn


@dataclass(frozen=True)
class LinkedInRecipientIdentity:
    public_identifier: str
    canonical_url: str
    member_urn: str
    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.errors


def _profile_dict(lead) -> dict:
    if not lead.description:
        return {}
    try:
        profile = json.loads(lead.description)
    except (json.JSONDecodeError, TypeError):
        return {}
    return profile if isinstance(profile, dict) else {}


def _normalized_public_identifier(value: object) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


def inspect_linkedin_recipient(lead) -> LinkedInRecipientIdentity:
    """Return the exact persisted LinkedIn identity or explicit blockers.

    The Lead column, canonical profile URL, and embedded Voyager profile must
    all name the same public identifier.  The embedded member URN must be a
    single valid ``fsd_profile`` URN and must not be stored on another Lead.
    """
    from crm.models import Lead
    from linkedin.db.urls import public_id_to_url, url_to_public_id
    from linkedin.notifications.sheets import linkedin_identity_key

    errors: list[str] = []
    field_public_id = _normalized_public_identifier(lead.public_identifier)
    url_public_id = _normalized_public_identifier(url_to_public_id(lead.linkedin_url or ""))
    canonical_url = linkedin_identity_key(lead.linkedin_url or "")

    if not field_public_id:
        errors.append("missing_linkedin_public_identifier")
    if not url_public_id or not canonical_url.startswith("https://www.linkedin.com/in/"):
        errors.append("missing_or_invalid_linkedin_profile_url")
    if field_public_id and url_public_id and field_public_id != url_public_id:
        errors.append("linkedin_url_public_identifier_mismatch")
    if field_public_id and canonical_url:
        expected_url = linkedin_identity_key(public_id_to_url(field_public_id))
        if canonical_url != expected_url:
            errors.append("linkedin_url_not_canonical_for_public_identifier")

    profile = _profile_dict(lead)
    if not profile:
        errors.append("missing_or_invalid_linkedin_profile_identity")
    profile_public_id = _normalized_public_identifier(profile.get("public_identifier"))
    if not profile_public_id:
        errors.append("missing_linkedin_profile_public_identifier")
    elif field_public_id and profile_public_id != field_public_id:
        errors.append("linkedin_profile_public_identifier_mismatch")

    profile_url = profile.get("url")
    profile_url_identity = (
        linkedin_identity_key(profile_url)
        if isinstance(profile_url, str) and profile_url.strip()
        else ""
    )
    if not profile_url_identity:
        errors.append("missing_linkedin_profile_url")
    elif canonical_url and profile_url_identity != canonical_url:
        errors.append("linkedin_profile_url_mismatch")

    member_urn = normalize_member_urn(profile.get("urn"))
    if not member_urn:
        errors.append("missing_linkedin_member_urn")
    elif not valid_member_urn(member_urn):
        errors.append("invalid_linkedin_member_urn")
    else:
        possible_collisions = Lead.objects.exclude(pk=lead.pk).filter(
            description__contains=member_urn,
        ).only("pk", "description")
        for other in possible_collisions:
            if normalize_member_urn(_profile_dict(other).get("urn")) == member_urn:
                errors.append("linkedin_member_urn_stored_on_another_lead")
                break

    if field_public_id:
        duplicate_public_id = Lead.objects.exclude(pk=lead.pk).filter(
            public_identifier__iexact=field_public_id,
        ).exists()
        if duplicate_public_id:
            errors.append("linkedin_public_identifier_stored_on_another_lead")

    return LinkedInRecipientIdentity(
        public_identifier=field_public_id,
        canonical_url=canonical_url,
        member_urn=member_urn,
        errors=tuple(sorted(set(errors))),
    )


def frozen_linkedin_identity_errors(
    *,
    lead,
    recipient_identity: str,
    member_urn: str,
) -> tuple[str, ...]:
    """Validate a lane's frozen URL and member URN against current Lead data."""
    identity = inspect_linkedin_recipient(lead)
    errors = list(identity.errors)
    if (recipient_identity or "").strip().casefold() != identity.canonical_url:
        errors.append("frozen_linkedin_recipient_identity_drifted")
    if normalize_member_urn(member_urn) != identity.member_urn:
        errors.append("frozen_linkedin_member_urn_drifted")
    return tuple(sorted(set(errors)))
