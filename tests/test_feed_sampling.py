import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from linkedin import feed_sampling as sampling
from linkedin.exceptions import FeedSampleInterruptedError
from linkedin.feed_collection import FeedPostRecord
from linkedin.models import LinkedInFeedCollectionJob, LinkedInFeedObservation, LinkedInFeedPost

NOW = datetime(2026, 10, 27, 10, tzinfo=ZoneInfo('America/Toronto'))


@pytest.fixture(autouse=True)
def hours(monkeypatch):
    for key, value in {'ACTIVE_START_HOUR': '9', 'ACTIVE_END_HOUR': '17', 'ACTIVE_TIMEZONE': 'America/Toronto', 'REST_DAYS': '5,6'}.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize('seconds', [3300, 3601, 3900])
def test_random_interval_persists_across_polling_and_restart(tmp_path, monkeypatch, seconds):
    choose = Mock(return_value=seconds)
    monkeypatch.setattr(sampling.random, 'randint', choose)
    schedule = sampling.FeedSampleSchedule(tmp_path, 'test@example.invalid')
    assert schedule.due(NOW)
    run_id = schedule.begin(NOW)
    sampling.write_json(schedule.receipt_path(run_id), {'status': 'completed', 'target_id': ''})
    finished = NOW + timedelta(seconds=20)
    schedule.finish(finished, 0)
    restarted = sampling.FeedSampleSchedule(tmp_path, 'test@example.invalid')
    assert not restarted.due(finished + timedelta(seconds=seconds - 1))
    assert restarted.due(finished + timedelta(seconds=seconds))
    choose.assert_called_once_with(3300, 3900)


@pytest.mark.parametrize('hour,weekday,expected', [(8, 27, False), (9, 27, True), (16, 27, True), (17, 27, False), (10, 31, False)])
def test_samples_respect_working_hours_and_weekends(tmp_path, hour, weekday, expected):
    schedule = sampling.FeedSampleSchedule(tmp_path, 'test@example.invalid')
    assert schedule.due(NOW.replace(day=weekday, hour=hour)) is expected


def test_restart_never_replays_interrupted_sample(tmp_path):
    schedule = sampling.FeedSampleSchedule(tmp_path, 'test@example.invalid')
    schedule.begin(NOW)
    restarted = sampling.FeedSampleSchedule(tmp_path, 'test@example.invalid')
    with pytest.raises(FeedSampleInterruptedError):
        restarted.due(NOW + timedelta(days=1))
    assert not restarted.due(NOW + timedelta(days=2))


def test_run_left_by_a_stopped_supervisor_becomes_a_hold_once(tmp_path):
    schedule = sampling.FeedSampleSchedule(tmp_path, 'test@example.invalid')
    schedule.begin(NOW)
    # The sample may still be running until the supervisor's own time limit.
    assert not schedule.due(NOW + timedelta(seconds=sampling.MAX_SECONDS + sampling.PROCESS_GRACE_SECONDS))
    assert not schedule.read().get('hold')
    # Past it, only a stopped supervisor can have left the reservation behind.
    # Raising reaches the supervisor's existing scheduling-hold alert.
    with pytest.raises(FeedSampleInterruptedError):
        schedule.due(NOW + timedelta(minutes=10))
    assert schedule.read()['hold']
    # Later checks stay quiet until the hold is reviewed and resumed.
    assert not schedule.due(NOW + timedelta(minutes=20))


@pytest.mark.parametrize('code,receipt', [(1, {}), (124, {}), (0, {}), (0, {'status': 'completed', 'target_id': 'unclosed'})])
def test_failure_missing_receipt_and_unclosed_tab_persist_hold(tmp_path, code, receipt):
    schedule = sampling.FeedSampleSchedule(tmp_path, 'test@example.invalid')
    run_id = schedule.begin(NOW)
    sampling.write_json(schedule.receipt_path(run_id), receipt)
    schedule.finish(NOW, code)
    assert schedule.read()['hold']
    assert not schedule.due(NOW + timedelta(days=1))


def record(n):
    return FeedPostRecord(activity_urn=f'urn:li:activity:{n}', post_url=f'https://www.linkedin.com/feed/update/urn:li:activity:{n}/', author_name='Test', author_headline='', author_profile_url='', post_text=f'Post {n}', timestamp_text='1h', posted_at=NOW, raw_payload={})


@pytest.fixture
def sample_setup(monkeypatch):
    page = Mock()
    page.url = 'https://www.linkedin.com/feed/'
    page.evaluate.return_value = False
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(sampling.time, 'monotonic', lambda: clock.now)
    page.wait_for_timeout.side_effect = lambda milliseconds: setattr(clock, 'now', clock.now + milliseconds / 1000)
    monkeypatch.setattr(sampling.random, 'uniform', Mock(return_value=4.5))
    scroll = Mock()
    monkeypatch.setattr(sampling, '_scroll_sample_page', scroll)
    return page, clock, scroll


def collect(page, deadline=180):
    return sampling.collect_sample(page, operator='Arian', account_username='test@example.invalid', deadline=deadline)


def test_time_limit_replaces_post_and_scroll_caps(monkeypatch, sample_setup):
    page, clock, scroll = sample_setup
    extract = Mock(side_effect=[[record(n) for n in range(start, start + 3)] for start in range(100, 300, 3)])
    monkeypatch.setattr('linkedin.feed_collection.extract_posts_from_page', extract)
    result = collect(page)
    assert result['posts_seen'] > 20
    assert result['scrolls'] == scroll.call_count > 5
    assert clock.now == 180
    assert result['stop_reason'] == 'time_limit'
    assert LinkedInFeedPost.objects.count() == result['posts_seen']
    assert LinkedInFeedObservation.objects.filter(job__isnull=True).count() == result['posts_seen']
    assert not LinkedInFeedCollectionJob.objects.exists()
    for call in extract.call_args_list:
        assert call.args == (page,)
        assert call.kwargs == {'allow_menu_lookup': False, 'visible_only': True, 'max_nodes': 20, 'diagnostics': {}}


@pytest.mark.parametrize('duration', [165, 180, 195])
def test_varied_reading_pauses_before_first_and_subsequent_scrolls(monkeypatch, sample_setup, duration):
    from itertools import cycle
    page, clock, scroll = sample_setup
    monkeypatch.setattr('linkedin.feed_collection.extract_posts_from_page', lambda *a, **kw: [])
    choose_pause = Mock(side_effect=cycle([3, 6]))
    monkeypatch.setattr(sampling.random, 'uniform', choose_pause)
    scroll_times = []
    scroll.side_effect = lambda page: scroll_times.append(clock.now)
    collect(page, deadline=duration)
    assert clock.now == duration
    assert scroll_times[0] == 3
    assert scroll_times[-1] < duration
    gaps = [right - left for left, right in zip(scroll_times, scroll_times[1:])]
    assert set(gaps) == {4, 7}  # reading pause plus one second to settle
    for call in choose_pause.call_args_list:
        assert call.args == (3, 6)


def test_repeated_posts_do_not_trigger_endless_scroll_or_duplicate_storage(monkeypatch, sample_setup):
    page, clock, scroll = sample_setup
    monkeypatch.setattr('linkedin.feed_collection.extract_posts_from_page', lambda *a, **kw: [record(100)])
    result = collect(page)
    assert result['posts_seen'] == 1
    assert result['scrolls'] > 5
    assert result['stop_reason'] == 'time_limit'
    collect(page, deadline=clock.now + 180)
    assert LinkedInFeedPost.objects.count() == 1
    assert LinkedInFeedObservation.objects.get().seen_count == 2


def test_deadline_consumed_by_extraction_prevents_writes_and_scroll(monkeypatch, sample_setup):
    page, clock, scroll = sample_setup
    def extract(*a, **kw):
        clock.now = 181
        return [record(100)]
    monkeypatch.setattr('linkedin.feed_collection.extract_posts_from_page', extract)
    result = collect(page)
    assert result['stop_reason'] == 'no_resolvable_posts'
    assert not LinkedInFeedPost.objects.exists()
    scroll.assert_not_called()


@pytest.mark.parametrize('url,blocked', [('https://www.linkedin.com/checkpoint/', False), ('https://www.linkedin.com/login', False), ('chrome-error://chromewebdata/', False), ('https://www.linkedin.com/feed/', True)])
def test_restriction_or_redirect_stops_without_extraction(monkeypatch, sample_setup, url, blocked):
    page, clock, scroll = sample_setup
    page.url = url
    page.evaluate.return_value = blocked
    extract = Mock()
    monkeypatch.setattr('linkedin.feed_collection.extract_posts_from_page', extract)
    with pytest.raises(RuntimeError):
        collect(page)
    extract.assert_not_called()
    scroll.assert_not_called()


def test_watchdog_closes_only_recorded_target_then_exits(monkeypatch, tmp_path):
    close, exit_process = Mock(), Mock()
    monkeypatch.setattr(sampling, 'close_sample_target', close)
    monkeypatch.setattr(sampling.os, '_exit', exit_process)
    watchdog = sampling.SampleWatchdog(9222, tmp_path / 'receipt.json')
    watchdog.target_created('owned-target')
    watchdog.expire()
    close.assert_called_once_with(9222, 'owned-target')
    exit_process.assert_called_once_with(124)


@pytest.mark.parametrize('duration', [165, 180, 195])
def test_duration_drawn_once_and_watchdog_allows_normal_cleanup(monkeypatch, tmp_path, duration):
    choose = Mock(return_value=duration)
    monkeypatch.setattr(sampling.random, 'randint', choose)
    monkeypatch.setattr(sampling.time, 'monotonic', lambda: 1000)
    watchdog = sampling.SampleWatchdog(9222, tmp_path / 'receipt.json')
    choose.assert_called_once_with(165, 195)
    assert watchdog.duration_seconds == duration
    assert watchdog.deadline == 1000 + duration
    assert watchdog.deadline < watchdog.hard_deadline < 1000 + duration + sampling.PROCESS_GRACE_SECONDS
    assert watchdog.timer.interval == watchdog.hard_deadline - 1000


@pytest.mark.parametrize('startup', [0, 4, 12, 30])
def test_sample_clock_starts_at_reservation_so_supervisor_limit_is_never_reached(monkeypatch, tmp_path, startup):
    monkeypatch.setattr(sampling.random, 'randint', Mock(return_value=sampling.MAX_SECONDS))
    monkeypatch.setattr(sampling.time, 'monotonic', lambda: 1000)
    # The supervisor reserves, then launches; its limit starts at launch.
    reserved = datetime.now(timezone.utc) - timedelta(seconds=startup)
    watchdog = sampling.SampleWatchdog(9222, tmp_path / 'receipt.json', started_at=reserved)
    supervisor_kill = 1000 - startup + sampling.MAX_SECONDS + sampling.PROCESS_GRACE_SECONDS
    assert watchdog.deadline == pytest.approx(1000 - startup + sampling.MAX_SECONDS, abs=0.5)
    assert watchdog.hard_deadline < supervisor_kill
    assert watchdog.timer.interval == pytest.approx(watchdog.hard_deadline - 1000)
    assert watchdog.duration_seconds == sampling.MAX_SECONDS


def test_invalid_target_never_makes_network_request(monkeypatch):
    opener = Mock(side_effect=AssertionError('No request'))
    monkeypatch.setattr(sampling, 'build_opener', opener)
    assert not sampling.close_sample_target(9222, '../another-target')
    opener.assert_not_called()


def test_sample_preserves_legacy_observation_job(monkeypatch, sample_setup):
    from linkedin.feed_collection import ensure_collection_jobs, upsert_feed_record
    page, clock, scroll = sample_setup
    job = ensure_collection_jobs(operator='Arian', account_username='test@example.invalid')
    upsert_feed_record(record(100), job=job)
    monkeypatch.setattr('linkedin.feed_collection.extract_posts_from_page', lambda *a, **kw: [record(100)])
    collect(page)
    assert LinkedInFeedObservation.objects.get().job_id == job.pk


def test_browser_start_failure_is_not_retried(monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    from playwright.sync_api import TimeoutError
    manager = MagicMock()
    manager.__enter__.return_value.chromium.connect_over_cdp.side_effect = TimeoutError('offline test')
    factory = Mock(return_value=manager)
    monkeypatch.setattr('playwright.sync_api.sync_playwright', factory)
    watchdog = sampling.SampleWatchdog(9222, tmp_path / 'receipt.json')
    with pytest.raises(TimeoutError):
        sampling.run_sample(operator='Arian', account_username='test@example.invalid', port=9222, watchdog=watchdog)
    factory.assert_called_once()


def test_empty_sample_stops_at_deadline_without_treating_it_as_restriction(monkeypatch, sample_setup):
    page, clock, scroll = sample_setup
    monkeypatch.setattr('linkedin.feed_collection.extract_posts_from_page', lambda *a, **kw: [])
    result = collect(page)
    assert result['posts_seen'] == 0
    assert clock.now == 180
    assert result['scrolls'] > 5
    assert result['stop_reason'] == 'no_resolvable_posts'


def test_no_last_moment_scroll_with_short_remaining_pause(monkeypatch, sample_setup):
    page, clock, scroll = sample_setup
    monkeypatch.setattr('linkedin.feed_collection.extract_posts_from_page', lambda *a, **kw: [record(100)])
    collect(page, deadline=2)
    assert clock.now == 2
    scroll.assert_not_called()


def test_checkpoint_appearing_during_reading_stops_next_scroll(monkeypatch, sample_setup):
    page, clock, scroll = sample_setup
    monkeypatch.setattr('linkedin.feed_collection.extract_posts_from_page', lambda *a, **kw: [])
    def wait(milliseconds):
        clock.now += milliseconds / 1000
        page.url = 'https://www.linkedin.com/checkpoint/'
    page.wait_for_timeout.side_effect = wait
    with pytest.raises(RuntimeError):
        collect(page)
    scroll.assert_not_called()


def test_coverage_includes_unresolved_cards_and_deduplicates_resightings(monkeypatch, sample_setup):
    page, clock, scroll = sample_setup
    def extract(page, **kwargs):
        kwargs['diagnostics']['cards'] = [
            {'card_key': 'post1', 'status': 'extracted', 'post_url': record(100).post_url},
            {'card_key': 'post2', 'status': 'missing_identity', 'post_url': ''},
            {'card_key': 'recommendations', 'status': 'non_post_module', 'post_url': ''},
        ]
        return [record(100)]
    monkeypatch.setattr('linkedin.feed_collection.extract_posts_from_page', extract)
    result = collect(page)
    assert result['coverage'] == {
        'post_cards_seen': 2, 'post_cards_extracted': 1, 'post_cards_saved': 1,
        'non_post_modules': 1, 'unsaved_reasons': {'missing_identity': 1},
    }


@pytest.fixture
def sample_command(tmp_path, monkeypatch):
    from django.contrib.auth.models import User
    from linkedin.management.commands import sample_linkedin_feed as command
    from linkedin.models import LinkedInProfile
    LinkedInProfile.objects.create(
        user=User.objects.create(username='sampler'), linkedin_username='sampler@example.invalid',
        linkedin_password='synthetic-only', active=True, subscribe_newsletter=False, legal_accepted=True,
    )
    monkeypatch.setenv('LINKEDIN_USERNAME', 'sampler@example.invalid')
    monkeypatch.setattr('linkedin.conf.get_daemon_handle', lambda: 'sampler')
    monkeypatch.setattr('linkedin.operators.resolve_operator', lambda username: 'Arian')
    schedule = sampling.FeedSampleSchedule(tmp_path, 'sampler@example.invalid')
    monkeypatch.setattr(command, 'FeedSampleSchedule', lambda root, username: schedule)
    run = Mock(return_value={'posts_seen': 0, 'posts_created': 0, 'scrolls': 0, 'stop_reason': 'no_resolvable_posts'})
    monkeypatch.setattr(command, 'run_sample', run)
    # The sample's clock starts at its reservation, so reserve at the real time.
    monkeypatch.setattr(sampling, 'in_working_hours', lambda now: True)
    monkeypatch.setattr(sampling.os, '_exit', Mock())
    return schedule, schedule.begin(datetime.now(timezone.utc)), run


def test_sample_command_times_itself_from_the_reservation(sample_command, monkeypatch):
    from django.core.management import call_command
    from linkedin.management.commands import sample_linkedin_feed as command
    schedule, run_id, run = sample_command
    created = []
    real = command.SampleWatchdog
    monkeypatch.setattr(command, 'SampleWatchdog', lambda *a, **kw: created.append(kw) or real(*a, **kw))
    call_command('sample_linkedin_feed', '--run-id', run_id)
    assert created[0]['started_at'] == datetime.fromisoformat(schedule.read()['started_at'])


def test_reservation_that_expired_before_startup_is_held_not_sampled(sample_command):
    from django.core.management import CommandError, call_command
    schedule, run_id, run = sample_command
    state = schedule.read()
    state['started_at'] = (datetime.now(timezone.utc) - timedelta(seconds=sampling.MAX_SECONDS)).isoformat()
    sampling.write_json(schedule.path, state)
    with pytest.raises(CommandError):
        call_command('sample_linkedin_feed', '--run-id', run_id)
    run.assert_not_called()
    assert json.loads(schedule.receipt_path(run_id).read_text())['status'] == 'failed'


def test_sample_command_holds_no_database_session_lock(sample_command):
    from django.core.management import call_command
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    schedule, run_id, run = sample_command
    # A session lock taken through a transaction pooler can stay on a pooled
    # session after unlock runs on another, holding every later sample.
    with CaptureQueriesContext(connection) as queries:
        call_command('sample_linkedin_feed', '--run-id', run_id)
    run.assert_called_once()
    assert not [query['sql'] for query in queries if 'advisory' in query['sql']]
    assert json.loads(schedule.receipt_path(run_id).read_text())['status'] == 'completed'


def test_second_process_for_a_claimed_run_neither_collects_nor_rewrites_its_receipt(sample_command):
    from django.core.management import CommandError, call_command
    schedule, run_id, run = sample_command
    receipt = schedule.receipt_path(run_id)
    sampling.write_json(receipt, {'status': 'running', 'target_id': 'owned-target', 'cdp_port': 9222})
    before = receipt.read_bytes()
    with pytest.raises(CommandError):
        call_command('sample_linkedin_feed', '--run-id', run_id)
    run.assert_not_called()
    assert receipt.read_bytes() == before


def test_receipt_claim_is_exclusive(tmp_path):
    path = tmp_path / ('a' * 32 + '.json')
    assert sampling.claim_receipt(path, {'status': 'starting'})
    assert not sampling.claim_receipt(path, {'status': 'starting', 'second': True})
    assert json.loads(path.read_text()) == {'status': 'starting'}


def test_deadline_after_extraction_does_not_claim_saved_coverage(monkeypatch, sample_setup):
    page, clock, scroll = sample_setup
    def extract(page, **kwargs):
        kwargs['diagnostics']['cards'] = [{'card_key': 'post1', 'status': 'extracted', 'post_url': record(100).post_url}]
        clock.now = 181
        return [record(100)]
    monkeypatch.setattr('linkedin.feed_collection.extract_posts_from_page', extract)
    result = collect(page)
    assert result['coverage']['post_cards_extracted'] == 1
    assert result['coverage']['post_cards_saved'] == 0
    assert result['coverage']['unsaved_reasons'] == {'not_saved_before_deadline': 1}
