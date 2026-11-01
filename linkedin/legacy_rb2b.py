"""Passive compatibility for historical RB2B receipts; never executes outreach."""
from __future__ import annotations
import hashlib
import os
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from crm.models import Message
from linkedin.models import ActionLog, RB2BNotification, RB2BVisitor
SENDER = "Arian"
OUTCOME_LABELS = {"sent": "connection request sent", "held": "held, not sent",
                  "failed": "failed, not sent", "uncertain": "uncertain; never retry"}

def budget_count(profile_id, since):
    return RB2BVisitor.objects.filter(linkedin_profile_id=profile_id, attempted_at__gte=since).count()

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

