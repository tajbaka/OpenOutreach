"""Unit coverage for RealtimeListener — pump, is_alive, and the SSE
dispatch path. The browser/CDP tab wiring (start/stop) is integration-
tested by hand (see the plan's Task 12).
"""
from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

from linkedin.realtime.listener import RealtimeListener


class _FakePage:
    def __init__(self):
        self.total_waited_ms = 0
        self._closed = False

    def wait_for_timeout(self, ms):
        self.total_waited_ms += ms

    def is_closed(self):
        return self._closed


def _listener_with_fake_page():
    session = MagicMock()
    session.linkedin_profile.linkedin_username = "arian@x.com"
    listener = RealtimeListener(session, operator="Arian")
    listener.page = _FakePage()
    return listener


def test_pump_waits_the_full_duration(monkeypatch):
    monkeypatch.setattr("linkedin.realtime.listener.LISTENER_PUMP_SLICE_SECONDS", 30)
    listener = _listener_with_fake_page()
    with patch("linkedin.realtime.heartbeat.write_heartbeat") as mock_hb:
        listener.pump(70)
    assert listener.page.total_waited_ms == 70_000
    # 30 + 30 + 10 → three slices → three heartbeat writes
    assert mock_hb.call_count == 3


def test_pump_zero_or_negative_is_noop(monkeypatch):
    monkeypatch.setattr("linkedin.realtime.listener.LISTENER_PUMP_SLICE_SECONDS", 30)
    listener = _listener_with_fake_page()
    with patch("linkedin.realtime.heartbeat.write_heartbeat") as mock_hb:
        listener.pump(0)
        listener.pump(-5)
    assert listener.page.total_waited_ms == 0
    mock_hb.assert_not_called()


def test_is_alive_false_when_no_page():
    listener = RealtimeListener(MagicMock())
    assert listener.is_alive is False


def test_dispatch_decodes_frames_and_calls_handler():
    """_dispatch: base64 → SSE framing → one handle_realtime_event per event."""
    listener = RealtimeListener(MagicMock(), operator="Arian")
    sse_text = 'data: {"a": 1}\n\ndata: {"b": 2}\n\n'
    chunk_b64 = base64.b64encode(sse_text.encode("utf-8")).decode("ascii")
    with patch("linkedin.realtime.listener.handle_realtime_event") as mock_handle:
        listener._dispatch(chunk_b64)
    assert mock_handle.call_count == 2
    assert mock_handle.call_args_list[0].args[0] == {"a": 1}
    assert mock_handle.call_args_list[1].kwargs == {"operator": "Arian"}


def test_dispatch_buffers_event_split_across_chunks():
    """An event split across two stream chunks is handled once, on completion."""
    listener = RealtimeListener(MagicMock(), operator="Arian")
    part1 = base64.b64encode(b'data: {"a":').decode("ascii")
    part2 = base64.b64encode(b' 1}\n\n').decode("ascii")
    with patch("linkedin.realtime.listener.handle_realtime_event") as mock_handle:
        listener._dispatch(part1)
        assert mock_handle.call_count == 0
        listener._dispatch(part2)
        assert mock_handle.call_count == 1
        assert mock_handle.call_args.args[0] == {"a": 1}
