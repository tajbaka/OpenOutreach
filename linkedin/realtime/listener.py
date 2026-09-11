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
import hashlib
import logging
import time
from pathlib import Path
from threading import Event

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from linkedin.conf import LISTENER_CDP_PORT, LISTENER_PUMP_SLICE_SECONDS, ROOT_DIR
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
# LinkedIn's messaging shell can keep loading long enough to miss the default
# navigation timeout. The listener only needs the navigation to start so CDP can
# observe /realtime/connect.
_MESSAGING_NAV_TIMEOUT_MS = 15_000


def run_listener(*, operator: str, username: str, cdp_port: int | None = None,
                 stop_event: Event | None = None) -> int:
    """Return 0 on requested shutdown, or 1 after repeated connection failures.

    Maintains a CDP connection to the daemon's browser; on any drop,
    reconnects after a short delay. Exits 1 only after
    `_MAX_CONSECUTIVE_FAILURES` quick failures in a row.
    """
    cdp_port = LISTENER_CDP_PORT if cdp_port is None else cdp_port
    stop_event = Event() if stop_event is None else stop_event
    failures = 0
    while not stop_event.is_set() and failures < _MAX_CONSECUTIVE_FAILURES:
        started = time.monotonic()
        try:
            _run_one_connection(cdp_port=cdp_port, operator=operator, username=username,
                                stop_event=stop_event)
        except PlaywrightError as e:
            if stop_event.is_set():
                return 0
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
            stop_event.wait(_RECONNECT_DELAY_SECONDS)
    if stop_event.is_set():
        return 0
    logger.error("listener: gave up after %d failed reconnects — exiting", failures)
    return 1


def _tab_record_path(username: str, cdp_port: int) -> Path:
    account = hashlib.sha256(username.strip().lower().encode()).hexdigest()
    return ROOT_DIR / "data" / f"listener-tab-{account}-{cdp_port}.txt"


def _target_id(context, page) -> str:
    cdp = context.new_cdp_session(page)
    try:
        return cdp.send("Target.getTargetInfo")["targetInfo"]["targetId"]
    finally:
        cdp.detach()


def _listener_page(context, record: Path):
    """Reclaim only our recorded CDP target, never a tab selected by URL.

    The record survives listener termination and a lost CDP connection. A new
    browser has different target IDs, so an obsolete record is replaced.
    The command's single-instance guard serializes listener processes.
    """
    try:
        target_id = record.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        target_id = None
    if target_id:
        for page in context.pages:
            if page.is_closed():
                continue
            if _target_id(context, page) == target_id:
                logger.info("listener: reusing owned messaging tab")
                return page

    record.parent.mkdir(parents=True, exist_ok=True)
    page = context.new_page()
    recorded = False
    try:
        target_id = _target_id(context, page)
        temporary = record.with_suffix(".tmp")
        temporary.write_text(target_id, encoding="utf-8")
        temporary.replace(record)
        recorded = True
    finally:
        # If recording ownership fails, do not leave an untracked tab behind.
        if not recorded:
            page.close()
    return page


def _close_listener_page(page, record: Path) -> None:
    try:
        page.close()
    except PlaywrightError:
        # The browser may still be alive after our CDP transport drops. Keep
        # its exact target ID so the next connection can reclaim the tab.
        logger.warning("listener: tab cleanup unavailable; retaining ownership for reconnect")
    else:
        record.unlink(missing_ok=True)


def _run_one_connection(*, cdp_port: int, operator: str, username: str,
                        stop_event: Event) -> None:
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
        record = _tab_record_path(username, cdp_port)
        page = _listener_page(context, record)
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

            _open_messaging_page(page)
            logger.info("listener: connected over CDP, observing %s", _REALTIME_CONNECT_PATH)

            slice_ms = LISTENER_PUMP_SLICE_SECONDS * 1000
            next_heartbeat = time.monotonic() + LISTENER_PUMP_SLICE_SECONDS
            while not stop_event.is_set():
                page.wait_for_timeout(min(slice_ms, 1000))
                if time.monotonic() >= next_heartbeat:
                    write_heartbeat(username)
                    next_heartbeat = time.monotonic() + LISTENER_PUMP_SLICE_SECONDS
        finally:
            _close_listener_page(page, record)


def _open_messaging_page(page) -> None:
    """Start LinkedIn messaging without waiting on the full app shell."""
    page.goto(
        MESSAGING_URL,
        wait_until="commit",
        timeout=_MESSAGING_NAV_TIMEOUT_MS,
    )
