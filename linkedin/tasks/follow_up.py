# linkedin/tasks/follow_up.py
"""Follow-up task — sends the rigid ICP DM to one CONNECTED lead.

Flow per task:
  1. Honor `ENABLE_FOLLOW_UP` kill-switch + the daily-cap rate limit.
  2. Resolve the lead, scan the LinkedIn thread for any reply from them.
     If they replied — auto-pin stops, mark Completed, return. We never
     re-pitch over an active human thread; the operator picks it up via
     the followup sheet's MET / Replied cohort.
  3. Classify ROLE via `linkedin.icp_outbound.classify_role(lead)`, look
     up the matching ICP template, fill `{first_name}` (the only dynamic
     span), send via `send_raw_message`.
  4. On success: mark Completed, record an ActionLog.
     On failure: re-enqueue in 24h.

Previously the handler had three send paths — a fixed
`POST_ACCEPT_MESSAGE_TEMPLATE` walkthrough (gated by
`POST_ACCEPT_VIDEO_LINK`), an LLM-driven agent fallback
(`linkedin.agents.follow_up.run_follow_up_agent`), and the daemon
honoring whichever was configured. Collapsed to a single path on
2026-05-12: rigid ICP template is the only send mode. The LLM agent
was unpredictable enough that operator feedback consistently flagged
drafts mentioning features that didn't exist, and per-lead
AI-personalization at the daemon's volume isn't worth the cost or the
inconsistency. The bespoke AI-personalized path now lives exclusively
in the followup-sheet workflow (`docs/followup-generation-workflow.md`)
where the operator reviews each draft before sending.
"""
from __future__ import annotations

import logging

from django.db import connections
from django.db.utils import InterfaceError, OperationalError
from termcolor import colored

from linkedin.conf import ENABLE_FOLLOW_UP
from linkedin.db.deals import get_profile_dict_for_public_id, set_profile_state
from linkedin.db.urls import public_id_to_url
from linkedin.icp_outbound import classify_role, fill_for_lead
from linkedin.models import ActionLog

logger = logging.getLogger(__name__)

# DB errors that mean "fresh connection needed" — Neon idle-timeout drops.
_DB_DEAD_ERRORS = (OperationalError, InterfaceError)


def handle_follow_up(task, session, qualifiers):
    if not ENABLE_FOLLOW_UP:
        # Defense in depth: should never fire, since daemon cancels these on
        # startup and enqueue_follow_up is also gated.
        logger.debug("follow_up disabled — skipping task %s", task.pk)
        return

    # Lazy imports so unit tests that don't touch the send path don't pull
    # in playwright / browser action modules.
    from crm.models import Deal
    from linkedin.actions.message import send_raw_message
    from linkedin.db.messages import lead_outbound_operators
    from linkedin.operators import resolve_operator
    from linkedin.tasks.connect import _seconds_until_tomorrow, enqueue_follow_up

    payload = task.payload
    public_id = payload["public_id"]
    campaign_id = payload["campaign_id"]

    logger.info(
        "[%s] %s %s",
        session.campaign, colored("▶ follow_up", "green", attrs=["bold"]), public_id,
    )

    our_operator = resolve_operator(session.linkedin_profile.linkedin_username)

    # Rate limit check — defer to tomorrow if we've hit today's cap.
    if not session.linkedin_profile.can_execute(ActionLog.ActionType.FOLLOW_UP):
        enqueue_follow_up(
            campaign_id, public_id,
            operator=our_operator,
            delay_seconds=_seconds_until_tomorrow(),
        )
        return

    profile_dict = get_profile_dict_for_public_id(session, public_id)
    if profile_dict is None:
        logger.warning("follow_up: no Deal for %s — skipping", public_id)
        return
    profile = profile_dict.get("profile") or profile_dict

    deal = (
        Deal.objects.filter(
            lead__linkedin_url=public_id_to_url(public_id),
            campaign=session.campaign,
        )
        .select_related("lead")
        .first()
    )
    if not deal:
        logger.warning("follow_up: no Deal for %s in campaign %s — skipping",
                       public_id, session.campaign)
        return

    # Owner-scoping guard (second line of defense — claim_next already
    # pre-filters by operator). The lead's outbound LinkedIn DM thread
    # was opened by whichever account sent the connection invite — that's
    # the only account that can DM them now. If a Task for someone else's
    # lead ends up in our queue anyway (legacy Task missing payload.operator,
    # or operator field never stamped), drop the send. Travis incident,
    # 2026-05-12: daemon as Arian almost sent to one of Chuka's connections.
    owning_operators = lead_outbound_operators(deal.lead)
    if owning_operators and our_operator not in owning_operators:
        logger.warning(
            "follow_up: %s belongs to %s, daemon logged in as %s (%s) — skipping send",
            public_id, owning_operators, our_operator,
            session.linkedin_profile.linkedin_username,
        )
        return

    # No-thread guard. A follow-up presumes there's an existing LinkedIn DM
    # thread to nudge — typically seeded by the connect lane's connection-
    # note send. If `crm.Message` has zero outbound on this lead, there's
    # nothing to follow up on: either the connect lane never ran (CSV-only
    # import) or its outbound was never persisted. The daemon would
    # otherwise try `messaging/thread/new/?recipient=urn:...` which fails
    # silently and gets re-enqueued every 24h forever. Mark Completed with
    # a clear reason and move on — the operator can re-seed via
    # `manage.py import_connections` (with a `Message` column in the CSV)
    # if they want to start the thread.
    from crm.models import Message
    has_outbound = Message.objects.filter(
        lead=deal.lead,
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.OUTBOUND,
    ).exists()
    if not has_outbound:
        logger.warning(
            "follow_up: no outbound LinkedIn thread for %s — marking Completed "
            "(use connect lane or import_connections --csv with Message column to seed)",
            public_id,
        )
        set_profile_state(
            session, public_id, "Completed",
            reason="No outbound LinkedIn thread to follow up on",
        )
        return

    # If the lead already sent us anything on this thread, stop — operator
    # picks it up from the followup sheet's REPLIED / MET cohort. Sending
    # the rigid pitch over an active human thread is the worst outcome here.
    #
    # Read from `crm.Message` rather than live-fetching via `get_conversation`
    # (which would navigate to the LinkedIn thread URL first, costing a full
    # page nav before send and leaving a bot-like double-navigation pattern
    # in the trace). `sync_sheets` + sweep_connections + backfill_messages
    # keep crm.Message current; the cached read is the canonical source.
    from crm.models import Message
    has_inbound = Message.objects.filter(
        lead=deal.lead,
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.INBOUND,
    ).exists()
    if has_inbound:
        set_profile_state(
            session, public_id, "Completed",
            reason="Lead replied; automation stopped",
        )
        return

    # Same-operator cross-campaign dedup. A follow-up DM is one-per-person-
    # per-operator, but follow_up Tasks are per-Deal (campaign-scoped) — a
    # Lead with CONNECTED Deals in two campaigns gets one Task per campaign,
    # and the per-campaign state writes never see each other. When the same
    # operator runs both campaigns (Arian's setup, 2026-05-15), both fire and
    # the person gets the same rigid pitch twice from one account.
    #
    # Scoped to *this operator*: a `daemon-send:` from a DIFFERENT operator
    # is a separate account's separate outreach and must not block our send.
    # (The owner-scoping guard above already blocks an operator who never
    # owned the thread; this handles the case where two operators both do.)
    # `daemon-send:` external_ids are written only by the daemon's own
    # follow-up sends (`save_chat_message`). Also closes the latent same-
    # campaign re-send hole when a prior send succeeded but its
    # `set_profile_state` write failed.
    prior_followup_senders = (
        Message.objects.filter(
            lead=deal.lead,
            source=Message.Source.LINKEDIN,
            direction=Message.Direction.OUTBOUND,
            external_id__startswith="daemon-send:",
        )
        .exclude(sender="")
        .values_list("sender", flat=True)
        .distinct()
    )
    if our_operator in {resolve_operator(s) for s in prior_followup_senders}:
        set_profile_state(
            session, public_id, "Completed",
            reason="Follow-up already sent by this operator (deduped across campaigns)",
        )
        return

    # ICP-keyed send. `my_name` is unused for LinkedIn channel (no
    # signature block in those templates), passed for symmetry with the
    # email channel where {my_name} fills the sign-off. Templates can
    # also embed `{add <filename>}` placeholders to attach a media file
    # (looked up in assets/followup/ then ROOT_DIR) — handled in
    # `_send_with_attachments_or_text` below.
    role = classify_role(deal.lead)
    filled = fill_for_lead(
        sender=our_operator, role=role, channel="linkedin_connect_followup",
        lead=deal.lead, my_name=our_operator,
    )
    # If the template included {add <filename>} placeholders, send via
    # the media path (first attachment only — LinkedIn's message form
    # accepts one inline media per send). Multiple attachments would
    # require sequential sends; today's templates use 0 or 1.
    if filled.attachments:
        from linkedin.actions.message import send_media_message
        sent = send_media_message(
            session, profile, filled.body, str(filled.attachments[0]),
        )
    else:
        sent = send_raw_message(session, profile, filled.body)
    if not sent:
        logger.warning("follow_up send failed for %s — re-enqueuing in 24h", public_id)
        enqueue_follow_up(
            campaign_id, public_id,
            operator=our_operator,
            delay_seconds=24 * 3600,
        )
        return

    # Post-send DB writes — critical: if `set_profile_state` doesn't run,
    # the Deal stays at CONNECTED and a future task will re-send the same
    # DM (duplicate). Wrap both writes so a Neon idle-timeout mid-task
    # (DM already sent on LinkedIn) gets a fresh conn + one retry before
    # we let the exception escape to the daemon's task except block.
    try:
        session.linkedin_profile.record_action(ActionLog.ActionType.FOLLOW_UP, session.campaign)
        set_profile_state(
            session, public_id, "Completed",
            reason=f"Sent ICP-{role} follow-up DM",
        )
    except _DB_DEAD_ERRORS as e:
        logger.warning(
            "follow_up post-send DB writes hit dead conn for %s (DM already "
            "sent on LinkedIn) — recycling conn and retrying once: %s",
            public_id, e,
        )
        connections.close_all()
        # Bound model instances refresh on next attribute access; method
        # calls on `session.linkedin_profile` reopen the connection.
        session.linkedin_profile.record_action(ActionLog.ActionType.FOLLOW_UP, session.campaign)
        set_profile_state(
            session, public_id, "Completed",
            reason=f"Sent ICP-{role} follow-up DM",
        )
    logger.info("follow_up sent to %s (role=%s)", public_id, role)
