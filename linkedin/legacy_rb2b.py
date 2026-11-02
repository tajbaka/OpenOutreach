"""Passive compatibility for historical RB2B receipts; never executes outreach."""
from __future__ import annotations
import hashlib
import logging
import os
import threading
from datetime import timedelta
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone
from crm.models import Message
from linkedin.models import ActionLog, RB2BNotification, RB2BVisitor
SENDER = "Arian"
logger = logging.getLogger(__name__)
OUTCOME_LABELS = {"sent": "connection request sent", "held": "held, not sent",
                  "failed": "failed, not sent", "uncertain": "uncertain; never retry"}

def budget_count(profile_id, since):
    return RB2BVisitor.objects.filter(linkedin_profile_id=profile_id, attempted_at__gte=since).count()

def _reply_sender(message, receiver=""):
    """The one sender a reply notice belongs to, or "" when not provable."""
    from linkedin.db.messages import linkedin_thread_owners

    if receiver:
        return receiver
    owners = linkedin_thread_owners(message.lead, message.thread_external_id)
    return next(iter(owners)) if len(owners) == 1 else ""


def queue_reply_notifications(lead, message_ids, *, receiver=""):
    """Queue one durable reply notice per new inbound message of a legacy-linked lead.

    `detail` holds the sender that received the reply. A reply whose sender
    cannot be proven is not queued, so the caller's normal notice applies.
    """
    row = RB2BVisitor.objects.filter(lead=lead).first()
    if not row:
        return
    for msg in Message.objects.filter(pk__in=message_ids, lead=lead, direction=Message.Direction.INBOUND):
        sender = _reply_sender(msg, receiver)
        if sender:
            RB2BNotification.objects.get_or_create(key=f"reply:{msg.pk}", defaults={
                "visitor": row, "message": msg, "kind": "reply", "detail": sender,
            })


def owns_reply_notice(message) -> bool:
    """Whether this inbound message's Slack notice is in the durable outbox."""
    return RB2BNotification.objects.filter(key=f"reply:{message.pk}").exists()


def flush_notifications(limit=50):
    from linkedin.notifications import slack
    delivered = 0
    ids = list(RB2BNotification.objects.filter(sent_at__isnull=True).filter(
        Q(key__startswith="visitor:") | Q(key__startswith="reply:", kind="reply")
    ).filter(
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
                # Notices queued before attribution existed fall back to the
                # thread's proven owner; an unproven one carries no sender, so
                # its Reply button cannot queue a reply from any account.
                ok = slack.notify_message_received(lead=row.lead, text=n.message.body,
                                                  operator=n.detail or _reply_sender(n.message),
                                                  thread_external_id=n.message.thread_external_id)
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



def legacy_notices_possible() -> bool:
    """Whether any legacy receipt can still produce a Slack notice.

    Only the retired worker linked visitors to Leads; the integration never
    does. So this is stable from startup.
    """
    return (RB2BVisitor.objects.filter(lead__isnull=False).exists()
            or RB2BNotification.objects.filter(sent_at__isnull=True).filter(
                Q(key__startswith="visitor:") | Q(key__startswith="reply:")).exists())


class NotificationRetryWorker(threading.Thread):
    """Retries pending durable RB2B Slack notices. Never executes outreach."""

    def __init__(self, interval_seconds=60):
        super().__init__(name="legacy-rb2b-notices", daemon=True)
        self.interval_seconds = interval_seconds
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.is_set():
            try:
                close_old_connections()
                flush_notifications()
            except Exception as exc:
                logger.warning("Legacy RB2B notice retry failed: %s", type(exc).__name__)
            finally:
                close_old_connections()
            self.stopped.wait(self.interval_seconds)

    def stop(self):
        self.stopped.set()


class LegacyReceiptHold(ValueError):
    pass


def owner():
    from linkedin.models import LinkedInProfile
    from linkedin.operators import resolve_operator
    profiles = list(LinkedInProfile.objects.filter(user__username="ariantajbakh", active=True))
    if len(profiles) != 1 or resolve_operator(profiles[0].linkedin_username) != SENDER:
        raise LegacyReceiptHold("arian_owner_not_unique")
    return profiles[0]


def canonical_url(url):
    from api.rb2b_webhook import normalize_visitor
    return normalize_visitor({"LinkedIn URL": url, "Seen At": "identity-check"})["url"]


def migrate_discovery():
    """Copy legacy receipts idempotently, retaining original row IDs and fields.

    Run after the v2 receiver is deployed, then once more to catch a request
    already in flight at cutover. No historical discovery row is deleted.
    """
    from copy import deepcopy
    from api.rb2b_webhook import normalize_visitor
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
                raise LegacyReceiptHold("invalid_legacy_receipts")
            url = canonical_url(old.linkedin_url)
            if url != f"https://www.linkedin.com/in/{pid.lower()}/":
                raise LegacyReceiptHold("legacy_identity_conflict")
            row, _ = RB2BVisitor.objects.select_for_update().get_or_create(
                public_identifier=pid.lower(), defaults={"linkedin_url": url, "linkedin_profile": profile},
            )
            if row.linkedin_profile_id != profile.pk or row.linkedin_url != url:
                raise LegacyReceiptHold("legacy_sender_or_identity_conflict")
            imports = row.review.setdefault("legacy_imports", {})
            prior = imports.get(str(pk), {}).get("events", {})
            for key, event in meta["events"].items():
                normalized = normalize_visitor(event.get("payload"))
                if normalized.get("fingerprint") != key or normalized.get("url") != url:
                    raise LegacyReceiptHold("legacy_payload_conflict")
                existing = row.events.get(key)
                if existing:
                    if existing["payload"] != event["payload"]:
                        raise LegacyReceiptHold("legacy_payload_conflict")
                    previous_count = prior.get(key, {}).get("receipt_count", 0)
                    existing["receipt_count"] += max(0, event["receipt_count"] - previous_count)
                    existing["first_received_at"] = min(existing["first_received_at"], event["first_received_at"])
                    existing["last_received_at"] = max(existing["last_received_at"], event["last_received_at"])
                    if event.get("review") and existing.get("review") not in ({}, event["review"]):
                        raise LegacyReceiptHold("legacy_review_conflict")
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

