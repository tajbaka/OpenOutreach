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
from typing import Iterator
from urllib.parse import quote, urlparse, urlunparse

logger = logging.getLogger(__name__)

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
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass
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
    parsed = urlparse(captured_url)
    new_parts = []
    for part in parsed.query.split("&"):
        if part.startswith("start="):
            new_parts.append("start={start}")
        elif part.startswith("count="):
            new_parts.append("count={count}")
        else:
            new_parts.append(part)
    rebuilt_query = "&".join(new_parts)
    return urlunparse(parsed._replace(query=rebuilt_query))


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
    encoded_list_urn = quote(f"urn:li:salesList:{list_id}", safe="")
    start = 0
    yielded = 0

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
        elements = data.get("elements") or []
        paging = data.get("paging") or {}
        total = paging.get("total", 0)

        if not elements:
            logger.warning(
                "Sales Nav list %s: no elements at start=%d. Response keys=%s, "
                "body preview=%s",
                list_id, start, list(data.keys()), res.text()[:300],
            )
            return

        for elem in elements:
            row = normalize_element(elem)
            if row is None:
                continue
            yield row
            yielded += 1
            if max_results is not None and yielded >= max_results:
                return

        start += len(elements)
        if start >= total:
            return
