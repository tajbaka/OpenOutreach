# linkedin/actions/connections.py
"""Scrape the My Network → Connections page to detect accepted invitations in bulk."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime

from linkedin.browser.nav import goto_page
from linkedin.db.urls import url_to_public_id

logger = logging.getLogger(__name__)

CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/"

# "Connected on April 14, 2026" / "Connected on Apr 14, 2026"
_CONNECTED_ON_RE = re.compile(r"^\s*Connected on\s+(.+?)\s*$")


@dataclass(frozen=True)
class ConnectionEntry:
    public_id: str
    name: str
    connected_on: date | None


@dataclass(frozen=True)
class ConnectionScrapeResult:
    entries: list[ConnectionEntry]
    rounds: int
    cards_inspected: int
    elapsed_seconds: float
    stop_reason: str
    oldest_connected_on: date | None

    @property
    def complete(self) -> bool:
        return self.stop_reason in {"cutoff", "idle", "no_pending"}


def _parse_connected_on(text: str) -> date | None:
    m = _CONNECTED_ON_RE.match(text or "")
    if not m:
        return None
    raw = m.group(1)
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    logger.debug("Could not parse connected_on date: %r", raw)
    return None


_EXTRACT_VISIBLE_CARDS_JS = """
(dateNodes) => dateNodes.map((dateNode) => {
  let card = dateNode;
  let link = null;
  while (card && card !== document.body) {
    link = card.querySelector('a[href*="/in/"]');
    if (link) break;
    card = card.parentElement;
  }
  if (!link) return null;
  const nameNode = link.querySelector("p");
  return {
    connected_text: (dateNode.textContent || "").trim(),
    href: link.getAttribute("href") || "",
    name: nameNode ? (nameNode.textContent || "").trim() : "",
  };
}).filter(Boolean)
"""


def _extract_visible_cards(session) -> list[ConnectionEntry]:
    """Read one rendered batch with one browser round-trip.

    LinkedIn may retain every loaded card or virtualize the list and replace
    earlier DOM nodes. The caller accumulates public IDs across batches, so
    either rendering strategy is safe.
    """
    rows = session.page.locator(
        "p",
        has_text="Connected on",
    ).evaluate_all(_EXTRACT_VISIBLE_CARDS_JS)
    entries: list[ConnectionEntry] = []
    for row in rows:
        public_id = url_to_public_id(row.get("href") or "")
        if not public_id:
            continue
        entries.append(
            ConnectionEntry(
                public_id=public_id,
                name=(row.get("name") or "").strip(),
                connected_on=_parse_connected_on(row.get("connected_text") or ""),
            ),
        )
    return entries


def _scroll_one_step(session) -> None:
    """One scroll step that actually triggers LinkedIn's lazy-load.

    `window.scrollTo` doesn't reliably scroll LinkedIn's Connections page
    because the cards live inside an internal scrollable container, not the
    document body. Instead:
      1. Move into the list area + use mouse-wheel events (fire real scroll
         events that virtualized lists respond to).
      2. Also scroll the last rendered card into view as a robustness hook —
         that triggers any IntersectionObserver-based lazy-load.
      3. Send PageDown via keyboard as a final fallback.
    """
    page = session.page

    # Anchor the cursor over the cards area so wheel events land on the right
    # scroll container.
    try:
        page.mouse.move(640, 500)
    except Exception:
        pass

    try:
        page.mouse.wheel(0, 3000)
    except Exception:
        pass

    date_nodes = page.locator("p", has_text="Connected on")
    try:
        if date_nodes.count() > 0:
            date_nodes.last.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass

    try:
        page.keyboard.press("PageDown")
    except Exception:
        pass


def _scan_connections_page(
    session,
    *,
    stop_before: date,
    max_seconds: float,
    max_rounds: int,
    max_idle_rounds: int = 3,
    pause_ms: int = 1500,
) -> ConnectionScrapeResult:
    """Collect rendered batches until the cutoff, idle state, or hard budget.

    Progress is based on newly seen profile IDs and the oldest rendered date,
    not DOM cardinality. That works for both accumulating and virtualized
    lists. ``max_seconds`` and ``max_rounds`` are independent hard stops so a
    maintenance sweep can never monopolize the single browser worker.
    """
    page = session.page
    started = time.monotonic()
    idle = 0
    last_oldest: date | None = None
    rounds = 0
    entries_by_public_id: dict[str, ConnectionEntry] = {}
    stop_reason = "max_rounds"

    while rounds < max_rounds:
        if time.monotonic() - started >= max_seconds:
            stop_reason = "max_seconds"
            break

        rounds += 1
        batch = _extract_visible_cards(session)
        before = len(entries_by_public_id)
        for entry in batch:
            entries_by_public_id[entry.public_id] = entry
        added = len(entries_by_public_id) - before

        dates = [
            entry.connected_on
            for entry in entries_by_public_id.values()
            if entry.connected_on is not None
        ]
        oldest = min(dates) if dates else None
        if oldest is not None and oldest < stop_before:
            stop_reason = "cutoff"
            break

        if added == 0 and oldest == last_oldest:
            idle += 1
        else:
            idle = 0
        last_oldest = oldest
        if idle >= max_idle_rounds:
            stop_reason = "idle" if entries_by_public_id else "empty"
            break

        _scroll_one_step(session)
        remaining_ms = max(
            int((max_seconds - (time.monotonic() - started)) * 1000),
            0,
        )
        if remaining_ms == 0:
            stop_reason = "max_seconds"
            break
        page.wait_for_timeout(min(pause_ms, remaining_ms))

    logger.info(
        "Connections scan stopped: reason=%s rounds=%d unique_cards=%d "
        "oldest=%s cutoff=%s elapsed=%.1fs",
        stop_reason,
        rounds,
        len(entries_by_public_id),
        last_oldest,
        stop_before,
        time.monotonic() - started,
    )
    return ConnectionScrapeResult(
        entries=list(entries_by_public_id.values()),
        rounds=rounds,
        cards_inspected=len(entries_by_public_id),
        elapsed_seconds=time.monotonic() - started,
        stop_reason=stop_reason,
        oldest_connected_on=last_oldest,
    )


def scrape_connections_with_stats(
    session,
    *,
    stop_before: date,
    max_seconds: float,
    max_rounds: int,
) -> ConnectionScrapeResult:
    """Navigate to Connections and return a bounded, instrumented scan."""
    session.ensure_browser()
    page = session.page

    goto_page(
        session,
        action=lambda: page.goto(CONNECTIONS_URL),
        expected_url_pattern="/mynetwork/invite-connect/connections",
        error_message="Failed to load My Network → Connections",
    )
    session.wait()
    result = _scan_connections_page(
        session,
        stop_before=stop_before,
        max_seconds=max_seconds,
        max_rounds=max_rounds,
    )

    logger.info("Scraped %d connections from %s", len(result.entries), CONNECTIONS_URL)
    return result


def scrape_connections(
    session,
    stop_before: date | None = None,
) -> list[ConnectionEntry]:
    """Compatibility wrapper for standalone full-history import commands.

    The daemon sweep uses :func:`scrape_connections_with_stats` with explicit
    budgets. Standalone imports keep their existing list return type but are
    also bounded to avoid an accidental infinite Connections-page crawl.
    """
    cutoff = stop_before or date.min
    return scrape_connections_with_stats(
        session,
        stop_before=cutoff,
        max_seconds=15 * 60,
        max_rounds=600,
    ).entries
