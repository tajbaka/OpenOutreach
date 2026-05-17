"""One-off capture spike for the realtime message listener.

Opens the daemon's browser, navigates a tab to /messaging/, attaches a CDP
session, and dumps every `Network.eventSourceMessageReceived` payload to
data/realtime-samples/event-NNN.json as it arrives. Not part of the daemon.

Usage:
    .venv/bin/python -m scripts.capture_realtime_events

While it runs: from a SECOND LinkedIn account, (a) send a DM to this
account, (b) start typing without sending, (c) open/read this account's
reply. Also send one DM FROM this account. Ctrl-C to stop. Then copy the
most representative event-NNN.json files into tests/fixtures/realtime/
with descriptive names (see SHAPE.md / the plan's Task 2 Step 3).
"""
from __future__ import annotations

import json
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

    page = session.context.new_page()
    page.goto(MESSAGING_URL, wait_until="domcontentloaded")
    cdp = session.context.new_cdp_session(page)
    cdp.send("Network.enable")

    def on_event(params):
        counter["n"] += 1
        path = OUT_DIR / f"event-{counter['n']:03d}.json"
        path.write_text(json.dumps(params, indent=2, ensure_ascii=False), encoding="utf-8")
        data_preview = (params.get("data") or "")[:200]
        logger.info("captured %s — data[:200]=%s", path.name, data_preview)

    cdp.on("Network.eventSourceMessageReceived", on_event)
    logger.info("Listening. Send test messages now. Ctrl-C to stop.")

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))
    while not stop["flag"]:
        page.wait_for_timeout(1000)

    logger.info("Captured %d events into %s", counter["n"], OUT_DIR)


if __name__ == "__main__":
    main()
