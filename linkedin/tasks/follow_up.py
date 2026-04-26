# linkedin/tasks/follow_up.py
"""Follow-up task — runs the agentic follow-up for one CONNECTED profile."""
from __future__ import annotations

import logging

from termcolor import colored

from linkedin.conf import (
    ENABLE_FOLLOW_UP,
    FOLLOW_UP_MEDIA_PATH,
    POST_ACCEPT_MESSAGE_TEMPLATE,
    POST_ACCEPT_VIDEO_LINK,
)
from linkedin.db.deals import get_profile_dict_for_public_id
from linkedin.models import ActionLog

logger = logging.getLogger(__name__)


def _build_post_accept_message(first_name: str) -> str:
    name = (first_name or "").strip() or "there"
    return POST_ACCEPT_MESSAGE_TEMPLATE.format(first_name=name, video_link=POST_ACCEPT_VIDEO_LINK)


def _send_post_accept_message(session, profile: dict, message: str) -> bool:
    from linkedin.actions.message import send_media_message, send_raw_message

    if FOLLOW_UP_MEDIA_PATH:
        sent = send_media_message(session, profile, message, FOLLOW_UP_MEDIA_PATH)
        if not sent:
            logger.warning("Media send failed for %s, falling back to text-only", profile.get("public_identifier"))
            sent = send_raw_message(session, profile, message)
        return sent

    return send_raw_message(session, profile, message)


def _handle_post_accept_video_flow(session, public_id: str, profile: dict, campaign_id: int) -> dict | None:
    """Run the deterministic accepted-connection flow when a tracked video link is configured.

    Returns None when the custom flow is disabled and the generic agent should run.
    Otherwise returns {"sent_message": bool} after handling the lead.
    """
    if not POST_ACCEPT_VIDEO_LINK:
        return None

    from crm.models import Deal

    from linkedin.actions.conversations import get_conversation
    from linkedin.db.deals import set_profile_state
    from linkedin.db.urls import public_id_to_url
    from linkedin.tasks.connect import enqueue_follow_up

    deal = (
        Deal.objects.filter(
            lead__linkedin_url=public_id_to_url(public_id),
            campaign=session.campaign,
        )
        .select_related("lead")
        .first()
    )
    if not deal:
        return None

    messages = get_conversation(session, public_id) or []

    lead_full_name = f"{deal.lead.first_name or ''} {deal.lead.last_name or ''}".strip().lower()
    lead_replied = any(
        (msg.get("sender", "") or "").strip().lower() == lead_full_name
        for msg in messages
    )

    # Prospect replied — stop automation, let the user handle the thread.
    if lead_replied:
        set_profile_state(
            session,
            public_id,
            "Completed",
            reason="Lead replied; automation stopped",
        )
        return {"sent_message": False}

    message = _build_post_accept_message(deal.lead.first_name)
    sent = _send_post_accept_message(session, profile, message)
    if not sent:
        logger.warning("Post-accept walkthrough send failed for %s — re-enqueuing in 24h", public_id)
        enqueue_follow_up(campaign_id, public_id, delay_seconds=24 * 3600)
        return {"sent_message": False}

    set_profile_state(
        session,
        public_id,
        "Completed",
        reason="Accepted without reply; sent walkthrough",
    )
    return {"sent_message": True}


def handle_follow_up(task, session, qualifiers):
    if not ENABLE_FOLLOW_UP:
        # Defense in depth: should never fire, since daemon cancels these on
        # startup and enqueue_follow_up is also gated.
        logger.debug("follow_up disabled \u2014 skipping task %s", task.pk)
        return

    from linkedin.agents.follow_up import run_follow_up_agent
    from linkedin.tasks.connect import _seconds_until_tomorrow, enqueue_follow_up

    payload = task.payload
    public_id = payload["public_id"]
    campaign_id = payload["campaign_id"]

    logger.info(
        "[%s] %s %s",
        session.campaign, colored("\u25b6 follow_up", "green", attrs=["bold"]), public_id,
    )

    # Rate limit check
    if not session.linkedin_profile.can_execute(ActionLog.ActionType.FOLLOW_UP):
        enqueue_follow_up(campaign_id, public_id, delay_seconds=_seconds_until_tomorrow())
        return

    profile_dict = get_profile_dict_for_public_id(session, public_id)
    if profile_dict is None:
        logger.warning("follow_up: no Deal for %s — skipping", public_id)
        return

    profile = profile_dict.get("profile") or profile_dict

    custom_result = _handle_post_accept_video_flow(session, public_id, profile, campaign_id)
    if custom_result is not None:
        if custom_result["sent_message"]:
            session.linkedin_profile.record_action(
                ActionLog.ActionType.FOLLOW_UP, session.campaign,
            )
        logger.info(
            "post_accept flow for %s: %s",
            public_id,
            "sent walkthrough" if custom_result["sent_message"] else "no message",
        )
        return

    result = run_follow_up_agent(session, public_id, profile, campaign_id)

    # Record action if any message was sent
    sent_messages = [a for a in result["actions"] if a["tool"] == "send_message"]
    if sent_messages:
        session.linkedin_profile.record_action(
            ActionLog.ActionType.FOLLOW_UP, session.campaign,
        )

    # Safety net: if agent didn't schedule or complete, re-enqueue
    terminal_tools = {"mark_completed", "schedule_follow_up"}
    if not any(a["tool"] in terminal_tools for a in result["actions"]):
        logger.warning("follow_up agent for %s did not schedule or complete — re-enqueuing in 72h", public_id)
        enqueue_follow_up(campaign_id, public_id, delay_seconds=72 * 3600)

    # Log summary
    action_names = [a["tool"] for a in result["actions"]]
    logger.info("follow_up agent for %s: %s", public_id, ", ".join(action_names) or "no actions")
