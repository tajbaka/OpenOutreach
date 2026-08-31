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
from enum import StrEnum
from math import isfinite
from pathlib import Path
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


def _drip_owns_linkedin(lead_id: int) -> bool:
    from drip.models import DripLane
    from drip.services.ownership import drip_owns_channel

    return drip_owns_channel(
        lead_id=lead_id,
        channel=DripLane.Channel.LINKEDIN,
    )

# DB errors that mean "fresh connection needed" — Neon idle-timeout drops.
_DB_DEAD_ERRORS = (OperationalError, InterfaceError)
DEFAULT_SEQUENCE_NAME = "linkedin_connect_followup"
DEFAULT_CHANNEL = "linkedin_connect_followup"


class _MediaFollowUpOutcome(StrEnum):
    SENT = "sent"
    RETRYABLE_FAILURE = "retryable_failure"
    BLOCKED = "blocked"


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


def _has_same_operator_followup(*, lead, operator: str) -> bool:
    """Return whether this operator already persisted any daemon follow-up."""
    from crm.models import Message
    from linkedin.operators import resolve_operator

    senders = (
        Message.objects.filter(
            lead=lead,
            source=Message.Source.LINKEDIN,
            direction=Message.Direction.OUTBOUND,
            external_id__startswith="daemon-send:",
        )
        .exclude(sender="")
        .values_list("sender", flat=True)
        .distinct()
    )
    return operator in {resolve_operator(sender) for sender in senders}


def _sequence_stop_reason(deal) -> str:
    from linkedin.tasks.stop_checks import automation_stop_reason

    return automation_stop_reason(deal)


def _finish_sequence_for_stop(session, deal, public_id: str, reason: str) -> None:
    """Persist the current sequence's terminal state for a shared stop."""
    deal.lead.refresh_from_db(fields=["disqualified"])
    failed = deal.lead.disqualified or reason.startswith("Suppression:")
    set_profile_state(
        session,
        public_id,
        "Failed" if failed else "Completed",
        reason=reason,
    )


def _approved_media_reference(attachment: Path) -> str:
    """Return the approved-root-relative reference for a rendered attachment."""
    from linkedin.conf import ROOT_DIR
    from linkedin.exceptions import LinkedInMediaValidationError

    try:
        approved_root = (ROOT_DIR / "assets" / "follow_up").resolve(strict=True)
        candidate = Path(attachment).resolve(strict=True)
    except OSError as exc:
        raise LinkedInMediaValidationError(
            f"LinkedIn follow-up media cannot be resolved: {attachment}"
        ) from exc
    try:
        return candidate.relative_to(approved_root).as_posix()
    except ValueError as exc:
        raise LinkedInMediaValidationError(
            f"LinkedIn follow-up media escapes assets/follow_up: {attachment}"
        ) from exc


def _send_media_follow_up(
    *,
    task,
    session,
    deal,
    public_id: str,
    body: str,
    attachment: Path,
    campaign_id: int,
    sequence_name: str,
    step_index: int,
    operator: str,
) -> _MediaFollowUpOutcome:
    """Send one validated follow-up attachment through the strict UI route."""
    from crm.models import Deal
    from linkedin.actions.message import (
        DirectMessageOutcome,
        MessageSubmissionAborted,
        send_direct_message_once,
    )
    from linkedin.db.chat import save_chat_message
    from linkedin.db.leads import resolve_urn
    from linkedin.db.messages import lead_outbound_operators
    from linkedin.enums import ProfileState
    from linkedin.exceptions import LinkedInMessageSubmissionUnclearError
    from linkedin.message_media import resolve_linkedin_media
    from linkedin.models import Campaign
    from linkedin.tasks.follow_up_submission import (
        has_unresolved_submission,
        persisted_submission_evidence,
        stamp_submission_attempt,
    )

    asset = resolve_linkedin_media(_approved_media_reference(attachment))
    member_urn = resolve_urn(public_id, session=session) or ""
    blocked: dict[str, object] = {}

    def _abort(kind: str, detail: str, *, fresh_deal=None) -> None:
        blocked.update(kind=kind, detail=detail, deal=fresh_deal)
        raise MessageSubmissionAborted(detail)

    def _final_submission_guard() -> None:
        if not Campaign.objects.filter(
            pk=campaign_id,
            status=Campaign.Status.ACTIVE,
        ).exists():
            _abort("campaign", "current LinkedIn campaign became inactive")

        fresh_deal = Deal.objects.select_related("lead").filter(pk=deal.pk).first()
        if fresh_deal is None:
            _abort("deal", "current LinkedIn Deal disappeared")
        if fresh_deal.state != ProfileState.CONNECTED:
            _abort(
                "deal",
                f"current LinkedIn Deal changed to {fresh_deal.state}",
                fresh_deal=fresh_deal,
            )
        if _drip_owns_linkedin(fresh_deal.lead_id):
            _abort(
                "drip",
                "drip took ownership of LinkedIn before submission",
                fresh_deal=fresh_deal,
            )
        owning_operators = lead_outbound_operators(fresh_deal.lead)
        if owning_operators and operator not in owning_operators:
            _abort(
                "owner",
                "LinkedIn thread ownership changed before submission",
                fresh_deal=fresh_deal,
            )
        stop_reason = _sequence_stop_reason(fresh_deal)
        if stop_reason:
            _abort("stop", stop_reason, fresh_deal=fresh_deal)
        if has_unresolved_submission(
            lead_id=fresh_deal.lead_id,
            operator=operator,
        ):
            _abort(
                "unclear",
                "another same-operator LinkedIn media submission became unresolved",
                fresh_deal=fresh_deal,
            )
        if _has_sent_sequence_step(
            deal=fresh_deal,
            operator=operator,
            sequence_name=sequence_name,
            step_index=step_index,
        ):
            _abort(
                "sent",
                "this LinkedIn sequence step was persisted before submission",
                fresh_deal=fresh_deal,
            )
        if step_index == 0 and _has_same_operator_followup(
            lead=fresh_deal.lead,
            operator=operator,
        ):
            _abort(
                "sent",
                "this operator persisted another LinkedIn follow-up before submission",
                fresh_deal=fresh_deal,
            )

    def _submission_callback() -> None:
        stamp_submission_attempt(
            task,
            lead_id=deal.lead_id,
            message_prefix=_step_external_id_prefix(
                operator=operator,
                deal_id=deal.pk,
                sequence_name=sequence_name,
                step_index=step_index,
            ),
            operator=operator,
            final_guard=_final_submission_guard,
        )

    result = send_direct_message_once(
        session,
        member_urn,
        body,
        recipient_label=deal.lead.linkedin_url or public_id,
        on_submit_attempt=_submission_callback,
        media=asset,
    )
    if result.outcome == DirectMessageOutcome.SENT:
        save_chat_message(
            session,
            public_id,
            body,
            deal_id=deal.pk,
            sequence_name=sequence_name,
            step_index=step_index,
            operator=operator,
            raw={"media": asset.evidence()},
        )
        try:
            evidence_persisted = persisted_submission_evidence(task.payload)
        except _DB_DEAD_ERRORS as exc:
            raise LinkedInMessageSubmissionUnclearError(
                "LinkedIn confirmed the media send, but durable Message evidence "
                f"could not be verified for Task {task.pk}"
            ) from exc
        if not evidence_persisted:
            raise LinkedInMessageSubmissionUnclearError(
                "LinkedIn confirmed the media send, but exact durable Message "
                f"evidence is absent for Task {task.pk}; sequence advancement is blocked"
            )
        return _MediaFollowUpOutcome.SENT
    if result.outcome == DirectMessageOutcome.UNCLEAR:
        raise LinkedInMessageSubmissionUnclearError(
            "LinkedIn media submission is unclear for Task "
            f"{task.pk} / {public_id}: {result.detail}"
        )
    if result.outcome != DirectMessageOutcome.PRE_SUBMIT_FAILED:
        raise ValueError(f"Unknown direct-message result: {result.outcome!r}")

    # Upload/navigation failures occur before the action primitive invokes its
    # submit callback. Re-run the same persisted guards before deciding to
    # create a retry so a reply or drip handoff that arrived during a failed
    # upload does not leave new automated work queued.
    if not blocked:
        try:
            _final_submission_guard()
        except MessageSubmissionAborted:
            pass

    if blocked:
        detail = str(blocked.get("detail") or "media follow-up blocked")
        logger.info("follow_up: %s blocked at media submit boundary: %s", public_id, detail)
        if blocked.get("kind") == "stop" and blocked.get("deal") is not None:
            _finish_sequence_for_stop(
                session,
                blocked["deal"],
                public_id,
                detail,
            )
        return _MediaFollowUpOutcome.BLOCKED
    logger.warning(
        "follow_up media pre-submit failure for %s: %s",
        public_id,
        result.detail,
    )
    return _MediaFollowUpOutcome.RETRYABLE_FAILURE


def _normalize_linkedin_due_at(minimum_due_at, *, current_time=None):
    """Return one absolute LinkedIn due time at or after the minimum.

    Overdue work starts from the evaluation instant rather than a historical
    active window.  Capturing that instant once also keeps callers from
    combining a stale pass timestamp with a relative delay calculated from a
    newer wall-clock read.
    """
    evaluated_at = current_time or timezone.now()
    effective_due_at = max(minimum_due_at, evaluated_at)
    if not ENABLE_ACTIVE_HOURS:
        return effective_due_at

    tz = ZoneInfo(ACTIVE_TIMEZONE)
    due = timezone.localtime(effective_due_at, timezone=tz)
    if due.weekday() not in REST_DAYS and ACTIVE_START_HOUR <= due.hour < ACTIVE_END_HOUR:
        return effective_due_at

    if due.weekday() in REST_DAYS or due.hour >= ACTIVE_END_HOUR:
        candidate = due + timedelta(days=1)
    else:
        candidate = due
    candidate = candidate.replace(
        hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0,
    )
    while candidate.weekday() in REST_DAYS:
        candidate += timedelta(days=1)
    return candidate


def _delay_seconds_to_active_due(
    delay_hours: float,
    *,
    reference_time=None,
) -> float:
    """Return a relative delay backed by one deterministic absolute due time.

    `delay_hours` is an offset from `reference_time`, normally
    `Deal.connected_at`. Falling back to now keeps legacy rows with no
    connection timestamp schedulable without special cases.
    """
    evaluated_at = timezone.now()
    anchor = reference_time or evaluated_at
    parsed_delay_hours = float(delay_hours)
    if not isfinite(parsed_delay_hours):
        raise ValueError(f"delay_hours must be finite, got {delay_hours!r}")
    minimum_due_at = anchor + timedelta(hours=max(parsed_delay_hours, 0.0))
    due_at = _normalize_linkedin_due_at(
        minimum_due_at,
        current_time=evaluated_at,
    )
    return max((due_at - evaluated_at).total_seconds(), 0.0)


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
    from linkedin.models import Campaign
    from linkedin.operators import resolve_operator
    from linkedin.tasks.connect import _seconds_until_tomorrow, enqueue_follow_up
    from linkedin.tasks.follow_up_submission import (
        has_unresolved_submission,
        persisted_submission_evidence,
        submission_attempted,
    )

    payload = task.payload
    public_id = payload["public_id"]
    campaign_id = payload["campaign_id"]
    sequence_name = payload.get("sequence_name") or DEFAULT_SEQUENCE_NAME
    channel = payload.get("channel") or sequence_name or DEFAULT_CHANNEL
    step_index = int(payload.get("step_index") or 0)
    queued_icp = (payload.get("icp") or "").strip()

    submission_was_attempted = submission_attempted(payload)
    submission_evidence_persisted = (
        persisted_submission_evidence(payload) if submission_was_attempted else False
    )
    if submission_was_attempted and not submission_evidence_persisted:
        from linkedin.exceptions import LinkedInMessageSubmissionUnclearError

        raise LinkedInMessageSubmissionUnclearError(
            "LinkedIn media submission is already marked unclear for Task "
            f"{task.pk}; automatic resend is blocked"
        )

    if not Campaign.objects.filter(
        pk=campaign_id,
        status=Campaign.Status.ACTIVE,
    ).exists():
        logger.info(
            "follow_up: campaign %s is not active - skipping task %s",
            campaign_id,
            task.pk,
        )
        return

    logger.info(
        "[%s] %s %s",
        session.campaign, colored("▶ follow_up", "green", attrs=["bold"]), public_id,
    )

    our_operator = resolve_operator(session.linkedin_profile.linkedin_username)

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
    if _drip_owns_linkedin(deal.lead_id):
        logger.info("follow_up: %s skipped - drip owns LinkedIn", public_id)
        return
    if has_unresolved_submission(lead_id=deal.lead_id, operator=our_operator):
        from linkedin.exceptions import LinkedInMessageSubmissionUnclearError

        raise LinkedInMessageSubmissionUnclearError(
            "Another current LinkedIn media submission is unresolved for "
            f"Lead {deal.lead_id} / {our_operator}; automatic send is blocked"
        )

    # Rate-limit deferral happens only after durable uncertainty is checked.
    # Otherwise a sibling campaign Task could manufacture an unmarked retry
    # for a send whose provider outcome is already ambiguous. A recovered
    # media Task with exact persisted evidence bypasses quota because its only
    # remaining work is the dedupe/successor/state repair below.
    if (
        not submission_evidence_persisted
        and not session.linkedin_profile.can_execute(ActionLog.ActionType.FOLLOW_UP)
    ):
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
        _finish_sequence_for_stop(session, deal, public_id, stop_reason)
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
    step_already_sent = (
        submission_evidence_persisted
        or _has_sent_sequence_step(
            deal=deal,
            operator=our_operator,
            sequence_name=sequence_name,
            step_index=step_index,
        )
    )

    # Prefer exact current-sequence evidence when it exists. In particular, a
    # recovered media Task can already have the confirmed step-0 Message while
    # still needing this handler to enqueue step 1. The broad legacy
    # cross-campaign guard applies only when this exact step was not sent.
    if (
        step_index == 0
        and not step_already_sent
        and _has_same_operator_followup(lead=deal.lead, operator=our_operator)
    ):
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
    # also embed `{add <filename>}` placeholders to attach one validated
    # GIF/MP4 from assets/follow_up/ — handled by the strict media branch
    # below.
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
    # Profile/template work can outlive an earlier listener or backfill write.
    # Recheck persisted state at the external mutation boundary without adding
    # a live conversation dependency.
    if _drip_owns_linkedin(deal.lead_id):
        logger.info(
            "follow_up: %s handed off before send - skipping",
            public_id,
        )
        return
    stop_reason = _sequence_stop_reason(deal)
    if stop_reason:
        _finish_sequence_for_stop(session, deal, public_id, stop_reason)
        return

    # Media rendering already enforces exactly zero or one validated asset.
    # The attachment branch uses the strict exact-URN route and performs its
    # final persisted stop check after upload/typing, immediately before the
    # only Send click. Text-only current follow-ups retain their established
    # sender path to keep this change isolated.
    if filled.attachments:
        media_outcome = _send_media_follow_up(
            task=task,
            session=session,
            deal=deal,
            public_id=public_id,
            body=filled.body,
            attachment=filled.attachments[0],
            campaign_id=campaign_id,
            sequence_name=sequence_name,
            step_index=step_index,
            operator=our_operator,
        )
        if media_outcome == _MediaFollowUpOutcome.BLOCKED:
            return
        sent = media_outcome == _MediaFollowUpOutcome.SENT
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
