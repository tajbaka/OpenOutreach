from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from time import monotonic, sleep
from unittest.mock import Mock, patch

import pytest
from django.db import InterfaceError, OperationalError, connection, connections, transaction

from crm.models import Deal
from linkedin.models import Campaign, LinkedInProfile, Task
from linkedin.exceptions import SupervisorControlError
from linkedin.supervisor_control import consume_sender_restart, read_sender_stop
from tests.factories import UserFactory


pytestmark = pytest.mark.django_db


def _profile(username="local@example.com", *, requested=True, active=True):
    return LinkedInProfile.objects.create(
        user=UserFactory(),
        linkedin_username=username,
        linkedin_password="unused",
        restart_requested=requested,
        active=active,
    )


def test_success_consumes_only_exact_senders_flag_after_restart():
    local = _profile()
    other = _profile("other@example.com")
    before = LinkedInProfile.objects.values().get(pk=local.pk)
    counts = {model: model.objects.count() for model in (Campaign, Deal, Task)}

    def restart():
        local.refresh_from_db()
        assert local.restart_requested is True
        assert other.restart_requested is True
        return True

    callback = Mock(side_effect=restart)
    assert consume_sender_restart(linkedin_username=" LOCAL@example.com ", restart=callback) is True
    callback.assert_called_once_with()
    assert LinkedInProfile.objects.values().get(pk=local.pk) == {
        **before, "restart_requested": False,
    }
    other.refresh_from_db()
    assert other.restart_requested is True
    assert {model: model.objects.count() for model in counts} == counts


def test_false_flag_does_not_restart():
    _profile(requested=False)
    callback = Mock(return_value=True)

    assert consume_sender_restart(linkedin_username="local@example.com", restart=callback) is False

    callback.assert_not_called()


@pytest.mark.parametrize("stopped", [False, True])
def test_stop_read_is_exact_sender_scoped_and_never_clears_flags(stopped):
    local = _profile(active=False)
    other = _profile("other@example.com")
    LinkedInProfile.objects.filter(pk=local.pk).update(stop_requested=stopped)
    LinkedInProfile.objects.filter(pk=other.pk).update(stop_requested=not stopped)
    before = list(LinkedInProfile.objects.order_by("pk").values())

    assert read_sender_stop(linkedin_username=" LOCAL@example.com ") is stopped
    assert read_sender_stop(linkedin_username=" LOCAL@example.com ") is stopped
    assert list(LinkedInProfile.objects.order_by("pk").values()) == before


@pytest.mark.parametrize("username", ["", "   ", "missing@example.com"])
def test_stop_read_fails_closed_on_missing_identity(username):
    _profile()
    with pytest.raises(SupervisorControlError):
        read_sender_stop(linkedin_username=username)


def test_stop_read_never_uses_django_identity_or_operator_alias():
    local = _profile("ariantajbakh@gmail.com")
    for username in (local.user.username, "Arian", "arian@boundera.io"):
        with pytest.raises(SupervisorControlError, match="matches no profiles"):
            read_sender_stop(linkedin_username=username)


def test_stop_read_fails_closed_on_case_insensitive_duplicate_even_if_inactive():
    local = _profile()
    _profile("LOCAL@example.com", active=False)
    with pytest.raises(SupervisorControlError, match="matches multiple profiles"):
        read_sender_stop(linkedin_username=local.linkedin_username)


def test_stop_read_database_error_propagates():
    with patch.object(LinkedInProfile.objects, "filter", side_effect=OperationalError("unavailable")):
        with pytest.raises(OperationalError, match="unavailable"):
            read_sender_stop(linkedin_username="local@example.com")


def test_stop_latch_prevents_restart_and_is_never_consumed():
    local = _profile()
    LinkedInProfile.objects.filter(pk=local.pk).update(stop_requested=True)
    before = LinkedInProfile.objects.values().get(pk=local.pk)
    callback = Mock(return_value=True)

    assert consume_sender_restart(linkedin_username=local.linkedin_username, restart=callback) is False

    callback.assert_not_called()
    assert LinkedInProfile.objects.values().get(pk=local.pk) == before


@pytest.mark.parametrize("username", ["", "   ", "missing@example.com"])
def test_missing_identity_does_not_choose_an_active_profile(username, caplog):
    other = _profile()
    callback = Mock(return_value=True)

    assert consume_sender_restart(linkedin_username=username, restart=callback) is False

    callback.assert_not_called()
    other.refresh_from_db()
    assert other.restart_requested is True
    assert "Skipping supervisor restart check" in caplog.text


def test_unrequested_local_sender_does_not_consume_other_senders_request():
    _profile(requested=False)
    other = _profile("other@example.com")
    callback = Mock(return_value=True)

    assert consume_sender_restart(linkedin_username="local@example.com", restart=callback) is False

    callback.assert_not_called()
    other.refresh_from_db()
    assert other.restart_requested is True


def test_django_username_is_not_a_local_linkedin_identity_fallback():
    local = _profile()
    callback = Mock(return_value=True)

    assert consume_sender_restart(linkedin_username=local.user.username, restart=callback) is False

    callback.assert_not_called()
    local.refresh_from_db()
    assert local.restart_requested is True


def test_inactive_local_sender_can_consume_its_exact_request():
    local = _profile(active=False)
    callback = Mock(return_value=True)

    assert consume_sender_restart(linkedin_username=local.linkedin_username, restart=callback) is True

    callback.assert_called_once_with()
    local.refresh_from_db()
    assert local.active is False
    assert local.restart_requested is False


def test_duplicate_case_insensitive_identity_is_not_consumed(caplog):
    local = _profile()
    duplicate = _profile("LOCAL@example.com", active=False)
    callback = Mock(return_value=True)

    assert consume_sender_restart(linkedin_username=local.linkedin_username, restart=callback) is False

    callback.assert_not_called()
    assert LinkedInProfile.objects.filter(pk__in=[local.pk, duplicate.pk], restart_requested=True).count() == 2
    assert "matches multiple profiles" in caplog.text


@pytest.mark.parametrize("result", [False, None, 1])
def test_unconfirmed_restart_keeps_request_pending(result):
    local = _profile()
    callback = Mock(return_value=result)

    assert consume_sender_restart(linkedin_username=local.linkedin_username, restart=callback) is False

    callback.assert_called_once_with()
    local.refresh_from_db()
    assert local.restart_requested is True


@pytest.mark.parametrize("error_type", [RuntimeError, OperationalError, InterfaceError])
def test_callback_failure_propagates_and_keeps_request_pending(error_type):
    local = _profile()
    callback = Mock(side_effect=error_type("restart failed"))

    with pytest.raises(error_type, match="restart failed"):
        consume_sender_restart(linkedin_username=local.linkedin_username, restart=callback)

    local.refresh_from_db()
    assert local.restart_requested is True


def test_database_outage_propagates_without_restart_or_acknowledgement():
    local = _profile()
    callback = Mock(return_value=True)
    with patch.object(LinkedInProfile.objects, "filter", side_effect=OperationalError("unavailable")):
        with pytest.raises(OperationalError, match="unavailable"):
            consume_sender_restart(linkedin_username=local.linkedin_username, restart=callback)

    callback.assert_not_called()
    local.refresh_from_db()
    assert local.restart_requested is True


@pytest.mark.django_db(transaction=True)
def test_concurrent_consumer_skips_claimed_request():
    local = _profile()
    entered = Event()
    release = Event()

    def first_consumer():
        def restart():
            entered.set()
            assert release.wait(timeout=10), "Test did not release restart callback"
            return True

        try:
            return consume_sender_restart(linkedin_username=local.linkedin_username, restart=restart)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(first_consumer)
        try:
            assert entered.wait(timeout=5), "First consumer did not claim request"
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '2s'")
            second_callback = Mock(return_value=True)
            assert consume_sender_restart(
                linkedin_username=local.linkedin_username, restart=second_callback,
            ) is False
            second_callback.assert_not_called()
        finally:
            release.set()
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = DEFAULT")
        assert first.result(timeout=10) is True

    local.refresh_from_db()
    assert local.restart_requested is False


@pytest.mark.django_db(transaction=True)
def test_locked_duplicate_does_not_make_other_profile_eligible():
    local = _profile()
    duplicate = _profile("LOCAL@example.com")

    def competing_consumer():
        try:
            callback = Mock(return_value=True)
            result = consume_sender_restart(linkedin_username=local.linkedin_username, restart=callback)
            callback.assert_not_called()
            return result
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            LinkedInProfile.objects.select_for_update().get(pk=local.pk)
            assert executor.submit(competing_consumer).result(timeout=5) is False

    assert LinkedInProfile.objects.filter(pk__in=[local.pk, duplicate.pk], restart_requested=True).count() == 2


@pytest.mark.django_db(transaction=True)
def test_new_request_waiting_during_restart_survives_acknowledgement():
    local = _profile()
    entered = Event()
    release = Event()
    consumer_pid = Queue()
    writer_pid = Queue()

    def consume():
        def restart():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                consumer_pid.put(cursor.fetchone()[0])
            entered.set()
            assert release.wait(timeout=10), "Test did not release restart callback"
            return True

        try:
            return consume_sender_restart(linkedin_username=local.linkedin_username, restart=restart)
        finally:
            connections.close_all()

    def request_again():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '10s'")
                cursor.execute("SELECT pg_backend_pid()")
                writer_pid.put(cursor.fetchone()[0])
            return LinkedInProfile.objects.filter(pk=local.pk).update(restart_requested=True)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        consumer = executor.submit(consume)
        try:
            assert entered.wait(timeout=5), "Consumer did not claim request"
            owner_pid = consumer_pid.get(timeout=5)
            writer = executor.submit(request_again)
            requester_pid = writer_pid.get(timeout=5)
            deadline = monotonic() + 5
            blocked = False
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [owner_pid, requester_pid])
                    blocked = cursor.fetchone()[0]
                if blocked:
                    break
                sleep(.01)
            assert blocked, "New request was not blocked by the consumer's row lock"
        finally:
            release.set()
        assert consumer.result(timeout=10) is True
        assert writer.result(timeout=10) == 1

    local.refresh_from_db()
    assert local.restart_requested is True
    next_callback = Mock(return_value=True)
    assert consume_sender_restart(linkedin_username=local.linkedin_username, restart=next_callback) is True
    next_callback.assert_called_once_with()
