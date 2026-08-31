# linkedin/actions/sales_nav_list.py
"""Scrape a Sales Navigator saved-leads list via the internal Sales Nav API.

Hits the same XHR endpoint the Sales Nav UI uses, paginates through every
result, and yields normalized dicts ready for `create_seed_leads_from_csv`.

Each yielded dict carries the encoded member URN (the leading segment of
`entityUrn`). The caller is expected to resolve that URN to a public_id slug
via `PlaywrightLinkedinAPI.get_profile()` — Voyager's `q=memberIdentity`
endpoint accepts the encoded URN directly and returns `publicIdentifier`.

Endpoint discovery: LinkedIn's Sales Nav decoration IDs and query syntax drift
over time, so rather than hardcoding a guess, `discover_list_url_template()`
opens the saved-list page in the authenticated Playwright session and sniffs
the actual XHR the UI fires. The captured URL becomes the pagination template.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Iterator
from urllib.parse import parse_qs, quote, urlparse, urlunparse

from linkedin.exceptions import SalesNavigatorSurfaceError

logger = logging.getLogger(__name__)

_PEOPLE_SEARCH_ENDPOINTS = {"salesApiLeadSearch", "salesApiPeopleSearch"}

# Encoded member URN sits between `(` and the first `,` of a sales profile URN:
#   urn:li:fs_salesProfile:(ACwAA...,NAME_SEARCH,xxxx)
_MEMBER_URN_RE = re.compile(r"urn:li:fs_salesProfile:\(([^,]+),")

# Best-effort default URL template. The decoration ID number ("-12") drifts
# over time; if pagination starts returning empty/incorrect results, replace
# this with the exact URL from your Arc Network tab and pass it via
# `url_template` argument to `iter_sales_nav_list()`.
DEFAULT_URL_TEMPLATE = (
    "https://www.linkedin.com/sales-api/salesApiPeopleSearch"
    "?q=peopleSearchQuery"
    "&start={start}&count={count}"
    "&decorationId=com.linkedin.sales.deco.desktop.searchv2.DecoratedPeopleSearchHit-12"
    "&query=(spotlightFilter:ALL,pivotParam:(pivotType:LIST,pivotName:{list_id}))"
)

PAGE_SIZE = 25


def _wait_for_captured_response(page, captured: list[str], timeout_ms: int) -> None:
    """Wait only for the target XHR, not Sales Navigator's never-idle page."""
    deadline = time.monotonic() + (timeout_ms / 1000)
    while not captured and time.monotonic() < deadline:
        page.wait_for_timeout(100)


def _parameterize_pagination_url(captured_url: str) -> str:
    """Replace the live paging values while preserving LinkedIn's encoding."""
    parsed = urlparse(captured_url)
    new_parts = []
    found_start = False
    found_count = False
    for part in parsed.query.split("&"):
        if part.startswith("start="):
            new_parts.append("start={start}")
            found_start = True
        elif part.startswith("count="):
            new_parts.append("count={count}")
            found_count = True
        else:
            new_parts.append(part)
    if not found_start or not found_count:
        raise SalesNavigatorSurfaceError(
            "Captured Sales Navigator endpoint has no complete start/count "
            "pagination controls."
        )
    return urlunparse(parsed._replace(query="&".join(new_parts)))


def extract_member_urn(entity_urn: str) -> str | None:
    """Pull the encoded member URN (e.g. `ACwAA...`) out of a sales profile URN."""
    if not entity_urn:
        return None
    m = _MEMBER_URN_RE.search(entity_urn)
    return m.group(1) if m else None


def normalize_element(elem: dict) -> dict | None:
    """Flatten a Sales Nav response element into our seed-import shape.

    Returns None if the element lacks an extractable member URN.
    """
    member_urn = extract_member_urn(elem.get("entityUrn", ""))
    if not member_urn:
        return None

    positions = elem.get("currentPositions") or []
    current = positions[0] if positions else {}

    return {
        "member_urn": member_urn,
        "first_name": (elem.get("firstName") or "").strip(),
        "last_name": (elem.get("lastName") or "").strip(),
        "full_name": (elem.get("fullName") or "").strip(),
        "company_name": (current.get("companyName") or "").strip(),
        "title": (current.get("title") or "").strip(),
        "geo_region": (elem.get("geoRegion") or "").strip(),
        "degree": elem.get("degree"),
    }


def discover_list_url_template(session, list_id: str, *, timeout_ms: int = 20_000) -> str:
    """Open the Sales Nav list page in the authenticated browser and sniff
    the actual XHR URL the UI uses to load the lead list.

    Returns a template string with `{start}` and `{count}` placeholders
    substituted into the captured request's query params.
    """
    page = session.page
    captured: list[str] = []

    def on_response(response):
        url = response.url
        if "salesApi" not in url:
            return
        # Match either the encoded URN form or the raw numeric ID.
        if list_id not in url:
            return
        if response.status != 200:
            return
        try:
            data = response.json()
        except Exception:
            return
        if isinstance(data, dict) and "elements" in data and "paging" in data:
            captured.append(url)

    page.on("response", on_response)
    try:
        page.goto(f"https://www.linkedin.com/sales/lists/people/{list_id}")
        page.wait_for_load_state("load")
        _wait_for_captured_response(page, captured, timeout_ms)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    if not captured:
        raise IOError(
            f"Could not auto-discover Sales Nav list URL for {list_id}. "
            f"The page may have failed to load — check the browser window."
        )

    captured_url = captured[0]
    logger.info("Discovered Sales Nav XHR URL: %s", captured_url)

    # Parameterize {start} and {count} for pagination. Important: split the
    # query string manually rather than via parse_qsl — the latter URL-decodes
    # values, and re-encoding can subtly differ from LinkedIn's original
    # encoding (which the server validates strictly, returning 400).
    return _parameterize_pagination_url(captured_url)


def discover_search_url_template(session, search_url: str, *, timeout_ms: int = 20_000) -> str:
    """Open a Sales Nav People search page and sniff the search XHR.

    Same shape as `discover_list_url_template`, but for search URLs (e.g.
    `linkedin.com/sales/search/people?query=...`) instead of saved-list pages.
    The captured XHR uses `salesApiPeopleSearch` with a `query=(...)` clause
    that bakes in the active filter selections. Saved-search pages emit an
    earlier unfiltered request with the same `savedSearchId`; for those pages,
    only a response carrying a non-empty `query` clause is authoritative.
    """
    page = session.page
    captured: list[str] = []
    expected_saved_search_ids = parse_qs(urlparse(search_url).query).get(
        "savedSearchId", []
    )
    expected_saved_search_id = (
        expected_saved_search_ids[0]
        if len(expected_saved_search_ids) == 1
        else None
    )

    def on_response(response):
        url = response.url
        parsed_response_url = urlparse(url)
        endpoint = parsed_response_url.path.rsplit("/", 1)[-1]
        if endpoint not in _PEOPLE_SEARCH_ENDPOINTS:
            return
        response_params = parse_qs(
            parsed_response_url.query,
            keep_blank_values=True,
        )
        if expected_saved_search_id:
            response_ids = response_params.get("savedSearchId", [])
            if response_ids != [expected_saved_search_id]:
                return
            response_queries = response_params.get("query", [])
            if len(response_queries) != 1 or not response_queries[0].strip():
                return
        if response.status != 200:
            return
        try:
            data = response.json()
        except Exception:
            return
        # The search XHR is distinguishable from sibling salesApi calls (Lego
        # widgets, Identity, etc.) by a structurally valid result collection
        # and paging total.
        elements = data.get("elements") if isinstance(data, dict) else None
        paging = data.get("paging") if isinstance(data, dict) else None
        total = paging.get("total") if isinstance(paging, dict) else None
        if not isinstance(elements, list):
            return
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            return
        captured.append(url)

    page.on("response", on_response)
    try:
        page.goto(search_url)
        page.wait_for_load_state("load")
        _wait_for_captured_response(page, captured, timeout_ms)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    if not captured:
        raise IOError(
            f"Could not auto-discover Sales Nav search XHR. "
            f"The page may have failed to load — check the browser window."
        )

    captured_url = captured[0]
    logger.info("Discovered Sales Nav search XHR URL: %s", captured_url)

    return _parameterize_pagination_url(captured_url)


def iter_sales_nav_list(
    api,
    list_id: str,
    *,
    page_size: int = PAGE_SIZE,
    max_results: int | None = None,
    url_template: str = DEFAULT_URL_TEMPLATE,
) -> Iterator[dict]:
    """Paginate a Sales Nav saved-leads list and yield normalized dicts.

    Args:
        api: An authenticated `PlaywrightLinkedinAPI` instance.
        list_id: The numeric list ID from the URL
            `linkedin.com/sales/lists/people/<list_id>`.
        page_size: Results per page (LinkedIn caps near 25–50).
        max_results: Stop after yielding this many records.
        url_template: Format string with `{list_id}`, `{start}`, `{count}`
            placeholders. Override if the default decoration ID is stale.
    """
    if "{start}" not in url_template or "{count}" not in url_template:
        raise SalesNavigatorSurfaceError(
            "Sales Navigator URL template must contain both {start} and {count}."
        )

    encoded_list_urn = quote(f"urn:li:salesList:{list_id}", safe="")
    start = 0
    yielded = 0
    page_signatures: set[tuple[str, ...]] = set()

    # Sales Nav API expects plain JSON; the Voyager-specific normalized
    # accept header on the client default returns a different shape (or
    # an empty body) for these endpoints.
    sales_nav_headers = {"accept": "application/json"}

    while True:
        # If the template has a {list_id} slot we fill it; discovered URLs
        # already bake the list reference into the query string.
        format_args = {"start": start, "count": page_size}
        if "{list_id}" in url_template:
            format_args["list_id"] = encoded_list_urn
        url = url_template.format(**format_args)
        res = api.get(url, headers=sales_nav_headers)
        if not res.ok:
            raise IOError(
                f"Sales Nav list fetch failed: HTTP {res.status} "
                f"(list_id={list_id}, start={start})\n"
                f"Body: {res.text()[:500]}"
            )

        data = res.json()
        if not isinstance(data, dict):
            raise SalesNavigatorSurfaceError(
                f"Sales Navigator returned a non-object page at start={start}."
            )
        elements = data.get("elements")
        paging = data.get("paging")
        if not isinstance(elements, list) or not isinstance(paging, dict):
            raise SalesNavigatorSurfaceError(
                f"Sales Navigator returned a malformed page at start={start}."
            )
        total = paging.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise SalesNavigatorSurfaceError(
                f"Sales Navigator returned an invalid paging total at start={start}."
            )
        if elements and (total == 0 or start >= total or start + len(elements) > total):
            raise SalesNavigatorSurfaceError(
                f"Sales Navigator returned contradictory paging data at start={start}."
            )

        if not elements:
            if start < total:
                raise SalesNavigatorSurfaceError(
                    f"Sales Navigator promised {total} results but returned an "
                    f"empty page at start={start}."
                )
            return

        if any(not isinstance(elem, dict) for elem in elements):
            raise SalesNavigatorSurfaceError(
                f"Sales Navigator returned a malformed result at start={start}."
            )
        signature = tuple(str(elem.get("entityUrn", "")) for elem in elements)
        if signature in page_signatures:
            raise SalesNavigatorSurfaceError(
                f"Sales Navigator repeated a result page at start={start}."
            )
        page_signatures.add(signature)

        for element_index, elem in enumerate(elements):
            row = normalize_element(elem)
            if row is None:
                raise SalesNavigatorSurfaceError(
                    f"Sales Navigator result has no resolvable member URN at "
                    f"start={start}, index={element_index}."
                )
            yield row
            yielded += 1
            if max_results is not None and yielded >= max_results:
                return

        start += len(elements)
        if start >= total:
            return
