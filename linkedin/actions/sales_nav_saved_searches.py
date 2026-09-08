"""Discover named Sales Navigator lead saved searches from the live UI."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlparse

from linkedin.exceptions import AuthenticationError, SalesNavigatorSurfaceError


DEFAULT_BOOTSTRAP_URL = "https://www.linkedin.com/sales/search/people"
SAVED_SEARCH_TRIGGER = "button[data-x--link--saved-searches]"
SAVED_SEARCH_LINK = "a[data-x--saved-search-panel--saved-search-link]"
_VIEW_LABEL_RE = re.compile(r"^View (?P<name>.+) lead saved search$")


@dataclass(frozen=True)
class SavedSalesSearch:
    name: str
    saved_search_id: str
    url: str


def validate_people_search_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "www.linkedin.com":
        raise SalesNavigatorSurfaceError(
            "Sales Navigator bootstrap URL must use https://www.linkedin.com."
        )
    if parsed.path != "/sales/search/people":
        raise SalesNavigatorSurfaceError(
            "Sales Navigator bootstrap URL must be a /sales/search/people page."
        )
    return url


def parse_saved_people_search_links(
    links: list[dict[str, str | None]],
    *,
    name_prefix: str,
    name_suffix: str | None = None,
) -> list[SavedSalesSearch]:
    """Validate and normalize saved-search link data extracted from the panel."""
    searches: list[SavedSalesSearch] = []
    seen_ids: set[str] = set()

    for link in links:
        label = (link.get("label") or "").strip()
        href = (link.get("href") or "").strip()
        match = _VIEW_LABEL_RE.fullmatch(label)
        if not match:
            raise SalesNavigatorSurfaceError(
                f"Unexpected lead saved-search label: {label or '(blank)'!r}."
            )

        name = match.group("name").strip()
        absolute_url = urljoin("https://www.linkedin.com", href)
        parsed = urlparse(absolute_url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "www.linkedin.com"
            or parsed.path != "/sales/search/people"
        ):
            raise SalesNavigatorSurfaceError(
                f"Unexpected lead saved-search destination for {name!r}: {href!r}."
            )

        ids = parse_qs(parsed.query).get("savedSearchId", [])
        if len(ids) != 1 or not ids[0].isdigit():
            raise SalesNavigatorSurfaceError(
                f"Saved search {name!r} has no unambiguous numeric savedSearchId."
            )
        saved_search_id = ids[0]

        if saved_search_id in seen_ids:
            continue
        seen_ids.add(saved_search_id)
        if name_prefix and not name.startswith(name_prefix):
            continue
        if name_suffix and not name.endswith(name_suffix):
            continue
        searches.append(SavedSalesSearch(name, saved_search_id, absolute_url))

    if not searches:
        scopes = []
        if name_prefix:
            scopes.append(f"prefix {name_prefix!r}")
        if name_suffix:
            scopes.append(f"suffix {name_suffix!r}")
        scope = f" matching {' and '.join(scopes)}" if scopes else ""
        raise SalesNavigatorSurfaceError(
            f"The Saved searches panel contained no valid lead searches{scope}."
        )
    return searches


def discover_saved_people_searches(
    session,
    *,
    bootstrap_url: str = DEFAULT_BOOTSTRAP_URL,
    name_prefix: str = "FMKT |",
    name_suffix: str | None = None,
    timeout_ms: int = 20_000,
) -> list[SavedSalesSearch]:
    """Open the live Saved searches panel and return scoped lead searches."""
    validate_people_search_url(bootstrap_url)
    page = session.page
    page.goto(bootstrap_url)
    page.wait_for_load_state("load")

    path = urlparse(page.url).path
    if path.startswith(("/login", "/uas/login", "/checkpoint")):
        raise AuthenticationError(
            "Sales Navigator redirected to authentication while opening saved searches."
        )
    if path != "/sales/search/people":
        raise SalesNavigatorSurfaceError(
            f"Sales Navigator opened an unexpected page: {page.url}"
        )

    trigger = page.locator(SAVED_SEARCH_TRIGGER)
    trigger.wait_for(state="visible", timeout=timeout_ms)
    trigger.click()

    dialog = page.get_by_role("dialog").filter(has_text="Saved searches")
    dialog.wait_for(state="visible", timeout=timeout_ms)
    anchors = dialog.locator(SAVED_SEARCH_LINK)
    anchors.first.wait_for(state="visible", timeout=timeout_ms)

    # The panel currently renders all entries at once. Scroll every nested
    # scrollable region a few bounded rounds as protection if LinkedIn lazily
    # renders longer lists in a future UI revision.
    previous_count = -1
    stable_rounds = 0
    for _round in range(10):
        count = anchors.count()
        stable_rounds = stable_rounds + 1 if count == previous_count else 0
        if stable_rounds >= 2:
            break
        previous_count = count
        dialog.evaluate(
            """element => {
                const nodes = [element, ...element.querySelectorAll('*')];
                for (const node of nodes) {
                    if (node.scrollHeight > node.clientHeight + 1) {
                        node.scrollTop = node.scrollHeight;
                    }
                }
            }"""
        )
        page.wait_for_timeout(250)

    raw_links = anchors.evaluate_all(
        "elements => elements.map(element => ({"
        "label: element.getAttribute('aria-label'), "
        "href: element.getAttribute('href')"
        "}))"
    )
    return parse_saved_people_search_links(
        raw_links,
        name_prefix=name_prefix,
        name_suffix=name_suffix,
    )
