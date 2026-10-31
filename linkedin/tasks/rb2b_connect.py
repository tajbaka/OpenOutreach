# linkedin/tasks/rb2b_connect.py
"""rb2b_connect — one reviewed RB2B visitor, one worker-sent invitation.

Runs inside the sender's existing daemon, so it shares the browser session,
active hours, daily/weekly/total limits and connect pacing with campaign
connects. Every LinkedIn check happens here, immediately before the single
send through the normal connect UI path. A send counts as ``sent`` only when
the sender's Sent Invitations page independently lists the person. Failures
before the reservation may retry; anything after it that is not confirmed is
recorded as failed or uncertain and is never retried. No Campaign, Deal or
ActionLog is created.
"""
from __future__ import annotations

import logging
import unicodedata
import uuid
from datetime import timedelta

from django.db import connections, transaction
from django.db.utils import InterfaceError, OperationalError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from linkedin import conf
from linkedin.enums import ProfileState
from linkedin.exceptions import ReachedConnectionLimit, SkipProfile
from linkedin.models import ActionLog
from linkedin.operators import resolve_operator

logger = logging.getLogger(__name__)

MAX_PRECHECK_ATTEMPTS = 3
PRECHECK_RETRY_SECONDS = 30 * 60


def _name_tokens(*values) -> set[str]:
    text = unicodedata.normalize("NFKD", " ".join(str(v or "") for v in values)).casefold()
    text = "".join(ch if ch.isalnum() else " " for ch in text if not unicodedata.combining(ch))
    return set(text.split())


def _live_profile(session, public_id: str) -> dict | None:
    """Voyager profile, or None when LinkedIn reports it inaccessible (403/404).

    Transient API errors raise, so the caller can retry before any send.
    """
    from linkedin.api.client import PlaywrightLinkedinAPI

    session.ensure_browser()
    profile, _raw = PlaywrightLinkedinAPI(session=session).get_profile(public_identifier=public_id)
    return profile


def _identity_problem(row, decision: dict, live: dict | None) -> str:
    """Why the live profile isn't provably the reviewed person, else ''."""
    from crm.models import Lead
    from linkedin.models import RB2BVisitor

    if not live:
        return "linkedin_profile_unavailable"
    if str(live.get("public_identifier") or "").lower() != row.public_identifier.lower():
        return "linkedin_alias_requires_review"
    # LinkedIn names often carry credentials ("Doe, CISSP"): every reviewed
    # name token must appear, extra live tokens are fine.
    expected = _name_tokens(decision.get("first_name"), decision.get("last_name"))
    if not expected or not expected <= _name_tokens(live.get("first_name"), live.get("last_name")):
        return "linkedin_name_does_not_match_review"
    urn = str(live.get("urn") or "")
    if not urn:
        return "linkedin_member_urn_missing"
    if RB2BVisitor.objects.filter(member_urn=urn).exclude(pk=row.pk).exists():
        return "duplicate_rb2b_identity"
    if Lead.objects.filter(description__contains=urn).exclude(pk=row.lead_id).exists():
        return "existing_crm_identity"
    return ""


def _sent_invitation_receipt(session, target: dict) -> str | None:
    """The Sent Invitations label for this exact person, or None if not listed.

    LinkedIn lists newest first, so a just-sent invitation is on the first
    screen; the scan stops once two-day-old cards are visible.
    """
    from linkedin.actions.invitations import SentInvitationTarget, scan_sent_invitations

    name = f"{target['first_name']} {target['last_name']}".strip()
    scan = scan_sent_invitations(session, [SentInvitationTarget(target["public_identifier"], name)],
                                 approximate_max_age_days=2)
    match = scan.by_public_identifier.get(target["public_identifier"].casefold())
    return (match.sent_label or "listed") if match else None


def _finish(session, row, status: str, reason: str = "", **outcome) -> None:
    """Close a reserved attempt: shared pacing gap, outcome, #rb2b-v2 notice.

    One retry covers a dropped DB connection. If the write still fails, the
    reservation stays ``attempting`` and rb2b.resolve_interrupted_attempts
    later records it as uncertain; it is never resent.
    """
    from linkedin import rb2b
    from linkedin.models import RB2BVisitor
    from linkedin.tasks.connect import recommended_action_delay

    profile = session.linkedin_profile
    for attempt in (1, 2):
        try:
            # Any reserved attempt may have clicked, so it always takes a full gap.
            cooldown = timezone.now() + timedelta(
                seconds=recommended_action_delay(profile, ActionLog.ActionType.CONNECT),
            )
            RB2BVisitor.objects.filter(pk=row.pk).update(cooldown_until=cooldown)
            rb2b.defer_campaign_connects(profile, cooldown)
            rb2b.record_outcome(row, status, reason, **outcome)
            break
        except (OperationalError, InterfaceError):
            if attempt == 2:
                raise
            logger.warning("rb2b_connect: outcome write hit a dead DB connection — retrying once")
            connections.close_all()
    rb2b.flush_notifications()


def handle_rb2b_connect(task, session, qualifiers):
    from linkedin import rb2b
    from linkedin.actions.connect import ExistingPendingInvite, send_connection_request
    from linkedin.actions.status import get_connection_status
    from linkedin.exceptions import InvitationWithdrawalError
    from linkedin.models import RB2BVisitor
    from linkedin.tasks.connect import _seconds_until_next_active_start, recommended_action_delay

    profile = session.linkedin_profile
    row = RB2BVisitor.objects.select_related("lead").filter(pk=task.payload.get("visitor_id")).first()
    if (row is None or row.linkedin_profile_id != profile.pk
            or resolve_operator(profile.linkedin_username) != rb2b.SENDER):
        logger.warning("rb2b_connect: task %s is not this sender's visitor — skipped", task.pk)
        return
    if row.status == "attempting":
        # A crash or restart interrupted a reserved send. Never click again.
        _finish(session, row, "uncertain", "Interrupted after the send reservation")
        return
    if row.status != "ready" or row.attempted_at:
        return  # held on re-review, or already handled
    if not conf.ENABLE_CONNECT or not rb2b.connect_ready():
        return  # stays ready; startup reconcile queues it again once enabled

    # Every check and outcome below belongs to this exact review. A re-review
    # meanwhile wins: it queued its own Task, which starts over.
    decision = row.review.get("decision", {})
    checked_review = (row.note, decision)

    def hold(reason: str) -> None:
        logger.info("rb2b_connect: %s held — %s", row.public_identifier, reason)
        rb2b.record_outcome(row, "held", reason, expect_review=checked_review)
        rb2b.flush_notifications()

    retries = {"precheck_attempts": task.payload["precheck_attempts"]} if task.payload.get("precheck_attempts") else {}
    if not row.lead_id:
        return hold("crm_contact_missing")
    reason = rb2b.exclusion(row.lead, row)
    if reason:
        return hold(reason)

    # Shared limits: the same counters as campaign connects (RB2B attempts
    # are included through rb2b.budget_count).
    if not profile.can_execute(ActionLog.ActionType.CONNECT):
        rb2b.enqueue_connect_task(
            row, at=timezone.now() + timedelta(seconds=_seconds_until_next_active_start()), **retries,
        )
        return

    # Shared pacing: one connect gap after the sender's last invitation of
    # either kind. Campaign connects wait on the same slot, so RB2B goes first.
    last = rb2b.last_connect_at(profile)
    if last is not None:
        stamp = last.isoformat()
        not_before = parse_datetime(task.payload.get("not_before") or "") if task.payload.get("paced_after") == stamp else None
        if not_before is None:
            not_before = last + timedelta(seconds=recommended_action_delay(profile, ActionLog.ActionType.CONNECT))
        if not_before > timezone.now():
            rb2b.defer_campaign_connects(profile, not_before)
            # Exactly the campaign slot: on a tie the claimer takes RB2B first.
            rb2b.enqueue_connect_task(
                row, at=not_before, paced_after=stamp, not_before=not_before.isoformat(), **retries,
            )
            return

    try:
        live = _live_profile(session, row.public_identifier)
        problem = _identity_problem(row, decision, live)
        if problem:
            return hold(problem)
        row.member_urn = live["urn"]
        row.save(update_fields=["member_urn", "updated_at"])
        if live.get("connection_degree") == 1:
            return hold("already_connected")
        target = {
            "public_identifier": row.public_identifier,
            "url": row.linkedin_url,
            "first_name": live.get("first_name") or "",
            "last_name": live.get("last_name") or "",
            "connection_degree": live.get("connection_degree"),
        }
        status = get_connection_status(session, target)
    except Exception as exc:
        attempts = int(task.payload.get("precheck_attempts") or 0) + 1
        if attempts < MAX_PRECHECK_ATTEMPTS:
            rb2b.enqueue_connect_task(row, at=timezone.now() + timedelta(seconds=PRECHECK_RETRY_SECONDS),
                                      precheck_attempts=attempts)
        else:
            rb2b.record_outcome(row, "failed", f"LinkedIn checks failed {attempts} times before any send "
                                               f"({type(exc).__name__})", expect_review=checked_review,
                                send_clicked=False)
            rb2b.flush_notifications()
        raise
    if status == ProfileState.CONNECTED:
        return hold("already_connected")
    if status == ProfileState.PENDING:
        return hold("pending_invitation_exists")

    # Persisted state may have changed during the browser checks (a reply,
    # suppression, or new campaign history). Recheck at the mutation boundary.
    row.lead.refresh_from_db()
    reason = rb2b.exclusion(row.lead, row)
    if reason:
        return hold(reason)

    # Reserve before the click: the attempt counts against shared budgets and
    # can never be repeated, whatever happens next.
    with transaction.atomic():
        locked = RB2BVisitor.objects.select_for_update().get(pk=row.pk)
        if locked.status != "ready" or locked.attempted_at:
            return
        if (locked.note, locked.review.get("decision", {})) != checked_review:
            # Re-reviewed mid-check: its own queued Task re-runs every check.
            logger.info("rb2b_connect: %s was re-reviewed during checks — not sent", row.public_identifier)
            return
        locked.status, locked.attempted_at, locked.attempt_token = "attempting", timezone.now(), uuid.uuid4()
        locked.outcome = {**locked.outcome, "task_id": task.pk}
        locked.save(update_fields=["status", "attempted_at", "attempt_token", "outcome", "updated_at"])
    row = locked

    logger.info("rb2b_connect: sending reviewed invitation to %s", row.public_identifier)
    try:
        state = send_connection_request(session=session, profile=target, note=row.note)
    except ReachedConnectionLimit as exc:
        profile.mark_exhausted(ActionLog.ActionType.CONNECT)
        # The custom-note limit is detected before Send; other limit pop-ups
        # can appear after the click.
        _finish(session, row, "failed" if "Invitation not sent" in str(exc) else "uncertain", str(exc)[:300])
        return
    except SkipProfile as exc:
        _finish(session, row, "failed", f"LinkedIn refused the invitation: {exc}"[:300])
        return
    except ExistingPendingInvite:
        # Could be our own Connect click; never assume either way.
        _finish(session, row, "uncertain", "LinkedIn showed a pending invitation during the send")
        return
    except Exception as exc:
        _finish(session, row, "uncertain", f"Send interrupted ({type(exc).__name__})")
        raise
    if state != ProfileState.PENDING:
        _finish(session, row, "uncertain", "LinkedIn did not confirm the invitation (Connect or note controls not found)")
        return
    # The helper's PENDING only means Send was clicked. Require the sender's
    # own Sent Invitations list to show this exact person.
    try:
        receipt = _sent_invitation_receipt(session, target)
    except InvitationWithdrawalError as exc:
        _finish(session, row, "uncertain", f"Send clicked; Sent Invitations check failed: {exc}"[:300])
        return
    except Exception as exc:
        _finish(session, row, "uncertain", f"Send clicked; Sent Invitations check interrupted ({type(exc).__name__})")
        raise
    if receipt is None:
        _finish(session, row, "uncertain", "Send clicked but the invitation is not on Sent Invitations")
    else:
        _finish(session, row, "sent", sent_invitations_label=receipt)
