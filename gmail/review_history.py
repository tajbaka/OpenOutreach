"""Read-only recovery of unresolved Gmail additions for the contextual review."""

from collections.abc import Collection, Iterable, Mapping

from googleapiclient.errors import HttpError

from gmail.review_threads import verified_review_users

REVIEW_HISTORY_HOLDS_API = 1


def recover_history_additions(
    service,
    additions: Iterable[Mapping[str, str]],
    *,
    expected_mailbox: str,
    required_message_ids: Collection[str],
    required_thread_ids: Collection[str],
    unavailable_required: list[dict] | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """Read exact History messages not already covered by fresh full reads.

    Call after completing current discovery. Required IDs come from current
    searches and evidence linked to review items. Only a History-only reference
    whose exact message and thread both return 404 contributes to the skip count.
    An explicit unavailable_required list opts into item holds for exact required
    references that return 404. They are never counted as optional History skips.
    Auth, quota, service errors and malformed responses still propagate.
    """
    users = verified_review_users(service, expected_mailbox)

    references = {}
    for addition in additions:
        message_id = addition["id"]
        thread_id = addition["threadId"]
        if not message_id or not thread_id:
            raise ValueError("Gmail History addition is missing its exact message or thread ID")
        if references.setdefault(message_id, thread_id) != thread_id:
            raise ValueError("Gmail History addition has conflicting thread IDs")

    recovered = []
    unavailable = 0
    for message_id, thread_id in references.items():
        try:
            message = users.messages().get(userId="me", id=message_id, format="full").execute()
        except HttpError as message_error:
            required = message_id in required_message_ids or thread_id in required_thread_ids
            if message_error.resp.status != 404 or (required and unavailable_required is None):
                raise
            try:
                thread = users.threads().get(userId="me", id=thread_id, format="full").execute()
            except HttpError as thread_error:
                if thread_error.resp.status != 404:
                    raise
                if required:
                    unavailable_required.append({"message_id": message_id, "thread_id": thread_id,
                        "reason": "required_history_message_and_thread_unavailable"})
                else:
                    unavailable += 1
                continue
            if required:
                messages = thread.get("messages")
                if thread.get("id") != thread_id or not isinstance(messages, list) or not messages:
                    raise ValueError("Gmail full thread response does not verify the required conversation")
                if any(not isinstance(m, dict) or not m.get("id") or m.get("threadId") != thread_id for m in messages):
                    raise ValueError("Gmail full thread contains an invalid message reference")
                if len({m["id"] for m in messages}) != len(messages):
                    raise ValueError("Gmail full thread contains duplicate message references")
                # Return the available exact conversation for normal contextual review.
                # The absent message stays an explicit item hold, never assumed fulfilled.
                recovered.extend(messages)
                if not any(m["id"] == message_id for m in messages):
                    unavailable_required.append({"message_id": message_id, "thread_id": thread_id,
                        "reason": "required_history_message_unavailable_thread_read"})
                continue
            # The thread still exists. The review needs to reconcile it.
            raise

        if message.get("id") != message_id or message.get("threadId") != thread_id:
            raise ValueError("Recovered Gmail message does not match the exact History reference")
        recovered.append(message)

    skipped = {"gmail_history_message_and_thread_not_found": unavailable} if unavailable else {}
    by_id = {}
    for message in recovered:
        previous = by_id.get(message["id"])
        if previous and previous["threadId"] != message["threadId"]:
            raise ValueError("Recovered Gmail messages have conflicting thread IDs")
        by_id[message["id"]] = message
    return list(by_id.values()), skipped
