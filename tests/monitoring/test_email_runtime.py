from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import OperationalError
from django.utils import timezone

from linkedin.models import DaemonHeartbeat
from linkedin.monitoring import email_runtime as runtime
from linkedin.monitoring.node_monitor import write_heartbeat


def process(code=None):
    return SimpleNamespace(poll=lambda: code)


@pytest.fixture
def monitor(monkeypatch):
    # Keep the pytest transaction's connection; the production wrapper has
    # separate timeout/cleanup coverage below.
    monkeypatch.setattr(runtime.EmailRuntimeHeartbeat, '_database', staticmethod(lambda operation: operation()))
    monkeypatch.setattr(runtime.conf, 'MONITOR_INTERVAL_SECONDS', 300)
    return runtime.EmailRuntimeHeartbeat('Chuka')


def test_refreshes_immediately_then_at_the_monitor_interval(monitor, monkeypatch):
    clock = SimpleNamespace(now=10)
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: clock.now)
    monitor.tick(process(), process(), stopped=False)
    row = DaemonHeartbeat.objects.get(sender='Chuka')
    first = row.last_alive
    assert row.runtime_mode == 'email_only'
    clock.now = 309
    monitor.tick(process(), process(), stopped=False)
    row.refresh_from_db()
    assert row.last_alive == first
    clock.now = 310
    monitor.tick(process(), process(), stopped=False)
    row.refresh_from_db()
    assert row.last_alive > first


@pytest.mark.parametrize('child,gmail,stopped', [
    (None, process(), False), (process(), None, False),
    (process(1), process(), False), (process(), process(0), False),
    (process(), process(), True),
])
def test_missing_exited_or_stopped_workers_do_not_claim_liveness(monitor, child, gmail, stopped):
    monitor.tick(child, gmail, stopped=stopped)
    assert not DaemonHeartbeat.objects.filter(sender='Chuka').exists()


def test_failed_worker_stops_refreshes_and_replacement_beats_without_waiting(monitor, monkeypatch):
    clock = SimpleNamespace(now=10)
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: clock.now)
    monitor.tick(process(), process(), stopped=False)
    first = DaemonHeartbeat.objects.get(sender='Chuka').last_alive
    clock.now = 11
    monitor.tick(process(), process(1), stopped=False)
    assert DaemonHeartbeat.objects.get(sender='Chuka').last_alive == first
    clock.now = 12
    monitor.tick(process(), process(), stopped=False)
    assert DaemonHeartbeat.objects.get(sender='Chuka').last_alive > first


def test_cleanup_requires_both_children_stopped(monitor):
    monitor.tick(process(), process(), stopped=False)
    monitor.stopped(process(0), process())
    assert DaemonHeartbeat.objects.get(sender='Chuka').last_alive is not None
    monitor.stopped(process(0), process(0))
    assert DaemonHeartbeat.objects.get(sender='Chuka').last_alive is None


@pytest.mark.parametrize('new_mode', ['email_only', 'linkedin'])
def test_old_supervisor_cannot_clear_replacement_heartbeat(monitor, new_mode):
    monitor.tick(process(), process(), stopped=False)
    replacement = timezone.now() + timedelta(seconds=1)
    DaemonHeartbeat.objects.filter(sender='Chuka').update(last_alive=replacement, runtime_mode=new_mode)
    monitor.stopped(process(0), process(0))
    assert DaemonHeartbeat.objects.get(sender='Chuka').last_alive == replacement


def test_monitor_failure_does_not_crash_workers_or_expose_credentials(monitor, monkeypatch, caplog):
    monkeypatch.setattr(runtime, 'write_heartbeat', Mock(side_effect=OperationalError('secret-value')))
    monitor.tick(process(), process(), stopped=False)
    assert 'OperationalError' in caplog.text and 'secret-value' not in caplog.text
    assert not DaemonHeartbeat.objects.filter(sender='Chuka').exists()


def test_database_operation_is_bounded_and_connection_closed_on_error(monkeypatch):
    connection = SimpleNamespace(settings_dict={'OPTIONS': {}}, close=Mock())
    monkeypatch.setattr(runtime, 'connection', connection)
    with pytest.raises(OperationalError):
        runtime.EmailRuntimeHeartbeat._database(Mock(side_effect=OperationalError()))
    assert connection.close.call_count == 2
    assert connection.settings_dict['OPTIONS']['connect_timeout'] == 5
    assert 'statement_timeout=5000' in connection.settings_dict['OPTIONS']['options']


def test_linkedin_resume_restores_normal_monitoring_mode(monitor):
    monitor.tick(process(), process(), stopped=False)
    write_heartbeat('Chuka')
    assert DaemonHeartbeat.objects.get(sender='Chuka').runtime_mode == 'linkedin'
