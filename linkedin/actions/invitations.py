"""Human-paced withdrawal actions on LinkedIn's Sent Invitations page."""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence
from urllib.parse import unquote, urlparse

from playwright.sync_api import Error as PlaywrightError

from linkedin.db.urls import url_to_public_id
from linkedin.exceptions import InvitationWithdrawalError

logger = logging.getLogger(__name__)

SENT_INVITATIONS_URL = (
    "https://www.linkedin.com/mynetwork/invitation-manager/sent/"
)
SENT_INVITATIONS_PATH = "/mynetwork/invitation-manager/sent/"
SENT_INVITATION_CARD_SELECTOR = (
    'div[role="listitem"]:has('
    'a[aria-label*="Withdraw invitation sent to" i])'
)
SENT_PROFILE_LINK_SELECTOR = 'a[href*="/in/"]'
SENT_WITHDRAW_SELECTOR = (
    'a[aria-label*="Withdraw invitation sent to" i]:visible, '
    'button[aria-label*="Withdraw invitation sent to" i]:visible, '
    '[role="button"][aria-label*="Withdraw invitation sent to" i]:visible'
)
VISIBLE_DIALOG_SELECTOR = (
    'dialog[open]:visible, '
    'div[role="dialog"]:visible, '
    'section[role="dialog"]:visible, '
    'div.artdeco-modal:visible'
)
WITHDRAW_CONFIRM_SELECTOR = (
    'button[aria-label="Withdraw invitation" i]:visible, '
    'button[aria-label="Withdraw" i]:visible, '
    '[role="button"][aria-label="Withdraw invitation" i]:visible, '
    '[role="button"][aria-label="Withdraw" i]:visible, '
    'button:has-text("Withdraw"):visible, '
    '[role="button"]:has-text("Withdraw"):visible'
)
CANCEL_DIALOG_SELECTOR = (
    'button[aria-label="Cancel" i]:visible, '
    '[role="button"][aria-label="Cancel" i]:visible, '
    'button:has-text("Cancel"):visible, '
    '[role="button"]:has-text("Cancel"):visible'
)
SCROLL_CONTAINER_SELECTOR = "main"
SCROLL_MIN_PIXELS = 450
SCROLL_MAX_PIXELS = 800
SCROLL_MIN_PAUSE_MS = 650
SCROLL_MAX_PAUSE_MS = 1200
SCROLL_MAX_SECONDS = 2 * 60 * 60
SCROLL_END_STAGNANT_ROUNDS = 5


class WithdrawalResult(Enum):
    WITHDRAWN = "withdrawn"
    NOT_PENDING = "not_pending"


@dataclass(frozen=True)
class SentInvitationTarget:
    public_identifier: str
    expected_name: str


@dataclass(frozen=True)
class SentInvitationMatch:
    public_identifier: str
    displayed_name: str
    sent_label: str


@dataclass(frozen=True)
class SentInvitationScan:
    matches: tuple[SentInvitationMatch, ...]
    cards_seen: int
    scroll_rounds: int
    reached_end: bool
    reached_timeline_depth: bool = False
    oldest_visible_days: int | None = None

    @property
    def by_public_identifier(self) -> dict[str, SentInvitationMatch]:
        return {
            match.public_identifier.casefold(): match
            for match in self.matches
        }


def _first_visible(locator):
    for index in range(locator.count()):
        candidate = locator.nth(index)
        if candidate.is_visible():
            return candidate
    return None


def _first_visible_until(page, selector: str, *, timeout_seconds: float = 4):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        candidate = _first_visible(page.locator(selector))
        if candidate is not None:
            return candidate
        page.wait_for_timeout(200)
    return _first_visible(page.locator(selector))


def _withdraw_confirmation(dialog):
    """Return an unambiguous Withdraw control inside the visible dialog."""
    candidates = dialog.locator(WITHDRAW_CONFIRM_SELECTOR)
    for index in range(candidates.count()):
        candidate = candidates.nth(index)
        if not candidate.is_visible():
            continue
        text = " ".join((candidate.inner_text() or "").split()).casefold()
        aria = (candidate.get_attribute("aria-label") or "").strip().casefold()
        if text == "withdraw" or aria in {"withdraw", "withdraw invitation"}:
            return candidate
    return None


def _dismiss_dialog(page, dialog) -> None:
    cancel = _first_visible(dialog.locator(CANCEL_DIALOG_SELECTOR))
    if cancel is not None:
        try:
            cancel.click(timeout=2_000)
        except PlaywrightError:
            # LinkedIn may remove the dialog between discovery and the click.
            # Cleanup is best-effort; the next exact-card lookup is authoritative.
            page.keyboard.press("Escape")
        return
    page.keyboard.press("Escape")


def _dismiss_visible_dialog(page) -> None:
    """Clear a confirmation dialog left behind by the previous withdrawal."""
    dialog = _first_visible(page.locator(VISIBLE_DIALOG_SELECTOR))
    if dialog is None:
        return
    logger.warning("Dismissing a leftover dialog before the next withdrawal")
    _dismiss_dialog(page, dialog)
    page.wait_for_timeout(500)


def _name_tokens(value: str) -> list[str]:
    return re.findall(r"[^\W_]+", (value or "").casefold(), flags=re.UNICODE)


def names_match(expected: str, displayed: str) -> bool:
    """Require the stored lead's first two name tokens on the exact URL card."""
    expected_tokens = _name_tokens(expected)
    displayed_tokens = _name_tokens(displayed)
    if not expected_tokens or not displayed_tokens:
        return False
    required = expected_tokens[:2]
    return all(token in displayed_tokens for token in required)


def _displayed_name(withdraw_control) -> str:
    aria = (withdraw_control.get_attribute("aria-label") or "").strip()
    prefix = "withdraw invitation sent to "
    if aria.casefold().startswith(prefix):
        return aria[len(prefix):].strip()
    return ""


def _sent_label(card) -> str:
    for line in (card.inner_text() or "").splitlines():
        normalized = " ".join(line.split())
        if normalized.casefold().startswith("sent "):
            return normalized
    return ""


def _sent_label_age_days(label: str) -> int | None:
    """Approximate LinkedIn's visible "Sent N units ago" labels in days."""
    normalized = " ".join((label or "").casefold().split())
    match = re.search(
        r"\bsent\s+(\d+|an?|one)\s+"
        r"(minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)"
        r"\s+ago\b",
        normalized,
    )
    if match is None:
        return None
    raw_amount, unit = match.groups()
    amount = 1 if raw_amount in {"a", "an", "one"} else int(raw_amount)
    if unit.startswith("minute") or unit.startswith("hour"):
        return 0
    if unit.startswith("day"):
        return amount
    if unit.startswith("week"):
        return amount * 7
    if unit.startswith("month"):
        return amount * 30
    if unit.startswith("year"):
        return amount * 365
    return None


def _oldest_visible_sent_age_days(page) -> int | None:
    payloads = _sent_card_payloads(page)
    if not isinstance(payloads, list):
        return None
    ages = [
        age
        for payload in payloads
        if (age := _sent_label_age_days(str(payload.get("sentLabel", "")))) is not None
    ]
    return max(ages) if ages else None


def _sent_card_payloads(page) -> list[dict[str, str]]:
    return page.locator(SENT_INVITATION_CARD_SELECTOR).evaluate_all(
        """
        elements => elements.map(element => {
            const profileLink = element.querySelector('a[href*="/in/"]');
            const withdrawControl = element.querySelector(
                'a[aria-label*="Withdraw invitation sent to" i], ' +
                'button[aria-label*="Withdraw invitation sent to" i], ' +
                '[role="button"][aria-label*="Withdraw invitation sent to" i]'
            );
            let sentLabel = '';
            for (const line of (element.innerText || '').split('\\n')) {
                const normalized = line.trim().replace(/\\s+/g, ' ');
                if (normalized.toLowerCase().startsWith('sent ')) {
                    sentLabel = normalized;
                    break;
                }
            }
            return {
                href: profileLink ? (profileLink.href || '') : '',
                withdrawAria: withdrawControl
                    ? (withdrawControl.getAttribute('aria-label') || '')
                    : '',
                sentLabel,
            };
        })
        """
    )


def _card_public_identifier(card) -> str:
    links = card.locator(SENT_PROFILE_LINK_SELECTOR)
    for index in range(links.count()):
        href = (links.nth(index).get_attribute("href") or "").strip()
        public_identifier = url_to_public_id(href)
        if public_identifier:
            return public_identifier
    return ""


def _card_match(card) -> SentInvitationMatch | None:
    public_identifier = _card_public_identifier(card)
    if not public_identifier:
        return None
    withdraw_control = _first_visible(card.locator(SENT_WITHDRAW_SELECTOR))
    if withdraw_control is None:
        return None
    return SentInvitationMatch(
        public_identifier=public_identifier,
        displayed_name=_displayed_name(withdraw_control),
        sent_label=_sent_label(card),
    )


def _loaded_profile_links(page):
    links = page.locator(SENT_PROFILE_LINK_SELECTOR)
    hrefs = links.evaluate_all(
        "elements => elements.map(element => element.href || '')"
    )
    return links, hrefs


def _find_sent_card(page, public_identifier: str):
    expected = public_identifier.casefold()
    for attempt in range(3):
        try:
            links, hrefs = _loaded_profile_links(page)
            break
        except PlaywrightError as error:
            if "Execution context was destroyed" not in str(error) or attempt == 2:
                raise
            logger.info(
                "Sent Invitations page navigated while locating %s; retrying",
                public_identifier,
            )
            time.sleep(0.5)
    for index, href in enumerate(hrefs):
        if (url_to_public_id(href) or "").casefold() != expected:
            continue
        card = links.nth(index).locator(
            'xpath=ancestor::*[@role="listitem"][1]'
        )
        if card.count() > 0:
            return card.first
    return None


def _reported_invitation_total(page) -> int | None:
    text = page.locator("body").inner_text()
    match = re.search(r"\bPeople\s*\(([\d,]+)\)", text)
    if match is None:
        return None
    return int(match.group(1).replace(",", ""))


def _scroll_state(container) -> tuple[int, int, int]:
    values = container.evaluate(
        "el => [el.scrollTop, el.scrollHeight, el.clientHeight]"
    )
    return int(values[0]), int(values[1]), int(values[2])


def _collect_target_matches(
    page,
    *,
    targets: Mapping[str, SentInvitationTarget],
    matches: dict[str, SentInvitationMatch],
) -> int:
    links, hrefs = _loaded_profile_links(page)
    loaded_targets: dict[str, object] = {}
    for index, href in enumerate(hrefs):
        key = (url_to_public_id(href) or "").casefold()
        if not key or key not in targets or key in matches:
            continue
        card = links.nth(index).locator(
            'xpath=ancestor::*[@role="listitem"][1]'
        )
        if card.count() > 0:
            loaded_targets[key] = card.first

    for key, card in loaded_targets.items():
        match = _card_match(card)
        if match is None:
            continue
        target = targets[key]
        if not names_match(target.expected_name, match.displayed_name):
            raise InvitationWithdrawalError(
                "Refusing Sent-card match for "
                f"{target.public_identifier}: DB name {target.expected_name!r} "
                f"does not match LinkedIn name {match.displayed_name!r}"
            )
        matches[key] = match
        logger.info(
            "Matched pending invitation %s: %s (%s)",
            target.public_identifier,
            match.displayed_name,
            match.sent_label or "date label unavailable",
        )
    return page.locator(SENT_INVITATION_CARD_SELECTOR).count()


def _collect_age_matches(
    page,
    *,
    matches: dict[str, SentInvitationMatch],
    min_age_days: int,
    max_age_days: int | None = None,
    match_limit: int | None = None,
) -> int:
    payloads = _sent_card_payloads(page)
    for payload in payloads:
        public_identifier = url_to_public_id(str(payload.get("href", "")))
        if not public_identifier:
            continue
        withdraw_aria = str(payload.get("withdrawAria", "")).strip()
        if not withdraw_aria:
            continue
        sent_label = str(payload.get("sentLabel", "")).strip()
        displayed_name = ""
        prefix = "withdraw invitation sent to "
        if withdraw_aria.casefold().startswith(prefix):
            displayed_name = withdraw_aria[len(prefix):].strip()

        key = public_identifier.casefold()
        if key in matches:
            continue
        age_days = _sent_label_age_days(sent_label)
        if age_days is None:
            continue
        if age_days < min_age_days:
            continue
        if max_age_days is not None and age_days > max_age_days:
            continue
        matches[key] = SentInvitationMatch(
            public_identifier=public_identifier,
            displayed_name=displayed_name,
            sent_label=sent_label,
        )
        logger.info(
            "Matched date-eligible invitation %s: %s (%s)",
            public_identifier,
            displayed_name or "name unavailable",
            sent_label or "date label unavailable",
        )
        if match_limit is not None and len(matches) >= match_limit:
            break
    return len(payloads)


def scan_sent_invitations(
    session,
    targets: Sequence[SentInvitationTarget],
    *,
    approximate_max_age_days: int | None = None,
) -> SentInvitationScan:
    """Human-scroll the Sent page until every target is found or the list ends."""
    session.ensure_browser()
    page = session.page
    page.goto(SENT_INVITATIONS_URL)
    page.wait_for_load_state("domcontentloaded")
    session.wait()

    path = unquote(urlparse(page.url).path)
    if not path.startswith(SENT_INVITATIONS_PATH):
        raise InvitationWithdrawalError(
            f"Sent Invitations navigation failed: got {page.url}"
        )

    targets_by_id = {
        target.public_identifier.casefold(): target
        for target in targets
    }
    if len(targets_by_id) != len(targets):
        raise InvitationWithdrawalError(
            "The selected batch contains duplicate LinkedIn public identifiers"
        )

    scroll_container = page.locator(SCROLL_CONTAINER_SELECTOR).first
    if scroll_container.count() == 0 or not scroll_container.is_visible():
        raise InvitationWithdrawalError(
            "LinkedIn Sent Invitations page has no visible scroll container"
        )
    scroll_container.hover()

    expected_total = _reported_invitation_total(page)
    matches: dict[str, SentInvitationMatch] = {}
    started = time.monotonic()
    scroll_rounds = 0
    stagnant_at_end = 0
    previous_card_count = -1
    previous_scroll_height = -1
    reached_end = False
    reached_timeline_depth = False
    cards_seen = 0
    oldest_visible_days = None

    while True:
        cards_seen = _collect_target_matches(
            page,
            targets=targets_by_id,
            matches=matches,
        )
        oldest_visible_days = _oldest_visible_sent_age_days(page)
        if len(matches) == len(targets_by_id):
            break

        scroll_top, scroll_height, client_height = _scroll_state(
            scroll_container
        )
        at_end = scroll_top + client_height >= scroll_height - 20
        if (
            approximate_max_age_days is not None
            and oldest_visible_days is not None
            and oldest_visible_days >= approximate_max_age_days
        ):
            reached_timeline_depth = True
            logger.info(
                "Sent Invitations scan reached approximate timeline depth: "
                "oldest_visible=%sd target=%sd cards=%d matches=%d/%d",
                oldest_visible_days,
                approximate_max_age_days,
                cards_seen,
                len(matches),
                len(targets_by_id),
            )
            break
        if expected_total is not None and cards_seen >= expected_total:
            reached_end = True
            break
        if (
            at_end
            and cards_seen == previous_card_count
            and scroll_height == previous_scroll_height
        ):
            stagnant_at_end += 1
        else:
            stagnant_at_end = 0
        if stagnant_at_end >= SCROLL_END_STAGNANT_ROUNDS:
            reached_end = True
            break
        if time.monotonic() - started >= SCROLL_MAX_SECONDS:
            raise InvitationWithdrawalError(
                "Sent Invitations scan exceeded its time limit before "
                "reaching the selected date"
            )

        previous_card_count = cards_seen
        previous_scroll_height = scroll_height
        page.mouse.wheel(
            0,
            random.randint(SCROLL_MIN_PIXELS, SCROLL_MAX_PIXELS),
        )
        page.wait_for_timeout(
            random.randint(SCROLL_MIN_PAUSE_MS, SCROLL_MAX_PAUSE_MS),
        )
        scroll_rounds += 1
        if scroll_rounds % 5 == 0:
            logger.info(
                "Sent Invitations scroll: rounds=%d cards=%d matches=%d/%d "
                "oldest_visible=%s",
                scroll_rounds,
                cards_seen,
                len(matches),
                len(targets_by_id),
                (
                    f"{oldest_visible_days}d"
                    if oldest_visible_days is not None
                    else "unknown"
                ),
            )

    return SentInvitationScan(
        matches=tuple(
            matches[key]
            for key in targets_by_id
            if key in matches
        ),
        cards_seen=cards_seen,
        scroll_rounds=scroll_rounds,
        reached_end=reached_end,
        reached_timeline_depth=reached_timeline_depth,
        oldest_visible_days=oldest_visible_days,
    )


def scan_sent_invitations_by_age(
    session,
    *,
    min_age_days: int,
    max_age_days: int | None = None,
    match_limit: int | None = None,
) -> SentInvitationScan:
    """Human-scroll the Sent page and collect visible cards by sent-age label."""
    if min_age_days < 0:
        raise ValueError("min_age_days must be non-negative")
    if max_age_days is not None and max_age_days < min_age_days:
        raise ValueError("max_age_days must be greater than or equal to min_age_days")
    if match_limit is not None and match_limit <= 0:
        raise ValueError("match_limit must be greater than zero")

    session.ensure_browser()
    page = session.page
    page.goto(SENT_INVITATIONS_URL)
    page.wait_for_load_state("domcontentloaded")
    session.wait()

    path = unquote(urlparse(page.url).path)
    if not path.startswith(SENT_INVITATIONS_PATH):
        raise InvitationWithdrawalError(
            f"Sent Invitations navigation failed: got {page.url}"
        )

    scroll_container = page.locator(SCROLL_CONTAINER_SELECTOR).first
    if scroll_container.count() == 0 or not scroll_container.is_visible():
        raise InvitationWithdrawalError(
            "LinkedIn Sent Invitations page has no visible scroll container"
        )
    scroll_container.hover()

    matches: dict[str, SentInvitationMatch] = {}
    started = time.monotonic()
    scroll_rounds = 0
    stagnant_at_end = 0
    previous_card_count = -1
    previous_scroll_height = -1
    reached_end = False
    reached_timeline_depth = False
    cards_seen = 0
    oldest_visible_days = None

    while True:
        cards_seen = _collect_age_matches(
            page,
            matches=matches,
            min_age_days=min_age_days,
            max_age_days=max_age_days,
            match_limit=match_limit,
        )
        oldest_visible_days = _oldest_visible_sent_age_days(page)
        if match_limit is not None and len(matches) >= match_limit:
            break

        scroll_top, scroll_height, client_height = _scroll_state(
            scroll_container
        )
        at_end = scroll_top + client_height >= scroll_height - 20
        if (
            max_age_days is not None
            and oldest_visible_days is not None
            and oldest_visible_days > max_age_days
        ):
            reached_timeline_depth = True
            logger.info(
                "Sent Invitations scan reached approximate since boundary: "
                "oldest_visible=%sd max_target=%sd cards=%d matches=%d",
                oldest_visible_days,
                max_age_days,
                cards_seen,
                len(matches),
            )
            break
        if (
            at_end
            and cards_seen == previous_card_count
            and scroll_height == previous_scroll_height
        ):
            stagnant_at_end += 1
        else:
            stagnant_at_end = 0
        if stagnant_at_end >= SCROLL_END_STAGNANT_ROUNDS:
            reached_end = True
            break
        if time.monotonic() - started >= SCROLL_MAX_SECONDS:
            raise InvitationWithdrawalError(
                "Sent Invitations scan exceeded its time limit before "
                "finishing the date-based cleanup"
            )

        previous_card_count = cards_seen
        previous_scroll_height = scroll_height
        page.mouse.wheel(
            0,
            random.randint(SCROLL_MIN_PIXELS, SCROLL_MAX_PIXELS),
        )
        page.wait_for_timeout(
            random.randint(SCROLL_MIN_PAUSE_MS, SCROLL_MAX_PAUSE_MS),
        )
        scroll_rounds += 1
        if scroll_rounds % 5 == 0:
            logger.info(
                "Sent Invitations scroll: rounds=%d cards=%d date_matches=%d "
                "oldest_visible=%s",
                scroll_rounds,
                cards_seen,
                len(matches),
                (
                    f"{oldest_visible_days}d"
                    if oldest_visible_days is not None
                    else "unknown"
                ),
            )

    return SentInvitationScan(
        matches=tuple(matches.values()),
        cards_seen=cards_seen,
        scroll_rounds=scroll_rounds,
        reached_end=reached_end,
        reached_timeline_depth=reached_timeline_depth,
        oldest_visible_days=oldest_visible_days,
    )


def withdraw_sent_invitation(
    session,
    target: SentInvitationTarget,
) -> WithdrawalResult:
    """Withdraw one exact URL/name-matched card and verify it disappears."""
    page = session.page
    card = _find_sent_card(page, target.public_identifier)
    if card is None:
        return WithdrawalResult.NOT_PENDING
    match = _card_match(card)
    if match is None:
        raise InvitationWithdrawalError(
            f"{target.public_identifier} card has no explicit Withdraw control"
        )
    if not names_match(target.expected_name, match.displayed_name):
        raise InvitationWithdrawalError(
            f"Refusing withdrawal for {target.public_identifier}: "
            f"DB name {target.expected_name!r} does not match "
            f"LinkedIn name {match.displayed_name!r}"
        )

    withdraw_control = _first_visible(card.locator(SENT_WITHDRAW_SELECTOR))
    if withdraw_control is None:
        return WithdrawalResult.NOT_PENDING
    withdraw_control.click()

    dialog = _first_visible_until(page, VISIBLE_DIALOG_SELECTOR)
    if dialog is None:
        raise InvitationWithdrawalError(
            f"{target.public_identifier} did not show a withdrawal dialog"
        )
    confirmation = _withdraw_confirmation(dialog)
    if confirmation is None:
        _dismiss_dialog(page, dialog)
        raise InvitationWithdrawalError(
            f"{target.public_identifier} dialog had no unambiguous Withdraw control"
        )
    confirmation.click()

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        page.wait_for_timeout(250)
        if _find_sent_card(page, target.public_identifier) is None:
            logger.info(
                "Confirmed Sent-card withdrawal for %s (%s)",
                target.public_identifier,
                match.displayed_name,
            )
            return WithdrawalResult.WITHDRAWN
    raise InvitationWithdrawalError(
        f"{target.public_identifier} Sent card remained after confirmation"
    )


def withdraw_sent_invitation_by_public_identifier(
    session,
    public_identifier: str,
) -> WithdrawalResult:
    """Withdraw one Sent card by URL only and verify it disappears."""
    page = session.page
    _dismiss_visible_dialog(page)
    card = _find_sent_card(page, public_identifier)
    if card is None:
        return WithdrawalResult.NOT_PENDING
    match = _card_match(card)
    if match is None:
        raise InvitationWithdrawalError(
            f"{public_identifier} card has no explicit Withdraw control"
        )

    withdraw_control = _first_visible(card.locator(SENT_WITHDRAW_SELECTOR))
    if withdraw_control is None:
        return WithdrawalResult.NOT_PENDING
    try:
        withdraw_control.click()
    except PlaywrightError as error:
        _dismiss_visible_dialog(page)
        raise InvitationWithdrawalError(
            f"{public_identifier} Withdraw click failed: {error}"
        ) from error

    dialog = _first_visible_until(page, VISIBLE_DIALOG_SELECTOR)
    if dialog is None:
        raise InvitationWithdrawalError(
            f"{public_identifier} did not show a withdrawal dialog"
        )
    confirmation = _withdraw_confirmation(dialog)
    if confirmation is None:
        _dismiss_dialog(page, dialog)
        raise InvitationWithdrawalError(
            f"{public_identifier} dialog had no unambiguous Withdraw control"
        )
    try:
        confirmation.click()
    except PlaywrightError as error:
        _dismiss_visible_dialog(page)
        raise InvitationWithdrawalError(
            f"{public_identifier} confirmation click failed: {error}"
        ) from error

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        page.wait_for_timeout(250)
        if _find_sent_card(page, public_identifier) is None:
            logger.info(
                "Confirmed date-based Sent-card withdrawal for %s (%s)",
                public_identifier,
                match.displayed_name or "name unavailable",
            )
            return WithdrawalResult.WITHDRAWN
    raise InvitationWithdrawalError(
        f"{public_identifier} Sent card remained after confirmation"
    )
