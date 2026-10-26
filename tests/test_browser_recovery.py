from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from linkedin.browser import recovery


@pytest.fixture
def browser(monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(recovery, "time", SimpleNamespace(monotonic=lambda: clock.now))
    page = Mock(url="https://www.linkedin.com/feed/")
    page.locator.return_value.evaluate.return_value = False
    page.goto.return_value = SimpleNamespace(ok=True)
    notify = Mock()
    guard = recovery.BrowserRecovery(sender="test", notify=notify)
    return SimpleNamespace(clock=clock, page=page, guard=guard, notify=notify)


def test_healthy_page_does_not_navigate(browser):
    assert browser.guard.can_claim(browser.page)
    browser.page.goto.assert_not_called()
    browser.notify.assert_not_called()


def test_offline_page_recovers_before_allowing_claims(browser):
    browser.page.locator.return_value.evaluate.return_value = True
    assert not browser.guard.can_claim(browser.page)
    browser.clock.now = 14
    assert not browser.guard.can_claim(browser.page)
    browser.page.goto.assert_not_called()
    browser.clock.now = 15
    browser.page.locator.return_value.evaluate.return_value = False
    assert browser.guard.can_claim(browser.page)
    browser.page.goto.assert_called_once_with(
        "https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000,
    )
    browser.notify.assert_not_called()


@pytest.mark.parametrize("failure", [
    PlaywrightError("Page.goto: net::ERR_INTERNET_DISCONNECTED"),
    PlaywrightTimeoutError("Page.goto: Timeout 30000ms exceeded"),
    None,
])
def test_three_attempts_then_latched_hold_without_claims_or_repeated_alerts(browser, failure):
    browser.page.locator.return_value.evaluate.return_value = True
    browser.page.goto.side_effect = failure
    assert not browser.guard.can_claim(browser.page)
    for before, due in [(14, 15), (44, 45), (104, 105)]:
        browser.clock.now = before
        attempts = browser.page.goto.call_count
        assert not browser.guard.can_claim(browser.page)
        assert browser.page.goto.call_count == attempts
        browser.clock.now = due
        assert not browser.guard.can_claim(browser.page)
    assert browser.page.goto.call_count == 3
    browser.notify.assert_called_once()
    browser.page.reset_mock()
    browser.clock.now = 10000
    for _ in range(5):
        assert not browser.guard.can_claim(browser.page)
    assert not browser.page.mock_calls
    browser.notify.assert_called_once()


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/login", "https://www.linkedin.com/checkpoint/challenge",
    "https://example.com/feed/", "http://www.linkedin.com/feed/",
])
def test_redirect_is_not_successful_recovery(browser, url):
    browser.guard.task_failed(
        PlaywrightError("Page.evaluate: TypeError: Failed to fetch"), read_only=True, task_id=1,
    )
    browser.clock.now = 15
    browser.page.url = url
    assert not browser.guard.can_claim(browser.page)
    assert browser.guard.blocked
    browser.notify.assert_called_once()


@pytest.mark.parametrize("response", [None, SimpleNamespace(ok=False)])
def test_feed_url_without_successful_response_does_not_release_queue(browser, response):
    browser.guard.task_failed(
        PlaywrightError("Page.goto: net::ERR_CONNECTION_RESET"), read_only=True, task_id=1,
    )
    browser.clock.now = 15
    browser.page.goto.return_value = response
    assert not browser.guard.can_claim(browser.page)
    assert browser.guard.pending


def test_repeated_read_failures_do_not_reset_recovery_budget(browser):
    error = PlaywrightError("Page.evaluate: TypeError: Failed to fetch")
    for delay in (15, 30, 60):
        assert browser.guard.task_failed(error, read_only=True, task_id=1)
        browser.clock.now += delay
        assert browser.guard.can_claim(browser.page)
    browser.guard.task_failed(error, read_only=True, task_id=1)
    assert browser.guard.blocked
    assert not browser.guard.can_claim(browser.page)
    assert browser.page.goto.call_count == 3


def test_successful_browser_task_resets_budget(browser):
    error = PlaywrightError("Page.evaluate: TypeError: Failed to fetch")
    browser.guard.task_failed(error, read_only=True, task_id=1)
    browser.clock.now = 15
    assert browser.guard.can_claim(browser.page)
    browser.guard.task_succeeded()
    browser.guard.task_failed(error, read_only=True, task_id=2)
    assert browser.guard.retry_at == 30
    assert browser.guard.attempts == 0


def test_potential_send_failure_holds_without_navigating_or_retrying(browser):
    assert browser.guard.task_failed(
        PlaywrightError("Page.evaluate: TypeError: Failed to fetch"), read_only=False, task_id=42,
    )
    assert not browser.guard.can_claim(browser.page)
    assert not browser.page.mock_calls
    assert "Task 42" in browser.notify.call_args.kwargs["detail"]


@pytest.mark.parametrize("error", [
    RuntimeError("Failed to fetch"),
    PlaywrightError("Page.evaluate: TypeError: Cannot read properties of undefined"),
    PlaywrightError("Page.goto: net::ERR_CERT_AUTHORITY_INVALID"),
    PlaywrightTimeoutError("Locator.click: Timeout 30000ms exceeded"),
])
def test_unrelated_task_errors_are_not_classified_as_network_failure(browser, error):
    assert not browser.guard.task_failed(error, read_only=True, task_id=1)
    assert not browser.guard.pending
    assert not browser.guard.blocked


def test_unexpected_preflight_error_propagates(browser):
    browser.page.locator.return_value.evaluate.side_effect = PlaywrightError("JS programming error")
    with pytest.raises(PlaywrightError, match="JS programming error"):
        browser.guard.can_claim(browser.page)
    browser.notify.assert_not_called()


def test_unexpected_recovery_navigation_error_propagates(browser):
    browser.guard.task_failed(
        PlaywrightError("Page.goto: net::ERR_CONNECTION_RESET"), read_only=True, task_id=1,
    )
    browser.clock.now = 15
    browser.page.goto.side_effect = PlaywrightError("Unexpected browser failure")
    with pytest.raises(PlaywrightError, match="Unexpected browser failure"):
        browser.guard.can_claim(browser.page)
    browser.notify.assert_not_called()


def _daemon_setup(monkeypatch, fake_session, browser):
    from linkedin import daemon

    fake_session.page = browser.page
    monkeypatch.setattr(daemon, "ENABLE_FREEMIUM_CAMPAIGN", False)
    monkeypatch.setattr(daemon, "ENABLE_REALTIME_LISTENER", False)
    monkeypatch.setattr(daemon, "ENABLE_NODE_MONITOR", False)
    monkeypatch.setattr(daemon, "_build_qualifiers", Mock(return_value={}))
    monkeypatch.setattr(daemon, "heal_tasks", Mock())
    monkeypatch.setattr(daemon, "_active_campaigns", Mock(return_value=[fake_session.campaign]))
    monkeypatch.setattr(daemon, "_sync_listener_supervisor", Mock())
    monkeypatch.setattr(daemon, "seconds_until_active", Mock(return_value=0))
    monkeypatch.setattr(daemon, "_claimable_task_types_now", Mock(return_value=None))
    monkeypatch.setattr(daemon, "notify_error", Mock())
    monkeypatch.setattr(daemon, "notify_degraded", browser.notify)
    monkeypatch.setattr(daemon.connections, "close_all", Mock())
    monkeypatch.setattr("linkedin.discovery.collector.discovery_enabled_for_sender", Mock(return_value=False))
    monkeypatch.setattr("linkedin.enrichment.worker.EnrichmentWorker", Mock())
    monkeypatch.setattr("linkedin.realtime.supervisor.ListenerSupervisor", Mock())
    monkeypatch.setattr("linkedin.monitoring.TaskFailureTracker", Mock())
    return daemon


def test_daemon_never_claims_pending_tasks_while_offline(monkeypatch, fake_session, browser):
    from linkedin.models import Task
    from django.utils import timezone

    daemon = _daemon_setup(monkeypatch, fake_session, browser)
    due = timezone.now()
    task = Task.objects.create(
        task_type=Task.TaskType.CONNECT, scheduled_at=due,
        payload={"campaign_id": fake_session.campaign.pk},
    )
    browser.page.locator.return_value.evaluate.return_value = True
    claim = Mock()
    monkeypatch.setattr(Task.objects, "claim_next", claim)

    def sleep(seconds):
        browser.clock.now += seconds
        if browser.clock.now >= 180:
            raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "time", SimpleNamespace(sleep=sleep))
    with pytest.raises(KeyboardInterrupt):
        daemon.run_daemon(fake_session)
    claim.assert_not_called()
    task.refresh_from_db()
    assert task.status == Task.Status.PENDING
    assert task.scheduled_at == due
    assert task.started_at is None
    assert browser.page.goto.call_count == 3
    browser.notify.assert_called_once()


@pytest.mark.parametrize("task_type", ["connect", "follow_up", "manual_reply", "drip_linkedin"])
def test_daemon_mid_task_network_failure_is_failed_once_and_not_requeued(
    monkeypatch, fake_session, browser, task_type,
):
    from linkedin.models import Task
    from django.utils import timezone

    daemon = _daemon_setup(monkeypatch, fake_session, browser)
    task = Task.objects.create(
        task_type=task_type, scheduled_at=timezone.now(),
        payload={
            "campaign_id": fake_session.campaign.pk,
            "operator": fake_session.linkedin_profile.linkedin_username,
            "public_id": "test", "lead_id": 1, "delivery_id": 1,
            "message": "Synthetic test message",
        },
    )
    claim = Mock(return_value=task)
    monkeypatch.setattr(Task.objects, "claim_next", claim)
    handler = Mock(side_effect=PlaywrightError("Page.evaluate: TypeError: Failed to fetch"))
    monkeypatch.setitem(daemon._HANDLERS, task_type, handler)
    enqueue = Mock()
    monkeypatch.setattr("linkedin.tasks.connect.enqueue_connect", enqueue)
    monkeypatch.setattr("linkedin.diagnostics.capture_failure", Mock(return_value=None))
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "time", SimpleNamespace(sleep=sleep))
    with pytest.raises(KeyboardInterrupt):
        daemon.run_daemon(fake_session)
    handler.assert_called_once()
    claim.assert_called_once()
    enqueue.assert_not_called()
    task.refresh_from_db()
    assert task.status == Task.Status.FAILED
    browser.page.goto.assert_not_called()
    browser.notify.assert_called_once()


def test_daemon_read_failure_recovers_then_claims_next_task_without_replay(
    monkeypatch, fake_session, browser,
):
    from linkedin.models import Task
    from django.utils import timezone

    daemon = _daemon_setup(monkeypatch, fake_session, browser)
    task = Task.objects.create(
        task_type=Task.TaskType.SWEEP_CONNECTIONS, scheduled_at=timezone.now(),
        payload={"operator": fake_session.linkedin_profile.linkedin_username},
    )
    claim = Mock(side_effect=[task, KeyboardInterrupt])
    monkeypatch.setattr(Task.objects, "claim_next", claim)
    handler = Mock(side_effect=PlaywrightError("Page.goto: net::ERR_INTERNET_DISCONNECTED"))
    monkeypatch.setitem(daemon._HANDLERS, task.task_type, handler)
    monkeypatch.setattr("linkedin.diagnostics.capture_failure", Mock(return_value=None))

    def sleep(seconds):
        assert claim.call_count == 1
        browser.clock.now += seconds

    monkeypatch.setattr(daemon, "time", SimpleNamespace(sleep=sleep))
    with pytest.raises(KeyboardInterrupt):
        daemon.run_daemon(fake_session)
    handler.assert_called_once()
    assert claim.call_count == 2
    browser.page.goto.assert_called_once()
    task.refresh_from_db()
    assert task.status == Task.Status.FAILED
    browser.notify.assert_not_called()
