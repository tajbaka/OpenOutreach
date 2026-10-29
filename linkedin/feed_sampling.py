"""Bounded fresh-feed samples and restart-safe, sender-local scheduling.

No historical job claiming, menu clicks, browser recovery, or outreach. The
supervisor owns scheduling; each sample gets one new tab and one attempt.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import ProxyHandler, build_opener
from zoneinfo import ZoneInfo

from linkedin.exceptions import FeedSampleInterruptedError

MIN_SECONDS = 165
MAX_SECONDS = 195
VISIBLE_BATCH_SIZE = 20
READ_PAUSE_MIN_SECONDS = 3
READ_PAUSE_MAX_SECONDS = 6
PROCESS_GRACE_SECONDS = 10


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(payload, stream, sort_keys=True)
    temporary.replace(path)


def claim_receipt(path: Path, payload: dict) -> bool:
    """Create a run's receipt exactly once; any later process for that run gets False."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)
    return True


def sample_directory(root: Path, username: str) -> Path:
    if not username.strip():
        raise ValueError("Feed sampling requires an exact configured LinkedIn account")
    key = hashlib.sha256(username.strip().lower().encode()).hexdigest()[:24]
    return root / "data" / "feed-sampling" / key


def in_working_hours(now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(os.getenv("ACTIVE_TIMEZONE", "America/Toronto")))
    start = int(os.getenv("ACTIVE_START_HOUR", "9"))
    end = int(os.getenv("ACTIVE_END_HOUR", "17"))
    rest = {int(day.strip()) for day in os.getenv("REST_DAYS", "5,6").split(",") if day.strip()}
    if not 0 <= start < end <= 24 or not rest <= set(range(7)):
        raise ValueError("Invalid working hours for feed sampling")
    return local.weekday() not in rest and start <= local.hour < end


class FeedSampleSchedule:
    def __init__(self, root: Path, username: str):
        self.directory = sample_directory(root, username)
        self.path = self.directory / "schedule.json"

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("Invalid feed sample schedule")
        return state

    def due(self, now: datetime) -> bool:
        state = self.read()
        # An interrupted run remains a hold across supervisor restarts.
        if state.get("hold"):
            return False
        if state.get("running"):
            started = datetime.fromisoformat(state["started_at"])
            if (now - started).total_seconds() > MAX_SECONDS + PROCESS_GRACE_SECONDS:
                # A live supervisor never checks during its own sample and kills it
                # by this limit, so a stopped supervisor left this reservation.
                # Hold once, and raise into the supervisor's scheduling alert.
                self.hold("Sample was interrupted when its supervisor stopped; review before resuming.")
                raise FeedSampleInterruptedError("Interrupted feed sample held for review")
            return False
        if not in_working_hours(now):
            return False
        due = state.get("next_due_at")
        return not due or now >= datetime.fromisoformat(due)

    def begin(self, now: datetime) -> str:
        if not self.due(now):
            raise ValueError("Feed sampling is not due or is held")
        run_id = uuid.uuid4().hex
        write_json(self.path, {"running": True, "run_id": run_id, "started_at": now.isoformat()})
        return run_id

    def receipt_path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Invalid feed sample run identity")
        return self.directory / f"{run_id}.json"

    def finish(self, now: datetime, exit_code: int) -> None:
        state = self.read()
        receipt_path = self.receipt_path(state["run_id"])
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        successful = exit_code == 0 and receipt.get("status") == "completed" and not receipt.get("target_id")
        state.update(running=False, finished_at=now.isoformat(), exit_code=exit_code)
        if successful:
            # Choose once after completion; polling and process restarts never redraw it.
            delay = random.randint(55 * 60, 65 * 60)
            state.update(next_due_at=(now + timedelta(seconds=delay)).isoformat(), interval_seconds=delay)
        else:
            state["hold"] = "Collection did not complete cleanly; review before resuming."
        write_json(self.path, state)

    def hold(self, reason: str) -> None:
        state = self.read()
        state.update(hold=reason)
        write_json(self.path, state)


def close_sample_target(port: int, target_id: str) -> bool:
    """Close only the exact target created by this sample, via loopback CDP."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target_id):
        return False
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{int(port)}/json/close/{target_id}", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


class SampleWatchdog:
    """Independent wall-clock guard, including hung page.evaluate/DB calls.

    Used only inside the dedicated sample subprocess. Never exits the supervisor
    or closes a shared context/browser. An emergency exit becomes a review hold.
    """
    def __init__(self, port: int, receipt: Path, started_at: datetime | None = None):
        self.port = port
        self.receipt = receipt
        self.target_id = ""
        self.duration_seconds = random.randint(MIN_SECONDS, MAX_SECONDS)
        # Count from the reservation, which precedes the parent's launch, so
        # process startup cannot push a normal run past the parent's limit.
        elapsed = 0.0
        if started_at is not None:
            elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
            elapsed = min(max(elapsed, 0.0), self.duration_seconds)
        self.deadline = time.monotonic() + self.duration_seconds - elapsed
        # Give a normal time-limit exit room to close its tab and save a receipt.
        # The parent has a separate MAX_SECONDS + PROCESS_GRACE_SECONDS limit.
        cleanup_seconds = PROCESS_GRACE_SECONDS / 2
        self.hard_deadline = self.deadline + cleanup_seconds
        self.timer = threading.Timer(self.duration_seconds - elapsed + cleanup_seconds, self.expire)
        self.timer.daemon = True

    def start(self) -> None:
        self.timer.start()

    def target_created(self, target_id: str) -> None:
        self.target_id = target_id
        write_json(self.receipt, {
            "status": "running", "target_id": target_id, "cdp_port": self.port,
            "duration_seconds": self.duration_seconds,
        })

    def expire(self) -> None:
        if self.target_id:
            close_sample_target(self.port, self.target_id)
        os._exit(124)

    def stop(self) -> None:
        self.timer.cancel()


def assert_feed_available(page) -> None:
    from urllib.parse import urlsplit

    url = urlsplit(page.url)
    if url.scheme != "https" or url.hostname != "www.linkedin.com" or not url.path.startswith("/feed/"):
        raise RuntimeError("Feed redirected away; collection held for review")
    # Inspect the document, but never include its contents in diagnostics.
    blocked = page.evaluate("""() => {
        if (document.querySelector('iframe[src*="captcha"], form[action*="checkpoint"]')) return true;
        // Posts, recommendation modules, news and messaging routinely discuss
        // "unusual activity" or "security verification". Only visible page text
        // outside them (dialogs, interstitials, error pages) can signal a checkpoint.
        const excluded = [
          'div.feed-shared-update-v2', '[data-urn]', '[data-id]', '[role="listitem"]', 'aside',
          'script', 'style', 'noscript', 'template',
        ].join(',');
        const walker = document.createTreeWalker(
          document.body || document.documentElement, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
          {acceptNode: (node) => node.nodeType !== Node.ELEMENT_NODE ? NodeFilter.FILTER_ACCEPT
            : node.matches(excluded) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_SKIP},
        );
        let text = '';
        while (text.length < 12000 && walker.nextNode()) {
          const parent = walker.currentNode.parentElement;
          if (parent?.checkVisibility({visibilityProperty: true, checkVisibilityCSS: true})) {
            text += ' ' + walker.currentNode.data;
          }
        }
        text = text.replace(/\\s+/g, ' ').toLowerCase();
        return ['your account has been restricted', 'your account is temporarily restricted',
                'verify your identity', 'security verification', 'unusual activity',
                'this site can’t be reached', 'err_internet_disconnected'].some(s => text.includes(s));
    }""")
    if blocked:
        raise RuntimeError("Feed checkpoint or unavailable document; collection held for review")


def _scroll_sample_page(page) -> None:
    """Move part of one viewport, preserving overlap between visible-card scans."""
    page.evaluate("""fraction => {
        const workspace = document.querySelector('main#workspace');
        const nested = workspace && workspace.scrollHeight > workspace.clientHeight;
        const scroller = nested ? workspace : window;
        const height = nested ? workspace.clientHeight : window.innerHeight;
        scroller.scrollBy({top: Math.floor(height * fraction), behavior: 'smooth'});
    }""", random.uniform(0.8, 1.0))


def collect_sample(page, *, operator: str, account_username: str, deadline: float) -> dict:
    from linkedin.feed_collection import extract_posts_from_page, upsert_feed_record

    seen = set()
    cards = {}
    saved_urls = set()
    result = {"posts_seen": 0, "posts_created": 0, "scrolls": 0, "stop_reason": "time_limit"}
    while time.monotonic() < deadline:
        assert_feed_available(page)
        if time.monotonic() >= deadline:
            break
        diagnostics = {}
        records = extract_posts_from_page(
            page, allow_menu_lookup=False, visible_only=True, max_nodes=VISIBLE_BATCH_SIZE,
            diagnostics=diagnostics,
        )
        for card in diagnostics.get("cards", []):
            previous = cards.get(card["card_key"])
            if previous is None or previous["status"] != "extracted":
                cards[card["card_key"]] = card
        for record in records:
            if time.monotonic() >= deadline:
                break
            identity = record.activity_urn or record.content_hash
            if identity in seen:
                continue
            seen.add(identity)
            created, _ = upsert_feed_record(record, operator=operator, account_username=account_username)
            result["posts_seen"] += 1
            result["posts_created"] += int(created)
            saved_urls.add(record.post_url)
        # Read before moving, including the first viewport. Never shorten a
        # reading pause to squeeze in another scroll just before the deadline.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        pause = random.uniform(READ_PAUSE_MIN_SECONDS, READ_PAUSE_MAX_SECONDS)
        page.wait_for_timeout(min(pause, remaining) * 1000)
        if time.monotonic() >= deadline:
            break
        assert_feed_available(page)
        # Do not expose another viewport when there is no time to scan it.
        if deadline - time.monotonic() < 2:
            page.wait_for_timeout(max(0, deadline - time.monotonic()) * 1000)
            break
        _scroll_sample_page(page)
        result["scrolls"] += 1
        # Allow native smooth scrolling to settle before scanning the viewport.
        remaining = deadline - time.monotonic()
        if remaining > 0:
            page.wait_for_timeout(min(1, remaining) * 1000)
    if not result["posts_seen"]:
        result["stop_reason"] = "no_resolvable_posts"
    post_cards = [card for card in cards.values() if card["status"] != "non_post_module"]
    failures = {}
    for card in post_cards:
        reason = card["status"] if card["status"] != "extracted" else "not_saved_before_deadline"
        if card["post_url"] not in saved_urls:
            failures[reason] = failures.get(reason, 0) + 1
    result["coverage"] = {
        "post_cards_seen": len(post_cards),
        "post_cards_extracted": sum(card["status"] == "extracted" for card in post_cards),
        "post_cards_saved": sum(card["post_url"] in saved_urls for card in post_cards),
        "non_post_modules": len(cards) - len(post_cards),
        "unsaved_reasons": failures,
    }
    return result


def run_sample(*, operator: str, account_username: str, port: int, watchdog: SampleWatchdog) -> dict:
    from playwright.sync_api import sync_playwright
    from linkedin.feed_collection import _feed_page

    # One transport, one tab, no retries. The watchdog also covers attach/startup.
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15_000)
        if not browser.contexts:
            raise RuntimeError("No shared browser context")
        with _feed_page(browser, on_target=watchdog.target_created) as page:
            page.set_default_timeout(5000)
            page.wait_for_timeout(2000)
            result = collect_sample(
                page, operator=operator, account_username=account_username, deadline=watchdog.deadline,
            )
        # Confirm cleanup independently; a timeout must not leave a lingering tab.
        remaining = browser.new_browser_cdp_session()
        try:
            for _ in range(20):
                targets = remaining.send("Target.getTargets")["targetInfos"]
                if not any(target["targetId"] == watchdog.target_id for target in targets):
                    break
                if time.monotonic() >= watchdog.hard_deadline:
                    raise RuntimeError("Collector tab did not close")
                time.sleep(0.1)
            else:
                raise RuntimeError("Collector tab did not close")
        finally:
            remaining.detach()
    return result
