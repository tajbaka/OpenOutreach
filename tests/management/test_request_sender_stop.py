from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from queue import Queue
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections
from django.forms import modelform_factory

from crm.models import Deal, Lead
from linkedin.admin import LinkedInProfileAdmin, LinkedInProfileAdminForm
from linkedin.models import Campaign, LinkedInProfile, Task
from linkedin.supervisor_control import consume_sender_restart


pytestmark = pytest.mark.django_db


def _profile(username="ariantajbakh@gmail.com", **kwargs):
    return LinkedInProfile.objects.create(
        user=User.objects.create(username=f"stop-fixture-{User.objects.count()}"),
        linkedin_username=username, linkedin_password="synthetic-only", **kwargs,
    )


def _request(operator="Arian", **kwargs):
    output = StringIO()
    call_command("request_sender_stop", operator=operator, stdout=output, **kwargs)
    return output.getvalue()


def _snapshot():
    return {
        model: list(model.objects.order_by("pk").values())
        for model in (User, LinkedInProfile, Campaign, Lead, Deal, Task)
    }


@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.parametrize("explicit_dry_run", [False, True])
def test_preview_never_writes_stop_or_restart(clear, explicit_dry_run):
    local = _profile(restart_requested=True)
    _profile("chukyjack", stop_requested=True)
    before = _snapshot()

    output = _request(clear=clear, dry_run=explicit_dry_run)

    assert f"Dry run: Arian, LinkedInProfile {local.pk}" in output
    assert "No changes made" in output
    assert _snapshot() == before


@pytest.mark.parametrize("operator,username,canonical", [
    ("Arian", "ariantajbakh@gmail.com", "Arian"),
    ("Chuka", "chukyjack", "Chuka"),
    ("Eddy", "chukyjack@gmail.com", "Chuka"),
    ("Athena", "athenaaghdami@gmail.com", "Athena"),
    ("Leili", "leili.ash2011@yahoo.com", "Leili"),
])
def test_stop_arms_only_exact_sender_and_cancels_restart(operator, username, canonical):
    local = _profile(username, restart_requested=True, active=False, connect_daily_limit=7)
    other = _profile("unmapped@example.invalid", restart_requested=True)
    before = _snapshot()

    output = _request(operator, apply=True)

    assert f"Emergency stop latched for {canonical}, LinkedInProfile {local.pk}" in output
    assert "before shutdown is confirmed" in output
    for profile in before[LinkedInProfile]:
        if profile["id"] == local.pk:
            profile.update(stop_requested=True, restart_requested=False)
    assert _snapshot() == before
    other.refresh_from_db()
    assert not other.stop_requested and other.restart_requested


def test_rearming_stop_cancels_a_new_pending_restart():
    local = _profile(stop_requested=True, restart_requested=True)

    _request(apply=True)

    local.refresh_from_db()
    assert local.stop_requested is True
    assert local.restart_requested is False


@pytest.mark.parametrize("already_stopped", [False, True])
def test_explicit_clear_changes_only_stop_and_never_requests_a_restart(already_stopped):
    local = _profile(stop_requested=already_stopped, restart_requested=True)
    before = _snapshot()

    output = _request(clear=True, apply=True)

    for profile in before[LinkedInProfile]:
        if profile["id"] == local.pk:
            profile["stop_requested"] = False
    assert _snapshot() == before
    assert "No workers started and no restart requested" in output


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("clear", [False, True])
def test_missing_sender_never_falls_back_to_django_identity(apply, clear):
    profile = _profile("unmapped@example.invalid")
    profile.user.username = "Arian"
    profile.user.save(update_fields=["username"])
    before = _snapshot()

    with pytest.raises(CommandError, match="found 0"):
        _request(apply=apply, clear=clear)

    assert _snapshot() == before


@pytest.mark.parametrize("apply", [False, True])
def test_duplicate_canonical_profiles_including_inactive_are_refused(apply):
    _profile()
    _profile("arian@boundera.io", active=False)
    before = _snapshot()

    with pytest.raises(CommandError, match="found 2"):
        _request(apply=apply)

    assert _snapshot() == before


@pytest.mark.parametrize("operator", ["", "unknown", "all"])
def test_unknown_operator_is_refused(operator):
    before = _snapshot()
    with pytest.raises(CommandError, match="Choose a known operator"):
        _request(operator, apply=True)
    assert _snapshot() == before


@pytest.mark.parametrize("apply", [False, True])
def test_restart_command_refuses_latched_stop_even_if_restart_is_already_true(apply):
    _profile(stop_requested=True, restart_requested=True)
    before = _snapshot()
    with pytest.raises(CommandError, match="emergency stop is latched"):
        call_command("request_sender_restart", operator="Arian", apply=apply, stdout=StringIO())
    assert _snapshot() == before


def test_admin_arming_stop_cancels_restart_and_preserves_concurrent_settings():
    profile = _profile(restart_requested=True)
    model_admin = LinkedInProfileAdmin(LinkedInProfile, admin.site)
    assert "stop_requested" in model_admin.list_display
    assert "stop_requested" in model_admin.list_editable
    assert "stop_requested" in model_admin.list_filter
    LinkedInProfile.objects.filter(pk=profile.pk).update(connect_daily_limit=31)
    profile.stop_requested = True

    model_admin.save_model(None, profile, SimpleNamespace(changed_data=["stop_requested"]), True)

    profile.refresh_from_db()
    assert profile.stop_requested is True
    assert profile.restart_requested is False
    assert profile.connect_daily_limit == 31


def test_admin_unrelated_edit_cannot_erase_new_stop_or_restart_flags():
    profile = _profile()
    LinkedInProfile.objects.filter(pk=profile.pk).update(stop_requested=True, restart_requested=True)
    profile.active = False

    LinkedInProfileAdmin(LinkedInProfile, admin.site).save_model(
        None, profile, SimpleNamespace(changed_data=["active"]), True,
    )

    profile.refresh_from_db()
    assert profile.active is False
    assert profile.stop_requested is True
    assert profile.restart_requested is True


def test_admin_concurrent_stop_rejects_stale_restart_edit():
    profile = _profile()
    LinkedInProfile.objects.filter(pk=profile.pk).update(stop_requested=True)
    profile.restart_requested = True
    before = _snapshot()

    with pytest.raises(ValidationError, match="Clear the emergency stop"):
        LinkedInProfileAdmin(LinkedInProfile, admin.site).save_model(
            None, profile, SimpleNamespace(changed_data=["restart_requested"]), True,
        )

    assert _snapshot() == before


def test_admin_explicit_stop_clear_does_not_change_restart_or_start_workers():
    profile = _profile(stop_requested=True, restart_requested=True)
    profile.stop_requested = False
    LinkedInProfileAdmin(LinkedInProfile, admin.site).save_model(
        None, profile, SimpleNamespace(changed_data=["stop_requested"]), True,
    )
    profile.refresh_from_db()
    assert not profile.stop_requested
    assert profile.restart_requested


def test_real_stale_admin_post_does_not_treat_unedited_checkboxes_as_flag_changes():
    profile = _profile(active=True)
    LinkedInProfile.objects.filter(pk=profile.pk).update(stop_requested=True, restart_requested=True)
    profile.refresh_from_db()
    form_type = modelform_factory(
        LinkedInProfile, form=LinkedInProfileAdminForm,
        fields=("active", "restart_requested", "stop_requested"),
    )
    # The browser rendered both flags false before the concurrent requests.
    # The operator changes only active, leaving both old checkboxes unchecked.
    form = form_type(
        data={"initial-stop_requested": "False", "initial-restart_requested": "False"},
        instance=profile,
    )
    assert form.is_valid(), form.errors
    assert form.changed_data == ["active"]
    obj = form.save(commit=False)

    LinkedInProfileAdmin(LinkedInProfile, admin.site).save_model(None, obj, form, True)

    profile.refresh_from_db()
    assert not profile.active
    assert profile.stop_requested and profile.restart_requested


def test_admin_changelist_uses_hidden_initial_control_values():
    form_type = LinkedInProfileAdmin(LinkedInProfile, admin.site).get_changelist_form(None)
    form = form_type(instance=_profile())
    assert form.fields["stop_requested"].show_hidden_initial
    assert form.fields["restart_requested"].show_hidden_initial


def test_admin_form_shows_restart_stop_conflict_as_validation_error():
    form_type = modelform_factory(
        LinkedInProfile, form=LinkedInProfileAdminForm,
        fields=("restart_requested", "stop_requested"),
    )
    form = form_type(
        data={
            "stop_requested": "on", "initial-stop_requested": "True",
            "restart_requested": "on", "initial-restart_requested": "False",
        },
        instance=_profile(stop_requested=True),
    )
    assert not form.is_valid()
    assert "restart_requested" in form.errors


@pytest.mark.django_db(transaction=True)
def test_stop_waiting_during_restart_stays_latched_after_acknowledgement():
    profile = _profile(restart_requested=True)
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
            assert release.wait(timeout=10)
            return True
        try:
            return consume_sender_restart(linkedin_username=profile.linkedin_username, restart=restart)
        finally:
            connections.close_all()

    def stop():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '10s'")
                cursor.execute("SELECT pg_backend_pid()")
                writer_pid.put(cursor.fetchone()[0])
            return _request(apply=True)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        consumer = executor.submit(consume)
        try:
            assert entered.wait(timeout=5)
            owner_pid = consumer_pid.get(timeout=5)
            writer = executor.submit(stop)
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
            assert blocked, "Stop command did not wait for the restart consumer's row lock"
        finally:
            release.set()
        assert consumer.result(timeout=10)
        assert "Emergency stop latched" in writer.result(timeout=10)

    profile.refresh_from_db()
    assert profile.stop_requested is True
    assert profile.restart_requested is False
    callback = Mock(return_value=True)
    assert not consume_sender_restart(linkedin_username=profile.linkedin_username, restart=callback)
    callback.assert_not_called()
