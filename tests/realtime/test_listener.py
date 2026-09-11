"""Listener reconnection, owned-tab recovery, and cooperative shutdown."""
from __future__ import annotations

from threading import Event
from unittest.mock import MagicMock, patch

import pytest
from playwright.sync_api import Error as PlaywrightError

from linkedin.realtime import listener


def test_run_listener_gives_up_after_max_quick_failures(monkeypatch):
    """Quick consecutive connect failures exhaust the cap → exit code 1."""
    monkeypatch.setattr(listener, "_RECONNECT_DELAY_SECONDS", 0)
    calls = {"n": 0}

    def always_fail(**kwargs):
        calls["n"] += 1
        raise PlaywrightError("no browser on CDP port")

    with patch.object(listener, "_run_one_connection", side_effect=always_fail), \
         patch.object(listener.time, "sleep"):
        code = listener.run_listener(operator="Arian", username="a@x.com", cdp_port=9222)

    assert code == 1
    assert calls["n"] == listener._MAX_CONSECUTIVE_FAILURES


def test_run_listener_resets_failures_after_a_real_connection(monkeypatch):
    """A connection that lasted a while (then dropped) resets the failure
    count — a long-lived listener that reconnects forever never exits."""
    monkeypatch.setattr(listener, "_RECONNECT_DELAY_SECONDS", 0)
    monkeypatch.setattr(listener, "_MAX_CONSECUTIVE_FAILURES", 3)
    state = {"n": 0}
    times = iter([0.0, 999.0, 999.0,   # call 1: lasted 999s → reset
                  1000.0, 1001.0,      # call 2: lasted 1s → failure 1
                  1002.0, 1003.0,      # call 3: 1s → failure 2
                  1004.0, 1005.0])     # call 4: 1s → failure 3 → give up

    def conn(**kwargs):
        state["n"] += 1
        raise PlaywrightError("dropped")

    monkeypatch.setattr(listener.time, "monotonic", lambda: next(times))
    with patch.object(listener, "_run_one_connection", side_effect=conn), \
         patch.object(listener.time, "sleep"):
        code = listener.run_listener(operator="Arian", username="a@x.com", cdp_port=9222)

    assert code == 1
    # call 1 reset the counter, so it took 1 (reset) + 3 (fail) = 4 attempts
    assert state["n"] == 4


def test_open_messaging_page_waits_only_for_navigation_commit():
    page = type("Page", (), {"calls": []})()

    def goto(url, *, wait_until, timeout):
        page.calls.append((url, wait_until, timeout))

    page.goto = goto

    listener._open_messaging_page(page)

    assert page.calls == [(
        listener.MESSAGING_URL,
        "commit",
        listener._MESSAGING_NAV_TIMEOUT_MS,
    )]


def _browser_context():
    context = MagicMock()
    context.pages = []

    def new_page():
        page = MagicMock()
        page.is_closed.return_value = False
        page.target_id = f"target-{len(context.pages)}"
        context.pages.append(page)
        return page

    def new_cdp_session(page):
        cdp = MagicMock()
        cdp.send.return_value = {"targetInfo": {"targetId": page.target_id}}
        return cdp

    context.new_page.side_effect = new_page
    context.new_cdp_session.side_effect = new_cdp_session
    return context


def test_reconnect_reclaims_exact_tab_after_cleanup_connection_loss(tmp_path):
    context = _browser_context()
    working = context.new_page()
    manual = context.new_page()
    record = tmp_path / "listener.txt"
    owned = listener._listener_page(context, record)
    owned.close.side_effect = PlaywrightError("CDP disconnected")
    listener._close_listener_page(owned, record)

    assert listener._listener_page(context, record) is owned
    assert len(context.pages) == 3
    working.close.assert_not_called()
    manual.close.assert_not_called()
    owned.goto.assert_not_called()  # selection itself cannot navigate any tab


def test_successful_shutdown_closes_owned_tab_and_removes_record(tmp_path):
    context = _browser_context()
    record = tmp_path / "listener.txt"
    owned = listener._listener_page(context, record)
    listener._close_listener_page(owned, record)
    owned.close.assert_called_once()
    assert not record.exists()


def test_obsolete_browser_target_creates_one_new_owned_tab(tmp_path):
    context = _browser_context()
    working = context.new_page()
    record = tmp_path / "listener.txt"
    record.write_text("target-from-previous-browser")
    page = listener._listener_page(context, record)
    assert page is not working
    assert record.read_text() == page.target_id
    assert listener._listener_page(context, record) is page
    working.close.assert_not_called()


def test_closed_owned_tab_is_replaced(tmp_path):
    context = _browser_context()
    record = tmp_path / "listener.txt"
    old = listener._listener_page(context, record)
    old.is_closed.return_value = True
    replacement = listener._listener_page(context, record)
    assert replacement is not old
    assert record.read_text() == replacement.target_id


def test_record_failure_closes_new_tab_and_propagates(tmp_path, monkeypatch):
    context = _browser_context()
    record = tmp_path / "listener.txt"
    with patch.object(type(record), "replace", side_effect=PermissionError("read only")):
        with pytest.raises(PermissionError):
            listener._listener_page(context, record)
    context.pages[0].close.assert_called_once()


def test_tab_records_are_scoped_by_account_and_port():
    path = listener._tab_record_path("A@x.com", 9222)
    assert path == listener._tab_record_path(" a@x.com ", 9222)
    assert path != listener._tab_record_path("b@x.com", 9222)
    assert path != listener._tab_record_path("a@x.com", 9223)


def test_shutdown_during_reconnect_delay_does_not_open_another_tab():
    stop = Event()

    def disconnected(**kwargs):
        stop.set()
        raise PlaywrightError("disconnected")

    with patch.object(listener, "_run_one_connection", side_effect=disconnected) as connect:
        assert listener.run_listener(operator="Arian", username="a@x.com", stop_event=stop) == 0
    connect.assert_called_once()


def test_unexpected_errors_crash_instead_of_reconnecting():
    with patch.object(listener, "_run_one_connection", side_effect=ValueError("bad state")):
        with pytest.raises(ValueError, match="bad state"):
            listener.run_listener(operator="Arian", username="a@x.com")


@pytest.mark.parametrize("navigation_fails", [False, True])
def test_connection_cleans_up_on_stop_or_navigation_failure(tmp_path, navigation_fails):
    context = _browser_context()
    record = tmp_path / "listener.txt"
    page = listener._listener_page(context, record)
    stop = Event()
    page.wait_for_timeout.side_effect = lambda _: stop.set()
    if navigation_fails:
        page.goto.side_effect = PlaywrightError("navigation timed out")
    pw = MagicMock()
    pw.chromium.connect_over_cdp.return_value.contexts = [context]

    with patch.object(listener, "sync_playwright") as playwright, \
         patch.object(listener, "_tab_record_path", return_value=record), \
         patch.object(listener, "write_heartbeat"):
        playwright.return_value.__enter__.return_value = pw
        if navigation_fails:
            with pytest.raises(PlaywrightError):
                listener._run_one_connection(cdp_port=9222, operator="Arian",
                                             username="a@x.com", stop_event=stop)
        else:
            listener._run_one_connection(cdp_port=9222, operator="Arian",
                                         username="a@x.com", stop_event=stop)
            page.wait_for_timeout.assert_called_once_with(1000)
    page.close.assert_called_once()
    assert not record.exists()
