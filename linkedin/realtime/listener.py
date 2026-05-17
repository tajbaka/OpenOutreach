"""Realtime listener tab: a second page in the daemon's existing browser
context, observing LinkedIn's own realtime stream via CDP.

LinkedIn's web client opens a long-lived streaming fetch to
/realtime/connect (text/event-stream) when /messaging/ is loaded. It is
NOT a native EventSource, so CDP's Network.eventSourceMessageReceived
never fires. The listener instead:
  - watches Network.requestWillBeSent for the /realtime/connect URL,
  - on Network.responseReceived calls Network.streamResourceContent for
    that request — after which Network.dataReceived events carry the body
    bytes as base64 (and the call's result holds whatever was buffered),
  - decodes those bytes, frames them with RealtimeSSEBuffer, and feeds
    each decoded event to handle_realtime_event.

Same browser context = same cookies, fingerprint, TLS, and IP as the
daemon. We open nothing LinkedIn's own client wouldn't.

The listener enqueues no tasks. Its callbacks fire whenever Python is
parked inside any Playwright call — including the daemon's chunked idle
pump() and (for free) during connect/follow-up task execution.
"""
from __future__ import annotations

import base64
import logging

from linkedin.conf import ENABLE_REALTIME_LISTENER, LISTENER_PUMP_SLICE_SECONDS
from linkedin.realtime.handler import handle_realtime_event
from linkedin.realtime.sse import RealtimeSSEBuffer

logger = logging.getLogger(__name__)

MESSAGING_URL = "https://www.linkedin.com/messaging/"
_REALTIME_CONNECT_PATH = "/realtime/connect"


class RealtimeListener:
    """Owns the listener tab + CDP session for one AccountSession."""

    def __init__(self, session, *, operator: str = ""):
        self.session = session
        self.operator = operator
        self.page = None
        self.cdp = None
        self._buffer = RealtimeSSEBuffer()
        self._stream_request_ids: set = set()

    @property
    def is_alive(self) -> bool:
        return self.page is not None and not self.page.is_closed()

    def start(self) -> None:
        """Open the listener tab, attach CDP, subscribe. Raises on failure —
        the caller (ensure_realtime_listener) degrades gracefully."""
        context = self.session.context
        self.page = context.new_page()
        self.cdp = context.new_cdp_session(self.page)
        self.cdp.send("Network.enable")
        self.cdp.on("Network.requestWillBeSent", self._on_request)
        self.cdp.on("Network.responseReceived", self._on_response)
        self.cdp.on("Network.dataReceived", self._on_data)
        self.page.goto(MESSAGING_URL, wait_until="domcontentloaded")
        logger.info("Realtime listener tab opened — observing %s", _REALTIME_CONNECT_PATH)

    def _on_request(self, params: dict) -> None:
        url = (params.get("request") or {}).get("url", "")
        request_id = params.get("requestId")
        if request_id and _REALTIME_CONNECT_PATH in url:
            self._stream_request_ids.add(request_id)

    def _on_response(self, params: dict) -> None:
        request_id = params.get("requestId")
        if request_id not in self._stream_request_ids:
            return
        # Enable body streaming for the realtime connection. The result
        # carries whatever was already buffered; subsequent dataReceived
        # events then carry the live chunks.
        try:
            result = self.cdp.send(
                "Network.streamResourceContent", {"requestId": request_id},
            )
        except Exception as e:
            logger.warning("streamResourceContent failed for %s: %s", request_id, e)
            return
        buffered = result.get("bufferedData")
        if buffered:
            self._dispatch(buffered)

    def _on_data(self, params: dict) -> None:
        if params.get("requestId") not in self._stream_request_ids:
            return
        data_b64 = params.get("data")
        if data_b64:
            self._dispatch(data_b64)

    def _dispatch(self, data_b64: str) -> None:
        """Decode a base64 stream chunk, frame it, handle each event.

        Never raises — handle_realtime_event is itself try/except wrapped,
        and a bad chunk here is logged and dropped.
        """
        try:
            text = base64.b64decode(data_b64).decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning("realtime: undecodable stream chunk dropped: %s", e)
            return
        for event in self._buffer.feed(text):
            handle_realtime_event(event, operator=self.operator)

    def pump(self, seconds: float) -> None:
        """Sleep `seconds` in slices via the listener page, refreshing the
        heartbeat each slice. Keeps the Playwright event loop pumped so CDP
        callbacks fire promptly. Used in place of the daemon's idle sleep."""
        from linkedin.realtime.heartbeat import write_heartbeat

        username = self.session.linkedin_profile.linkedin_username
        remaining = seconds
        while remaining > 0:
            chunk = min(LISTENER_PUMP_SLICE_SECONDS, remaining)
            self.page.wait_for_timeout(int(chunk * 1000))
            write_heartbeat(username)
            remaining -= chunk

    def stop(self) -> None:
        """Detach CDP and close the listener tab. Idempotent, never raises."""
        was_open = self.page is not None
        try:
            if self.cdp is not None:
                self.cdp.detach()
        except Exception as e:
            logger.debug("Realtime listener CDP detach failed: %s", e)
        try:
            if self.page is not None and not self.page.is_closed():
                self.page.close()
        except Exception as e:
            logger.debug("Realtime listener tab close failed: %s", e)
        self.page = self.cdp = None
        if was_open:
            logger.info("Realtime listener tab closed")


def ensure_realtime_listener(session, *, operator: str = ""):
    """Return a live RealtimeListener for `session`, creating/recovering it.

    No-op (returns None) when ENABLE_REALTIME_LISTENER is false. A failure
    to open the tab is logged and swallowed — the daemon continues without
    realtime (degrades to polling). The listener is cached on
    `session.realtime_listener`.
    """
    if not ENABLE_REALTIME_LISTENER:
        return None

    listener = getattr(session, "realtime_listener", None)
    if listener is not None and listener.is_alive:
        return listener

    if listener is not None:
        listener.stop()  # dead tab — clean up before recreating

    listener = RealtimeListener(session, operator=operator)
    try:
        listener.start()
    except Exception as e:
        logger.warning(
            "Realtime listener failed to start — continuing without realtime: %s", e,
        )
        session.realtime_listener = None
        return None

    session.realtime_listener = listener
    return listener


def stop_realtime_listener(session) -> None:
    """Tear down the session's listener if one exists. Idempotent."""
    listener = getattr(session, "realtime_listener", None)
    if listener is not None:
        listener.stop()
    session.realtime_listener = None
