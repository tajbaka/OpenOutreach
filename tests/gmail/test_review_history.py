from unittest.mock import Mock, call

import pytest
from googleapiclient.errors import HttpError

from gmail.review_history import recover_history_additions


MAILBOX = "review@example.test"
ADDITION = {"id": "message-1", "threadId": "thread-1"}
NOT_FOUND = "gmail_history_message_and_thread_not_found"


@pytest.fixture
def service():
    service = Mock()
    service.users().getProfile.return_value.execute.return_value = {"emailAddress": MAILBOX}
    return service


def error(status):
    return HttpError(Mock(status=status, reason=""), b"")


def recover(service, additions, *, required_messages=(), required_threads=()):
    return recover_history_additions(
        service,
        additions,
        expected_mailbox=MAILBOX,
        required_message_ids=required_messages,
        required_thread_ids=required_threads,
    )


def test_counts_missing_history_additions_once_and_reads_remaining_messages(service):
    users = service.users()
    second = {"id": "message-2", "threadId": "thread-2"}
    third = {"id": "message-3", "threadId": "thread-3"}
    recovered_message = {**third, "payload": {"headers": []}}
    users.messages().get.return_value.execute.side_effect = [error(404), error(404), recovered_message]
    users.threads().get.return_value.execute.side_effect = [error(404), error(404)]

    messages, skipped = recover(service, [ADDITION, ADDITION, second, third])

    assert messages == [recovered_message]
    assert skipped == {NOT_FOUND: 2}
    assert users.messages().get.call_args_list == [
        call(userId="me", id=addition["id"], format="full")
        for addition in (ADDITION, second, third)
    ]
    assert users.threads().get.call_args_list == [
        call(userId="me", id="thread-1", format="full"),
        call(userId="me", id="thread-2", format="full"),
    ]


@pytest.mark.parametrize(
    "required_messages,required_threads", [({"message-1"}, ()), ((), {"thread-1"})],
)
def test_missing_required_message_or_thread_cannot_be_counted_as_a_skip(
    service, required_messages, required_threads,
):
    users = service.users()
    users.messages().get.return_value.execute.side_effect = error(404)

    with pytest.raises(HttpError) as exc:
        recover(
            service,
            [ADDITION],
            required_messages=required_messages,
            required_threads=required_threads,
        )

    assert exc.value.resp.status == 404
    users.threads.assert_not_called()


@pytest.mark.parametrize("thread_status", [200, 403, 503])
def test_existing_thread_and_thread_request_errors_are_not_skipped(service, thread_status):
    users = service.users()
    users.messages().get.return_value.execute.side_effect = error(404)
    thread = users.threads().get.return_value.execute
    if thread_status == 200:
        thread.return_value = {"id": "thread-1", "messages": []}
    else:
        thread.side_effect = error(thread_status)

    with pytest.raises(HttpError) as exc:
        recover(service, [ADDITION])

    assert exc.value.resp.status == (404 if thread_status == 200 else thread_status)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_message_errors_other_than_not_found_propagate(service, status):
    users = service.users()
    users.messages().get.return_value.execute.side_effect = error(status)

    with pytest.raises(HttpError) as exc:
        recover(service, [ADDITION])

    assert exc.value.resp.status == status
    users.threads.assert_not_called()


def test_mismatched_mailbox_stops_before_reading_messages(service):
    users = service.users()
    users.getProfile.return_value.execute.return_value = {"emailAddress": "other@example.test"}

    with pytest.raises(ValueError, match="Gmail profile does not match"):
        recover(service, [ADDITION])

    users.messages.assert_not_called()


def test_conflicting_history_references_stop_before_reading_messages(service):
    users = service.users()

    with pytest.raises(ValueError, match="conflicting thread IDs"):
        recover(service, [ADDITION, {**ADDITION, "threadId": "other-thread"}])

    users.messages.assert_not_called()


def test_recovered_message_must_match_history_reference(service):
    service.users().messages().get.return_value.execute.return_value = {
        **ADDITION, "threadId": "other-thread",
    }

    with pytest.raises(ValueError, match="does not match the exact History reference"):
        recover(service, [ADDITION])


def recover_with_holds(service, additions, holds):
    return recover_history_additions(service, additions, expected_mailbox=MAILBOX,
        required_message_ids={ADDITION["id"]}, required_thread_ids=(), unavailable_required=holds)


def test_missing_required_reference_is_held_not_skipped_and_independent_reads_continue(service):
    users = service.users()
    available = {"id": "message-2", "threadId": "thread-2"}
    users.messages().get.return_value.execute.side_effect = [error(404), available]
    users.threads().get.return_value.execute.side_effect = error(404)
    holds = []
    messages, skipped = recover_with_holds(service, [ADDITION, ADDITION, available], holds)
    assert messages == [available]
    assert skipped == {}
    assert holds == [{"message_id": "message-1", "thread_id": "thread-1",
        "reason": "required_history_message_and_thread_unavailable"}]


@pytest.mark.parametrize("exact_available", [False, True])
def test_reads_current_full_thread_before_holding_only_the_unavailable_reference(service, exact_available):
    users = service.users()
    users.messages().get.return_value.execute.side_effect = error(404)
    available = {"id": "message-1" if exact_available else "message-2", "threadId": "thread-1"}
    users.threads().get.return_value.execute.return_value = {"id": "thread-1", "messages": [available]}
    holds = []
    messages, skipped = recover_with_holds(service, [ADDITION], holds)
    assert messages == [available]
    assert skipped == {}
    assert len(holds) == (0 if exact_available else 1)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_required_holds_never_hide_shared_read_failures(service, status):
    service.users().messages().get.return_value.execute.side_effect = error(404)
    service.users().threads().get.return_value.execute.side_effect = error(status)
    holds = []
    with pytest.raises(HttpError):
        recover_with_holds(service, [ADDITION], holds)
    assert holds == []


@pytest.mark.parametrize("thread", [
    {"id": "wrong", "messages": [{"id": "x", "threadId": "thread-1"}]},
    {"id": "thread-1", "messages": []},
    {"id": "thread-1", "messages": [{"id": "x", "threadId": "wrong"}]},
    {"id": "thread-1", "messages": [{"id": "x", "threadId": "thread-1"}] * 2},
])
def test_required_holds_reject_malformed_or_mismatched_threads(service, thread):
    users = service.users()
    users.messages().get.return_value.execute.side_effect = error(404)
    users.threads().get.return_value.execute.return_value = thread
    with pytest.raises(ValueError):
        recover_with_holds(service, [ADDITION], [])


def test_required_thread_recovery_cannot_merge_conflicting_message_identities(service):
    users = service.users()
    another = {"id": "message-2", "threadId": "thread-2"}
    users.messages().get.return_value.execute.side_effect = [error(404), another]
    users.threads().get.return_value.execute.return_value = {
        "id": "thread-1", "messages": [{"id": "message-2", "threadId": "thread-1"}],
    }
    with pytest.raises(ValueError, match="conflicting thread IDs"):
        recover_with_holds(service, [ADDITION, another], [])
