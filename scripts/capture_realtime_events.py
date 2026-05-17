"""One-off capture spike for the realtime message listener.

LinkedIn's web client opens a long-lived streaming connection to
/realtime/connect. It is NOT a native EventSource, so CDP's
`Network.eventSourceMessageReceived` never fires for it. Instead this
script attaches CDP, watches for the /realtime/connect request, and calls
`Network.streamResourceContent` on it — after which `Network.dataReceived`
events deliver the raw stream chunks. Each chunk is written to
data/realtime-samples/chunk-NNN.txt as it arrives.

Usage:
    .venv/bin/python -m scripts.capture_realtime_events

While it runs: from a SECOND LinkedIn account, (a) send a DM to this
account, (b) start typing without sending, (c) open/read this account's
reply. Also send one DM FROM this account. Ctrl-C to stop. Then inspect
the chunk-NNN.txt files — copy the representative ones into
tests/fixtures/realtime/ (see the plan's Task 2 Step 3).
"""
from __future__ import annotations

import base64
import logging
import os
import signal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")

import django

django.setup()

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("capture")

from linkedin.conf import ROOT_DIR, get_daemon_handle
from linkedin.browser.registry import get_or_create_session

MESSAGING_URL = "https://www.linkedin.com/messaging/"
OUT_DIR = ROOT_DIR / "data" / "realtime-samples"


def main():
    handle = get_daemon_handle()
    if not handle:
        raise SystemExit("No daemon handle — set LINKEDIN_USERNAME in .env")

    session = get_or_create_session(handle=handle)
    session.campaign = session.campaigns.first()
    session.ensure_browser()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    counter = {"n": 0}
    stream_request_ids: set[str] = set()

    page = session.context.new_page()

    cdp = session.context.new_cdp_session(page)
    cdp.send("Network.enable")

    def _write_chunk(raw: bytes, source: str):
        counter["n"] += 1
        path = OUT_DIR / f"chunk-{counter['n']:03d}.txt"
        path.write_bytes(raw)
        preview = raw[:280].decode("utf-8", errors="replace").replace("\n", "\\n")
        logger.info("captured chunk-%03d (%s, %d bytes): %s",
                    counter["n"], source, len(raw), preview)

    def on_request(params):
        url = (params.get("request") or {}).get("url", "")
        # The long-lived realtime stream. Match /realtime/connect only —
        # the other /realtime/* calls are short polls, not the message feed.
        if "/realtime/connect" in url:
            rid = params["requestId"]
            stream_request_ids.add(rid)
            logger.info("realtime stream request %s -> %s", rid, url[:120])

    def on_response(params):
        rid = params.get("requestId")
        if rid not in stream_request_ids:
            return
        resp = params.get("response") or {}
        logger.info("realtime stream response rid=%s type=%s mime=%s",
                    rid, params.get("type"), resp.get("mimeType"))
        try:
            result = cdp.send("Network.streamResourceContent", {"requestId": rid})
            logger.info("streamResourceContent enabled for %s", rid)
            buffered = result.get("bufferedData")
            if buffered:
                _write_chunk(base64.b64decode(buffered), "buffered")
        except Exception as e:
            logger.warning("streamResourceContent failed for %s: %s", rid, e)

    def on_data(params):
        rid = params.get("requestId")
        if rid not in stream_request_ids:
            return
        data_b64 = params.get("data")
        if not data_b64:
            return  # not streamed (only dataLength) — ignore
        try:
            _write_chunk(base64.b64decode(data_b64), "stream")
        except Exception as e:
            logger.warning("could not decode dataReceived chunk: %s", e)

    def on_sse(params):
        # Keep the legacy EventSource handler too, in case some events
        # still arrive that way.
        _write_chunk((params.get("data") or "").encode("utf-8"), "eventsource")

    cdp.on("Network.requestWillBeSent", on_request)
    cdp.on("Network.responseReceived", on_response)
    cdp.on("Network.dataReceived", on_data)
    cdp.on("Network.eventSourceMessageReceived", on_sse)

    page.goto(MESSAGING_URL, wait_until="domcontentloaded")
    logger.info("Messaging page loaded. Listening. Send test messages now. Ctrl-C to stop.")

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))
    ticks = 0
    while not stop["flag"]:
        page.wait_for_timeout(1000)
        ticks += 1
        if ticks % 15 == 0:
            logger.info("…still listening (%d chunks captured, %d realtime streams)",
                        counter["n"], len(stream_request_ids))

    logger.info("Captured %d chunks into %s", counter["n"], OUT_DIR)


if __name__ == "__main__":
    main()
