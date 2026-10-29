from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import daemon_supervisor as supervisor
from linkedin.feed_sampling import FeedSampleSchedule, write_json


def test_supervisor_only_launches_new_sample_command(monkeypatch):
    launch = Mock()
    monkeypatch.setattr(supervisor, '_launch', launch)
    supervisor._start_feed_collector('a' * 32)
    assert launch.call_args.args[0][1:] == ['manage.py', 'sample_linkedin_feed', '--run-id', 'a' * 32]


def test_enabled_supervisor_uses_persisted_schedule_not_daily_jobs(monkeypatch, tmp_path):
    monkeypatch.setenv('ENABLE_LINKEDIN_FEED_COLLECTOR', 'true')
    schedule = FeedSampleSchedule(tmp_path, 'test@example.invalid')
    now = datetime(2026, 10, 27, 10, tzinfo=ZoneInfo('America/Toronto'))
    assert supervisor._feed_collection_should_start(now, schedule=schedule)
    schedule.begin(now)
    assert not supervisor._feed_collection_should_start(now, schedule=schedule)


def test_reservation_left_by_a_stopped_supervisor_reaches_scheduling_alert(monkeypatch, tmp_path):
    from datetime import timedelta
    import pytest
    from linkedin.exceptions import FeedSampleInterruptedError
    monkeypatch.setenv('ENABLE_LINKEDIN_FEED_COLLECTOR', 'true')
    schedule = FeedSampleSchedule(tmp_path, 'test@example.invalid')
    now = datetime(2026, 10, 27, 10, tzinfo=ZoneInfo('America/Toronto'))
    schedule.begin(now)
    # The supervisor's existing feed-scheduling handler alerts on this error.
    with pytest.raises(FeedSampleInterruptedError):
        supervisor._feed_collection_should_start(now + timedelta(minutes=10), schedule=schedule)

def test_disabled_collector_never_opens_schedule(monkeypatch):
    monkeypatch.setenv('ENABLE_LINKEDIN_FEED_COLLECTOR', 'false')
    schedule = SimpleNamespace(due=Mock(side_effect=AssertionError('disabled')))
    assert not supervisor._feed_collection_should_start(schedule=schedule)


def test_cleanup_only_uses_current_sample_target(monkeypatch, tmp_path):
    schedule = FeedSampleSchedule(tmp_path, 'test@example.invalid')
    run_id = 'a' * 32
    write_json(schedule.path, {'run_id': run_id})
    write_json(schedule.receipt_path(run_id), {'target_id': 'sample-target', 'cdp_port': 9222})
    close = Mock()
    monkeypatch.setattr(supervisor, 'close_sample_target', close)
    supervisor._close_owned_sample_tab(schedule)
    close.assert_called_once_with(9222, 'sample-target')


def test_due_check_is_not_rounded_up_to_next_minute(tmp_path):
    from datetime import timedelta
    schedule = FeedSampleSchedule(tmp_path, 'test@example.invalid')
    now = datetime(2026, 10, 27, 10, tzinfo=ZoneInfo('America/Toronto'))
    write_json(schedule.path, {'next_due_at': (now + timedelta(seconds=7)).isoformat()})
    assert supervisor._next_feed_check_delay(schedule, now) == 7
