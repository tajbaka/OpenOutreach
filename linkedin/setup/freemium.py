# linkedin/setup/freemium.py
"""Freemium campaign creation from kit config."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def import_freemium_campaign(kit_config: dict):
    """Create or update a freemium Campaign from kit config.

    New kits must carry an explicit sender and email start choice.
    Returns the Campaign instance or None.
    """
    from django.core.exceptions import ValidationError
    from django.db import transaction
    from linkedin.campaign_setup import (
        exact_campaign_owner, explicit_gmail_start_mode,
        message_program_draft, snapshot_campaign_program,
    )
    from linkedin.models import Campaign

    campaign_name = kit_config.get("campaign_name", "Freemium Outreach")
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().filter(name=campaign_name).first()
        created = campaign is None
        if created:
            campaign = Campaign(
                name=campaign_name,
                gmail_start_mode=explicit_gmail_start_mode(kit_config.get("gmail_start_mode")),
                user=exact_campaign_owner(kit_config.get("owner_username")),
            )
        else:
            if "gmail_start_mode" in kit_config:
                campaign.gmail_start_mode = explicit_gmail_start_mode(kit_config["gmail_start_mode"])
            if "owner_username" in kit_config:
                campaign.user = exact_campaign_owner(kit_config["owner_username"])
            campaign.clean()
        program_key = kit_config.get("message_program_key")
        if created and campaign.gmail_start_mode == Campaign.GmailStartMode.INVITATION_SENT and not program_key:
            raise ValidationError("Pre-acceptance email requires an explicit message_program_key.")
        if program_key:
            campaign.active_message_version = snapshot_campaign_program(
                message_program_draft(program_key), campaign_name=campaign_name,
            )
        for field, value in {
            "product_docs": kit_config["product_docs"],
            "campaign_objective": kit_config["campaign_objective"],
            "booking_link": kit_config["booking_link"],
            "is_freemium": True,
            "action_fraction": kit_config["action_fraction"],
        }.items():
            setattr(campaign, field, value)
        logger.info("[Freemium] Campaign preview: %s; sender=%s; gmail_start_mode=%s", campaign_name, campaign.user.username, campaign.gmail_start_mode)
        campaign.full_clean()
        campaign.save()

    logger.info("[Freemium] Campaign imported: %s (action_fraction=%.2f)",
               campaign_name, kit_config["action_fraction"])
    return campaign


def seed_profiles(session, kit_config: dict):
    """Seed Lead (with embedding) + QUALIFIED Deal for profiles listed in kit config."""
    from crm.models import Lead

    from linkedin.db.deals import create_freemium_deal
    from linkedin.db.enrichment import ensure_profile_embedded
    from linkedin.db.urls import public_id_to_url

    public_ids = kit_config.get("seed_profiles", [])
    if not public_ids:
        return

    for public_id in public_ids:
        url = public_id_to_url(public_id)

        lead, _ = Lead.objects.get_or_create(linkedin_url=url, defaults={"public_identifier": public_id})

        ensure_profile_embedded(lead.pk, public_id, session, quiet=True)
        create_freemium_deal(session, public_id)
