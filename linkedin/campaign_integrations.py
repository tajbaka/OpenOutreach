"""Preview-first external enrollment into existing versioned campaigns.

No send implementation, new Task type, note override or automatic resubmission.
Caller references identify immutable enrollments; ordinary campaign workers own
the connection, acceptance and sequence from that point onward.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from django.db import IntegrityError, transaction
from django.db.models import Q

from crm.models import Account, Deal, Lead, Message, OpportunityContact
from linkedin import conf
from linkedin.campaign_setup import exact_campaign_owner
from linkedin.enums import ProfileState
from linkedin.message_delivery import ensure_message_enrollment, get_or_create_delivery, index_message_version
from linkedin.models import Campaign, CampaignSubmission, OutboundDelivery, Task
from linkedin.operators import resolve_operator
from linkedin.suppression import lead_suppression_match, normalize_company_name, normalize_domain


class EnrollmentHold(ValueError):
    pass


def _enabled():
    if not conf.ENABLE_CAMPAIGN_INTEGRATIONS:
        raise EnrollmentHold("campaign_integrations_disabled")


def normalized_input(body):
    if not isinstance(body, dict):
        raise EnrollmentHold("object_required")
    allowed = {"source", "reference_id", "campaign_id", "owner_username", "message_version_id",
               "linkedin_url", "first_name", "last_name", "company_name", "email", "icp", "role_tag", "callback_url"}
    if set(body) - allowed:
        raise EnrollmentHold("unsupported_fields")
    result = {}
    for key in allowed - {"campaign_id", "message_version_id"}:
        value = body.get(key, "")
        if not isinstance(value, str):
            raise EnrollmentHold("text_required:" + key)
        result[key] = value.strip()
    for key in ("source", "reference_id", "owner_username", "first_name", "last_name", "company_name", "icp"):
        if not result[key]:
            raise EnrollmentHold("required:" + key)
    for key in ("campaign_id", "message_version_id"):
        value = body.get(key)
        if type(value) is not int or value <= 0:
            raise EnrollmentHold("positive_integer_required:" + key)
        result[key] = value
    if len(result["source"]) > 80 or len(result["reference_id"]) > 200:
        raise EnrollmentHold("reference_too_long")
    parsed = urlsplit(result["linkedin_url"])
    if (parsed.scheme != "https" or parsed.netloc.lower() not in {"linkedin.com", "www.linkedin.com"}
            or parsed.query or parsed.fragment or not re.fullmatch(r"/in/[A-Za-z0-9_-]+/?", parsed.path)):
        raise EnrollmentHold("exact_linkedin_vanity_url_required")
    result["public_identifier"] = parsed.path.strip("/").split("/")[1].lower()
    result["linkedin_url"] = "https://www.linkedin.com/in/" + result["public_identifier"] + "/"
    if result["callback_url"]:
        from linkedin.campaign_callbacks import validate_callback_url
        validate_callback_url(result["callback_url"])
    return result


def _digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _campaign(body, *, lock=False):
    query = Campaign.objects.select_related("user", "active_message_version__program")
    if lock:
        query = query.select_for_update(of=("self",))
    campaign = query.filter(pk=body["campaign_id"], user__username=body["owner_username"]).first()
    if campaign is None:
        raise EnrollmentHold("campaign_owner_mismatch")
    if campaign.status == Campaign.Status.FINISHED or campaign.is_freemium:
        raise EnrollmentHold("campaign_unavailable")
    owner = exact_campaign_owner(body["owner_username"])
    campaign._integration_operator = resolve_operator(owner.linkedin_profile.linkedin_username)
    if campaign.active_message_version_id != body["message_version_id"]:
        raise EnrollmentHold("message_version_changed")
    audience = index_message_version(campaign.active_message_version).resolve_audience(body["icp"])
    if audience != body["icp"]:
        raise EnrollmentHold("exact_audience_key_required")
    return campaign


def _identity(body, *, validate_database=True):
    # Older imports can retain locale hosts, http, tracking queries or no slash.
    # These are aliases of the exact public ID, never a name/company match.
    url_pattern = (r"^https?://([a-z0-9-]+\.)?linkedin\.com/in/" + re.escape(body["public_identifier"])
                   + r"/?([?#].*)?$")
    matches = list(Lead.objects.filter(
        Q(public_identifier__iexact=body["public_identifier"])
        | Q(linkedin_url__iregex=url_pattern)
    ).order_by("pk")[:2])
    if len(matches) > 1:
        raise EnrollmentHold("identity_conflict")
    lead = matches[0] if matches else Lead()
    for field in ("first_name", "last_name", "company_name", "email", "icp", "role_tag"):
        value = body[field]
        existing = getattr(lead, field)
        if existing and value and existing.strip().casefold() != value.casefold():
            raise EnrollmentHold("existing_identity_or_classification_conflict:" + field)
        if not existing:
            setattr(lead, field, value)
    if lead.public_identifier and lead.public_identifier.lower() != body["public_identifier"]:
        raise EnrollmentHold("identity_conflict")
    lead.public_identifier = body["public_identifier"]
    lead.linkedin_url = body["linkedin_url"]
    lead.full_clean(validate_unique=validate_database, validate_constraints=validate_database)
    return lead


def _exclusions(lead):
    if lead.disqualified or lead_suppression_match(lead):
        raise EnrollmentHold("suppressed_or_disqualified")
    domain = normalize_domain(lead.email)
    for account in Account.objects.all().only("name", "domain", "normalized_name"):
        if ((domain and account.domain and domain == account.domain)
                or normalize_company_name(account.name) == normalize_company_name(lead.company_name)):
            raise EnrollmentHold("existing_account_relationship")
    if lead.email and Lead.objects.filter(email__iexact=lead.email).exclude(pk=lead.pk).exists():
        raise EnrollmentHold("email_identity_conflict")
    if lead.pk:
        from drip.models import DripEnrollment
        from crm.models import Meeting, MeetingParticipant
        if Deal.objects.filter(lead=lead).exists() or DripEnrollment.objects.filter(lead=lead).exists():
            raise EnrollmentHold("existing_campaign_history")
        if (Message.objects.filter(lead=lead).exists() or Meeting.objects.filter(lead=lead).exists()
                or MeetingParticipant.objects.filter(lead=lead).exists() or OpportunityContact.objects.filter(lead=lead).exists()):
            raise EnrollmentHold("existing_conversation_or_opportunity")
    person = Q(payload__public_id=lead.public_identifier)
    if lead.pk:
        person |= Q(payload__lead_id=lead.pk) | Q(payload__lead_id=str(lead.pk))
    if Task.objects.filter(status__in=["pending", "running"]).filter(person).exists():
        raise EnrollmentHold("pending_person_work")


def status(row):
    enrollment = row.enrollment
    deal = enrollment.deal
    delivery = OutboundDelivery.objects.filter(enrollment=enrollment, channel="linkedin_connect", step_index=0).first()
    outcome, reason = "enrolled", ""
    if deal.invitation_sent_at and deal.invitation_sender == enrollment.operator:
        outcome = "sent"
    elif delivery and delivery.status == OutboundDelivery.Status.UNCLEAR:
        outcome, reason = "uncertain", "delivery_unclear"
    elif deal.state == ProfileState.FAILED or deal.closing_reason:
        outcome, reason = "failed", "campaign_stopped"
    elif deal.connected_at and delivery and delivery.status == OutboundDelivery.Status.SENT:
        outcome, reason = "held", "already_connected"
    elif delivery and delivery.status == OutboundDelivery.Status.STOPPED:
        outcome, reason = "held", "delivery_stopped"
    return {"submission_id": row.pk, "enrollment_id": enrollment.pk, "deal_id": deal.pk,
            "campaign_id": deal.campaign_id, "message_version_id": enrollment.message_version_id,
            "source": row.source, "reference_id": row.reference_id, "status": outcome, "reason": reason,
            "note": delivery.frozen_body if delivery else "", "delivery_id": delivery.pk if delivery else None}


def _existing(body, digest):
    row = CampaignSubmission.objects.select_related("enrollment__deal__lead").filter(
        source=body["source"], reference_id=body["reference_id"]).first()
    if row and row.input_hash != digest:
        raise EnrollmentHold("reference_payload_conflict")
    return status(row) if row else None


def submit(body, *, apply=False):
    body = normalized_input(body)
    digest = _digest(body)
    existing = _existing(body, digest)
    if existing:
        return existing
    if apply:
        _enabled()
    campaign = _campaign(body)
    lead = _identity(body, validate_database=not apply)
    index = index_message_version(campaign.active_message_version)
    routes = index.effective_routes(audience_key=body["icp"], operator=campaign._integration_operator)
    if not any(group[0].channel == "linkedin_connect" for group in routes):
        raise EnrollmentHold("connection_route_required")
    if not apply:
        _exclusions(lead)
        return {"status": "eligible", "campaign_id": campaign.pk, "message_version_id": campaign.active_message_version_id,
                "source": body["source"], "reference_id": body["reference_id"],
                "connection_priority": campaign.connection_priority, "campaign_status": campaign.status}
    try:
        with transaction.atomic():
            # Lock the canonical person before campaign/enrollment rows, matching
            # normal campaign/drip ownership lock order. URL uniqueness handles
            # simultaneous creation; a second reference never creates a second Deal.
            if not lead.pk:
                lead, _ = Lead.objects.get_or_create(linkedin_url=body["linkedin_url"], defaults={
                    key: getattr(lead, key) for key in ("public_identifier", "first_name", "last_name", "company_name", "email", "icp", "role_tag")})
            Lead.objects.select_for_update().get(pk=lead.pk)
            existing = _existing(body, digest)
            if existing:
                return existing
            lead = _identity(body)
            _exclusions(lead)
            campaign = _campaign(body, lock=True)
            lead.save(update_fields=["first_name", "last_name", "company_name", "email", "icp", "role_tag", "public_identifier", "linkedin_url"])
            deal = Deal.objects.create(lead=lead, campaign=campaign, state=ProfileState.READY_TO_CONNECT)
            enrollment = ensure_message_enrollment(deal=deal, audience_key=body["icp"], operator=campaign._integration_operator)
            get_or_create_delivery(enrollment=enrollment, channel="linkedin_connect", step_index=0)
            row = CampaignSubmission.objects.create(source=body["source"], reference_id=body["reference_id"],
                input_hash=digest, enrollment=enrollment, callback_url=body["callback_url"])
            seeds = list(campaign.seed_public_ids or [])
            if lead.public_identifier not in seeds:
                campaign.seed_public_ids = seeds + [lead.public_identifier]
                campaign.save(update_fields=["seed_public_ids"])
            # Reuse the normal campaign task and pacing; never create a second
            # connect Task or change an existing task's date.
            if campaign.status == Campaign.Status.ACTIVE:
                from linkedin.daemon import _ensure_connect_task_for_campaign
                _ensure_connect_task_for_campaign(campaign)
            return status(row)
    except IntegrityError:
        existing = _existing(body, digest)
        if existing:
            return existing
        raise EnrollmentHold("concurrent_identity_or_enrollment_conflict") from None
