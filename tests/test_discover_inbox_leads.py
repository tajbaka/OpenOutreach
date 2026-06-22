"""Tests for manage.py discover_inbox_leads helpers."""
from __future__ import annotations

from unittest.mock import patch

from django.utils import timezone

from crm.models import Deal, Lead, Message
from linkedin.enums import ProfileState
from linkedin.management.commands.discover_inbox_leads import (
    InboxLeadDecision,
    _fallback_profile,
    _decision_icp,
    _next_sync_token,
    _other_participant,
    apply_inbox_import,
    existing_skip_reason,
    iter_inbox_conversations,
)


def _participant(
    urn: str,
    first: str,
    last: str,
    public_id: str = "",
    *,
    profile_url: str = "",
    headline: str = "",
) -> dict:
    return {
        "hostIdentityUrn": urn,
        "participantType": {
            "member": {
                "firstName": {"text": first},
                "lastName": {"text": last},
                "publicIdentifier": public_id,
                "profileUrl": profile_url,
                "headline": {"text": headline},
            },
        },
    }


def _conversation(urn: str, other_public_id: str, *, delivered_at: int | None = None) -> dict:
    conv = {
        "entityUrn": urn,
        "conversationParticipants": [
            _participant("urn:li:fsd_profile:self", "Arian", "Taj"),
            _participant("urn:li:fsd_profile:other", "Jane", "Buyer", other_public_id),
        ],
    }
    if delivered_at:
        conv["lastActivityAt"] = delivered_at
    return conv


def _raw_page(elements: list[dict], token: str = "") -> dict:
    box = {"elements": elements}
    if token:
        box["nextSyncToken"] = token
    return {"data": {"messengerConversationsBySyncToken": box}}


def test_other_participant_returns_non_self():
    conv = _conversation("urn:li:msg_conv:1", "jane-buyer")
    info = _other_participant(
        conv,
        self_urn="urn:li:fsd_profile:self",
        self_name="Arian Taj",
    )
    assert info is not None
    assert info.public_id == "jane-buyer"
    assert info.full_name == "Jane Buyer"


def test_other_participant_skips_group_conversation():
    conv = _conversation("urn:li:msg_conv:1", "jane-buyer")
    conv["conversationParticipants"].append(
        _participant("urn:li:fsd_profile:third", "Third", "Person", "third-person")
    )
    assert _other_participant(
        conv,
        self_urn="urn:li:fsd_profile:self",
        self_name="Arian Taj",
    ) is None


def test_next_sync_token_reads_nested_page_token():
    raw = {"data": {"messengerConversationsBySyncToken": {"paging": {"nextSyncToken": "next-1"}}}}
    assert _next_sync_token(raw) == "next-1"


def test_fallback_profile_uses_inbox_participant_payload():
    conv = _conversation("urn:li:msg_conv:1", "")
    conv["conversationParticipants"][1] = _participant(
        "urn:li:fsd_profile:ACoPRIVATE",
        "Jane",
        "Buyer",
        profile_url="https://www.linkedin.com/in/ACoPRIVATE/",
        headline="FedRAMP Program Lead at Acme",
    )

    info = _other_participant(
        conv,
        self_urn="urn:li:fsd_profile:self",
        self_name="Arian Taj",
    )
    profile = _fallback_profile(info)

    assert profile["public_identifier"] == "ACoPRIVATE"
    assert profile["url"] == "https://www.linkedin.com/in/ACoPRIVATE/"
    assert profile["urn"] == "urn:li:fsd_profile:ACoPRIVATE"
    assert profile["headline"] == "FedRAMP Program Lead at Acme"


def test_iter_inbox_conversations_uses_category_pagination_for_older_pages():
    now_ms = int(timezone.now().timestamp() * 1000)
    first_page = _raw_page([
        _conversation("urn:li:msg_conv:1", "jane-buyer", delivered_at=now_ms),
    ])
    older_page = {
        "data": {
            "messengerConversationsByCategoryQuery": {
                "elements": [
                    _conversation("urn:li:msg_conv:2", "john-ciso", delivered_at=now_ms),
                ],
            }
        }
    }

    with patch(
        "linkedin.management.commands.discover_inbox_leads.fetch_conversations",
        return_value=first_page,
    ) as fetch, patch(
        "linkedin.management.commands.discover_inbox_leads.fetch_conversations_by_category",
        return_value=older_page,
    ) as fetch_older:
        conversations = list(
            iter_inbox_conversations(
                "api",
                self_urn="urn:li:fsd_profile:self",
                self_name="Arian Taj",
                since_days=90,
                max_pages=3,
                page_size=20,
                page_delay_seconds=0,
            )
        )

    assert [c.conversation_urn for c in conversations] == [
        "urn:li:msg_conv:1",
        "urn:li:msg_conv:2",
    ]
    assert fetch.call_args_list[0].kwargs["sync_token"] is None
    assert fetch_older.call_args_list[0].kwargs["last_updated_before"] == now_ms


def test_existing_skip_reason_skips_existing_lead(db):
    Lead.objects.create(linkedin_url="https://www.linkedin.com/in/jane-buyer/")
    assert existing_skip_reason(
        public_id="jane-buyer",
        conversation_urn="urn:li:msg_conv:new",
    ) == "existing lead"


def test_existing_skip_reason_skips_existing_thread(db):
    lead = Lead.objects.create(linkedin_url="https://www.linkedin.com/in/existing-thread/")
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id="urn:li:msg:1",
        direction=Message.Direction.OUTBOUND,
        sender="Arian Taj",
        body="hello",
        sent_at=timezone.now(),
        thread_external_id="urn:li:msg_conv:existing",
    )
    assert existing_skip_reason(
        public_id="new-person",
        conversation_urn="urn:li:msg_conv:existing",
    ) == "existing thread"


def test_existing_skip_reason_skips_existing_member_urn(db):
    Lead.objects.create(
        linkedin_url="https://www.linkedin.com/in/jane-buyer/",
        description='{"urn": "urn:li:fsd_profile:ACoPRIVATE"}',
    )
    assert existing_skip_reason(
        public_id="ACoPRIVATE",
        conversation_urn="urn:li:msg_conv:new",
        member_urn="urn:li:fsd_profile:ACoPRIVATE",
    ) == "existing lead"


def test_decision_icp_normalizes_bucket_aliases():
    decision = InboxLeadDecision(
        should_import=True,
        icp="3paos",
        category="3paos",
        reason="FedRAMP assessor.",
    )
    assert _decision_icp(decision) == "3PAOs/Assessors"


def test_apply_inbox_import_creates_connected_deal_and_messages(fake_session, monkeypatch):
    def fake_store(lead, public_id, profile):
        lead.first_name = profile["first_name"]
        lead.last_name = profile["last_name"]
        lead.company_name = profile["positions"][0]["company_name"]
        lead.description = "{}"
        lead.save(update_fields=["first_name", "last_name", "company_name", "description"])

    monkeypatch.setattr(
        "linkedin.management.commands.discover_inbox_leads._store_profile_and_embedding",
        fake_store,
    )

    profile = {
        "public_identifier": "jane-buyer",
        "first_name": "Jane",
        "last_name": "Buyer",
        "positions": [{"company_name": "Acme GovCloud"}],
    }
    parsed = [
        {
            "entity_urn": "urn:li:msg:out",
            "sender": "Arian Taj",
            "text": "Jane, can we compare FedRAMP notes?",
            "timestamp": "2026-06-01 12:00",
        },
        {
            "entity_urn": "urn:li:msg:in",
            "sender": "Jane Buyer",
            "text": "Yes, we are looking at FedRAMP 20x.",
            "timestamp": "2026-06-02 13:00",
        },
    ]
    decision = InboxLeadDecision(
        should_import=True,
        icp="CSPs",
        category="CSPs",
        reason="GovCloud compliance buyer discussing FedRAMP.",
    )

    result = apply_inbox_import(
        campaign=fake_session.campaign,
        public_id="jane-buyer",
        profile=profile,
        parsed_messages=parsed,
        conversation_urn="urn:li:msg_conv:jane",
        decision=decision,
        sender="Arian Taj",
    )

    assert result.status == "created"
    lead = Lead.objects.get(linkedin_url="https://www.linkedin.com/in/jane-buyer/")
    assert lead.company_name == "Acme GovCloud"
    assert lead.icp == "CSPs"
    deal = Deal.objects.get(lead=lead, campaign=fake_session.campaign)
    assert deal.state == ProfileState.CONNECTED
    assert deal.last_reply_at is not None
    assert "Inbox discovery" in deal.reason
    assert Message.objects.filter(lead=lead).count() == 2
    assert Message.objects.filter(lead=lead, direction=Message.Direction.INBOUND).count() == 1

    second = apply_inbox_import(
        campaign=fake_session.campaign,
        public_id="jane-buyer",
        profile=profile,
        parsed_messages=parsed,
        conversation_urn="urn:li:msg_conv:jane",
        decision=decision,
        sender="Arian Taj",
    )
    assert second.status == "skipped"
    assert second.reason == "existing lead"
    assert Lead.objects.filter(linkedin_url="https://www.linkedin.com/in/jane-buyer/").count() == 1
