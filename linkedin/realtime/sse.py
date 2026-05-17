"""Frames LinkedIn's realtime Server-Sent-Events stream.

CDP delivers the /realtime/connect response body as raw text chunks via
Network.dataReceived. One chunk may contain several SSE events, and a
single event may be split across two chunks. RealtimeSSEBuffer accumulates
the text and yields complete, JSON-decoded events.

SSE framing: events are separated by a blank line; payload lines start
with `data:`. LinkedIn sends one compact (single-line) JSON object per
event's `data:` line.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


class RealtimeSSEBuffer:
    """Stateful buffer: feed() raw stream text, get back decoded events."""

    def __init__(self):
        self._pending = ""

    def feed(self, text: str) -> list[dict]:
        """Append `text`; return every complete event decoded since last call.

        Incomplete trailing data is held until the next feed().
        """
        self._pending += text.replace("\r\n", "\n")
        events: list[dict] = []
        while "\n\n" in self._pending:
            block, self._pending = self._pending.split("\n\n", 1)
            payload = self._data_payload(block)
            if not payload:
                continue
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError as e:
                logger.debug("realtime SSE: undecodable data line skipped: %s", e)
        return events

    @staticmethod
    def _data_payload(block: str) -> str:
        """Join the `data:` lines of one SSE event block into one string."""
        parts = [
            line[len("data:"):].lstrip(" ")
            for line in block.split("\n")
            if line.startswith("data:")
        ]
        return "\n".join(parts)
