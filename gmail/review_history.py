"""Read-only recovery of unresolved Gmail additions for the contextual review."""

from collections.abc import Collection, Iterable, Mapping

from googleapiclient.errors import HttpError


def recover_history_additions(
    service,
    additions: Iterable[Mapping[str, str]],
    *,
    expected_mailbox: str,
    required_message_ids: Collection[str],
    required_thread_ids: Collection[str],
) -> tuple[list[dict], dict[str, int]]:
    """Read exact History messages not already covered by fresh full reads.

    Call after completing current discovery. Required IDs come from current
    searches and evidence linked to review items. Only a History-only reference
    whose exact message and thread both return 404 contributes to the skip count.
    """
    users = service.users()
    profile = users.getProfile(userId="me").execute()
    actual_mailbox = profile.get("emailAddress", "")
    if not expected_mailbox or actual_mailbox.casefold() != expected_mailbox.casefold():
        raise ValueError("Gmail profile does not match the expected review mailbox")

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
            if (
                message_error.resp.status != 404
                or message_id in required_message_ids
                or thread_id in required_thread_ids
            ):
                raise
            try:
                users.threads().get(userId="me", id=thread_id, format="full").execute()
            except HttpError as thread_error:
                if thread_error.resp.status != 404:
                    raise
                unavailable += 1
                continue
            # The thread still exists. The review needs to reconcile it.
            raise

        if message.get("id") != message_id or message.get("threadId") != thread_id:
            raise ValueError("Recovered Gmail message does not match the exact History reference")
        recovered.append(message)

    skipped = {"gmail_history_message_and_thread_not_found": unavailable} if unavailable else {}
    return recovered, skipped
