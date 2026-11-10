"""PostgreSQL lock order for email assets: Lead, then assignment, then asset.

The submission transaction locks Lead -> Task -> delivery and then the final
attachment recheck locks the Lead's slot assignments and the assets. Every
other path that takes more than one of these locks must use the same order,
or a concurrent reassignment can deadlock with a send.
"""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from time import monotonic, sleep

import pytest
from django.db import connection, connections, transaction
from django.test.utils import CaptureQueriesContext

from crm.models import Lead
from email_assets.models import EmailAsset, LeadEmailAsset
from email_assets.requirements import AttachmentRequirement
from email_assets.resolution import recheck_resolved_attachments, resolve_attachments
from email_assets.services import assign_asset, remove_assignments
from tests.email_assets.helpers import FakeSource, approved_asset, pdf_bytes


pytestmark = pytest.mark.django_db(transaction=True)

SLOT = AttachmentRequirement(slot="marketplace_pdf")


@pytest.fixture(autouse=True)
def _postgres_only():
    if connection.vendor != "postgresql":
        pytest.skip("row-lock ordering needs PostgreSQL (run via scripts/qa_campaigns.py)")


@pytest.fixture
def reassignable():
    source = FakeSource()
    first = approved_asset(source, key="acme-v1", content=pdf_bytes("one"))
    second = approved_asset(source, key="acme-v2", content=pdf_bytes("two"))
    lead = Lead.objects.create(
        first_name="Ada", linkedin_url="https://www.linkedin.com/in/ada-locks/",
        public_identifier="ada-locks",
    )
    assign_asset(key=first.key, slot="marketplace_pdf", lead_ids=[lead.pk], assigned_by="Arian", apply=True)
    return source, lead, first, second


def _wait_until_blocked_by_us(pid: int) -> bool:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
            if cursor.fetchone()[0]:
                return True
        sleep(.01)
    return False


@pytest.mark.parametrize("operation", ["reassign", "remove"])
def test_assignment_changes_wait_on_the_lead_before_locking_assignment_or_asset(reassignable, operation):
    """While a send holds the Lead, a reassignment holds nothing it needs."""
    _source, lead, first, second = reassignable
    worker_pid = Queue()

    def change():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                worker_pid.put(cursor.fetchone()[0])
            if operation == "reassign":
                return assign_asset(
                    key=second.key, slot="marketplace_pdf", lead_ids=[lead.pk],
                    assigned_by="Arian", apply=True,
                )
            return remove_assignments(slot="marketplace_pdf", lead_ids=[lead.pk], apply=True)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            # The submission transaction's first lock.
            Lead.objects.select_for_update().get(pk=lead.pk)
            future = executor.submit(change)
            assert _wait_until_blocked_by_us(worker_pid.get(timeout=5)), (
                "the assignment change did not wait on the Lead the send holds"
            )
            # The send's next locks must be free: the change holds neither.
            LeadEmailAsset.objects.select_for_update(nowait=True).filter(lead=lead).count()
            EmailAsset.objects.select_for_update(nowait=True).filter(
                pk__in=[first.pk, second.pk],
            ).count()
        future.result(timeout=10)
    expected = [second.pk] if operation == "reassign" else []
    assert list(LeadEmailAsset.objects.filter(lead=lead).values_list("asset_id", flat=True)) == expected


def _lock_order(captured) -> list[str]:
    tables = {
        '"crm_lead"': "lead",
        '"email_assets_leademailasset"': "assignment",
        '"email_assets_emailasset"': "asset",
    }
    order = []
    for query in captured.captured_queries:
        sql = query["sql"]
        if "FOR UPDATE" not in sql:
            continue
        from_clause = sql.split(" FROM ", 1)[1].split(" ", 1)[0]
        if from_clause in tables and (not order or order[-1] != tables[from_clause]):
            order.append(tables[from_clause])
    return order


def test_send_recheck_and_assignment_use_one_lock_order(reassignable):
    source, lead, _first, second = reassignable
    resolved = resolve_attachments(lead_id=lead.pk, requirements=[SLOT], source_for=lambda _p: source)

    with CaptureQueriesContext(connection) as send_locks, transaction.atomic():
        recheck_resolved_attachments(lead_id=lead.pk, requirements=[SLOT], resolved=resolved)
    with CaptureQueriesContext(connection) as assign_locks:
        assign_asset(key=second.key, slot="marketplace_pdf", lead_ids=[lead.pk], assigned_by="Arian", apply=True)

    assert _lock_order(send_locks) == ["lead", "assignment", "asset"]
    assert _lock_order(assign_locks) == ["lead", "assignment", "asset"]
