from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Barrier
from time import monotonic, sleep

import pytest
from django.db import connection, connections, transaction
from django.test.utils import CaptureQueriesContext

from crm.models import Deal, Lead
from linkedin.message_delivery import ensure_message_enrollment, get_or_create_delivery
from linkedin.models import CampaignMessageEnrollment, OutboundDelivery
from tests.test_message_delivery import _deal, _row, _version


pytestmark = pytest.mark.django_db(transaction=True)


def _domain():
    deal = _deal(_version(_row(), key="lock-order"))
    enrollment = ensure_message_enrollment(
        deal=deal, audience_key="csp-small-founder-ceo", operator="Arian",
    )
    return deal, enrollment


@pytest.mark.parametrize("operation", ["enrollment", "delivery"])
def test_canonical_message_functions_lock_lead_before_other_mutable_rows(operation):
    """A real competing SQL connection waits on Lead without holding graph rows."""
    deal, enrollment = _domain()
    worker_pid = Queue()

    def materialize():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                worker_pid.put(cursor.fetchone()[0])
            if operation == "enrollment":
                return ensure_message_enrollment(
                    deal=deal, audience_key="csp-small-founder-ceo", operator="Arian",
                ).pk
            delivery, _ = get_or_create_delivery(enrollment=enrollment, channel="gmail", step_index=0)
            return delivery.pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            Lead.objects.select_for_update().get(pk=deal.lead_id)
            future = executor.submit(materialize)
            pid = worker_pid.get(timeout=5)
            deadline = monotonic() + 5
            blocked = False
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
                    blocked = cursor.fetchone()[0]
                if blocked:
                    break
                sleep(.01)
            assert blocked, "Competing materialization did not reach the held Lead row"
            # The old implicit joined locks grabbed Deal/Enrollment before Lead,
            # creating a cycle with Lead-first Gmail/drip ownership transactions.
            target = Deal if operation == "enrollment" else CampaignMessageEnrollment
            target.objects.select_for_update(nowait=True).get(
                pk=deal.pk if operation == "enrollment" else enrollment.pk,
            )
        assert future.result(timeout=10)


def test_materialization_does_not_lock_immutable_joined_program_rows():
    deal, enrollment = _domain()
    with CaptureQueriesContext(connection) as captured:
        ensure_message_enrollment(deal=deal, audience_key="csp-small-founder-ceo", operator="Arian")
        get_or_create_delivery(enrollment=enrollment, channel="gmail", step_index=0)
    joined_locks = [
        query["sql"] for query in captured.captured_queries
        if "FOR UPDATE" in query["sql"] and "JOIN" in query["sql"]
    ]
    assert joined_locks
    assert all("FOR UPDATE OF " in sql for sql in joined_locks)
    assert all('"linkedin_messageprogramversion"' not in sql.split("FOR UPDATE OF ", 1)[1] for sql in joined_locks)
    assert all('"linkedin_messageprogram"' not in sql.split("FOR UPDATE OF ", 1)[1] for sql in joined_locks)


def test_simultaneous_direct_materializations_return_one_frozen_delivery():
    _deal_instance, enrollment = _domain()
    gate = Barrier(2)

    def materialize():
        try:
            local_enrollment = CampaignMessageEnrollment.objects.get(pk=enrollment.pk)
            gate.wait(timeout=5)
            delivery, created = get_or_create_delivery(
                enrollment=local_enrollment, channel="gmail", step_index=0,
            )
            return delivery.pk, created, delivery.frozen_body, delivery.scheduled_at
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(materialize) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0][0] == results[1][0]
    assert sorted(result[1] for result in results) == [False, True]
    assert results[0][2:] == results[1][2:]
    assert OutboundDelivery.objects.filter(enrollment=enrollment).count() == 1
