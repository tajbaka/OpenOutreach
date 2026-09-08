"""Immutable message-program enrollment, selection, rendering, and delivery.

Published ``MessageProgramVersion`` rows are the only copy source in this
module.  A Deal that is enrolled in a version never consults the legacy JSON
template stores, and neither a later Campaign version change nor mutable Lead
data can change a delivery after it has been materialized.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from string import Formatter

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from linkedin import conf
from linkedin.message_roles import role_wording
from linkedin.models import (
    Campaign,
    CampaignMessageEnrollment,
    MessageProgramVersion,
    OutboundDelivery,
)
from linkedin.operators import CANONICAL_OPERATOR_HANDLES, resolve_operator


SUPPORTED_SCHEMA_VERSION = 1
SUPPORTED_PLACEHOLDERS = frozenset({
    "role",
    "first_name",
    "last_name",
    "company_name",
    "my_name",
    "our_company_name",
    "our_website_url",
})
SUPPORTED_CHANNELS = (
    OutboundDelivery.Channel.LINKEDIN_CONNECT,
    OutboundDelivery.Channel.LINKEDIN_FOLLOWUP,
    OutboundDelivery.Channel.GMAIL,
)
_CHANNEL_ORDER = {channel: index for index, channel in enumerate(SUPPORTED_CHANNELS)}
_KEY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,159})$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_FORMATTER = Formatter()
_TOP_LEVEL_FIELDS = frozenset({"program_key", "program_name", "messages"})
_MESSAGE_FIELDS = frozenset({
    "audience_key",
    "role_persona",
    "fedramp_segment",
    "channel",
    "step_key",
    "step_index",
    "delay_hours",
    "variant_key",
    "subject",
    "body",
    "media",
    "sender_override",
    "notes",
})


class MessageDeliveryError(Exception):
    """A published message route cannot be used safely."""


@dataclass(frozen=True)
class PublishedMessage:
    audience_key: str
    role_persona: str
    fedramp_segment: str
    channel: str
    step_key: str
    step_index: int
    delay_hours: int | float
    variant_key: str
    subject: str
    body: str
    media: tuple[str, ...]
    sender_override: str
    notes: str


@dataclass(frozen=True)
class SelectedMessage:
    audience_key: str
    channel: str
    step_key: str
    step_index: int
    delay_hours: int | float
    variant_key: str
    subject: str
    body: str
    media: tuple[str, ...]
    sender_override: str


@dataclass(frozen=True)
class RenderedMessage:
    subject: str
    body: str
    media: tuple[str, ...]
    render_hash: str


@dataclass(frozen=True)
class MessageProgramIndex:
    version_id: int
    program_key: str
    program_name: str
    content_hash: str
    messages: tuple[PublishedMessage, ...]

    @property
    def audience_keys(self) -> tuple[str, ...]:
        return tuple(sorted({message.audience_key for message in self.messages}))

    def resolve_audience(self, audience_input: str) -> str:
        requested = _nonblank_text(audience_input, "audience input", max_length=160)
        if requested in self.audience_keys:
            return requested
        matches = sorted({
            message.audience_key
            for message in self.messages
            if message.role_persona == requested
        })
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise MessageDeliveryError(
                f"role persona {requested!r} maps to multiple audience keys: {matches!r}"
            )
        raise MessageDeliveryError(
            f"message version has no audience key or exact role persona {requested!r}"
        )

    def effective_routes(
        self,
        *,
        audience_key: str,
        operator: str,
        channel: str | None = None,
    ) -> tuple[tuple[PublishedMessage, ...], ...]:
        """Return operator-effective variant groups, exact override before shared.

        Exact sender routes shadow shared routes with the same step key *or*
        step index.  This permits a sender-specific sequence shape while
        ensuring the resulting route set cannot violate the Delivery model's
        unique key/index identities.
        """
        if audience_key not in self.audience_keys:
            raise MessageDeliveryError(
                f"message version has no audience {audience_key!r}"
            )
        if channel is not None:
            _validate_channel(channel)

        relevant = [
            message
            for message in self.messages
            if message.audience_key == audience_key
            and (channel is None or message.channel == channel)
            and message.sender_override in {"", operator}
        ]
        exact: dict[tuple[str, str, int], list[PublishedMessage]] = {}
        shared: dict[tuple[str, str, int], list[PublishedMessage]] = {}
        for message in relevant:
            route = (message.channel, message.step_key, message.step_index)
            target = exact if message.sender_override == operator else shared
            target.setdefault(route, []).append(message)

        chosen: dict[tuple[str, str, int], list[PublishedMessage]] = dict(exact)
        exact_keys = {(route[0], route[1]) for route in exact}
        exact_indexes = {(route[0], route[2]) for route in exact}
        for route, variants in shared.items():
            if (route[0], route[1]) in exact_keys or (route[0], route[2]) in exact_indexes:
                continue
            chosen[route] = variants

        return tuple(
            tuple(sorted(variants, key=lambda message: message.variant_key))
            for _route, variants in sorted(
                chosen.items(),
                key=lambda item: (
                    _CHANNEL_ORDER[item[0][0]],
                    item[0][2],
                    item[0][1],
                ),
            )
        )


def index_message_version(version: MessageProgramVersion) -> MessageProgramIndex:
    """Strictly validate and index one immutable published payload."""
    if not version.pk:
        raise MessageDeliveryError("message version must be saved before delivery")
    if version.schema_version != SUPPORTED_SCHEMA_VERSION:
        raise MessageDeliveryError(
            f"unsupported message schema version {version.schema_version}; "
            f"expected {SUPPORTED_SCHEMA_VERSION}"
        )
    payload = version.payload
    _exact_object_fields(payload, _TOP_LEVEL_FIELDS, "message payload")
    program_key = _key(payload["program_key"], "message payload program_key")
    if program_key != version.program.key:
        raise MessageDeliveryError(
            f"payload program_key {program_key!r} does not match {version.program.key!r}"
        )
    program_name = _nonblank_text(
        payload["program_name"],
        "message payload program_name",
        max_length=200,
    )
    raw_messages = payload["messages"]
    if not isinstance(raw_messages, list) or not raw_messages:
        raise MessageDeliveryError("message payload messages must be a nonempty list")

    expected_hash = _canonical_hash(payload)
    if not isinstance(version.content_hash, str) or not _HASH_RE.fullmatch(version.content_hash):
        raise MessageDeliveryError("message version content_hash must be lowercase SHA-256")
    if version.content_hash != expected_hash:
        raise MessageDeliveryError("message version content_hash does not match its payload")

    messages: list[PublishedMessage] = []
    unique_routes: set[tuple[str, str, str, str, str]] = set()
    audience_metadata: dict[str, tuple[str, str]] = {}
    step_key_by_index: dict[tuple[str, str, str, int], str] = {}
    step_index_by_key: dict[tuple[str, str, str, str], int] = {}
    step_delay_by_key: dict[tuple[str, str, str, str], int | float] = {}

    for position, raw in enumerate(raw_messages, start=1):
        context = f"message payload row {position}"
        _exact_object_fields(raw, _MESSAGE_FIELDS, context)
        audience_key = _key(raw["audience_key"], f"{context} audience_key")
        role_persona = _text(raw["role_persona"], f"{context} role_persona")
        fedramp_segment = _text(raw["fedramp_segment"], f"{context} fedramp_segment")
        channel = _text(raw["channel"], f"{context} channel")
        _validate_channel(channel)
        step_key = _key(raw["step_key"], f"{context} step_key")
        step_index = _nonnegative_integer(raw["step_index"], f"{context} step_index")
        delay_hours = _nonnegative_number(raw["delay_hours"], f"{context} delay_hours")
        variant_key = _key(raw["variant_key"], f"{context} variant_key")
        subject = _text(raw["subject"], f"{context} subject", max_length=998)
        body = _nonblank_text(raw["body"], f"{context} body")
        media = _media(raw["media"], f"{context} media")
        sender_override = _text(
            raw["sender_override"],
            f"{context} sender_override",
            max_length=64,
        )
        if sender_override and sender_override not in CANONICAL_OPERATOR_HANDLES:
            raise MessageDeliveryError(
                f"{context} sender_override must be blank or a canonical operator; "
                f"got {sender_override!r}"
            )
        notes = _text(raw["notes"], f"{context} notes")

        if channel == OutboundDelivery.Channel.GMAIL:
            if not subject:
                raise MessageDeliveryError(f"{context} Gmail subject is required")
        elif subject:
            raise MessageDeliveryError(f"{context} LinkedIn subject must be blank")
        _validate_placeholders(subject, f"{context} subject")
        _validate_placeholders(body, f"{context} body")

        metadata = (role_persona, fedramp_segment)
        previous_metadata = audience_metadata.setdefault(audience_key, metadata)
        if previous_metadata != metadata:
            raise MessageDeliveryError(
                f"{context} changes role_persona or fedramp_segment for "
                f"audience {audience_key!r}"
            )

        route = (
            audience_key,
            channel,
            step_key,
            variant_key,
            sender_override.casefold(),
        )
        if route in unique_routes:
            raise MessageDeliveryError(f"{context} duplicates message route {route!r}")
        unique_routes.add(route)

        sender_route = sender_override.casefold()
        index_identity = (audience_key, channel, sender_route, step_index)
        prior_key = step_key_by_index.setdefault(index_identity, step_key)
        if prior_key != step_key:
            raise MessageDeliveryError(
                f"{context} maps step index {step_index} to both {prior_key!r} "
                f"and {step_key!r}"
            )
        key_identity = (audience_key, channel, sender_route, step_key)
        prior_index = step_index_by_key.setdefault(key_identity, step_index)
        if prior_index != step_index:
            raise MessageDeliveryError(
                f"{context} maps step key {step_key!r} to both indexes "
                f"{prior_index} and {step_index}"
            )
        prior_delay = step_delay_by_key.setdefault(key_identity, delay_hours)
        if prior_delay != delay_hours:
            raise MessageDeliveryError(
                f"{context} changes delay_hours for step {step_key!r}"
            )

        messages.append(PublishedMessage(
            audience_key=audience_key,
            role_persona=role_persona,
            fedramp_segment=fedramp_segment,
            channel=channel,
            step_key=step_key,
            step_index=step_index,
            delay_hours=delay_hours,
            variant_key=variant_key,
            subject=subject,
            body=body,
            media=media,
            sender_override=sender_override,
            notes=notes,
        ))

    messages.sort(key=lambda message: (
        message.audience_key,
        _CHANNEL_ORDER[message.channel],
        message.step_index,
        message.step_key,
        message.variant_key,
        message.sender_override,
    ))
    return MessageProgramIndex(
        version_id=version.pk,
        program_key=program_key,
        program_name=program_name,
        content_hash=version.content_hash,
        messages=tuple(messages),
    )


def resolve_audience_key(
    version_or_index: MessageProgramVersion | MessageProgramIndex,
    audience_input: str,
) -> str:
    """Resolve an exact key, else one exact and unique role-persona label."""
    index = (
        version_or_index
        if isinstance(version_or_index, MessageProgramIndex)
        else index_message_version(version_or_index)
    )
    return index.resolve_audience(audience_input)


def ensure_message_enrollment(
    *,
    deal,
    audience_key: str,
    operator: str,
) -> CampaignMessageEnrollment:
    """Create once against the active version, or return the frozen enrollment."""
    if not getattr(deal, "pk", None):
        raise MessageDeliveryError("Deal must be saved before message enrollment")
    normalized_operator = resolve_operator(operator)
    normalized_operator = _nonblank_text(
        normalized_operator,
        "enrollment operator",
        max_length=64,
    )

    from crm.models import Deal

    with transaction.atomic():
        locked_deal = (
            Deal.objects.select_for_update()
            .select_related("lead")
            .get(pk=deal.pk)
        )
        campaign = (
            # Only the campaign pointer is mutable. Locking the nullable
            # version join is invalid on PostgreSQL; versions are immutable.
            Campaign.objects.select_for_update(of=("self",))
            .select_related("active_message_version__program")
            .get(pk=locked_deal.campaign_id)
        )
        locked_deal.campaign = campaign
        existing = (
            CampaignMessageEnrollment.objects.select_for_update()
            .select_related("message_version__program", "deal__lead", "deal__campaign")
            .filter(deal_id=locked_deal.pk)
            .first()
        )
        if existing is not None:
            index = index_message_version(existing.message_version)
            canonical_audience = index.resolve_audience(audience_key)
            if existing.audience_key != canonical_audience:
                raise MessageDeliveryError(
                    f"Deal {locked_deal.pk} is already frozen to audience "
                    f"{existing.audience_key!r}, not {canonical_audience!r}"
                )
            if existing.operator != normalized_operator:
                raise MessageDeliveryError(
                    f"Deal {locked_deal.pk} is already frozen to operator "
                    f"{existing.operator!r}, not {normalized_operator!r}"
                )
            _require_effective_routes(index, existing)
            return existing

        version = campaign.active_message_version
        if version is None:
            raise MessageDeliveryError(
                f"Deal {locked_deal.pk} campaign has no active message version"
            )
        index = index_message_version(version)
        canonical_audience = index.resolve_audience(audience_key)
        # ``locked_deal`` was read before the Campaign row lock.  Point its
        # relation cache at the locked/fresh Campaign so model.full_clean()
        # validates against the same active version selected above.
        locked_deal.campaign = campaign
        enrollment = CampaignMessageEnrollment(
            deal=locked_deal,
            message_version=version,
            audience_key=canonical_audience,
            operator=normalized_operator,
        )
        _require_effective_routes(index, enrollment)
        enrollment.full_clean()
        enrollment.save()
        return enrollment


def get_message_enrollment(*, deal) -> CampaignMessageEnrollment:
    """Return and validate a Deal's frozen enrollment; never load legacy JSON."""
    if not getattr(deal, "pk", None):
        raise MessageDeliveryError("Deal must be saved before loading enrollment")
    enrollment = (
        CampaignMessageEnrollment.objects
        .select_related("message_version__program", "deal__lead", "deal__campaign")
        .filter(deal_id=deal.pk)
        .first()
    )
    if enrollment is None:
        raise MessageDeliveryError(f"Deal {deal.pk} has no message enrollment")
    index = index_message_version(enrollment.message_version)
    canonical = index.resolve_audience(enrollment.audience_key)
    if canonical != enrollment.audience_key:
        raise MessageDeliveryError("stored enrollment audience is not canonical")
    _require_effective_routes(index, enrollment)
    return enrollment


def select_message(
    *,
    enrollment: CampaignMessageEnrollment,
    channel: str,
    step_key: str | None = None,
    step_index: int | None = None,
) -> SelectedMessage:
    """Select one stable variant for an exact enrollment route."""
    enrollment = _require_saved_enrollment(enrollment)
    _validate_channel(channel)
    if step_key is None and step_index is None:
        raise MessageDeliveryError("step_key or step_index is required")
    if step_key is not None:
        step_key = _key(step_key, "step_key")
    if step_index is not None:
        step_index = _nonnegative_integer(step_index, "step_index")

    index = index_message_version(enrollment.message_version)
    groups = index.effective_routes(
        audience_key=enrollment.audience_key,
        operator=enrollment.operator,
        channel=channel,
    )
    matches = [
        group
        for group in groups
        if (step_key is None or group[0].step_key == step_key)
        and (step_index is None or group[0].step_index == step_index)
    ]
    if not matches:
        identity = step_key if step_key is not None else step_index
        raise MessageDeliveryError(
            f"audience {enrollment.audience_key!r} has no {channel!r} step {identity!r} "
            f"for operator {enrollment.operator!r}"
        )
    if len(matches) != 1:
        raise MessageDeliveryError("message route is ambiguous after sender selection")
    variants = matches[0]
    digest = _canonical_hash({
        "enrollment_id": enrollment.pk,
        "deal_id": enrollment.deal_id,
        "channel": channel,
        "step_key": variants[0].step_key,
        "step_index": variants[0].step_index,
    })
    selected = variants[int(digest, 16) % len(variants)]
    return SelectedMessage(
        audience_key=selected.audience_key,
        channel=selected.channel,
        step_key=selected.step_key,
        step_index=selected.step_index,
        delay_hours=selected.delay_hours,
        variant_key=selected.variant_key,
        subject=selected.subject,
        body=selected.body,
        media=selected.media,
        sender_override=selected.sender_override,
    )


def render_message(
    *,
    enrollment: CampaignMessageEnrollment,
    message: SelectedMessage,
) -> RenderedMessage:
    """Mechanically render supported placeholders and hash the result."""
    enrollment = _require_saved_enrollment(enrollment)
    if message.audience_key != enrollment.audience_key:
        raise MessageDeliveryError("selected message audience does not match enrollment")
    from linkedin.icp_outbound import greeting_first_name, safe_company_name

    lead = enrollment.deal.lead
    values = {
        "first_name": greeting_first_name(str(lead.first_name or "")),
        "last_name": str(lead.last_name or "").strip(),
        "company_name": safe_company_name(str(lead.company_name or "")),
        "my_name": enrollment.operator,
        "our_company_name": conf.OUR_COMPANY_NAME,
        "our_website_url": conf.OUR_WEBSITE_URL,
    }
    if message.channel == OutboundDelivery.Channel.GMAIL:
        from gmail.auth import GMAIL_OPERATOR_MAPPING

        sender_mapping = GMAIL_OPERATOR_MAPPING.get(enrollment.operator) or {}
        values["my_name"] = sender_mapping.get("display_name") or enrollment.operator
    if "{role}" in message.subject or "{role}" in message.body:
        values["role"] = role_wording(lead.role_tag)
    subject = _render_template(message.subject, values, "message subject")
    body = _render_template(message.body, values, "message body")
    if not body.strip():
        raise MessageDeliveryError("rendered message body is blank")
    if message.channel == OutboundDelivery.Channel.GMAIL and not subject.strip():
        raise MessageDeliveryError("rendered Gmail subject is blank")
    media = tuple(message.media)
    render_hash = _delivery_render_hash(
        enrollment=enrollment,
        channel=message.channel,
        step_key=message.step_key,
        step_index=message.step_index,
        variant_key=message.variant_key,
        subject=subject,
        body=body,
        media=media,
    )
    return RenderedMessage(
        subject=subject,
        body=body,
        media=media,
        render_hash=render_hash,
    )


def get_or_create_delivery(
    *,
    enrollment: CampaignMessageEnrollment,
    channel: str,
    step_key: str | None = None,
    step_index: int | None = None,
    reference_at: datetime | None = None,
) -> tuple[OutboundDelivery, bool]:
    """Materialize one immutable delivery, or return its existing frozen row."""
    enrollment = _require_saved_enrollment(enrollment)
    with transaction.atomic():
        locked = (
            CampaignMessageEnrollment.objects.select_for_update()
            .select_related("message_version__program", "deal__lead", "deal__campaign")
            .get(pk=enrollment.pk)
        )
        selected = select_message(
            enrollment=locked,
            channel=channel,
            step_key=step_key,
            step_index=step_index,
        )
        existing = list(
            OutboundDelivery.objects.select_for_update().filter(
                Q(step_key=selected.step_key) | Q(step_index=selected.step_index),
                enrollment=locked,
                channel=selected.channel,
            )
        )
        if existing:
            if len(existing) != 1:
                raise MessageDeliveryError("conflicting frozen deliveries exist for route")
            delivery = existing[0]
            if (
                delivery.step_key != selected.step_key
                or delivery.step_index != selected.step_index
                or delivery.variant_key != selected.variant_key
                or delivery.operator != locked.operator
            ):
                raise MessageDeliveryError(
                    "existing frozen delivery identity does not match selected message route"
                )
            _validate_existing_delivery(delivery)
            return delivery, False

        reference = reference_at if reference_at is not None else locked.created_at
        if not isinstance(reference, datetime) or timezone.is_naive(reference):
            raise MessageDeliveryError("reference_at must be a timezone-aware datetime")
        rendered = render_message(enrollment=locked, message=selected)
        scheduled_at = reference + timedelta(hours=float(selected.delay_hours))
        delivery = OutboundDelivery(
            enrollment=locked,
            channel=selected.channel,
            step_key=selected.step_key,
            step_index=selected.step_index,
            variant_key=selected.variant_key,
            operator=locked.operator,
            scheduled_at=scheduled_at,
            frozen_subject=rendered.subject,
            frozen_body=rendered.body,
            frozen_media=list(rendered.media),
            render_hash=rendered.render_hash,
        )
        delivery.full_clean()
        delivery.save()
        return delivery, True


def list_message_steps(
    *,
    enrollment: CampaignMessageEnrollment,
    channel: str | None = None,
) -> tuple[SelectedMessage, ...]:
    """List one deterministic selected variant for every effective route."""
    enrollment = _require_saved_enrollment(enrollment)
    if channel is not None:
        _validate_channel(channel)
    index = index_message_version(enrollment.message_version)
    groups = index.effective_routes(
        audience_key=enrollment.audience_key,
        operator=enrollment.operator,
        channel=channel,
    )
    if not groups:
        target = f" channel {channel!r}" if channel is not None else ""
        raise MessageDeliveryError(
            f"audience {enrollment.audience_key!r} has no effective{target} routes "
            f"for operator {enrollment.operator!r}"
        )
    return tuple(
        select_message(
            enrollment=enrollment,
            channel=group[0].channel,
            step_key=group[0].step_key,
            step_index=group[0].step_index,
        )
        for group in groups
    )


def next_message_step(
    *,
    enrollment: CampaignMessageEnrollment,
    channel: str | None = None,
) -> SelectedMessage | None:
    """Return the first effective route without a frozen Delivery."""
    steps = list_message_steps(enrollment=enrollment, channel=channel)
    delivered = set(
        OutboundDelivery.objects.filter(
            enrollment_id=enrollment.pk,
            **({"channel": channel} if channel is not None else {}),
        ).values_list("channel", "step_key", "step_index")
    )
    return next(
        (
            step
            for step in steps
            if (step.channel, step.step_key, step.step_index) not in delivered
        ),
        None,
    )


def _require_effective_routes(
    index: MessageProgramIndex,
    enrollment: CampaignMessageEnrollment,
) -> None:
    routes = index.effective_routes(
        audience_key=enrollment.audience_key,
        operator=enrollment.operator,
    )
    if not routes:
        raise MessageDeliveryError(
            f"audience {enrollment.audience_key!r} has no shared or exact routes "
            f"for operator {enrollment.operator!r}"
        )


def _require_saved_enrollment(
    enrollment: CampaignMessageEnrollment,
) -> CampaignMessageEnrollment:
    if not enrollment.pk:
        raise MessageDeliveryError("message enrollment must be saved")
    if not enrollment.audience_key or not enrollment.operator:
        raise MessageDeliveryError("message enrollment has blank routing identity")
    persisted = (
        CampaignMessageEnrollment.objects
        .select_related("message_version__program", "deal__lead", "deal__campaign")
        .get(pk=enrollment.pk)
    )
    if not persisted.audience_key or not persisted.operator:
        raise MessageDeliveryError("message enrollment has blank routing identity")
    return persisted


def _exact_object_fields(value, expected: frozenset[str], context: str) -> None:
    if not isinstance(value, dict):
        raise MessageDeliveryError(f"{context} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise MessageDeliveryError(
            f"{context} has invalid fields; missing={missing!r}, extra={extra!r}"
        )


def _text(value, context: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str):
        raise MessageDeliveryError(f"{context} must be a string")
    if value != value.strip():
        raise MessageDeliveryError(f"{context} must not have outer whitespace")
    if max_length is not None and len(value) > max_length:
        raise MessageDeliveryError(f"{context} exceeds {max_length} characters")
    return value


def _nonblank_text(value, context: str, *, max_length: int | None = None) -> str:
    text = _text(value, context, max_length=max_length)
    if not text:
        raise MessageDeliveryError(f"{context} must be nonblank")
    return text


def _key(value, context: str) -> str:
    key = _nonblank_text(value, context, max_length=160)
    if not _KEY_RE.fullmatch(key):
        raise MessageDeliveryError(f"{context} is not a valid lowercase key")
    return key


def _nonnegative_integer(value, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MessageDeliveryError(f"{context} must be a nonnegative integer")
    return value


def _nonnegative_number(value, context: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MessageDeliveryError(f"{context} must be a nonnegative number")
    if not math.isfinite(value) or value < 0:
        raise MessageDeliveryError(f"{context} must be a finite nonnegative number")
    return value


def _validate_channel(channel: str) -> None:
    if channel not in SUPPORTED_CHANNELS:
        raise MessageDeliveryError(
            f"channel must be one of {list(SUPPORTED_CHANNELS)!r}; got {channel!r}"
        )


def _media(value, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise MessageDeliveryError(f"{context} must be a list")
    normalized = []
    for item in value:
        filename = _nonblank_text(item, f"{context} entry")
        if filename in normalized:
            raise MessageDeliveryError(f"{context} contains duplicate {filename!r}")
        try:
            parts = list(_FORMATTER.parse(filename))
        except ValueError as exc:
            raise MessageDeliveryError(f"{context} entry has invalid braces: {exc}") from exc
        if any(field_name is not None for _lit, field_name, _spec, _conv in parts):
            raise MessageDeliveryError(f"{context} entries cannot contain placeholders")
        normalized.append(filename)
    return tuple(normalized)


def _validate_placeholders(template: str, context: str) -> None:
    try:
        parts = list(_FORMATTER.parse(template))
    except ValueError as exc:
        raise MessageDeliveryError(f"{context} has invalid braces: {exc}") from exc
    for _literal, field_name, format_spec, conversion in parts:
        if field_name is None:
            continue
        if not field_name:
            raise MessageDeliveryError(f"{context} uses a positional placeholder")
        if any(separator in field_name for separator in (".", "[")):
            raise MessageDeliveryError(f"{context} uses a nested placeholder")
        if format_spec or conversion:
            raise MessageDeliveryError(f"{context} uses placeholder formatting")
        if field_name not in SUPPORTED_PLACEHOLDERS:
            raise MessageDeliveryError(
                f"{context} has unsupported placeholder {field_name!r}"
            )


def _render_template(template: str, values: dict[str, str], context: str) -> str:
    _validate_placeholders(template, context)
    try:
        return template.format_map(values)
    except (KeyError, ValueError) as exc:
        raise MessageDeliveryError(f"could not render {context}: {exc}") from exc


def _delivery_render_hash(
    *,
    enrollment: CampaignMessageEnrollment,
    channel: str,
    step_key: str,
    step_index: int,
    variant_key: str,
    subject: str,
    body: str,
    media: tuple[str, ...],
) -> str:
    return _canonical_hash({
        "message_version_id": enrollment.message_version_id,
        "message_content_hash": enrollment.message_version.content_hash,
        "enrollment_id": enrollment.pk,
        "deal_id": enrollment.deal_id,
        "audience_key": enrollment.audience_key,
        "operator": enrollment.operator,
        "channel": channel,
        "step_key": step_key,
        "step_index": step_index,
        "variant_key": variant_key,
        "subject": subject,
        "body": body,
        "media": list(media),
    })


def _validate_existing_delivery(delivery: OutboundDelivery) -> None:
    subject = _text(
        delivery.frozen_subject,
        "frozen delivery subject",
        max_length=998,
    )
    body = _nonblank_text(delivery.frozen_body, "frozen delivery body")
    media = _media(delivery.frozen_media, "frozen delivery media")
    if delivery.channel == OutboundDelivery.Channel.GMAIL:
        if not subject:
            raise MessageDeliveryError("frozen Gmail delivery subject is blank")
    elif subject:
        raise MessageDeliveryError("frozen LinkedIn delivery subject is not blank")
    expected_hash = _delivery_render_hash(
        enrollment=delivery.enrollment,
        channel=delivery.channel,
        step_key=delivery.step_key,
        step_index=delivery.step_index,
        variant_key=delivery.variant_key,
        subject=subject,
        body=body,
        media=media,
    )
    if delivery.render_hash != expected_hash:
        raise MessageDeliveryError(
            f"frozen delivery {delivery.pk} render_hash does not match its content"
        )


def _canonical_hash(value) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
