"""Reviewed RB2B intake -> CRM, with no campaign enrollment or browser driver.

The Windows Codex reviewer supplies sourced decisions and uses IAB. These
commands enforce storage, identity, concurrency, budget and receipt boundaries.
"""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import os
import re
import uuid
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from api.rb2b_webhook import normalize_visitor
from crm.models import Lead, Message, Deal, Meeting, OpportunityContact
from linkedin import conf
from linkedin.models import (ActionLog, Campaign, DaemonHeartbeat, LinkedInProfile,
                             RB2BVisitor, RB2BNotification, Task)
from linkedin.operators import resolve_operator
from linkedin.suppression import lead_suppression_match

# Canonical-URL digests of two privately verified historical invitations.
# Keep their profile details and original receipts out of this public repo.
NO_RESEND = {
    "33943bcf4b8673e3aeb175435950487148b06a49ebe57573793195f7d8ce49ed",
    "6774bcd0e03da3e40b8994ae054b8ecc5f56393d972229c6ea390cc71b303652",
}
PROTOCOL = 1


class RB2BHeld(ValueError):
    """A stable, non-sensitive reason the requested transition cannot happen."""


def owner():
    profiles = list(LinkedInProfile.objects.filter(user__username="ariantajbakh", active=True))
    if len(profiles) != 1 or resolve_operator(profiles[0].linkedin_username) != "Arian":
        raise RB2BHeld("arian_owner_not_unique")
    return profiles[0]


def canonical_url(url):
    return normalize_visitor({"LinkedIn URL": url, "Seen At": "identity-check"})["url"]


def fresh(evidence, *, minutes=5):
    at = parse_datetime(str(evidence.get("checked_at", "")))
    if not at or timezone.is_naive(at) or not timedelta(0) <= timezone.now() - at <= timedelta(minutes=minutes):
        raise RB2BHeld("fresh_dated_evidence_required")


def _validated_identity(row, decision):
    if canonical_url(decision.get("linkedin_url", "")) != row.linkedin_url:
        raise RB2BHeld("identity_mismatch")
    urn = str(decision.get("member_urn", ""))
    if not re.fullmatch(r"urn:li:(?:fsd_profile|fs_miniProfile|member):[A-Za-z0-9_-]+", urn):
        raise RB2BHeld("verified_member_urn_required")
    for key in ("identity_verified", "fit_verified", "relationships_checked", "invitations_checked"):
        if decision.get(key) is not True:
            raise RB2BHeld(key + "_required")
    sources = decision.get("evidence_urls")
    if (not isinstance(sources, list) or not sources or not all(isinstance(s, str) and s.startswith("https://") for s in sources)
            or not isinstance(decision.get("fit_reason"), str) or not decision["fit_reason"].strip()):
        raise RB2BHeld("sourced_fit_evidence_required")
    fresh(decision, minutes=60)
    return urn


def _identity_matches(row, decision, urn):
    q = Q(public_identifier__iexact=row.public_identifier) | Q(linkedin_url__iexact=row.linkedin_url)
    q |= Q(linkedin_url__iexact=row.linkedin_url.rstrip("/")) | Q(description__contains=urn)
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
    if row.attempted_at or row.status in {"sent", "uncertain", "failed", "attempting"}:
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
    urn = _validated_identity(row, decision)
    note = str(decision.get("note", "")).strip()
    icp = str(decision.get("icp", "")).strip()
    if not note or len(note) > 200 or not icp or len(icp) > 160:
        raise RB2BHeld("explicit_icp_and_note_under_201_characters_required")
    # Resolve only a known audience; never use resolve_icp's writing fallback.
    from linkedin.icp_outbound import load_icp_messages
    from linkedin.models import MessageProgramVersion
    known = icp in load_icp_messages("Arian")
    if not known:
        # Published programs are reviewed separately; require the exact version
        # and audience supplied by the reviewer, without creating enrollment.
        version = MessageProgramVersion.objects.filter(pk=decision.get("message_version_id")).first()
        from linkedin.message_delivery import index_message_version
        index = index_message_version(version) if version else None
        known = bool(index and icp in index.audience_keys and index.effective_routes(
            audience_key=icp, operator="Arian", channel="linkedin_connect"))
    if not known:
        raise RB2BHeld("unknown_audience")
    lead = _identity_matches(row, decision, urn)
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
        lead.description = json.dumps({"source": "rb2b", "urn": urn, "headline": decision.get("title", "")})
        lead.full_clean()
        lead.save()
    # Existing CRM fields are operator-owned. Exact association belongs here.
    row.lead, row.member_urn, row.note, row.status = lead, urn, note, "ready"
    row.review = {**row.review, "decision": decision, "icp": icp}
    row.save()
    return row


def budget_count(profile_id, since):
    # All reserved attempts count, including failures/uncertainty. No optimistic
    # refunds and no synthetic Campaign/Deal/ActionLog are necessary.
    return RB2BVisitor.objects.filter(linkedin_profile_id=profile_id, attempted_at__gte=since).count()


def coordination_config():
    keys = ("ENABLE_CONNECT", "ENABLE_ACTIVE_HOURS", "ACTIVE_START_HOUR", "ACTIVE_END_HOUR",
            "ACTIVE_TIMEZONE", "REST_DAYS", "CONNECT_DAILY_LIMIT", "CONNECT_WEEKLY_LIMIT",
            "FOLLOW_UP_DAILY_LIMIT", "MAX_TOTAL_DAILY_ACTIONS", "ENABLE_PACING_CATCH_UP",
            "ENABLE_REALTIME_LISTENER", "ENABLE_LINKEDIN_DAEMON")
    return {"protocol": PROTOCOL, "config": {k: getattr(conf, k) for k in keys},
            "min_action_interval": conf.CAMPAIGN_CONFIG["min_action_interval"]}


def worker_ready():
    beat = DaemonHeartbeat.objects.filter(sender="Arian").first()
    if not conf.ENABLE_REALTIME_LISTENER or not conf.ENABLE_LINKEDIN_DAEMON:
        raise RB2BHeld("reply_listener_and_linkedin_worker_required")
    expected = json.loads(json.dumps(coordination_config()))
    if not beat or beat.rb2b_coordination != expected or not beat.last_alive or (
        timezone.now() - beat.last_alive > timedelta(minutes=5)
    ):
        raise RB2BHeld("updated_arian_worker_and_matching_config_required")


def task_profile(task):
    if task.task_type not in Task.linked_account_scoped_task_types():
        return None
    campaign_id = task.payload.get("campaign_id")
    if campaign_id:
        return LinkedInProfile.objects.filter(user__campaigns__pk=campaign_id).first()
    operator = task.payload.get("operator")
    matches = [p for p in LinkedInProfile.objects.all() if resolve_operator(p.linkedin_username) == operator]
    return matches[0] if len(matches) == 1 else None


def sender_blocked(profile_id, *, pacing=False):
    rows = RB2BVisitor.objects.filter(linkedin_profile_id=profile_id)
    if rows.filter(status="attempting").exists():
        return True
    return pacing and rows.filter(cooldown_until__gt=timezone.now()).exists()


@transaction.atomic
def begin(visitor_id, evidence):
    # Profile first, then visitor/Lead: the Task claimer uses the same sender
    # lock, making claim vs IAB reservation mutually exclusive across machines.
    profile = LinkedInProfile.objects.select_for_update().get(pk=owner().pk)
    row = RB2BVisitor.objects.select_for_update(of=("self",)).select_related("lead").get(pk=visitor_id)
    if row.linkedin_profile_id != profile.pk or row.status != "ready" or row.attempted_at:
        raise RB2BHeld("not_ready_or_already_attempted")
    if os.getenv("RB2B_IAB_SENDS_ENABLED", "").lower() != "true":
        raise RB2BHeld("rb2b_iab_sends_not_enabled")
    worker_ready()
    if not os.getenv("RB2B_SLACK_CHANNEL_ID", "") or not conf.SLACK_BOT_TOKEN or not conf.SLACK_REPLIES_WEBHOOK_URL:
        raise RB2BHeld("slack_outcome_and_reply_channels_required")
    fresh(evidence)
    for key in ("sender_verified", "identity_verified", "no_pending_invitation", "not_connected", "no_conversation", "relationships_checked", "no_platform_restriction"):
        if evidence.get(key) is not True:
            raise RB2BHeld(key + "_required")
    if evidence.get("sender") != "Arian" or canonical_url(evidence.get("linkedin_url", "")) != row.linkedin_url:
        raise RB2BHeld("live_identity_mismatch")
    local = timezone.localtime(timezone=ZoneInfo(conf.ACTIVE_TIMEZONE))
    if (not conf.ENABLE_CONNECT or profile.stop_requested or not profile.active
            or local.weekday() in conf.REST_DAYS or (conf.ENABLE_ACTIVE_HOURS and not
                conf.ACTIVE_START_HOUR <= local.hour < conf.ACTIVE_END_HOUR)):
        raise RB2BHeld("sender_stopped_or_outside_hours")
    if sender_blocked(profile.pk, pacing=True):
        raise RB2BHeld("sender_reserved_or_pacing")
    if not profile.can_execute("connect"):
        raise RB2BHeld("shared_sender_limit")
    campaign_ids = list(Campaign.objects.filter(user=profile.user).values_list("pk", flat=True))
    if Task.objects.owned_linkedin_by("Arian", campaign_ids).filter(status=Task.Status.RUNNING).exists():
        raise RB2BHeld("sender_worker_busy")
    # Independent supervised reposts are not Tasks.
    from linkedin.models import LinkedInRepost
    if LinkedInRepost.objects.filter(operator="Arian", status="running").exists():
        raise RB2BHeld("sender_repost_busy")
    Lead.objects.select_for_update().get(pk=row.lead_id)
    row.lead.refresh_from_db()
    reason = exclusion(row.lead, row)
    if reason:
        raise RB2BHeld(reason)
    from linkedin.tasks.connect import recommended_action_delay
    delay = recommended_action_delay(profile, "connect")
    latest = ActionLog.objects.filter(linkedin_profile=profile).order_by("-created_at").first()
    if latest and latest.created_at + timedelta(seconds=delay) > timezone.now():
        raise RB2BHeld("shared_sender_pacing")
    row.status, row.attempt_token, row.attempted_at = "attempting", uuid.uuid4(), timezone.now()
    row.cooldown_until = row.attempted_at + timedelta(seconds=delay)
    row.outcome = {"preflight": evidence}
    row.save(update_fields=["status", "attempt_token", "attempted_at", "cooldown_until", "outcome", "updated_at"])
    return row


@transaction.atomic
def finish(visitor_id, token, evidence):
    row = RB2BVisitor.objects.select_for_update().get(pk=visitor_id)
    if str(row.attempt_token) != str(token) or not row.attempt_token:
        raise RB2BHeld("attempt_token_mismatch")
    if row.status in {"sent", "failed", "uncertain"}:
        if row.outcome.get("result") == evidence:
            return row
        raise RB2BHeld("terminal_receipt_requires_manual_review")
    if row.status != "attempting":
        raise RB2BHeld("no_attempt_to_finish")
    status = evidence.get("status")
    if status not in {"sent", "failed", "uncertain"}:
        raise RB2BHeld("invalid_outcome")
    if status == "sent":
        fresh(evidence, minutes=60)
        if (evidence.get("sender") != "Arian" or evidence.get("note") != row.note
                or canonical_url(evidence.get("linkedin_url", "")) != row.linkedin_url
                or evidence.get("sent_invitations_verified") is not True or not evidence.get("screenshot")):
            raise RB2BHeld("independent_sent_invitation_receipt_required")
        row.confirmed_at = timezone.now()
    if status == "failed" and evidence.get("send_clicked") is not False:
        raise RB2BHeld("possible_send_must_be_uncertain")
    row.status = status
    row.outcome = {**row.outcome, "result": evidence}
    row.save(update_fields=["status", "confirmed_at", "outcome", "updated_at"])
    _notify(row, status, str(evidence.get("reason", ""))[:500])
    return row


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
                                                  operator="Arian", thread_external_id=n.message.thread_external_id)
            else:
                channel = os.getenv("RB2B_SLACK_CHANNEL_ID", "").strip()
                detail = f"RB2B {n.kind}: {row.linkedin_url} (Arian)"
                if n.kind == "sent":
                    detail += f"\nCompany: {row.lead.company_name}\nICP: {row.review.get('icp', '')}\nConnection note: {row.note}"
                elif n.detail:
                    detail += "\n" + n.detail
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


@transaction.atomic
def ingest_replies(visitor_id, evidence):
    """Import an IAB-observed thread after downtime; never fetch browser data here."""
    row = RB2BVisitor.objects.select_for_update(of=("self",)).select_related("lead").get(pk=visitor_id)
    fresh(evidence, minutes=60)
    if not row.lead_id or evidence.get("sender") != "Arian" or canonical_url(evidence.get("linkedin_url", "")) != row.linkedin_url:
        raise RB2BHeld("reply_identity_mismatch")
    thread = str(evidence.get("thread_external_id", ""))
    if not thread or evidence.get("identity_verified") is not True:
        raise RB2BHeld("verified_thread_identity_required")
    if Message.objects.filter(source="linkedin", thread_external_id=thread).exclude(lead=row.lead).exists():
        raise RB2BHeld("thread_identity_conflict")
    from linkedin.db.messages import persist_thread
    parsed = evidence.get("messages", [])
    # Require stable message identity + exact timestamp; never invent a timestamp
    # or text-hash identity that could duplicate a realtime event.
    if not isinstance(parsed, list):
        raise RB2BHeld("messages_must_be_a_list")
    for msg in parsed:
        if not str(msg.get("entity_urn", "")).startswith("urn:li:") or not msg.get("timestamp") or not msg.get("sender"):
            raise RB2BHeld("stable_message_identity_required")
        from datetime import datetime
        try:
            datetime.strptime(msg["timestamp"], "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            raise RB2BHeld("exact_utc_message_timestamp_required") from None
        existing = Message.objects.filter(source="linkedin", external_id=msg["entity_urn"]).first()
        if existing and (existing.lead_id != row.lead_id or existing.body != msg.get("text", "")):
            raise RB2BHeld("message_identity_conflict")
    count = persist_thread(lead=row.lead, parsed=parsed, thread_external_id=thread, outbound_senders={"Arian"})
    row.review = {**row.review, "last_reply_scan": evidence["checked_at"]}
    row.save(update_fields=["review", "updated_at"])
    return count


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
