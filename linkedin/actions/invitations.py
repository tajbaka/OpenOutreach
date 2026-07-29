"""Exact-profile UI actions for project-sent LinkedIn invitations."""
from __future__ import annotations

import logging
import time
from enum import Enum

from linkedin.actions.search import _matches_profile_redirect
from linkedin.actions.status import get_connection_status
from linkedin.browser.nav import find_top_card
from linkedin.db.urls import url_to_public_id
from linkedin.enums import ProfileState
from linkedin.exceptions import InvitationWithdrawalError

logger = logging.getLogger(__name__)

PENDING_WITHDRAW_SELECTOR = (
    'button[aria-label*="Pending" i][aria-label*="withdraw invitation" i]:visible, '
    'a[aria-label*="Pending" i][aria-label*="withdraw invitation" i]:visible, '
    '[role="button"][aria-label*="Pending" i][aria-label*="withdraw invitation" i]:visible'
)
VISIBLE_DIALOG_SELECTOR = (
    'div[role="dialog"]:visible, '
    'section[role="dialog"]:visible, '
    'div.artdeco-modal:visible'
)
WITHDRAW_CONFIRM_SELECTOR = (
    'button[aria-label="Withdraw invitation" i]:visible, '
    'button[aria-label="Withdraw" i]:visible, '
    'button:has-text("Withdraw"):visible'
)


class WithdrawalResult(Enum):
    WITHDRAWN = "withdrawn"
    CONNECTED = "connected"
    NOT_PENDING = "not_pending"


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
    """Return an unambiguous Withdraw button within the visible dialog."""
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


def withdraw_pending_invitation(session, profile: dict) -> WithdrawalResult:
    """Withdraw the pending invite from one exact LinkedIn profile page.

    The action trusts neither a list row nor a stale CRM state. It navigates to
    the target profile, reconciles a last-second acceptance first, requires
    LinkedIn's explicit Pending/withdraw surface, confirms in the scoped modal,
    and verifies that the profile is no longer pending after the click.
    """
    expected_public_id = (profile.get("public_identifier") or "").strip()
    if not expected_public_id:
        raise InvitationWithdrawalError("Profile has no public_identifier")

    status = get_connection_status(session, profile)
    if status == ProfileState.CONNECTED:
        return WithdrawalResult.CONNECTED
    if status != ProfileState.PENDING:
        return WithdrawalResult.NOT_PENDING

    current_url = session.page.url
    if not _matches_profile_redirect(current_url, expected_public_id):
        current_public_id = url_to_public_id(current_url) or "unknown"
        raise InvitationWithdrawalError(
            f"Refusing withdrawal: expected {expected_public_id}, visible profile is "
            f"{current_public_id}"
        )

    top_card = find_top_card(session)
    pending = _first_visible(top_card.locator(PENDING_WITHDRAW_SELECTOR))
    if pending is None:
        raise InvitationWithdrawalError(
            f"{expected_public_id} is pending but has no explicit withdrawal surface"
        )

    pending.click()

    dialog = _first_visible_until(session.page, VISIBLE_DIALOG_SELECTOR)
    if dialog is None:
        raise InvitationWithdrawalError(
            f"{expected_public_id} did not show a withdrawal confirmation dialog"
        )
    confirmation = _withdraw_confirmation(dialog)
    if confirmation is None:
        raise InvitationWithdrawalError(
            f"{expected_public_id} withdrawal dialog had no unambiguous Withdraw button"
        )

    confirmation.click()
    session.page.wait_for_timeout(1200)

    final_status = get_connection_status(session, profile)
    if final_status == ProfileState.CONNECTED:
        return WithdrawalResult.CONNECTED
    if final_status == ProfileState.PENDING:
        raise InvitationWithdrawalError(
            f"{expected_public_id} still appears pending after withdrawal confirmation"
        )

    logger.info("Confirmed invitation withdrawal on exact profile %s", expected_public_id)
    return WithdrawalResult.WITHDRAWN
