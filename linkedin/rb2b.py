"""Reviewed RB2B intake -> CRM -> one worker-sent connection request.

The scheduled Windows Codex review supplies sourced decisions without opening
LinkedIn. The existing Arian daemon does every LinkedIn check and the single
send (linkedin/tasks/rb2b_connect.py). No Campaign, Deal, ActionLog, follow-up
or enrollment is created.
"""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import os

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from api.rb2b_webhook import normalize_visitor
from crm.models import Lead, Message, Deal, Meeting, OpportunityContact
from linkedin import conf
from linkedin.models import (ActionLog, Campaign, LinkedInProfile,
                             RB2BVisitor, RB2BNotification, Task)
from linkedin.operators import resolve_operator
from linkedin.suppression import lead_suppression_match

# Canonical-URL digests of two privately verified historical invitations.
# Keep their profile details and original receipts out of this public repo.
NO_RESEND = {
    "33943bcf4b8673e3aeb175435950487148b06a49ebe57573793195f7d8ce49ed",
    "6774bcd0e03da3e40b8994ae054b8ecc5f56393d972229c6ea390cc71b303652",
}
PROTOCOL = 2
SENDER = "Arian"
REPLY_SCAN_MINUTES = 60


class RB2BHeld(ValueError):
    """A stable, non-sensitive reason the requested transition cannot happen."""


def owner():
    profiles = list(LinkedInProfile.objects.filter(user__username="ariantajbakh", active=True))
    if len(profiles) != 1 or resolve_operator(profiles[0].linkedin_username) != SENDER:
        raise RB2BHeld("arian_owner_not_unique")
    return profiles[0]


def canonical_url(url):
    return normalize_visitor({"LinkedIn URL": url, "Seen At": "identity-check"})["url"]


def fresh(evidence, *, minutes=5):
    at = parse_datetime(str(evidence.get("checked_at", "")))
    if not at or timezone.is_naive(at) or not timedelta(0) <= timezone.now() - at <= timedelta(minutes=minutes):
        raise RB2BHeld("fresh_dated_evidence_required")


def _validate_decision(row, decision):
    # LinkedIn itself is checked by the worker immediately before sending;
    # the review is sourced identity/fit/relationship evidence only.
    if canonical_url(decision.get("linkedin_url", "")) != row.linkedin_url:
        raise RB2BHeld("identity_mismatch")
    for key in ("identity_verified", "fit_verified", "relationships_checked"):
        if decision.get(key) is not True:
            raise RB2BHeld(key + "_required")
    if not str(decision.get("first_name", "")).strip() or not str(decision.get("last_name", "")).strip():
        raise RB2BHeld("verified_first_and_last_name_required")
    sources = decision.get("evidence_urls")
    if (not isinstance(sources, list) or not sources or not all(isinstance(s, str) and s.startswith("https://") for s in sources)
            or not isinstance(decision.get("fit_reason"), str) or not decision["fit_reason"].strip()):
        raise RB2BHeld("sourced_fit_evidence_required")
    fresh(decision, minutes=60)


def _identity_matches(row, decision):
    q = Q(public_identifier__iexact=row.public_identifier) | Q(linkedin_url__iexact=row.linkedin_url)
    q |= Q(linkedin_url__iexact=row.linkedin_url.rstrip("/"))
    email = str(decision.get("email", "")).strip().lower()
    if email:
        q |= Q(email__iexact=email)
    matches = list(Lead.objects.select_for_update().filter(q))
    if len(matches) > 1:
        raise RB2BHeld("ambiguous_crm_identity")
    if not matches:
        return None
    lead = matches[0]
    # An email match is evidence for a hold, never authority to merge identities.
    if lead.linkedin_url and canonical_url(lead.linkedin_url) != row.linkedin_url:
        raise RB2BHeld("existing_profile_conflict")
    if lead.public_identifier and lead.public_identifier.lower() != row.public_identifier:
        raise RB2BHeld("existing_alias_requires_review")
    return lead


def exclusion(lead, row):
    from drip.models import DripEnrollment
    from crm.models import Opportunity
    from crm.models.sales import normalize_account_name
    company = normalize_account_name(lead.company_name)
    if company and Opportunity.objects.filter(account__normalized_name=company).exists():
        return "existing_account_relationship"
    if row.review.get("legacy_processing_hold"):
        return "legacy_processing_review_required"
    if hashlib.sha256(row.linkedin_url.encode()).hexdigest() in NO_RESEND:
        return "historical_invitation_no_resend"
    if lead.disqualified or lead_suppression_match(lead):
        return "suppressed"
    if lead.pk:
        if Deal.objects.filter(lead=lead).exists() or DripEnrollment.objects.filter(lead=lead).exists():
            return "existing_campaign_history"
        if Message.objects.filter(lead=lead).exists() or Meeting.objects.filter(lead=lead).exists():
            return "existing_conversation"
        if OpportunityContact.objects.filter(lead=lead).exists():
            return "existing_opportunity_or_customer"
        if RB2BVisitor.objects.filter(lead=lead).exclude(pk=row.pk).exists():
            return "existing_rb2b_identity"
        pending = Task.objects.filter(status__in=[Task.Status.PENDING, Task.Status.RUNNING])
        if pending.filter(Q(payload__lead_id=lead.pk) | Q(payload__lead_id=str(lead.pk))
                          | Q(payload__public_id=row.public_identifier)).exists():
            return "existing_pending_work"
    return ""


def _notify(row, kind, detail=""):
    key = f"visitor:{row.pk}:{kind}:" + hashlib.sha256(detail.encode()).hexdigest()[:24]
    RB2BNotification.objects.get_or_create(key=key, defaults={"visitor": row, "kind": kind, "detail": detail})


@transaction.atomic
def review(visitor_id, decision):
    row = RB2BVisitor.objects.select_for_update().get(pk=visitor_id)
    # A failure before any reservation (attempted_at unset) may be re-reviewed.
    if row.attempted_at or row.status in {"sent", "uncertain", "attempting"}:
        raise RB2BHeld("attempt_history_is_immutable")
    if row.linkedin_profile_id != owner().pk:
        raise RB2BHeld("sender_mismatch")
    event_hash = decision.get("event_hash")
    if event_hash not in row.events:
        raise RB2BHeld("unknown_source_event")
    # Hold decisions don't create CRM contacts. Existing records remain intact.
    if decision.get("decision") == "hold":
        if not decision.get("reason"):
            raise RB2BHeld("hold_reason_required")
        row.status = "held"
        row.review = {**row.review, "decision": decision}
        row.save(update_fields=["status", "review", "updated_at"])
        _notify(row, "held", str(decision["reason"])[:500])
        return row
    if decision.get("decision") != "qualify":
        raise RB2BHeld("explicit_qualify_or_hold_decision_required")
    _validate_decision(row, decision)
    note = str(decision.get("note", "")).strip()
    icp = str(decision.get("icp", "")).strip()
    if not note or len(note) > 200 or not icp or len(icp) > 160:
        raise RB2BHeld("explicit_icp_and_note_under_201_characters_required")
    # Resolve only a known audience; never use resolve_icp's writing fallback.
    from linkedin.icp_outbound import load_icp_messages
    from linkedin.models import MessageProgramVersion
    known = icp in load_icp_messages(SENDER)
    if not known:
        # Published programs are reviewed separately; require the exact version
        # and audience supplied by the reviewer, without creating enrollment.
        version = MessageProgramVersion.objects.filter(pk=decision.get("message_version_id")).first()
        from linkedin.message_delivery import index_message_version
        index = index_message_version(version) if version else None
        known = bool(index and icp in index.audience_keys and index.effective_routes(
            audience_key=icp, operator=SENDER, channel="linkedin_connect"))
    if not known:
        raise RB2BHeld("unknown_audience")
    lead = _identity_matches(row, decision)
    if lead is None:
        lead = Lead(public_identifier=row.public_identifier, linkedin_url=row.linkedin_url,
                    first_name=str(decision.get("first_name", ""))[:100],
                    last_name=str(decision.get("last_name", ""))[:100],
                    company_name=str(decision.get("company_name", ""))[:200],
                    email=str(decision.get("email", ""))[:200], icp=icp)
    reason = exclusion(lead, row)
    if reason:
        row.status = "held"
        row.review = {**row.review, "decision": decision, "hold_reason": reason}
        row.save(update_fields=["status", "review", "updated_at"])
        _notify(row, "held", reason)
        return row
    if not lead.pk:
        lead.description = json.dumps({"source": "rb2b", "headline": decision.get("title", "")})
        lead.full_clean()
        lead.save()
    # Existing CRM fields are operator-owned. Exact association belongs here;
    # the worker stores the verified member URN on this receipt before sending.
    row.lead, row.note, row.status = lead, note, "ready"
    row.review = {**row.review, "decision": decision, "icp": icp}
    row.save()
    enqueue_connect_task(row)
    return row


def connect_ready():
    """Worker sends need the flag, the reply listener, and the outcome and reply
    Slack destinations; otherwise RB2B work stays pending and unclaimed."""
    return bool(conf.ENABLE_RB2B_CONNECT and conf.ENABLE_REALTIME_LISTENER
                and os.getenv("RB2B_SLACK_CHANNEL_ID", "").strip()
                and conf.SLACK_BOT_TOKEN and conf.SLACK_REPLIES_WEBHOOK_URL)


def heartbeat_state():
    # Lets `rb2b_workflow preflight` see whether the live worker will send.
    return {"protocol": PROTOCOL, "connect_ready": connect_ready()}


def enqueue_connect_task(row, *, at=None, **progress):
    """One pending worker task per ready visitor, due at ``at`` (default now).

    A re-review reuses the pending task and never pulls a paced slot earlier.
    """
    at = at or timezone.now()
    payload = {"operator": SENDER, "visitor_id": row.pk, **progress}
    task = Task.objects.filter(task_type=Task.TaskType.RB2B_CONNECT, status=Task.Status.PENDING,
                               payload__visitor_id=row.pk).first()
    if task is None:
        return Task.objects.create(task_type=Task.TaskType.RB2B_CONNECT, scheduled_at=at, payload=payload)
    task.payload = {**task.payload, **payload}
    task.scheduled_at = max(task.scheduled_at, at)
    task.save(update_fields=["payload", "scheduled_at"])
    return task


def _active_connect_tasks():
    return Task.objects.filter(task_type=Task.TaskType.RB2B_CONNECT,
                               status__in=[Task.Status.PENDING, Task.Status.RUNNING])


def resolve_interrupted_attempts(profile):
    """Reserved attempts with no live Task become uncertain, with a notice.

    The outcome write can fail after the click (DB outage, crash). A reserved
    attempt is never resent; it is resolved for manual Sent-invitations review.
    """
    resolved = 0
    for row in RB2BVisitor.objects.filter(linkedin_profile=profile, status="attempting"):
        if not _active_connect_tasks().filter(payload__visitor_id=row.pk).exists():
            record_outcome(row, "uncertain", "Interrupted after the send reservation; outcome was not recorded")
            resolved += 1
    return resolved


def reconcile_connect_tasks(profile):
    """Startup net for this sender's RB2B work.

    Queues ready visitors whose Task is missing (never reserved, so queueing
    can't repeat a send), resolves stranded reservations, keeps the reply scan
    scheduled and delivers any pending Slack notices. Returns tasks queued.
    """
    if not connect_ready() or resolve_operator(profile.linkedin_username) != SENDER:
        return 0
    queued = 0
    for row in RB2BVisitor.objects.filter(linkedin_profile=profile, status="ready", attempted_at__isnull=True):
        if not _active_connect_tasks().filter(payload__visitor_id=row.pk).exists():
            enqueue_connect_task(row)
            queued += 1
    resolve_interrupted_attempts(profile)
    enqueue_reply_scan()
    flush_notifications()
    return queued


def _pending_reply_scan():
    return Task.objects.filter(task_type=Task.TaskType.RB2B_REPLIES, status=Task.Status.PENDING,
                               payload__operator=SENDER).first()


def enqueue_reply_scan(*, at=None, since=None):
    """One pending downtime reply catch-up for the sender (never duplicated).

    ``since`` is the start of the last complete scan; the next scan pages back
    to it. Without one, the scan starts at the oldest RB2B invitation.
    """
    if _pending_reply_scan():
        return None
    payload = {"operator": SENDER, **({"since": since} if since else {})}
    return Task.objects.create(task_type=Task.TaskType.RB2B_REPLIES, scheduled_at=at or timezone.now(),
                               payload=payload)


def advance_reply_scan(*, since):
    """After a complete pass, move the next scan's window start forward."""
    task = _pending_reply_scan()
    if task:
        task.payload = {**task.payload, "since": since.isoformat()}
        task.save(update_fields=["payload"])


def budget_count(profile_id, since):
    # All reserved attempts count, including failures/uncertainty. No optimistic
    # refunds and no synthetic Campaign/Deal/ActionLog are necessary.
    return RB2BVisitor.objects.filter(linkedin_profile_id=profile_id, attempted_at__gte=since).count()


def last_connect_at(profile):
    """Latest connection request by this sender, campaign or RB2B."""
    times = (
        ActionLog.objects.filter(linkedin_profile=profile, action_type=ActionLog.ActionType.CONNECT)
        .aggregate(at=Max("created_at"))["at"],
        RB2BVisitor.objects.filter(linkedin_profile=profile).aggregate(at=Max("attempted_at"))["at"],
    )
    return max((at for at in times if at), default=None)


def defer_campaign_connects(profile, until):
    """Hold this sender's campaign connect slots until ``until``.

    RB2B and campaign invitations share one pacing gap. Moving due campaign
    connects onto the RB2B slot makes both due together, and the claimer then
    takes RB2B first. Campaign payloads, follow-ups and other senders are
    untouched.
    """
    campaign_ids = list(Campaign.objects.filter(user=profile.user).values_list("pk", flat=True))
    return Task.objects.filter(
        task_type=Task.TaskType.CONNECT, status=Task.Status.PENDING,
        payload__campaign_id__in=campaign_ids, scheduled_at__lt=until,
    ).update(scheduled_at=until)


def record_outcome(row, status, reason="", *, expect_review=None, **outcome):
    """Terminal worker outcome plus its #rb2b-v2 notification, in one transaction.

    ``expect_review`` (note, decision) guards a pre-send outcome: if the visitor
    was re-reviewed meanwhile, nothing is recorded and None is returned.
    """
    with transaction.atomic():
        row = RB2BVisitor.objects.select_for_update().get(pk=row.pk)
        if expect_review is not None and (row.note, row.review.get("decision", {})) != expect_review:
            return None
        row.status = status
        row.outcome = {**row.outcome, "result": {"status": status, "reason": reason, **outcome}}
        fields = ["status", "outcome", "updated_at"]
        if status == "sent":
            row.confirmed_at = timezone.now()
            fields.append("confirmed_at")
        if status == "held":
            row.review = {**row.review, "hold_reason": reason}
            fields.append("review")
        row.save(update_fields=fields)
        _notify(row, status, reason[:500])
    return row


OUTCOME_LABELS = {
    "sent": "connection request sent",
    "held": "held, not sent",
    "failed": "failed, not sent",
    "uncertain": "uncertain, check Sent invitations (never retried)",
}


def queue_reply_notifications(lead, message_ids):
    row = RB2BVisitor.objects.filter(lead=lead).first()
    if not row:
        return
    for msg in Message.objects.filter(pk__in=message_ids, lead=lead, direction=Message.Direction.INBOUND):
        RB2BNotification.objects.get_or_create(key=f"reply:{msg.pk}", defaults={
            "visitor": row, "message": msg, "kind": "reply",
        })


def flush_notifications(limit=50):
    from linkedin.notifications import slack
    delivered = 0
    ids = list(RB2BNotification.objects.filter(sent_at__isnull=True).filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=timezone.now())
    ).order_by("pk").values_list("pk", flat=True)[:limit])
    for pk in ids:
        # Bounded HTTP call inside a single-row lock prevents competing flushes.
        # A lost HTTP acknowledgement can still duplicate a Slack notification;
        # it can NEVER replay an invitation. Slack webhooks offer no idempotency key.
        with transaction.atomic():
            n = RB2BNotification.objects.select_for_update(of=("self",)).select_related("visitor__lead", "message").get(pk=pk)
            if n.sent_at or (n.next_attempt_at and n.next_attempt_at > timezone.now()):
                continue
            row = n.visitor
            if n.kind == "reply":
                ok = slack.notify_message_received(lead=row.lead, text=n.message.body,
                                                  operator=SENDER, thread_external_id=n.message.thread_external_id)
            else:
                channel = os.getenv("RB2B_SLACK_CHANNEL_ID", "").strip()
                lead = row.lead
                who = " ".join(p for p in (lead.first_name, lead.last_name) if p) if lead else ""
                detail = f"RB2B {OUTCOME_LABELS.get(n.kind, n.kind)}: {who or row.public_identifier}"
                if lead and lead.company_name:
                    detail += f" ({lead.company_name})"
                detail += f"\n{row.linkedin_url} · {SENDER}"
                if n.kind == "sent":
                    detail += f"\nICP: {row.review.get('icp', '')}\nConnection note: {row.note}"
                if n.detail:
                    detail += "\nReason: " + n.detail
                # Plain text only: untrusted source text cannot create Slack mentions.
                detail = detail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                ok = bool(channel) and slack._slack_api("chat.postMessage", {
                    "channel": channel, "text": detail, "unfurl_links": False, "unfurl_media": False,
                }, "rb2b-outcome")
            n.attempts += 1
            if ok:
                n.sent_at = timezone.now()
                delivered += 1
            n.next_attempt_at = timezone.now() + timedelta(minutes=min(60, 2 ** min(n.attempts, 6)))
            n.save(update_fields=["attempts", "sent_at", "next_attempt_at"])
    return delivered


def migrate_discovery():
    """Copy legacy receipts idempotently, retaining original row IDs and fields.

    Run after the v2 receiver is deployed, then once more to catch a request
    already in flight at cutover. No historical discovery row is deleted.
    """
    from copy import deepcopy
    from django.db import connection
    from linkedin.models import LinkedInDiscoveryLead
    profile = owner()
    migrated = []
    for pk, pid in LinkedInDiscoveryLead.objects.filter(
        profile_data__rb2b_intake__isnull=False,
    ).values_list("pk", "public_identifier"):
        with transaction.atomic():
            if connection.vendor == "postgresql":
                lock = int.from_bytes(hashlib.sha256(("rb2b:" + pid.lower()).encode()).digest()[:8], "big", signed=True)
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock])
            old = LinkedInDiscoveryLead.objects.select_for_update().get(pk=pk)
            meta = old.profile_data.get("rb2b_intake", {})
            if not isinstance(meta.get("events"), dict) or meta.get("schema_version") != 1:
                raise RB2BHeld("invalid_legacy_receipts")
            url = canonical_url(old.linkedin_url)
            if url != f"https://www.linkedin.com/in/{pid.lower()}/":
                raise RB2BHeld("legacy_identity_conflict")
            row, _ = RB2BVisitor.objects.select_for_update().get_or_create(
                public_identifier=pid.lower(), defaults={"linkedin_url": url, "linkedin_profile": profile},
            )
            if row.linkedin_profile_id != profile.pk or row.linkedin_url != url:
                raise RB2BHeld("legacy_sender_or_identity_conflict")
            imports = row.review.setdefault("legacy_imports", {})
            prior = imports.get(str(pk), {}).get("events", {})
            for key, event in meta["events"].items():
                normalized = normalize_visitor(event.get("payload"))
                if normalized.get("fingerprint") != key or normalized.get("url") != url:
                    raise RB2BHeld("legacy_payload_conflict")
                existing = row.events.get(key)
                if existing:
                    if existing["payload"] != event["payload"]:
                        raise RB2BHeld("legacy_payload_conflict")
                    previous_count = prior.get(key, {}).get("receipt_count", 0)
                    existing["receipt_count"] += max(0, event["receipt_count"] - previous_count)
                    existing["first_received_at"] = min(existing["first_received_at"], event["first_received_at"])
                    existing["last_received_at"] = max(existing["last_received_at"], event["last_received_at"])
                    if event.get("review") and existing.get("review") not in ({}, event["review"]):
                        raise RB2BHeld("legacy_review_conflict")
                    if event.get("review"):
                        existing["review"] = deepcopy(event["review"])
                    if event.get("review_status") not in {None, "new", "pending"}:
                        existing["review_status"] = event["review_status"]
                else:
                    row.events[key] = deepcopy(event)
                # Any old processing evidence requires review before a new send.
                if event.get("review") or event.get("review_status") not in {None, "new", "pending"}:
                    row.review["legacy_processing_hold"] = True
                    if not row.attempted_at:
                        row.status = "held"
            imports[str(pk)] = {"events": deepcopy(meta["events"]), "discovery_id": pk}
            row.save()
            data = deepcopy(old.profile_data)
            data["rb2b_migrated_to"] = row.pk
            old.profile_data = data
            old.save(update_fields=["profile_data", "updated_at"])
            migrated.append({"discovery_id": pk, "visitor_id": row.pk})
    return migrated


def export_queue():
    return [{"id": row.pk, "status": row.status, "linkedin_url": row.linkedin_url,
             "events": row.events, "review": row.review, "lead_id": row.lead_id,
             "member_urn": row.member_urn, "note": row.note,
             "attempted_at": row.attempted_at.isoformat() if row.attempted_at else None,
             "outcome": row.outcome}
            for row in RB2BVisitor.objects.order_by("created_at")]
