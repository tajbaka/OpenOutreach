import copy
import hashlib
import json
from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Deal, Lead
from linkedin.message_delivery import (
    MessageDeliveryError,
    get_or_create_delivery,
    index_message_version,
    list_message_steps,
    next_message_step,
    render_message,
    resolve_audience_key,
    select_message,
    ensure_message_enrollment,
)
from linkedin.models import Campaign, MessageProgram, MessageProgramVersion, OutboundDelivery
from tests.factories import UserFactory


pytestmark = pytest.mark.django_db


def _hash(payload):
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _row(
    *,
    audience_key="csp-small-founder-ceo",
    role_persona="CSP Small | Founder/CEO",
    fedramp_segment="Rev5 Ready",
    channel="gmail",
    step_key="email-1",
    step_index=0,
    delay_hours=0,
    variant_key="control",
    subject="FedRAMP at {company_name}",
    body="Hi {first_name}, a note for {company_name}. Best, {my_name}",
    media=None,
    sender_override="",
    notes="",
):
    return {
        "audience_key": audience_key,
        "role_persona": role_persona,
        "fedramp_segment": fedramp_segment,
        "channel": channel,
        "step_key": step_key,
        "step_index": step_index,
        "delay_hours": delay_hours,
        "variant_key": variant_key,
        "subject": subject,
        "body": body,
        "media": [] if media is None else media,
        "sender_override": sender_override,
        "notes": notes,
    }


def _payload(*rows, key="marketplace-csp", name="Marketplace CSP"):
    return {
        "program_key": key,
        "program_name": name,
        "messages": list(rows),
    }


def _version(*rows, key="marketplace-csp", version=1, program=None):
    payload = _payload(*rows, key=key)
    if program is None:
        program = MessageProgram.objects.create(key=key, name="Marketplace CSP")
    return MessageProgramVersion.objects.create(
        program=program,
        version=version,
        schema_version=1,
        payload=payload,
        content_hash=_hash(payload),
        published_by="reviewer@example.com",
        based_on_version=(program.versions.order_by("-version").first() if version > 1 else None),
    )


def _deal(version, *, first_name="Ada", company_name="Example Cloud"):
    user = UserFactory()
    campaign = Campaign.objects.create(
        name=f"Delivery campaign {version.program.key} {user.pk}",
        user=user,
        active_message_version=version,
    )
    lead = Lead.objects.create(
        first_name=first_name,
        last_name="Lovelace",
        company_name=company_name,
        email=f"ada-{user.pk}@example.com",
        icp="CSP Small | Founder/CEO",
    )
    return Deal.objects.create(lead=lead, campaign=campaign)


def test_index_is_strict_canonical_and_resolves_exact_role_persona():
    later = _row(step_key="email-2", step_index=1, variant_key="later")
    first = _row()
    version = _version(later, first, key="strict-index")

    index = index_message_version(version)

    assert [message.step_index for message in index.messages] == [0, 1]
    assert resolve_audience_key(index, "csp-small-founder-ceo") == "csp-small-founder-ceo"
    assert resolve_audience_key(version, "CSP Small | Founder/CEO") == "csp-small-founder-ceo"

    ambiguous = _row(
        audience_key="csp-mid-founder-ceo",
        step_key="mid-email-1",
    )
    ambiguous_version = _version(first, ambiguous, key="ambiguous-role")
    with pytest.raises(MessageDeliveryError, match="multiple audience keys"):
        resolve_audience_key(ambiguous_version, "CSP Small | Founder/CEO")


def test_intent_specific_audiences_require_the_exact_audience_key():
    direct = _row(
        audience_key=(
            "csp-small-founder-ceo-20x-initial-implementation-direct-agency"
        ),
        fedramp_segment="20x Initial Implementation",
    )
    unclear = _row(
        audience_key="csp-small-founder-ceo-20x-initial-implementation-unclear",
        fedramp_segment="20x Initial Implementation",
    )
    version = _version(direct, unclear, key="intent-specific-audiences")

    assert resolve_audience_key(version, direct["audience_key"]) == direct["audience_key"]
    assert resolve_audience_key(version, unclear["audience_key"]) == unclear["audience_key"]
    with pytest.raises(MessageDeliveryError, match="multiple audience keys"):
        resolve_audience_key(version, "CSP Small | Founder/CEO")


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda payload: payload.update({"unexpected": True}), "invalid fields"),
        (lambda payload: payload["messages"][0].update(channel="sms"), "channel must"),
        (lambda payload: payload["messages"][0].update(delay_hours=-1), "nonnegative"),
        (lambda payload: payload["messages"][0].update(subject=""), "subject is required"),
        (
            lambda payload: payload["messages"][0].update(body="Hi {unknown}"),
            "unsupported placeholder",
        ),
        (lambda payload: payload["messages"][0].update(media="demo.gif"), "must be a list"),
        (
            lambda payload: payload["messages"][0].update(sender_override="eddy"),
            "canonical operator",
        ),
    ],
)
def test_index_fails_closed_on_malformed_published_payload(mutation, match):
    payload = _payload(_row(), key="bad-payload")
    mutation(payload)
    program = MessageProgram.objects.create(key="bad-payload", name="Bad Payload")
    version = MessageProgramVersion.objects.create(
        program=program,
        version=1,
        payload=payload,
        content_hash=_hash(payload),
        published_by="reviewer@example.com",
    )

    with pytest.raises(MessageDeliveryError, match=match):
        index_message_version(version)


def test_index_rejects_content_hash_mismatch_and_duplicate_route():
    row = _row()
    duplicate = copy.deepcopy(row)
    version = _version(row, duplicate, key="duplicate-route")
    with pytest.raises(MessageDeliveryError, match="duplicates message route"):
        index_message_version(version)

    valid = _version(_row(), key="bad-content-hash")
    MessageProgramVersion.objects.filter(pk=valid.pk).update(content_hash="0" * 64)
    valid.refresh_from_db()
    with pytest.raises(MessageDeliveryError, match="does not match"):
        index_message_version(valid)


def test_enrollment_freezes_active_version_and_canonical_audience_after_campaign_advances():
    version = _version(_row(), key="frozen-enrollment")
    deal = _deal(version)

    enrollment = ensure_message_enrollment(
        deal=deal,
        audience_key="CSP Small | Founder/CEO",
        operator="ariant@boundera.io",
    )
    assert enrollment.audience_key == "csp-small-founder-ceo"
    assert enrollment.operator == "Arian"
    assert enrollment.message_version == version

    next_row = _row(notes="second immutable publication")
    next_version = _version(
        next_row,
        key="frozen-enrollment",
        version=2,
        program=version.program,
    )
    deal.campaign.active_message_version = next_version
    deal.campaign.save(update_fields={"active_message_version"})

    repeated = ensure_message_enrollment(
        deal=deal,
        audience_key="CSP Small | Founder/CEO",
        operator="Arian",
    )
    assert repeated.pk == enrollment.pk
    assert repeated.message_version == version

    with pytest.raises(MessageDeliveryError, match="already frozen to operator"):
        ensure_message_enrollment(
            deal=deal,
            audience_key="CSP Small | Founder/CEO",
            operator="Chuka",
        )


def test_enrollment_fails_without_active_version_or_effective_sender_route():
    chuka_only = _row(sender_override="Chuka")
    version = _version(chuka_only, key="sender-route")
    deal = _deal(version)
    with pytest.raises(MessageDeliveryError, match="no shared or exact routes"):
        ensure_message_enrollment(
            deal=deal,
            audience_key="csp-small-founder-ceo",
            operator="Arian",
        )

    deal.campaign.active_message_version = None
    deal.campaign.save(update_fields={"active_message_version"})
    with pytest.raises(MessageDeliveryError, match="no active message version"):
        ensure_message_enrollment(
            deal=deal,
            audience_key="csp-small-founder-ceo",
            operator="Chuka",
        )


def test_sender_override_precedence_and_variant_choice_are_stable():
    rows = [
        _row(variant_key="shared-b", body="shared b"),
        _row(variant_key="arian-b", body="arian b", sender_override="Arian"),
        _row(variant_key="shared-a", body="shared a"),
        _row(variant_key="arian-a", body="arian a", sender_override="Arian"),
        _row(
            step_key="email-2",
            step_index=1,
            variant_key="chuka-only",
            body="chuka only",
            sender_override="Chuka",
        ),
    ]
    version = _version(*rows, key="sender-precedence")
    enrollment = ensure_message_enrollment(
        deal=_deal(version),
        audience_key="csp-small-founder-ceo",
        operator="Arian",
    )

    selected = select_message(
        enrollment=enrollment,
        channel="gmail",
        step_index=0,
    )
    repeated = select_message(
        enrollment=enrollment,
        channel="gmail",
        step_key="email-1",
    )
    assert selected == repeated
    assert selected.sender_override == "Arian"
    assert selected.variant_key in {"arian-a", "arian-b"}

    with pytest.raises(MessageDeliveryError, match="has no 'gmail' step"):
        select_message(enrollment=enrollment, channel="gmail", step_index=1)


@pytest.mark.parametrize("channel", ["gmail", "linkedin_connect", "linkedin_followup"])
@pytest.mark.parametrize("operator", ["Arian", "Chuka", "Leili", "Athena"])
def test_render_uses_gmail_display_name_without_changing_operator(channel, operator):
    version = _version(_row(
        channel=channel,
        subject="From {my_name}" if channel == "gmail" else "",
        body="Best, {my_name}",
    ), key="sender-display-name")
    deal = _deal(version)
    enrollment = ensure_message_enrollment(
        deal=deal, audience_key="csp-small-founder-ceo", operator=operator,
    )
    selected = select_message(enrollment=enrollment, channel=channel, step_index=0)
    rendered = render_message(enrollment=enrollment, message=selected)

    expected_name = "Eddy" if channel == "gmail" and operator == "Chuka" else operator
    assert rendered.subject == (f"From {expected_name}" if channel == "gmail" else "")
    assert rendered.body == f"Best, {expected_name}"
    enrollment.refresh_from_db()
    assert enrollment.operator == operator


def test_gmail_display_name_change_preserves_frozen_delivery(monkeypatch):
    from gmail.auth import GMAIL_OPERATOR_MAPPING

    version = _version(
        _row(body="Best, {my_name}"),
        _row(step_key="email-2", step_index=1, body="Thanks, {my_name}"),
        key="frozen-sender-display-name",
    )
    deal = _deal(version)
    enrollment = ensure_message_enrollment(
        deal=deal, audience_key="csp-small-founder-ceo", operator="Chuka",
    )
    # Reproduce an already-frozen signature before the public name changed.
    monkeypatch.setitem(GMAIL_OPERATOR_MAPPING["Chuka"], "display_name", "Chuka")
    original, created = get_or_create_delivery(
        enrollment=enrollment, channel="gmail", step_index=0,
    )
    assert created
    assert original.frozen_body == "Best, Chuka"
    original_hash = original.render_hash

    monkeypatch.setitem(GMAIL_OPERATOR_MAPPING["Chuka"], "display_name", "Eddy")
    existing, created = get_or_create_delivery(
        enrollment=enrollment, channel="gmail", step_index=0,
    )
    assert not created
    assert existing.pk == original.pk
    assert existing.frozen_body == "Best, Chuka"
    assert existing.render_hash == original_hash

    following, created = get_or_create_delivery(
        enrollment=enrollment, channel="gmail", step_index=1,
    )
    assert created
    assert following.frozen_body == "Thanks, Eddy"
    assert existing.operator == following.operator == "Chuka"


def test_render_uses_safe_names_and_freezes_delivery_idempotently(monkeypatch):
    row = _row(
        delay_hours=1.5,
        subject="{first_name}: {our_company_name}",
        body=(
            "Hi {first_name} {last_name}, {company_name}; I am {my_name} from "
            "{our_company_name}: {our_website_url}"
        ),
        media=["demo.gif"],
    )
    version = _version(row, key="render-freeze")
    deal = _deal(version, first_name='Dr. Allen "Al"', company_name="Unknown Company")
    enrollment = ensure_message_enrollment(
        deal=deal,
        audience_key="CSP Small | Founder/CEO",
        operator="Arian",
    )
    monkeypatch.setattr("linkedin.conf.OUR_COMPANY_NAME", "Boundera")
    monkeypatch.setattr("linkedin.conf.OUR_WEBSITE_URL", "https://boundera.io")

    selected = select_message(enrollment=enrollment, channel="gmail", step_index=0)
    rendered = render_message(enrollment=enrollment, message=selected)
    assert rendered.subject == "Allen: Boundera"
    assert "Hi Allen Lovelace, your team" in rendered.body

    reference = timezone.now().replace(microsecond=0)
    delivery, created = get_or_create_delivery(
        enrollment=enrollment,
        channel="gmail",
        step_index=0,
        reference_at=reference,
    )
    assert created is True
    assert delivery.scheduled_at == reference + timedelta(hours=1.5)
    assert delivery.frozen_subject == rendered.subject
    assert delivery.frozen_body == rendered.body
    assert delivery.frozen_media == ["demo.gif"]
    assert len(delivery.render_hash) == 64
    assert delivery.status == OutboundDelivery.Status.PLANNED

    frozen = (
        delivery.frozen_subject,
        delivery.frozen_body,
        delivery.frozen_media,
        delivery.render_hash,
        delivery.scheduled_at,
    )
    deal.lead.first_name = "Changed"
    deal.lead.company_name = "Changed Cloud"
    deal.lead.save(update_fields={"first_name", "company_name"})
    again, created = get_or_create_delivery(
        enrollment=enrollment,
        channel="gmail",
        step_key="email-1",
        reference_at=reference + timedelta(days=20),
    )
    assert created is False
    assert again.pk == delivery.pk
    assert (
        again.frozen_subject,
        again.frozen_body,
        again.frozen_media,
        again.render_hash,
        again.scheduled_at,
    ) == frozen


@pytest.mark.parametrize('channel', ['linkedin_connect', 'linkedin_followup', 'gmail'])
@pytest.mark.parametrize(('tag', 'expected'), [
    ('CFO/Finance', 'finance leaders'), ('Founder/CEO', 'founders'), ('', 'companies'),
])
def test_role_wording_changes_only_rendered_text_and_is_frozen(channel, tag, expected):
    version = _version(_row(
        channel=channel,
        subject='For {role}' if channel == 'gmail' else '',
        body='I work with {role}.',
    ), key='role-wording')
    deal = _deal(version)
    deal.lead.role_tag = tag
    deal.lead.save(update_fields=['role_tag'])
    original_icp = deal.lead.icp
    enrollment = ensure_message_enrollment(
        deal=deal, audience_key='csp-small-founder-ceo', operator='Arian',
    )
    payload_before = copy.deepcopy(version.payload)
    delivery, created = get_or_create_delivery(enrollment=enrollment, channel=channel, step_index=0)
    assert created
    assert delivery.frozen_body == f'I work with {expected}.'
    assert delivery.frozen_subject == (f'For {expected}' if channel == 'gmail' else '')
    frozen_hash = delivery.render_hash
    deal.lead.refresh_from_db()
    assert deal.lead.role_tag == tag and deal.lead.icp == original_icp
    assert enrollment.audience_key == 'csp-small-founder-ceo'
    version.refresh_from_db()
    assert version.payload == payload_before

    deal.lead.role_tag = 'CRO/Revenue'
    deal.lead.save(update_fields=['role_tag'])
    again, created = get_or_create_delivery(enrollment=enrollment, channel=channel, step_index=0)
    assert not created and again.pk == delivery.pk
    assert again.frozen_body == f'I work with {expected}.'
    assert again.render_hash == frozen_hash


def test_list_and_next_step_use_effective_routes_and_existing_delivery_state():
    rows = [
        _row(
            channel="linkedin_connect",
            step_key="connect",
            subject="",
            body="Hi {first_name}",
        ),
        _row(step_key="email-2", step_index=1, delay_hours=24, variant_key="nudge"),
        _row(),
    ]
    version = _version(*rows, key="list-next")
    enrollment = ensure_message_enrollment(
        deal=_deal(version),
        audience_key="csp-small-founder-ceo",
        operator="Arian",
    )

    all_steps = list_message_steps(enrollment=enrollment)
    assert [(step.channel, step.step_index) for step in all_steps] == [
        ("linkedin_connect", 0),
        ("gmail", 0),
        ("gmail", 1),
    ]
    assert next_message_step(enrollment=enrollment, channel="gmail").step_index == 0

    first, _ = get_or_create_delivery(
        enrollment=enrollment,
        channel="gmail",
        step_index=0,
    )
    assert first.step_key == "email-1"
    assert next_message_step(enrollment=enrollment, channel="gmail").step_index == 1

    get_or_create_delivery(enrollment=enrollment, channel="gmail", step_index=1)
    assert next_message_step(enrollment=enrollment, channel="gmail") is None


def test_bound_version_never_falls_back_to_legacy_json(monkeypatch):
    version = _version(_row(), key="no-legacy-fallback")
    enrollment = ensure_message_enrollment(
        deal=_deal(version),
        audience_key="csp-small-founder-ceo",
        operator="Arian",
    )

    def fail_legacy(*_args, **_kwargs):
        raise AssertionError("legacy JSON must not be read")

    monkeypatch.setattr("linkedin.icp_outbound.load_icp_messages", fail_legacy)
    with pytest.raises(MessageDeliveryError, match="has no 'linkedin_followup' step"):
        select_message(
            enrollment=enrollment,
            channel="linkedin_followup",
            step_index=0,
        )


def test_render_rejects_templates_that_become_blank(monkeypatch):
    version = _version(
        _row(subject="{our_company_name}", body="{our_company_name}"),
        key="blank-after-render",
    )
    enrollment = ensure_message_enrollment(
        deal=_deal(version),
        audience_key="csp-small-founder-ceo",
        operator="Arian",
    )
    selected = select_message(enrollment=enrollment, channel="gmail", step_index=0)
    monkeypatch.setattr("linkedin.conf.OUR_COMPANY_NAME", "")

    with pytest.raises(MessageDeliveryError, match="body is blank"):
        render_message(enrollment=enrollment, message=selected)


def test_existing_delivery_content_hash_is_verified_before_reuse():
    version = _version(_row(), key="delivery-tamper")
    enrollment = ensure_message_enrollment(
        deal=_deal(version),
        audience_key="csp-small-founder-ceo",
        operator="Arian",
    )
    delivery, _created = get_or_create_delivery(
        enrollment=enrollment,
        channel="gmail",
        step_index=0,
    )
    OutboundDelivery.objects.filter(pk=delivery.pk).update(
        frozen_body="tampered frozen copy",
    )

    with pytest.raises(MessageDeliveryError, match="render_hash does not match"):
        get_or_create_delivery(
            enrollment=enrollment,
            channel="gmail",
            step_index=0,
        )
