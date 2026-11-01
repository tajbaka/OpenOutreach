"""Durable completion notices for opt-in campaign enrollments.

This service reads ordinary campaign receipts; it never executes, retries or
changes outreach. Callback state is separate from Task and delivery state.
"""
from datetime import timedelta
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from django.db import close_old_connections, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from linkedin import conf
from linkedin.models import CampaignSubmission, OutboundDelivery

logger = logging.getLogger(__name__)


def validate_callback_url(url):
    parsed = urlsplit(url)
    allowed = {h.strip().lower() for h in conf.OUTREACH_CALLBACK_ALLOWED_HOSTS.split(",") if h.strip()}
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("invalid_callback_port") from None
    if (len(url) > 1000 or any(ord(c) <= 32 or ord(c) > 126 for c in url)
            or parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in allowed
            or parsed.username is not None or parsed.password is not None or port not in {None, 443}
            or parsed.fragment or parsed.query or len(conf.OUTREACH_CALLBACK_SECRET) < 32):
        raise ValueError("invalid_callback_destination_or_configuration")
    return parsed


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Resolve once, validate every returned address, and connect to that IP.
        # No proxy, redirect, private-address delivery or second DNS resolution.
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("non_public_callback_destination")
        sock = socket.create_connection((addresses[0][4][0], self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def post(url, payload):
    parsed = validate_callback_url(url)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(conf.OUTREACH_CALLBACK_SECRET.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    connection = PublicHTTPSConnection(parsed.hostname, 443, timeout=10, context=ssl.create_default_context())
    try:
        connection.request("POST", parsed.path or "/", body=body, headers={
            "Content-Type": "application/json", "X-OpenOutreach-Signature": f"t={stamp},v1={signature}"})
        response = connection.getresponse()
        return 200 <= response.status < 300  # Redirects are never followed.
    finally:
        connection.close()


def reconcile(*, operator, limit=50, source=None, include_local=False):
    from linkedin.campaign_integrations import status
    if not conf.ENABLE_CAMPAIGN_INTEGRATIONS:
        return 0
    # Limit completed candidates, not the first N enrollments: a backlog waiting
    # for connection slots must not starve newer completed receipts.
    terminal_delivery = OutboundDelivery.objects.filter(
        enrollment_id=OuterRef("enrollment_id"), channel="linkedin_connect", step_index=0,
    ).filter(Q(status__in=[OutboundDelivery.Status.UNCLEAR, OutboundDelivery.Status.STOPPED])
             | Q(status=OutboundDelivery.Status.SENT, enrollment__deal__connected_at__isnull=False))
    from linkedin.enums import ProfileState
    candidates = CampaignSubmission.objects.filter(enrollment__operator=operator, callback_ready_at__isnull=True)
    if source is not None:
        candidates = candidates.filter(source=source)
    if not include_local:
        candidates = candidates.exclude(callback_url="")
    ids = list(candidates.alias(_terminal=Exists(terminal_delivery)).filter(
                   Q(_terminal=True) | Q(enrollment__deal__state=ProfileState.FAILED)
                   | (~Q(enrollment__deal__closing_reason="") & Q(enrollment__deal__closing_reason__isnull=False))
                   | Q(enrollment__deal__invitation_sent_at__isnull=False,
                       enrollment__deal__invitation_sender=F("enrollment__operator")))
               .order_by("pk").values_list("pk", flat=True)[:limit])
    count = 0
    for pk in ids:
        with transaction.atomic():
            row = CampaignSubmission.objects.select_for_update(of=("self",)).select_related("enrollment__deal__lead").get(pk=pk)
            result = status(row)
            if row.callback_ready_at or result["status"] == "enrolled":
                continue
            lead = row.enrollment.deal.lead
            row.callback_payload = {
                **result, "event_type": "campaign.connection_completed", "event_id": str(row.event_id),
                "outcome": result["status"], "contact": {key: getattr(lead, key) for key in
                    ("linkedin_url", "first_name", "last_name", "company_name")},
            }
            row.callback_ready_at = timezone.now()
            row.save(update_fields=["callback_payload", "callback_ready_at"])
            count += 1
    return count


def local_outcomes(*, operator, source, limit=50):
    """Freeze/export normal completion events for a trusted local integration.

    Empty callback destinations are explicit local transport. The same UUID,
    payload freeze and durable receipt are used; this never executes outreach.
    """
    if not conf.ENABLE_CAMPAIGN_INTEGRATIONS:
        raise ValueError("campaign_integrations_disabled")
    if not source or not operator:
        raise ValueError("source_and_operator_required")
    reconcile(operator=operator, source=source, include_local=True, limit=limit)
    return list(CampaignSubmission.objects.filter(
        source=source, enrollment__operator=operator, callback_url="",
        callback_ready_at__isnull=False, callback_delivered_at__isnull=True,
    ).order_by("pk").values_list("callback_payload", flat=True)[:limit])


@transaction.atomic
def acknowledge_local_outcomes(*, operator, source, receipts):
    """Acknowledge a durable integration handoff, never an invitation send."""
    if not conf.ENABLE_CAMPAIGN_INTEGRATIONS:
        raise ValueError("campaign_integrations_disabled")
    if not source or not operator or not isinstance(receipts, list) or len(receipts) > 50:
        raise ValueError("invalid_local_outcome_receipts")
    seen = set()
    rows = []
    for receipt in receipts:
        if not isinstance(receipt, dict) or type(receipt.get("submission_id")) is not int:
            raise ValueError("invalid_local_outcome_identity")
        pk = receipt["submission_id"]
        if pk in seen:
            raise ValueError("duplicate_local_outcome_identity")
        seen.add(pk)
        row = CampaignSubmission.objects.select_for_update().filter(
            pk=pk, source=source, enrollment__operator=operator, callback_url="",
            callback_ready_at__isnull=False,
        ).first()
        if row is None or str(row.event_id) != receipt.get("event_id"):
            raise ValueError("local_outcome_identity_mismatch")
        rows.append(row)
    acknowledged = 0
    for row in rows:
        if row.callback_delivered_at is None:
            row.callback_delivered_at = timezone.now()
            row.save(update_fields=["callback_delivered_at"])
            acknowledged += 1
    return acknowledged


def deliver(*, operator, limit=50):
    if not conf.ENABLE_CAMPAIGN_INTEGRATIONS:
        return 0
    now = timezone.now()
    ids = list(CampaignSubmission.objects.filter(enrollment__operator=operator).exclude(callback_url="").filter(
        callback_ready_at__isnull=False, callback_delivered_at__isnull=True,
        callback_abandoned_at__isnull=True).filter(Q(callback_next_at__isnull=True) | Q(callback_next_at__lte=now))
        .order_by("pk").values_list("pk", flat=True)[:limit])
    delivered = 0
    for pk in ids:
        with transaction.atomic():
            row = CampaignSubmission.objects.select_for_update().get(pk=pk)
            if row.callback_delivered_at or row.callback_abandoned_at or (row.callback_next_at and row.callback_next_at > now):
                continue
            if now - row.callback_ready_at >= timedelta(hours=48):
                row.callback_abandoned_at = now
                row.callback_error = "delivery_window_expired"
                row.save(update_fields=["callback_abandoned_at", "callback_error"])
                continue
            row.callback_attempts += 1
            attempt = row.callback_attempts
            row.callback_next_at = now + timedelta(minutes=5)  # Crash-safe lease.
            row.save(update_fields=["callback_attempts", "callback_next_at"])
            url, payload = row.callback_url, row.callback_payload
        ok, error = False, "http_failure"
        try:
            ok = post(url, payload)
        except Exception as exc:
            error = type(exc).__name__  # No destination, tokens or payload in logs.
        with transaction.atomic():
            row = CampaignSubmission.objects.select_for_update().get(pk=pk)
            if row.callback_attempts != attempt or row.callback_delivered_at:
                continue
            row.callback_error = "" if ok else error
            row.callback_delivered_at = timezone.now() if ok else None
            row.callback_next_at = None if ok else timezone.now() + timedelta(seconds=min(3600, 30 * 2 ** min(attempt, 7)))
            row.save(update_fields=["callback_error", "callback_delivered_at", "callback_next_at"])
            delivered += int(ok)
    return delivered


class CampaignCallbackWorker(threading.Thread):
    def __init__(self, operator):
        super().__init__(name="campaign-callbacks", daemon=True)
        self.operator = operator
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.is_set():
            try:
                close_old_connections()
                reconcile(operator=self.operator)
                deliver(operator=self.operator)
            except Exception as exc:
                logger.warning("Campaign callback cycle failed: %s", type(exc).__name__)
            finally:
                close_old_connections()
            self.stopped.wait(15)

    def stop(self):
        self.stopped.set()
