from datetime import timedelta
from io import StringIO
import json

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from linkedin.models import Campaign, Task

pytestmark = pytest.mark.django_db


def campaign(name, priority=0, **kwargs):
    owner, _ = User.objects.get_or_create(username=kwargs.pop("owner", "Arian"))
    return Campaign.objects.create(name=name, user=owner, connection_priority=priority, **kwargs)


def task(c, age=60, kind="connect", **kwargs):
    return Task.objects.create(task_type=kind, scheduled_at=timezone.now() - timedelta(seconds=age),
                               payload={"campaign_id": c.pk, "operator": c.user.username,
                                        "public_id": "qa-person", "lead_id": 1,
                                        "post_id": 1, "message": "Synthetic QA"}, **kwargs)


@pytest.mark.parametrize("priority", [0, 10])
def test_default_and_equal_priority_keep_oldest_connection(priority):
    a, b = campaign("first", priority), campaign("second", priority)
    old, new = task(a, 100), task(b, 50)
    assert Task.objects.claim_next(operator="Arian", campaign_ids=[a.pk, b.pk]).pk == old.pk
    assert Task.objects.claim_next(operator="Arian", campaign_ids=[a.pk, b.pk]).pk == new.pk


def test_highest_priority_then_existing_schedule_order_and_no_retiming():
    low, high, highest = campaign("low"), campaign("high", 5), campaign("highest", 20)
    rows = [task(low, 100), task(high, 90), task(highest, 70), task(highest, 80)]
    dates = dict(Task.objects.values_list("pk", "scheduled_at"))
    assert [Task.objects.claim_next().pk for _ in rows] == [rows[3].pk, rows[2].pk, rows[1].pk, rows[0].pk]
    assert dict(Task.objects.values_list("pk", "scheduled_at")) == dates
    assert Task.objects.claim_next() is None


@pytest.mark.parametrize("kind,age", [
    ("follow_up", 180), ("manual_reply", 10), ("feed_comment", 10),
    ("feed_like", 10), ("status_summary", 10), ("sweep_connections", 7200),
])
def test_existing_nonconnect_turns_are_preserved(kind, age):
    low, high = campaign("low"), campaign("high", 20)
    expected = task(low, age, kind)
    task(low, 90)
    task(high, 30)
    assert Task.objects.claim_next(operator="Arian", campaign_ids=[low.pk, high.pk]).pk == expected.pk


@pytest.mark.parametrize("reason", ["foreign", "disabled", "finished", "future", "running", "filtered"])
def test_priority_does_not_make_ineligible_connections_eligible(reason):
    low = campaign("low")
    high = campaign("high", 20, owner="Chuka" if reason == "foreign" else "Arian",
                    status=reason if reason in {"disabled", "finished"} else "active")
    old = task(low, 90)
    excluded = task(high, -300 if reason == "future" else 30,
                    status="running" if reason == "running" else "pending")
    owned = [low.pk] if reason == "foreign" else [low.pk, high.pk]
    query = Task.objects.exclude(pk=excluded.pk) if reason == "filtered" else Task.objects
    assert query.claim_next(operator="Arian", campaign_ids=owned).pk == old.pk
    excluded.refresh_from_db()
    assert excluded.status == ("running" if reason == "running" else "pending")


def test_followup_only_window_does_not_claim_priority_connect():
    low, high = campaign("low"), campaign("high", 20)
    expected = task(low, 60, "follow_up")
    task(high, 180)
    assert Task.objects.claim_next(operator="Arian", campaign_ids=[low.pk, high.pk],
                                   task_types={"follow_up"}).pk == expected.pk


def test_priority_never_pulls_a_later_connection_across_a_due_followup():
    low, high = campaign("low"), campaign("high", 20)
    first = task(low, 180)
    followup = task(low, 120, "follow_up")
    later = task(high, 60)
    assert [Task.objects.claim_next().pk for _ in range(3)] == [first.pk, followup.pk, later.pk]


def test_priority_reorders_connections_without_moving_followup_position():
    low, high = campaign("low"), campaign("high", 20)
    first = task(low, 240)
    prioritized = task(high, 180)
    followup = task(low, 120, "follow_up")
    later = task(high, 60)
    assert [Task.objects.claim_next().pk for _ in range(4)] == [prioritized.pk, first.pk, followup.pk, later.pk]


def test_priority_updates_apply_to_existing_pending_tasks_without_rewriting_them():
    low, high = campaign("low"), campaign("high")
    task(low, 90)
    expected = task(high, 30)
    before = list(Task.objects.order_by("pk").values())
    high.connection_priority = 10
    high.save(update_fields=["connection_priority"])
    assert list(Task.objects.order_by("pk").values()) == before
    assert Task.objects.claim_next().pk == expected.pk


def test_command_previews_then_changes_only_exact_campaign_priority():
    c = campaign("priority command", status="disabled")
    task(c)
    before = list(Task.objects.values())
    original = Campaign.objects.filter(pk=c.pk).values().get()
    output = StringIO()
    call_command("set_campaign_priority", c.pk, 10, owner_username="Arian", stdout=output)
    c.refresh_from_db()
    assert c.connection_priority == 0
    assert json.loads(output.getvalue())["applied"] is False
    call_command("set_campaign_priority", c.pk, 10, owner_username="Arian", apply=True, stdout=StringIO())
    original["connection_priority"] = 10
    assert Campaign.objects.filter(pk=c.pk).values().get() == original
    assert list(Task.objects.values()) == before
    with pytest.raises(CommandError):
        call_command("set_campaign_priority", c.pk, 20, owner_username="Chuka", apply=True)


@pytest.mark.parametrize("value", [-1, 32768])
def test_command_rejects_out_of_range_priority(value):
    c = campaign("range")
    with pytest.raises(CommandError):
        call_command("set_campaign_priority", c.pk, value, owner_username="Arian", apply=True)
    c.refresh_from_db()
    assert c.connection_priority == 0


def test_import_export_roundtrip_and_missing_priority_preserves_value(tmp_path):
    c = campaign("roundtrip", 15)
    output = StringIO()
    call_command("export_campaign", str(c.pk), stdout=output)
    payload = json.loads(output.getvalue())
    assert payload["connection_priority"] == 15
    # Updating an existing campaign needs no new owner/timing decision.
    payload.pop("owner_username")
    payload.pop("gmail_start_mode")
    path = tmp_path / "campaign.json"
    for priority in (20, None):
        if priority is None:
            payload.pop("connection_priority")
        else:
            payload["connection_priority"] = priority
        path.write_text(json.dumps(payload))
        call_command("import_campaign", str(path), stdout=StringIO())
        c.refresh_from_db()
        assert c.connection_priority == 20


@pytest.mark.parametrize("value", [None, -1, 32768, 1.2, "10", True])
def test_import_rejects_invalid_priority_without_mutation(tmp_path, value):
    c = campaign("invalid")
    before = Campaign.objects.filter(pk=c.pk).values().get()
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps({"name": c.name, "connection_priority": value}))
    with pytest.raises(CommandError):
        call_command("import_campaign", str(path), stdout=StringIO())
    assert Campaign.objects.filter(pk=c.pk).values().get() == before
