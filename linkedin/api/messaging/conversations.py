# linkedin/api/messaging/conversations.py
"""Retrieve conversations and messages via Voyager Messaging GraphQL API."""
import logging

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from linkedin.api.client import PlaywrightLinkedinAPI
from linkedin.api.messaging.utils import get_self_urn, encode_urn, check_response

logger = logging.getLogger(__name__)

_GRAPHQL_BASE = "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql"
_CONVERSATIONS_QUERY_ID = "messengerConversations.0d5e6781bbee71c3e51c8843c6519f48"
_CONVERSATIONS_CATEGORY_QUERY_ID = "messengerConversations.9501074288a12f3ae9e3c7ea243bccbf"
_MESSAGES_QUERY_ID = "messengerMessages.5846eeb71c981f11e0134cb6626cc314"


def _graphql_headers(api: PlaywrightLinkedinAPI) -> dict:
    headers = {**api.headers}
    headers["accept"] = "application/graphql"
    return headers


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type(IOError),
    reraise=True,
)
def fetch_conversations(
    api: PlaywrightLinkedinAPI,
    *,
    sync_token: str | None = None,
    count: int | None = None,
) -> dict:
    """Fetch a conversations page. Returns raw API response.

    ``sync_token`` and ``count`` are optional so existing recent-inbox callers
    keep their old behavior, while inbox-crawl commands can walk additional
    pages when LinkedIn returns a next sync token.
    """
    mailbox_urn = get_self_urn(api)
    variables = [f"mailboxUrn:{encode_urn(mailbox_urn)}"]
    if sync_token:
        variables.append(f"syncToken:{encode_urn(sync_token)}")
    if count:
        variables.append(f"count:{int(count)}")
    url = (
        f"{_GRAPHQL_BASE}"
        f"?queryId={_CONVERSATIONS_QUERY_ID}"
        f"&variables=({','.join(variables)})"
    )
    res = api.get(url, headers=_graphql_headers(api))
    check_response(res, "fetch_conversations")
    return res.json()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type(IOError),
    reraise=True,
)
def fetch_conversations_by_category(
    api: PlaywrightLinkedinAPI,
    *,
    last_updated_before: int | None = None,
    next_cursor: str | None = None,
    count: int | None = None,
    category: str = "PRIMARY_INBOX",
) -> dict:
    """Fetch an older inbox conversation page using LinkedIn's scrolled-list query.

    The initial recent-inbox query returns only LinkedIn's first visible batch.
    When the Messaging UI scrolls the conversation list, it switches to this
    category query with ``lastUpdatedBefore`` for the first older page and
    ``nextCursor`` for later pages.
    """
    if next_cursor and last_updated_before is not None:
        raise ValueError("Pass either next_cursor or last_updated_before, not both.")
    if not next_cursor and last_updated_before is None:
        raise ValueError("Need next_cursor or last_updated_before.")

    mailbox_urn = get_self_urn(api)
    variables = [
        f"query:(predicateUnions:List((conversationCategoryPredicate:(category:{category}))))",
        f"count:{int(count or 20)}",
        f"mailboxUrn:{encode_urn(mailbox_urn)}",
    ]
    if next_cursor:
        variables.append(f"nextCursor:{encode_urn(next_cursor)}")
    else:
        variables.append(f"lastUpdatedBefore:{int(last_updated_before)}")

    url = (
        f"{_GRAPHQL_BASE}"
        f"?queryId={_CONVERSATIONS_CATEGORY_QUERY_ID}"
        f"&variables=({','.join(variables)})"
    )
    res = api.get(url, headers=_graphql_headers(api))
    check_response(res, "fetch_conversations_by_category")
    return res.json()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type(IOError),
    reraise=True,
)
def fetch_messages(api: PlaywrightLinkedinAPI, conversation_urn: str) -> dict:
    """Fetch messages for a conversation. Returns raw API response."""
    url = (
        f"{_GRAPHQL_BASE}"
        f"?queryId={_MESSAGES_QUERY_ID}"
        f"&variables=(conversationUrn:{encode_urn(conversation_urn)})"
    )
    res = api.get(url, headers=_graphql_headers(api))
    check_response(res, "fetch_messages")
    return res.json()


if __name__ == "__main__":
    import os
    import argparse
    import json

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")

    import django
    django.setup()

    from linkedin.conf import get_first_active_profile_handle
    from linkedin.browser.registry import get_or_create_session

    logging.basicConfig(level=logging.DEBUG, format="[%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Fetch raw Voyager messaging data")
    parser.add_argument("--handle", default=None)
    parser.add_argument("--conversations", action="store_true", help="List recent conversations")
    parser.add_argument("--messages", default=None, metavar="CONVERSATION_URN", help="Fetch messages for a conversation URN")
    args = parser.parse_args()

    handle = args.handle or get_first_active_profile_handle()
    if not handle:
        print("No active LinkedInProfile found.")
        raise SystemExit(1)

    session = get_or_create_session(handle=handle)
    session.campaign = session.campaigns.first()
    session.ensure_browser()

    api = PlaywrightLinkedinAPI(session=session)

    if args.conversations:
        raw = fetch_conversations(api)
        elements = raw.get("data", {}).get("messengerConversationsBySyncToken", {}).get("elements", [])
        print(f"Got {len(elements)} conversations:\n")
        for conv in elements:
            urn = conv.get("entityUrn", "")
            participants = []
            for p in conv.get("conversationParticipants", []):
                member = p.get("participantType", {}).get("member", {})
                first = (member.get("firstName") or {}).get("text", "")
                last = (member.get("lastName") or {}).get("text", "")
                name = f"{first} {last}".strip()
                if name:
                    participants.append(name)
            print(f"  {', '.join(participants)}")
            print(f"    URN: {urn}\n")

    elif args.messages:
        raw = fetch_messages(api, args.messages)
        print(json.dumps(raw, indent=2, default=str)[:10000])

    else:
        parser.print_help()
