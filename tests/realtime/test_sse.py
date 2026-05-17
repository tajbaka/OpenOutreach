"""Tests for the realtime SSE framing buffer."""
from __future__ import annotations

from pathlib import Path

from linkedin.realtime.sse import RealtimeSSEBuffer

FIXTURES = Path(__file__).parent.parent / "fixtures" / "realtime"


def test_single_event_one_feed():
    buf = RealtimeSSEBuffer()
    assert buf.feed('data: {"a": 1}\n\n') == [{"a": 1}]


def test_two_events_one_feed():
    buf = RealtimeSSEBuffer()
    assert buf.feed('data: {"a": 1}\n\ndata: {"b": 2}\n\n') == [{"a": 1}, {"b": 2}]


def test_event_split_across_feeds():
    buf = RealtimeSSEBuffer()
    assert buf.feed('data: {"a":') == []
    assert buf.feed(' 1}\n\n') == [{"a": 1}]


def test_non_data_lines_ignored():
    buf = RealtimeSSEBuffer()
    assert buf.feed(':comment\nevent: ping\ndata: {"ok": true}\n\n') == [{"ok": True}]


def test_malformed_json_skipped_no_raise():
    buf = RealtimeSSEBuffer()
    assert buf.feed('data: {bad\n\ndata: {"good": 1}\n\n') == [{"good": 1}]


def test_crlf_normalized():
    buf = RealtimeSSEBuffer()
    assert buf.feed('data: {"a": 1}\r\n\r\n') == [{"a": 1}]


def test_real_raw_chunk_yields_decorated_event():
    raw = (FIXTURES / "raw_stream_chunk.txt").read_text(encoding="utf-8")
    buf = RealtimeSSEBuffer()
    events = buf.feed(raw + "\n\n")
    assert events
    assert any("com.linkedin.realtimefrontend.DecoratedEvent" in e for e in events)
