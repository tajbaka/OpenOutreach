"""Realtime listener — runs as a child process spawned by the daemon.

Connects to the daemon's already-running Chromium over CDP (the daemon
launches a persistent context with a fixed --remote-debugging-port),
shares that one context (= one device, one cookie jar), opens its own
/messaging/ tab, and streams LinkedIn's realtime feed via CDP
Network.streamResourceContent.

Because this runs in a SEPARATE process from the daemon, it has its own
Playwright/asyncio loop — the sync-API greenlet corruption that killed the
in-process design cannot occur here. This is the topology the capture
spike proved out.

The daemon's supervisor (linkedin/realtime/supervisor.py) spawns and
restarts this process; the entrypoint is `manage.py listen_realtime`.
"""
from __future__ import annotations

import base64
import logging
import time

from playwright.sync_api import sync_playwright

from linkedin.conf import LISTENER_CDP_PORT, LISTENER_PUMP_SLICE_SECONDS
from linkedin.realtime.handler import handle_realtime_event
from linkedin.realtime.heartbeat import write_heartbeat
from linkedin.realtime.sse import RealtimeSSEBuffer

logger = logging.getLogger(__name__)

MESSAGING_URL = "https://www.linkedin.com/messaging/"
_REALTIME_CONNECT_PATH = "/realtime/connect"
_RECONNECT_DELAY_SECONDS = 10
# After this many quick consecutive connect failures the daemon's browser
# is presumed genuinely gone; the process exits non-zero and the daemon's
# supervisor decides whether to respawn.
_MAX_CONSECUTIVE_FAILURES = 30
# A connection that survived at least this long counts as "worked, then
# dropped" — the failure counter resets so a long-lived listener that
# reconnects across daemon browser-relaunches never exhausts the cap.
_HEALTHY_CONNECTION_SECONDS = 60


def run_listener(*, operator: str, username: str, cdp_port: int | None = None) -> int:
    """Listener process main loop. Returns a process exit code (0 never —
    it loops until the cap is hit, then returns 1).

    Maintains a CDP connection to the daemon's browser; on any drop,
    reconnects after a short delay. Exits 1 only after
    `_MAX_CONSECUTIVE_FAILURES` quick failures in a row.
    """
    cdp_port = LISTENER_CDP_PORT if cdp_port is None else cdp_port
    failures = 0
    while failures < _MAX_CONSECUTIVE_FAILURES:
        started = time.monotonic()
        try:
            _run_one_connection(cdp_port=cdp_port, operator=operator, username=username)
        except Exception as e:
            lasted = time.monotonic() - started
            if lasted >= _HEALTHY_CONNECTION_SECONDS:
                failures = 0
                logger.warning("listener: connection dropped after %.0fs — reconnecting", lasted)
            else:
                failures += 1
                logger.warning(
                    "listener: connect attempt failed (%d/%d): %s",
                    failures, _MAX_CONSECUTIVE_FAILURES, e,
                )
            time.sleep(_RECONNECT_DELAY_SECONDS)
    logger.error("listener: gave up after %d failed reconnects — exiting", failures)
    return 1


def _run_one_connection(*, cdp_port: int, operator: str, username: str) -> None:
    """One CDP connection lifecycle: connect, wire the stream, pump until
    the connection drops (at which point a Playwright call raises and the
    exception propagates to `run_listener`'s reconnect loop).
    """
    buffer = RealtimeSSEBuffer()
    stream_request_ids: set[str] = set()

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
        if not browser.contexts:
            raise RuntimeError("no shared browser context available over CDP")
        context = browser.contexts[0]
        page = context.new_page()
        try:
            cdp = context.new_cdp_session(page)
            cdp.send("Network.enable")

            def _dispatch(data_b64: str) -> None:
                try:
                    text = base64.b64decode(data_b64).decode("utf-8", errors="replace")
                except Exception as e:
                    logger.warning("listener: undecodable stream chunk dropped: %s", e)
                    return
                for event in buffer.feed(text):
                    handle_realtime_event(event, operator=operator)

            def _on_request(params: dict) -> None:
                url = (params.get("request") or {}).get("url", "")
                rid = params.get("requestId")
                if rid and _REALTIME_CONNECT_PATH in url:
                    stream_request_ids.add(rid)

            def _on_response(params: dict) -> None:
                rid = params.get("requestId")
                if rid not in stream_request_ids:
                    return
                try:
                    result = cdp.send("Network.streamResourceContent", {"requestId": rid})
                except Exception as e:
                    logger.warning("listener: streamResourceContent failed: %s", e)
                    return
                buffered = result.get("bufferedData")
                if buffered:
                    _dispatch(buffered)

            def _on_data(params: dict) -> None:
                if params.get("requestId") not in stream_request_ids:
                    return
                data_b64 = params.get("data")
                if data_b64:
                    _dispatch(data_b64)

            cdp.on("Network.requestWillBeSent", _on_request)
            cdp.on("Network.responseReceived", _on_response)
            cdp.on("Network.dataReceived", _on_data)

            page.goto(MESSAGING_URL, wait_until="domcontentloaded")
            logger.info("listener: connected over CDP, observing %s", _REALTIME_CONNECT_PATH)

            slice_ms = LISTENER_PUMP_SLICE_SECONDS * 1000
            while True:
                page.wait_for_timeout(slice_ms)
                write_heartbeat(username)
        finally:
            try:
                page.close()
            except Exception:
                pass
