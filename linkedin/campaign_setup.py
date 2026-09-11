"""Explicit user choices shared by campaign creation surfaces, never runtime routing."""
from __future__ import annotations

from django.core.exceptions import ValidationError


EMAIL_START_QUESTION = "Should this campaign's email sequence start before or after LinkedIn acceptance?"


def explicit_gmail_start_mode(value: object) -> str:
    from linkedin.models import Campaign

    if not isinstance(value, str) or value not in Campaign.GmailStartMode.values:
        raise ValidationError(
            "An explicit gmail_start_mode is required: invitation_sent (pre-acceptance) "
            "or post_acceptance. No default is selected for new campaigns."
        )
    return value


def exact_campaign_owner(username: object):
    from linkedin.models import LinkedInProfile
    from linkedin.operators import CANONICAL_OPERATOR_HANDLES, resolve_operator

    if not isinstance(username, str) or not username.strip():
        raise ValidationError("An explicit owner_username is required for a new campaign.")
    profile = LinkedInProfile.objects.select_related("user").filter(
        user__username=username, active=True,
    ).first()
    if profile is None:
        raise ValidationError(f"No active LinkedInProfile exists for exact owner_username {username!r}.")
    profile_operator = resolve_operator(profile.linkedin_username)
    user_operator = resolve_operator(profile.user.username)
    if profile_operator not in CANONICAL_OPERATOR_HANDLES or (
        user_operator in CANONICAL_OPERATOR_HANDLES and user_operator != profile_operator
    ):
        raise ValidationError(f"Campaign owner {username!r} does not have one matching canonical LinkedIn sender.")
    return profile.user


def prompt_gmail_start_mode() -> str:
    from linkedin.models import Campaign

    print(EMAIL_START_QUESTION)
    print("  pre: start after the connection request is confirmed sent; do not wait for acceptance")
    print("  post: wait for LinkedIn acceptance")
    while True:
        answer = input("Choose pre or post (required, no default): ").strip().lower()
        if answer in {"pre", Campaign.GmailStartMode.INVITATION_SENT}:
            return Campaign.GmailStartMode.INVITATION_SENT
        if answer in {"post", Campaign.GmailStartMode.POST_ACCEPTANCE}:
            return Campaign.GmailStartMode.POST_ACCEPTANCE
        print("Please explicitly choose pre or post before creating the campaign.")


def message_program_draft(program_key: str):
    from linkedin.general_icp_json import load_general_message_programs

    drafts = {draft.key: draft for draft in load_general_message_programs()}
    if program_key not in drafts:
        raise ValidationError(
            f"Unknown message_program_key {program_key!r}; available: {sorted(drafts)!r}"
        )
    return drafts[program_key]


def snapshot_campaign_program(draft, *, campaign_name: str):
    from linkedin.general_icp_messages import publish_message_programs
    from linkedin.models import MessageProgramVersion

    (decision,) = publish_message_programs(
        (draft,), published_by=f"campaign-import:{campaign_name}"[:150],
    )
    return MessageProgramVersion.objects.get(
        program__key=draft.key, version=decision.target_version,
    )
