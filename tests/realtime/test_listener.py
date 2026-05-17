"""Tests for the realtime listener process — the reconnect control loop.

The CDP/browser path (_run_one_connection) needs a live browser and is
covered by the deferred manual integration test, not here.
"""
from __future__ import annotations

from unittest.mock import patch

from linkedin.realtime import listener


def test_run_listener_gives_up_after_max_quick_failures(monkeypatch):
    """Quick consecutive connect failures exhaust the cap → exit code 1."""
    monkeypatch.setattr(listener, "_RECONNECT_DELAY_SECONDS", 0)
    calls = {"n": 0}

    def always_fail(**kwargs):
        calls["n"] += 1
        raise RuntimeError("no browser on CDP port")

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
        raise RuntimeError("dropped")

    monkeypatch.setattr(listener.time, "monotonic", lambda: next(times))
    with patch.object(listener, "_run_one_connection", side_effect=conn), \
         patch.object(listener.time, "sleep"):
        code = listener.run_listener(operator="Arian", username="a@x.com", cdp_port=9222)

    assert code == 1
    # call 1 reset the counter, so it took 1 (reset) + 3 (fail) = 4 attempts
    assert state["n"] == 4
