from __future__ import annotations

import base64
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from django.core.management import call_command

from crm.models import Lead, Meeting, SalesOwner
from gmail import data_sync


class _Request:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _Messages:
    def __init__(self, messages):
        self.messages = messages
        self.list_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _Request({
            "messages": [{"id": msg["id"], "threadId": msg.get("threadId", "")}
                         for msg in self.messages],
        })

    def get(self, **kwargs):
        message_id = kwargs["id"]
        return _Request(next(msg for msg in self.messages if msg["id"] == message_id))


class _Threads:
    def __init__(self, threads):
        self.threads = {thread["id"]: thread for thread in threads}
        self.get_calls = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return _Request(self.threads[kwargs["id"]])


class _Users:
    def __init__(self, messages, threads):
        self._messages = _Messages(messages)
        self._threads = _Threads(threads)

    def messages(self):
        return self._messages

    def threads(self):
        return self._threads


class _Service:
    def __init__(self, messages, threads=()):
        self._users = _Users(messages, threads)

    def users(self):
        return self._users


class _Client:
    account_key = "eddy_boundera"
    operator = "Arian"

    def __init__(self, messages, threads=()):
        self._service = _Service(messages, threads)


def _gmail_note_message(*, id="m1", subject, body, when):
    encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
    return {
        "id": id,
        "threadId": f"thread-{id}",
        "internalDate": str(int(when.timestamp() * 1000)),
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "Gemini <gemini-notes@google.com>"},
                {"name": "Date", "value": when.strftime("%a, %d %b %Y %H:%M:%S +0000")},
            ],
            "mimeType": "text/plain",
            "body": {"data": encoded},
        },
    }


def _thread_message(
    *,
    id,
    thread_id,
    sender,
    to,
    subject="Working session",
    when,
    extra_headers=(),
    labels=(),
):
    return {
        "id": id,
        "threadId": thread_id,
        "internalDate": str(int(when.timestamp() * 1000)),
        "labelIds": list(labels),
        "snippet": f"message {id}",
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": when.strftime("%a, %d %b %Y %H:%M:%S +0000")},
                *[
                    {"name": name, "value": value}
                    for name, value in extra_headers
                ],
            ],
        },
    }


@pytest.fixture
def lead(db):
    return Lead.objects.create(
        first_name="Rene",
        last_name="Jones",
        company_name="Boundera",
        linkedin_url="https://www.linkedin.com/in/rene-jones/",
        email="rene@example.com",
    )


def test_candidate_leads_include_email_only_and_do_not_outreach_records(db):
    email_only = Lead.objects.create(
        first_name="Zelia",
        last_name="Pantani",
        company_name="Ramp",
        linkedin_url="https://www.linkedin.com/in/zelia-pantani/",
        email="zelia@ramp.example",
    )
    do_not_outreach = Lead.objects.create(
        first_name="Taylor",
        last_name="Stack",
        company_name="stackArmor",
        linkedin_url="https://www.linkedin.com/in/taylor-stack/",
        email="taylor@stackarmor.example",
        disqualified=True,
    )
    Lead.objects.create(
        first_name="LinkedIn",
        last_name="Only",
        linkedin_url="https://www.linkedin.com/in/linkedin-only/",
    )

    assert list(data_sync.candidate_leads().values_list("id", flat=True)) == [
        email_only.id,
        do_not_outreach.id,
    ]


def test_sync_threads_batches_exact_addresses_and_fetches_each_thread_once(db):
    leads = [
        Lead.objects.create(
            first_name=f"Person{i}",
            linkedin_url=f"https://www.linkedin.com/in/person-{i}/",
            email=f"person{i}@example.com",
        )
        for i in range(41)
    ]
    client = _Client([])

    result = data_sync.sync_gmail_threads(
        client=client,
        leads=leads,
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=True,
        discover_unmapped=False,
    )

    calls = client._service.users().messages().list_calls
    assert result.gmail_search_queries == 2
    assert len(calls) == 2
    assert all("newer_than:365d" in call["q"] for call in calls)
    assert all("from:" in call["q"] and "to:" in call["q"] for call in calls)
    assert client._service.users().threads().get_calls == []


def test_sync_threads_requires_a_valid_mailbox_identity(db):
    with pytest.raises(ValueError, match="valid mailbox identity"):
        data_sync.sync_gmail_threads(
            client=_Client([]),
            leads=[],
            self_emails={"not-an-email"},
            since_days=365,
            dry_run=True,
        )


def test_list_messages_enforces_cross_page_item_cap():
    calls = []

    class PagedMessages:
        def list(self, **kwargs):
            calls.append(kwargs)
            start = 0 if "pageToken" not in kwargs else 500
            return _Request({
                "messages": [
                    {"id": f"m-{index}", "threadId": f"t-{index}"}
                    for index in range(start, start + 500)
                ],
                "nextPageToken": "page-2",
            })

    class PagedUsers:
        def messages(self):
            return PagedMessages()

    class PagedService:
        def users(self):
            return PagedUsers()

    found = list(data_sync._list_messages(
        PagedService(),
        query="newer_than:90d",
        max_items=501,
    ))

    assert len(found) == 501
    assert [call["maxResults"] for call in calls] == [500, 1]


def test_sync_threads_discovers_ramp_style_lead_without_deal_and_is_idempotent(db):
    lead = Lead.objects.create(
        first_name="Zelia",
        last_name="Pantani",
        company_name="Ramp",
        linkedin_url="https://www.linkedin.com/in/zelia-ramp/",
        email="Zelia@Ramp.example",
        disqualified=True,
    )
    when = datetime.now(tz=timezone.utc) - timedelta(days=6)
    outbound = _thread_message(
        id="out-1",
        thread_id="thread-ramp",
        sender="Arian <arian@getboundera.com>",
        to="Zelia Pantani <zelia@ramp.example>",
        when=when,
    )
    inbound = _thread_message(
        id="in-1",
        thread_id="thread-ramp",
        sender="Zelia Pantani <zelia@ramp.example>",
        to="Arian <arian@getboundera.com>",
        when=when.replace(hour=15),
    )
    client = _Client(
        [outbound, inbound],
        [{"id": "thread-ramp", "messages": [outbound, inbound]}],
    )

    first = data_sync.sync_gmail_threads(
        client=client,
        leads=list(data_sync.candidate_leads()),
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=False,
        discover_unmapped=False,
    )
    second = data_sync.sync_gmail_threads(
        client=client,
        leads=list(data_sync.candidate_leads()),
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=False,
        discover_unmapped=False,
    )

    assert first.leads_with_email_threads == 1
    assert first.gmail_threads_matched == 1
    assert first.gmail_human_inbound_messages == 1
    assert first.gmail_messages_created == 2
    assert second.gmail_messages_created == 0
    assert client._service.users().threads().get_calls[0]["format"] == "full"
    assert set(lead.messages.values_list("direction", flat=True)) == {
        "inbound",
        "outbound",
    }


def test_sync_threads_excludes_auto_reply_from_meaningful_evidence(db):
    lead = Lead.objects.create(
        first_name="Riley",
        linkedin_url="https://www.linkedin.com/in/riley-auto/",
        email="riley@example.com",
    )
    when = datetime.now(tz=timezone.utc) - timedelta(days=6)
    outbound = _thread_message(
        id="out-auto",
        thread_id="thread-auto",
        sender="Arian <arian@getboundera.com>",
        to="Riley <riley@example.com>",
        when=when,
    )
    auto_reply = _thread_message(
        id="in-auto",
        thread_id="thread-auto",
        sender="Riley <riley@example.com>",
        to="Arian <arian@getboundera.com>",
        subject="Automatic reply: Working session",
        when=when.replace(hour=15),
        extra_headers=(("Auto-Submitted", "auto-replied"),),
    )
    client = _Client(
        [outbound, auto_reply],
        [{"id": "thread-auto", "messages": [outbound, auto_reply]}],
    )

    result = data_sync.sync_gmail_threads(
        client=client,
        leads=[lead],
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=False,
        discover_unmapped=False,
    )

    assert result.gmail_automated_messages_skipped == 1
    assert result.gmail_human_inbound_messages == 0
    assert result.gmail_messages_created == 1
    assert list(lead.messages.values_list("external_id", flat=True)) == [
        "eddy_boundera:out-auto"
    ]


def test_sync_threads_returns_only_bidirectional_unmapped_humans(db):
    when = (datetime.now(tz=timezone.utc) - timedelta(days=6)).replace(
        microsecond=0
    )
    outbound = _thread_message(
        id="out-new",
        thread_id="thread-new",
        sender="Arian <arian@getboundera.com>",
        to="Maddie Advisor <maddie@steelpatriot.example>",
        when=when,
    )
    inbound = _thread_message(
        id="in-new",
        thread_id="thread-new",
        sender="Maddie Advisor <maddie@steelpatriot.example>",
        to="Arian <arian@getboundera.com>",
        when=when.replace(hour=15),
    )
    client = _Client(
        [outbound, inbound],
        [{"id": "thread-new", "messages": [outbound, inbound]}],
    )

    result = data_sync.sync_gmail_threads(
        client=client,
        leads=[],
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=True,
        discover_unmapped=True,
        discovery_since_days=90,
        discovery_max_messages=50,
        discovery_max_threads=10,
    )

    assert result.discovery_messages_scanned == 2
    assert result.discovery_threads_selected == 1
    assert client._service.users().threads().get_calls == [{
        "userId": "me",
        "id": "thread-new",
        "format": "metadata",
        "metadataHeaders": list(data_sync._DISCOVERY_METADATA_HEADERS),
    }]
    assert result.unmapped_external_participants == [{
        "account_key": "eddy_boundera",
        "email": "maddie@steelpatriot.example",
        "display_name": "Maddie Advisor",
        "domain": "steelpatriot.example",
        "last_inbound_at": when.replace(hour=15).isoformat(),
        "latest_thread_id": "eddy_boundera:thread-new",
        "thread_count": 1,
    }]
    assert Lead.objects.count() == 0


def test_unmapped_discovery_rejects_newsletters_and_one_sided_mail(db):
    when = datetime.now(tz=timezone.utc) - timedelta(days=6)
    newsletter = _thread_message(
        id="news-1",
        thread_id="thread-news",
        sender="Alex Human <alex@vendor.example>",
        to="Arian <arian@getboundera.com>",
        when=when,
        extra_headers=(("List-Unsubscribe", "<mailto:unsubscribe@vendor.example>"),),
    )
    one_sided = _thread_message(
        id="one-1",
        thread_id="thread-one",
        sender="Casey Human <casey@buyer.example>",
        to="Arian <arian@getboundera.com>",
        when=when.replace(hour=15),
    )
    client = _Client(
        [newsletter, one_sided],
        [
            {"id": "thread-news", "messages": [newsletter]},
            {"id": "thread-one", "messages": [one_sided]},
        ],
    )

    result = data_sync.sync_gmail_threads(
        client=client,
        leads=[],
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=True,
        discover_unmapped=True,
    )

    assert result.gmail_automated_messages_skipped == 1
    assert result.unmapped_external_participants == []


def test_unmapped_discovery_excludes_same_company_mailbox_domain(db):
    when = datetime.now(tz=timezone.utc) - timedelta(days=6)
    outbound = _thread_message(
        id="out-internal",
        thread_id="thread-internal",
        sender="Arian <arian@getboundera.com>",
        to="Teammate <teammate@getboundera.com>",
        when=when,
    )
    inbound = _thread_message(
        id="in-internal",
        thread_id="thread-internal",
        sender="Teammate <teammate@getboundera.com>",
        to="Arian <arian@getboundera.com>",
        when=when.replace(hour=15),
    )
    client = _Client(
        [outbound, inbound],
        [{"id": "thread-internal", "messages": [outbound, inbound]}],
    )

    result = data_sync.sync_gmail_threads(
        client=client,
        leads=[],
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=True,
        discover_unmapped=True,
    )

    assert result.unmapped_external_participants == []


def test_unmapped_discovery_excludes_drafts_and_old_cross_window_replies(db):
    now = datetime.now(tz=timezone.utc)
    old_outbound = _thread_message(
        id="out-old",
        thread_id="thread-window",
        sender="Arian <arian@getboundera.com>",
        to="Buyer <buyer@fresh.example>",
        when=now - timedelta(days=120),
    )
    inbound = _thread_message(
        id="in-fresh",
        thread_id="thread-window",
        sender="Buyer <buyer@fresh.example>",
        to="Arian <arian@getboundera.com>",
        when=now - timedelta(days=1),
    )
    draft = _thread_message(
        id="draft-1",
        thread_id="thread-draft",
        sender="Arian <arian@getboundera.com>",
        to="Draft Buyer <draft@fresh.example>",
        when=now - timedelta(days=1),
        labels=("DRAFT",),
    )
    draft_inbound = _thread_message(
        id="draft-in",
        thread_id="thread-draft",
        sender="Draft Buyer <draft@fresh.example>",
        to="Arian <arian@getboundera.com>",
        when=now,
    )
    client = _Client(
        [old_outbound, inbound, draft, draft_inbound],
        [
            {"id": "thread-window", "messages": [old_outbound, inbound]},
            {"id": "thread-draft", "messages": [draft, draft_inbound]},
        ],
    )

    result = data_sync.sync_gmail_threads(
        client=client,
        leads=[],
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=True,
        discover_unmapped=True,
        discovery_since_days=90,
    )

    assert result.gmail_unsent_messages_skipped == 1
    assert result.unmapped_external_participants == []


def test_multi_party_thread_maps_each_inbound_to_its_exact_lead(db):
    zelia = Lead.objects.create(
        first_name="Zelia",
        linkedin_url="https://www.linkedin.com/in/zelia-multi/",
        email="zelia@ramp.example",
    )
    lindsey = Lead.objects.create(
        first_name="Lindsey",
        linkedin_url="https://www.linkedin.com/in/lindsey-multi/",
        email="lindsey@ramp.example",
    )
    when = datetime.now(tz=timezone.utc) - timedelta(days=1)
    outbound = _thread_message(
        id="multi-out",
        thread_id="thread-multi",
        sender="Arian <arian@getboundera.com>",
        to=(
            "Zelia <zelia@ramp.example>, "
            "Lindsey <lindsey@ramp.example>"
        ),
        when=when,
    )
    zelia_in = _thread_message(
        id="zelia-in",
        thread_id="thread-multi",
        sender="Zelia <zelia@ramp.example>",
        to="Arian <arian@getboundera.com>",
        when=when + timedelta(hours=1),
    )
    lindsey_in = _thread_message(
        id="lindsey-in",
        thread_id="thread-multi",
        sender="Lindsey <lindsey@ramp.example>",
        to="Arian <arian@getboundera.com>",
        when=when + timedelta(hours=2),
    )
    client = _Client(
        [outbound, zelia_in, lindsey_in],
        [{
            "id": "thread-multi",
            "messages": [outbound, zelia_in, lindsey_in],
        }],
    )

    result = data_sync.sync_gmail_threads(
        client=client,
        leads=[zelia, lindsey],
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=False,
        discover_unmapped=False,
    )

    assert result.gmail_threads_ambiguous == 1
    assert result.gmail_messages_created == 2
    assert list(zelia.messages.values_list("external_id", flat=True)) == [
        "eddy_boundera:zelia-in"
    ]
    assert list(lindsey.messages.values_list("external_id", flat=True)) == [
        "eddy_boundera:lindsey-in"
    ]


def test_shared_mailbox_outbound_owner_comes_from_send_as_alias(db):
    leili_owner = SalesOwner.objects.get(handle="Leili")
    lead = Lead.objects.create(
        first_name="Buyer",
        linkedin_url="https://www.linkedin.com/in/shared-mailbox-buyer/",
        email="buyer@example.com",
    )
    message = _thread_message(
        id="leili-out",
        thread_id="thread-leili",
        sender="Leili <leili@getboundera.com>",
        to="Buyer <buyer@example.com>",
        when=datetime.now(tz=timezone.utc) - timedelta(days=1),
    )
    client = _Client(
        [message],
        [{"id": "thread-leili", "messages": [message]}],
    )

    result = data_sync.sync_gmail_threads(
        client=client,
        leads=[lead],
        self_emails={"ariant@getboundera.com", "leili@getboundera.com"},
        since_days=365,
        dry_run=False,
        discover_unmapped=False,
        operator_by_self_email={"leili@getboundera.com": "Leili"},
    )

    stored = lead.messages.get(external_id="eddy_boundera:leili-out")
    assert result.gmail_messages_created == 1
    assert stored.operator == leili_owner


def test_thread_fetch_budget_defers_unseen_threads(db):
    lead = Lead.objects.create(
        first_name="Buyer",
        linkedin_url="https://www.linkedin.com/in/quota-buyer/",
        email="buyer@example.com",
    )
    when = datetime.now(tz=timezone.utc) - timedelta(days=1)
    messages = [
        _thread_message(
            id=f"quota-{index}",
            thread_id=f"thread-quota-{index}",
            sender="Buyer <buyer@example.com>",
            to="Arian <arian@getboundera.com>",
            when=when + timedelta(minutes=index),
        )
        for index in range(5)
    ]
    client = _Client(
        messages,
        [
            {"id": message["threadId"], "messages": [message]}
            for message in messages
        ],
    )

    result = data_sync.sync_gmail_threads(
        client=client,
        leads=[lead],
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=True,
        discover_unmapped=False,
        max_thread_fetches=2,
    )

    assert result.gmail_threads_fetched == 2
    assert result.gmail_threads_deferred == 3


def test_thread_checkpoint_rotates_past_unpersisted_threads(db):
    lead = Lead.objects.create(
        first_name="Buyer",
        linkedin_url="https://www.linkedin.com/in/checkpoint-buyer/",
        email="buyer@example.com",
    )
    when = datetime.now(tz=timezone.utc) - timedelta(days=1)
    messages = [
        _thread_message(
            id=f"checkpoint-{index}",
            thread_id=f"thread-checkpoint-{index}",
            sender="Buyer <buyer@example.com>",
            to="Arian <arian@getboundera.com>",
            when=when + timedelta(minutes=index),
        )
        for index in range(5)
    ]
    threads = [
        {"id": message["threadId"], "messages": [message]}
        for message in messages
    ]

    first = data_sync.sync_gmail_threads(
        client=_Client(messages, threads),
        leads=[lead],
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=True,
        discover_unmapped=False,
        max_thread_fetches=2,
    )
    second = data_sync.sync_gmail_threads(
        client=_Client(messages, threads),
        leads=[lead],
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=True,
        discover_unmapped=False,
        max_thread_fetches=2,
        processed_thread_versions=first.gmail_processed_thread_versions,
    )

    assert first.gmail_threads_fetched == 2
    assert first.gmail_threads_deferred == 3
    assert second.gmail_threads_fetched == 2
    assert second.gmail_threads_deferred == 1
    assert len(second.gmail_processed_thread_versions) == 4


def test_thread_checkpoint_reopens_when_a_new_message_arrives(db):
    lead = Lead.objects.create(
        first_name="Buyer",
        linkedin_url="https://www.linkedin.com/in/checkpoint-reopen/",
        email="buyer@example.com",
    )
    when = datetime.now(tz=timezone.utc) - timedelta(days=1)
    first_message = _thread_message(
        id="checkpoint-first",
        thread_id="thread-checkpoint",
        sender="Buyer <buyer@example.com>",
        to="Arian <arian@getboundera.com>",
        when=when,
    )
    first = data_sync.sync_gmail_threads(
        client=_Client(
            [first_message],
            [{"id": "thread-checkpoint", "messages": [first_message]}],
        ),
        leads=[lead],
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=True,
        discover_unmapped=False,
    )
    reply = _thread_message(
        id="checkpoint-reply",
        thread_id="thread-checkpoint",
        sender="Arian <arian@getboundera.com>",
        to="Buyer <buyer@example.com>",
        when=when + timedelta(hours=1),
    )
    second = data_sync.sync_gmail_threads(
        client=_Client(
            [first_message, reply],
            [{
                "id": "thread-checkpoint",
                "messages": [first_message, reply],
            }],
        ),
        leads=[lead],
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=True,
        discover_unmapped=False,
        processed_thread_versions=first.gmail_processed_thread_versions,
    )

    assert first.gmail_threads_fetched == 1
    assert second.gmail_threads_fetched == 1
    assert (
        second.gmail_processed_thread_versions
        != first.gmail_processed_thread_versions
    )


def test_truncated_known_batch_splits_for_fair_address_discovery(db):
    leads = [
        Lead.objects.create(
            first_name=f"Person{index}",
            linkedin_url=f"https://www.linkedin.com/in/fair-{index}/",
            email=f"person{index}@example.com",
        )
        for index in range(2)
    ]
    calls = []

    class QueryMessages:
        def list(self, **kwargs):
            calls.append(kwargs)
            query = kwargs["q"]
            if "person0@example.com" in query and "person1@example.com" in query:
                return _Request({
                    "messages": [
                        {"id": f"noisy-{index}", "threadId": f"noisy-{index}"}
                        for index in range(500)
                    ],
                    "nextPageToken": "more",
                })
            person = "person0" if "person0@example.com" in query else "person1"
            return _Request({
                "messages": [{"id": f"{person}-1", "threadId": f"{person}-thread"}],
            })

    class QueryUsers:
        def messages(self):
            return QueryMessages()

    class QueryService:
        def users(self):
            return QueryUsers()

    client = SimpleNamespace(account_key="fair", _service=QueryService())
    result = data_sync.sync_gmail_threads(
        client=client,
        leads=leads,
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=True,
        discover_unmapped=False,
        max_thread_fetches=0,
    )

    assert result.gmail_search_queries == 3
    assert result.gmail_search_batches_split == 1
    assert result.gmail_search_queries_at_cap == 0
    assert result.gmail_search_messages_seen == 2
    assert len(calls) == 3


def test_human_known_contact_is_not_dropped_by_gmail_category_label(db):
    lead = Lead.objects.create(
        first_name="Buyer",
        linkedin_url="https://www.linkedin.com/in/category-human/",
        email="buyer@example.com",
    )
    message = _thread_message(
        id="category-human",
        thread_id="thread-category-human",
        sender="Buyer <buyer@example.com>",
        to="Arian <arian@getboundera.com>",
        subject="OOO coverage plan",
        when=datetime.now(tz=timezone.utc) - timedelta(days=1),
        labels=("CATEGORY_PROMOTIONS",),
    )
    result = data_sync.sync_gmail_threads(
        client=_Client(
            [message],
            [{"id": "thread-category-human", "messages": [message]}],
        ),
        leads=[lead],
        self_emails={"arian@getboundera.com"},
        since_days=365,
        dry_run=False,
        discover_unmapped=False,
    )

    assert result.gmail_automated_messages_skipped == 0
    assert result.gmail_messages_created == 1


def test_gmail_message_ids_are_scoped_to_the_mailbox(db):
    first_lead = Lead.objects.create(
        first_name="First",
        linkedin_url="https://www.linkedin.com/in/mailbox-first/",
        email="first@example.com",
    )
    second_lead = Lead.objects.create(
        first_name="Second",
        linkedin_url="https://www.linkedin.com/in/mailbox-second/",
        email="second@example.com",
    )
    when = datetime.now(tz=timezone.utc) - timedelta(days=1)
    first_message = _thread_message(
        id="same-raw-id",
        thread_id="same-thread-id",
        sender="First <first@example.com>",
        to="Arian <arian@getboundera.com>",
        when=when,
    )
    second_message = _thread_message(
        id="same-raw-id",
        thread_id="same-thread-id",
        sender="Second <second@example.com>",
        to="Eddy <eddy@getboundera.com>",
        when=when,
    )
    first_client = _Client(
        [first_message],
        [{"id": "same-thread-id", "messages": [first_message]}],
    )
    first_client.account_key = "arian_boundera"
    second_client = _Client(
        [second_message],
        [{"id": "same-thread-id", "messages": [second_message]}],
    )
    second_client.account_key = "eddy_boundera"

    for client, lead, self_email in (
        (first_client, first_lead, "arian@getboundera.com"),
        (second_client, second_lead, "eddy@getboundera.com"),
    ):
        data_sync.sync_gmail_threads(
            client=client,
            leads=[lead],
            self_emails={self_email},
            since_days=365,
            dry_run=False,
            discover_unmapped=False,
        )

    assert set(first_lead.messages.values_list("external_id", flat=True)) == {
        "arian_boundera:same-raw-id"
    }
    assert set(second_lead.messages.values_list("external_id", flat=True)) == {
        "eddy_boundera:same-raw-id"
    }


def test_sync_note_email_attaches_to_existing_meeting(lead):
    meeting = Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="cal-1",
        lead=lead,
        start_at=datetime(2026, 6, 29, 16, 0, tzinfo=timezone.utc),
        title="Rene Boundera Intro",
    )
    msg = _gmail_note_message(
        subject="Notes: “Rene Boundera Intro” Jun 29, 2026",
        body="Notes from Rene Boundera Intro Summary discussed FedRAMP next steps.",
        when=datetime(2026, 6, 29, 17, 0, tzinfo=timezone.utc),
    )

    result = data_sync.sync_gmail_note_emails(
        client=_Client([msg]),
        leads=[lead],
        since_days=365,
        dry_run=False,
    )

    meeting.refresh_from_db()
    assert result.note_emails_seen == 1
    assert result.note_emails_matched == 1
    assert result.note_emails_updated_meetings == 1
    assert meeting.gemini_doc_id == "gmail:eddy_boundera:m1"
    assert "FedRAMP next steps" in meeting.gemini_notes_raw


def test_sync_note_email_creates_meeting_for_unique_named_lead(db):
    lead = Lead.objects.create(
        first_name="Michael",
        last_name="Schroeder",
        company_name="Excentium",
        linkedin_url="https://www.linkedin.com/in/michael-schroeder/",
        email="michael@example.com",
    )
    msg = _gmail_note_message(
        subject="Notes: “Michael Schroeder Boundera Catchup” Jun 25, 2026",
        body="Notes from Michael Schroeder Boundera Catchup Summary asked for a Loom.",
        when=datetime(2026, 6, 25, 19, 0, tzinfo=timezone.utc),
    )

    result = data_sync.sync_gmail_note_emails(
        client=_Client([msg]),
        leads=[lead],
        since_days=365,
        dry_run=False,
    )

    meeting = Meeting.objects.get()
    assert result.note_emails_created_meetings == 1
    assert meeting.lead == lead
    assert meeting.external_id == "gmail-note:m1"
    assert meeting.title == "Michael Schroeder Boundera Catchup"
    assert "asked for a Loom" in meeting.gemini_notes_raw


def test_sync_note_email_leaves_generic_title_unmatched(lead):
    msg = _gmail_note_message(
        subject="Notes: Meeting Jul 6, 2026 at 12:39 PM EDT",
        body="Notes from generic meeting Summary no attendee names here.",
        when=datetime(2026, 7, 6, 17, 0, tzinfo=timezone.utc),
    )

    result = data_sync.sync_gmail_note_emails(
        client=_Client([msg]),
        leads=[lead],
        since_days=365,
        dry_run=False,
    )

    assert result.note_emails_unmatched == 1
    assert Meeting.objects.count() == 0


def test_note_title_identity_uses_exact_tokens_and_ignores_surname_initial(db):
    wrong = Lead.objects.create(
        first_name="John",
        last_name="S.",
        company_name="Cloudflare",
        linkedin_url="https://www.linkedin.com/in/john-s-cloudflare/",
        email="john.s@cloudflare.example",
    )
    correct = Lead.objects.create(
        first_name="John",
        last_name="Allison",
        company_name="Mind Anvil",
        linkedin_url="https://www.linkedin.com/in/john-allison/",
        email="john@mindanvil.example",
    )

    assert data_sync._unique_lead_for_note_title(
        "John Allison Catchup",
        [wrong, correct],
    ) == correct
    assert data_sync._unique_lead_for_note_title(
        "John Allison Catchup",
        [wrong],
    ) is None


def test_sync_note_email_does_not_preserve_identity_invalid_synthetic_match(db):
    wrong = Lead.objects.create(
        first_name="John",
        last_name="S.",
        company_name="Cloudflare",
        linkedin_url="https://www.linkedin.com/in/john-s-cloudflare-existing/",
        email="john.s.existing@cloudflare.example",
    )
    correct = Lead.objects.create(
        first_name="John",
        last_name="Allison",
        company_name="Mind Anvil",
        linkedin_url="https://www.linkedin.com/in/john-allison-existing/",
        email="john.existing@mindanvil.example",
    )
    when = datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)
    bad_meeting = Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="gmail-note:m1",
        lead=wrong,
        start_at=when,
        title="John Allison Catchup",
        gemini_notes_raw="previous notes must remain untouched",
        raw={
            "source": "gmail_note_email",
            "message_id": "m1",
            "subject": "Notes: John Allison Catchup Jul 15, 2026",
        },
    )
    msg = _gmail_note_message(
        subject="Notes: John Allison Catchup Jul 15, 2026",
        body="New notes that must not be attached to Cloudflare.",
        when=when,
    )

    result = data_sync.sync_gmail_note_emails(
        client=_Client([msg]),
        leads=[wrong, correct],
        since_days=365,
        dry_run=False,
    )

    bad_meeting.refresh_from_db()
    assert result.note_emails_matched == 0
    assert result.note_emails_unmatched == 1
    assert Meeting.objects.count() == 1
    assert bad_meeting.lead == wrong
    assert bad_meeting.gemini_notes_raw == "previous notes must remain untouched"


def test_audit_gmail_note_meetings_reports_only_invalid_rows_and_never_writes(
    db,
    tmp_path,
):
    wrong = Lead.objects.create(
        first_name="John",
        last_name="S.",
        company_name="Cloudflare",
        linkedin_url="https://www.linkedin.com/in/john-s-cloudflare-audit/",
        email="john.s.audit@cloudflare.example",
    )
    correct = Lead.objects.create(
        first_name="John",
        last_name="Allison",
        company_name="Mind Anvil",
        linkedin_url="https://www.linkedin.com/in/john-allison-audit/",
        email="john.audit@mindanvil.example",
    )
    when = datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)
    invalid = Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="gmail-note:invalid",
        lead=wrong,
        start_at=when,
        title="John Allison Catchup",
        raw={
            "source": "gmail_note_email",
            "subject": "Notes: John Allison Catchup Jul 15, 2026",
        },
    )
    Meeting.objects.create(
        source=Meeting.Source.GOOGLE_CALENDAR,
        external_id="gmail-note:valid",
        lead=correct,
        start_at=when,
        title="John Allison Catchup",
        raw={
            "source": "gmail_note_email",
            "subject": "Notes: John Allison Catchup Jul 15, 2026",
        },
    )
    before = list(Meeting.objects.order_by("id").values())

    issues = data_sync.audit_gmail_note_meeting_identities()
    stdout = io.StringIO()
    private_output = tmp_path / "gmail-note-audit.json"
    call_command(
        "audit_gmail_note_meetings",
        output=str(private_output),
        stdout=stdout,
    )
    payload = json.loads(stdout.getvalue())
    private_payload = json.loads(private_output.read_text(encoding="utf-8"))

    assert [issue["meeting_id"] for issue in issues] == [invalid.id]
    assert payload["read_only"] is True
    assert payload["issue_count"] == 1
    assert payload["private_output_created"] is True
    assert "issues" not in payload
    assert private_payload["issues"][0]["meeting_id"] == invalid.id
    assert private_payload["issues"][0]["linked_company"] == "Cloudflare"
    assert private_output.stat().st_mode & 0o777 == 0o600
    assert list(Meeting.objects.order_by("id").values()) == before
