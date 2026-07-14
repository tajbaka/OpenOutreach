# linkedin/tasks/follow_up.py
"""Follow-up task — sends the rigid ICP DM to one CONNECTED lead.

Flow per task:
  1. Honor `ENABLE_FOLLOW_UP` kill-switch + the daily-cap rate limit.
  2. Resolve the lead, scan the LinkedIn thread for any reply from them.
     If they replied — auto-pin stops, mark Completed, return. We never
     re-pitch over an active human thread; the operator picks it up via
     the followup sheet's MET / Replied cohort.
  3. Resolve the lead's persisted ICP bucket via
     `linkedin.icp_outbound.resolve_icp(lead)`, look up the matching ICP
     template, fill `{first_name}` (the only dynamic
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
from datetime import timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from django.db import connections
from django.db.utils import InterfaceError, OperationalError
from django.utils import timezone
from termcolor import colored

from linkedin.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    ACTIVE_TIMEZONE,
    ENABLE_ACTIVE_HOURS,
    ENABLE_FOLLOW_UP,
    REST_DAYS,
)
from linkedin.db.deals import get_profile_dict_for_public_id, set_profile_state
from linkedin.db.urls import public_id_to_url
from linkedin.icp_outbound import channel_steps, fill_message, resolve_icp
from linkedin.models import ActionLog

logger = logging.getLogger(__name__)

# DB errors that mean "fresh connection needed" — Neon idle-timeout drops.
_DB_DEAD_ERRORS = (OperationalError, InterfaceError)
DEFAULT_SEQUENCE_NAME = "linkedin_connect_followup"
DEFAULT_CHANNEL = "linkedin_connect_followup"


def _step_external_id_prefix(*, operator: str, deal_id: int, sequence_name: str, step_index: int) -> str:
    return f"daemon-send:{operator}:{deal_id}:{sequence_name}:step-{step_index}:"


def _has_sent_sequence_step(*, deal, operator: str, sequence_name: str, step_index: int) -> bool:
    from crm.models import Message

    return Message.objects.filter(
        lead=deal.lead,
        source=Message.Source.LINKEDIN,
        direction=Message.Direction.OUTBOUND,
        external_id__startswith=_step_external_id_prefix(
            operator=operator,
            deal_id=deal.pk,
            sequence_name=sequence_name,
            step_index=step_index,
        ),
    ).exists()


def _sequence_stop_reason(deal) -> str:
    from linkedin.tasks.stop_checks import automation_stop_reason

    return automation_stop_reason(deal)


def _delay_seconds_to_active_due(
    delay_hours: float,
    *,
    reference_time=None,
) -> float:
    """Return a delay whose target lands inside the active-hours window.

    `delay_hours` is an offset from `reference_time`, normally
    `Deal.connected_at`. Falling back to now keeps legacy rows with no
    connection timestamp schedulable without special cases.
    """
    anchor = reference_time or timezone.now()
    parsed_delay_hours = float(delay_hours)
    if not isfinite(parsed_delay_hours):
        raise ValueError(f"delay_hours must be finite, got {delay_hours!r}")
    target = anchor + timedelta(hours=max(parsed_delay_hours, 0.0))
    raw_delay = max((target - timezone.now()).total_seconds(), 0.0)
    if not ENABLE_ACTIVE_HOURS:
        return float(raw_delay)

    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.now()
    due = timezone.localtime(now + timedelta(seconds=raw_delay), timezone=tz)
    if due.weekday() not in REST_DAYS and ACTIVE_START_HOUR <= due.hour < ACTIVE_END_HOUR:
        return float(raw_delay)

    if due.weekday() in REST_DAYS or due.hour >= ACTIVE_END_HOUR:
        candidate = due + timedelta(days=1)
    else:
        candidate = due
    candidate = candidate.replace(
        hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0,
    )
    while candidate.weekday() in REST_DAYS:
        candidate += timedelta(days=1)
    return max((candidate - timezone.localtime(now, timezone=tz)).total_seconds(), 0.0)


def handle_follow_up(task, session, qualifiers):
    if not ENABLE_FOLLOW_UP:
        # Defense in depth: should never fire, since daemon cancels these on
        # startup and enqueue_follow_up is also gated.
        logger.debug("follow_up disabled — skipping task %s", task.pk)
        return

    # Lazy imports so unit tests that don't touch the send path don't pull
    # in playwright / browser action modules.
    from crm.models import ClosingReason, Deal
    from linkedin.actions.message import send_raw_message
    from linkedin.db.messages import lead_outbound_operators
    from linkedin.operators import resolve_operator
    from linkedin.tasks.connect import _seconds_until_tomorrow, enqueue_follow_up

    payload = task.payload
    public_id = payload["public_id"]
    campaign_id = payload["campaign_id"]
    sequence_name = payload.get("sequence_name") or DEFAULT_SEQUENCE_NAME
    channel = payload.get("channel") or sequence_name or DEFAULT_CHANNEL
    step_index = int(payload.get("step_index") or 0)
    queued_icp = (payload.get("icp") or "").strip()

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
            icp=queued_icp or None,
            delay_seconds=_seconds_until_tomorrow(),
            sequence_name=sequence_name,
            channel=channel,
            step_index=step_index,
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

    from linkedin.suppression import lead_suppression_match

    suppression = lead_suppression_match(deal.lead)
    if suppression:
        reason = f"Suppression: {suppression.value}"
        logger.warning("follow_up: %s blocked by %s - skipping send", public_id, reason)
        deal.lead.disqualified = True
        deal.lead.save(update_fields=["disqualified"])
        set_profile_state(session, public_id, "Failed", reason=reason)
        deal.closing_reason = ClosingReason.DISQUALIFIED
        deal.reason = reason
        deal.save(update_fields=["closing_reason", "reason"])
        return

    from linkedin.enums import ProfileState
    if deal.state != ProfileState.CONNECTED:
        logger.info(
            "follow_up: %s state is %s, not CONNECTED — skipping send",
            public_id, deal.state,
        )
        return

    stop_reason = _sequence_stop_reason(deal)
    if stop_reason:
        set_profile_state(
            session,
            public_id,
            "Completed" if not deal.lead.disqualified else "Failed",
            reason=stop_reason,
        )
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
        set_profile_state(
            session, public_id, "Connected",
            reason=(
                "Follow-up skipped: LinkedIn thread belongs to "
                f"{', '.join(sorted(owning_operators))}"
            ),
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
    step_already_sent = _has_sent_sequence_step(
        deal=deal,
        operator=our_operator,
        sequence_name=sequence_name,
        step_index=step_index,
    )

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
    if step_index == 0 and our_operator in {resolve_operator(s) for s in prior_followup_senders}:
        set_profile_state(
            session, public_id, "Completed",
            reason="Follow-up already sent by this operator (deduped across campaigns)",
        )
        return

    # Resolve the sequence after the legacy same-operator guard. That guard
    # can complete old rows without needing a sender template block; step-
    # aware dedup needs the sequence length to know whether this step is
    # final or whether the next step must stay queued.
    resolved_icp = resolve_icp(deal.lead)
    icp = queued_icp or resolved_icp
    if queued_icp and resolved_icp and queued_icp != resolved_icp:
        logger.info(
            "follow_up using queued ICP %s for %s; current Lead.icp resolves to %s",
            queued_icp, public_id, resolved_icp,
        )
    steps = channel_steps(sender=our_operator, icp=icp, channel=channel)

    if step_already_sent:
        next_step_index = step_index + 1
        if next_step_index < len(steps):
            enqueue_follow_up(
                campaign_id,
                public_id,
                operator=our_operator,
                icp=icp,
                delay_seconds=_delay_seconds_to_active_due(
                    steps[next_step_index].delay_hours,
                    reference_time=deal.connected_at,
                ),
                sequence_name=sequence_name,
                channel=channel,
                step_index=next_step_index,
            )
            set_profile_state(
                session, public_id, "Connected",
                reason=f"Follow-up step {step_index} already sent by this operator",
            )
        else:
            set_profile_state(
                session, public_id, "Completed",
                reason=f"Follow-up step {step_index} already sent by this operator",
            )
        return

    # ICP-keyed send. `my_name` is unused for LinkedIn channel (no
    # signature block in those templates), passed for symmetry with the
    # email channel where {my_name} fills the sign-off. Templates can
    # also embed `{add <filename>}` placeholders to attach a media file
    # (looked up in assets/followup/ then ROOT_DIR) — handled in
    # `_send_with_attachments_or_text` below.
    filled = fill_message(
        sender=our_operator,
        icp=icp,
        channel=channel,
        first_name=deal.lead.first_name or "",
        last_name=deal.lead.last_name or "",
        company_name=deal.lead.company_name or "",
        my_name=our_operator,
        lead_id=deal.lead_id,
        step_index=step_index,
    )
    # If the template included {add <filename>} placeholders, send via
    # the media path (first attachment only — LinkedIn's message form
    # accepts one inline media per send). Multiple attachments would
    # require sequential sends; today's templates use 0 or 1.
    if filled.attachments:
        from linkedin.actions.message import send_media_message
        sent = send_media_message(
            session, profile, filled.body, str(filled.attachments[0]),
            deal_id=deal.pk,
            sequence_name=sequence_name,
            step_index=step_index,
            operator=our_operator,
        )
    else:
        sent = send_raw_message(
            session,
            profile,
            filled.body,
            deal_id=deal.pk,
            sequence_name=sequence_name,
            step_index=step_index,
            operator=our_operator,
        )
    if not sent:
        logger.warning("follow_up send failed for %s — re-enqueuing in 24h", public_id)
        enqueue_follow_up(
            campaign_id, public_id,
            operator=our_operator,
            icp=icp,
            delay_seconds=24 * 3600,
            sequence_name=sequence_name,
            channel=channel,
            step_index=step_index,
        )
        return

    def _record_action():
        session.linkedin_profile.record_action(ActionLog.ActionType.FOLLOW_UP, session.campaign)

    # `record_action` and next-step enqueue are intentionally outside the
    # retried state-write block below. If Neon drops the connection on
    # set_profile_state, retrying only the state write keeps the post-send
    # path from double-counting the rate limit or creating duplicate next
    # tasks.
    try:
        _record_action()
    except _DB_DEAD_ERRORS as e:
        logger.warning(
            "follow_up action-log write hit dead conn for %s (DM already "
            "sent on LinkedIn) — recycling conn and retrying once: %s",
            public_id, e,
        )
        connections.close_all()
        _record_action()

    next_step_index = step_index + 1
    if next_step_index < len(steps):
        delay_seconds = _delay_seconds_to_active_due(
            steps[next_step_index].delay_hours,
            reference_time=deal.connected_at,
        )
        enqueue_follow_up(
            campaign_id,
            public_id,
            operator=our_operator,
            icp=icp,
            delay_seconds=delay_seconds,
            sequence_name=sequence_name,
            channel=channel,
            step_index=next_step_index,
        )

    def _record_success_state():
        if next_step_index < len(steps):
            set_profile_state(
                session, public_id, "Connected",
                reason=f"Sent ICP-{icp} follow-up step {step_index}",
            )
        else:
            set_profile_state(
                session, public_id, "Completed",
                reason=f"Sent ICP-{icp} follow-up DM",
            )

    # Post-send state write — critical: if `set_profile_state` doesn't run,
    # the Deal stays at CONNECTED and a future task may re-send the same DM.
    # Wrap only this write so a Neon idle-timeout mid-task (DM already sent
    # on LinkedIn) gets a fresh conn + one retry without duplicating the
    # ActionLog row or next-step Task created above.
    try:
        _record_success_state()
    except _DB_DEAD_ERRORS as e:
        logger.warning(
            "follow_up post-send DB writes hit dead conn for %s (DM already "
            "sent on LinkedIn) — recycling conn and retrying once: %s",
            public_id, e,
        )
        connections.close_all()
        _record_success_state()
    logger.info("follow_up sent to %s (icp=%s, step=%s/%s)", public_id, icp, step_index, len(steps) - 1)
