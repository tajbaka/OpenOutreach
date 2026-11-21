import json
from collections import deque
from unittest.mock import Mock, call

import pytest
from googleapiclient.errors import HttpError

from gmail import review_threads


MAILBOX = "review@example.test"


def full_thread(thread_id):
    return {"id": thread_id, "messages": [{"id": f"message-{thread_id}", "threadId": thread_id}]}


def http_error(status, reason=None):
    body = {"error": {"errors": [{"reason": reason}]}}
    return HttpError(Mock(status=status, reason=""), json.dumps(body).encode())


@pytest.fixture
def service():
    client = Mock()
    client.users().getProfile.return_value.execute.return_value = {"emailAddress": MAILBOX}
    return client


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0.0

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(review_threads, "monotonic", lambda: clock.now)
    monkeypatch.setattr(review_threads, "sleep", clock.sleep)
    return clock


def test_full_scope_stays_under_minute_quota_and_deduplicates(service, clock):
    recent = deque()

    def request(*, userId, id, format):
        assert (userId, format) == ("me", "full")

        def execute():
            while recent and recent[0] <= clock.now - 60:
                recent.popleft()
            recent.append(clock.now)
            assert len(recent) * 120 <= 4800
            return full_thread(id)

        return Mock(execute=execute)

    get = service.users().threads().get
    get.side_effect = request
    ids = [f"thread-{i}" for i in range(985)]
    result = review_threads.iter_review_threads(service, [*ids, ids[0]], expected_mailbox=MAILBOX)

    assert [thread["id"] for thread in result] == ids
    assert get.call_count == len(ids)
    assert clock.now == 984 * 1.5


@pytest.mark.parametrize(
    "status,reason", [(403, "rateLimitExceeded"), (403, "userRateLimitExceeded"), (429, None)],
)
def test_rate_limit_retries_the_same_thread_with_backoff(service, clock, status, reason):
    get = service.users().threads().get
    request_times = []
    throttled = http_error(status, reason)
    outcomes = iter([throttled, throttled, full_thread("a"), full_thread("b")])

    def execute():
        request_times.append(clock.now)
        outcome = next(outcomes)
        if isinstance(outcome, HttpError):
            raise outcome
        return outcome

    get.return_value.execute.side_effect = execute

    threads = list(review_threads.iter_review_threads(service, ["a", "b"], expected_mailbox=MAILBOX))

    assert threads == [full_thread("a"), full_thread("b")]
    assert get.call_args_list == [call(userId="me", id=id, format="full") for id in ("a", "a", "a", "b")]
    assert request_times == [0, 2, 6, 7.5]


def test_exhausted_retry_keeps_completed_progress_and_leaves_coverage_incomplete(service, clock):
    get = service.users().threads().get
    error = http_error(403, "rateLimitExceeded")
    get.return_value.execute.side_effect = [full_thread("a"), *([error] * 7)]
    threads = review_threads.iter_review_threads(service, ["a", "b", "c"], expected_mailbox=MAILBOX)

    assert next(threads) == full_thread("a")
    with pytest.raises(HttpError) as exc:
        next(threads)

    assert exc.value is error
    assert [request.kwargs["id"] for request in get.call_args_list] == ["a", *(["b"] * 7)]
    assert clock.now == 1.5 + 2 + 4 + 8 + 16 + 32 + 60


@pytest.mark.parametrize(
    "status,reason", [(401, None), (403, "dailyLimitExceeded"), (403, "domainPolicy"), (404, None)],
)
def test_other_errors_remain_required_coverage_failures(service, clock, status, reason):
    get = service.users().threads().get
    error = http_error(status, reason)
    get.return_value.execute.side_effect = error

    with pytest.raises(HttpError) as exc:
        list(review_threads.iter_review_threads(service, ["a"], expected_mailbox=MAILBOX))

    assert exc.value is error
    get.assert_called_once()
    assert clock.now == 0


def test_mismatched_mailbox_stops_before_reading_threads(service):
    with pytest.raises(ValueError, match="profile does not match"):
        list(review_threads.iter_review_threads(service, ["a"], expected_mailbox="other@example.test"))

    service.users().threads.assert_not_called()


@pytest.mark.parametrize(
    "response,reason",
    [
        (full_thread("other"), "different thread"),
        ({"id": "a"}, "missing its messages list"),
        ({"id": "a", "messages": {}}, "missing its messages list"),
    ],
)
def test_invalid_full_response_is_not_accepted_as_coverage(service, response, reason):
    service.users().threads().get.return_value.execute.return_value = response

    with pytest.raises(ValueError, match=reason):
        list(review_threads.iter_review_threads(service, ["a"], expected_mailbox=MAILBOX))
