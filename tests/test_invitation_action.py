from unittest.mock import MagicMock, Mock, patch

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from linkedin.actions.invitations import (
    CANCEL_DIALOG_SELECTOR,
    SENT_INVITATIONS_URL,
    SENT_WITHDRAW_SELECTOR,
    VISIBLE_DIALOG_SELECTOR,
    WITHDRAW_CONFIRM_SELECTOR,
    SentInvitationMatch,
    SentInvitationTarget,
    WithdrawalResult,
    _find_sent_card,
    _sent_label_age_days,
    names_match,
    scan_sent_invitations,
    scan_sent_invitations_by_age,
    withdraw_sent_invitation,
    withdraw_sent_invitation_by_public_identifier,
)
from linkedin.exceptions import InvitationWithdrawalError


class _Collection:
    def __init__(self, *items):
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class _Element:
    def __init__(self, *, text="", aria="", visible=True, child=None):
        self.text = text
        self.aria = aria
        self.visible = visible
        self.child = child
        self.clicked = False

    def is_visible(self):
        return self.visible

    def inner_text(self):
        return self.text

    def get_attribute(self, name):
        return self.aria if name == "aria-label" else None

    def click(self, **_kwargs):
        self.clicked = True

    def locator(self, selector):
        if selector == WITHDRAW_CONFIRM_SELECTOR:
            return _Collection(self.child) if self.child else _Collection()
        if selector == CANCEL_DIALOG_SELECTOR:
            return _Collection()
        raise AssertionError(selector)


def _session():
    confirm = _Element(text="Withdraw")
    dialog = _Element(child=confirm)
    withdraw = _Element(aria="Withdraw invitation sent to Alice Smith")
    card = Mock()
    card.locator.return_value = _Collection(withdraw)
    page = Mock()
    page.locator.side_effect = (
        lambda selector: _Collection(dialog)
        if selector == VISIBLE_DIALOG_SELECTOR
        else _Collection()
    )
    session = Mock(page=page)
    return session, card, withdraw, confirm


def _target():
    return SentInvitationTarget(
        public_identifier="alice",
        expected_name="Alice Smith",
    )


def _match(*, displayed_name="Alice Smith"):
    return SentInvitationMatch(
        public_identifier="alice",
        displayed_name=displayed_name,
        sent_label="Sent 8 weeks ago",
    )


def test_names_match_uses_first_two_stored_name_tokens():
    assert names_match("James Tabron, CISSP", "James Tabron, CISSP")
    assert names_match("Matt D.", "Matt D.")
    assert not names_match("Alice Smith", "Alice Jones")


def test_sent_label_age_days_parses_linkedin_relative_labels():
    assert _sent_label_age_days("Sent 9 hours ago") == 0
    assert _sent_label_age_days("Sent 2 days ago") == 2
    assert _sent_label_age_days("Sent 8 weeks ago") == 56
    assert _sent_label_age_days("Sent 2 months ago") == 60


@patch("linkedin.actions.invitations.time.sleep")
@patch("linkedin.actions.invitations._loaded_profile_links")
def test_find_sent_card_retries_navigation_race(loaded_links, sleep):
    card = Mock()
    card.count.return_value = 1
    card.first = card
    link = Mock()
    link.locator.return_value = card
    links = Mock()
    links.nth.return_value = link
    loaded_links.side_effect = [
        PlaywrightError("Execution context was destroyed"),
        (links, ["https://www.linkedin.com/in/alice/"]),
    ]

    assert _find_sent_card(Mock(), "alice") is card
    sleep.assert_called_once_with(0.5)


@patch("linkedin.actions.invitations._card_match")
@patch("linkedin.actions.invitations._find_sent_card")
def test_withdraws_sent_card_by_public_identifier_without_name_check(
    find_card,
    card_match,
):
    session, card, withdraw, confirm = _session()
    find_card.side_effect = [card, None]
    card_match.return_value = _match(displayed_name="Someone Else")

    result = withdraw_sent_invitation_by_public_identifier(session, "alice")

    assert result == WithdrawalResult.WITHDRAWN
    assert withdraw.clicked
    assert confirm.clicked


@patch("linkedin.actions.invitations._card_match")
@patch("linkedin.actions.invitations._find_sent_card")
def test_date_withdrawal_dismisses_leftover_dialog_before_click(
    find_card,
    card_match,
):
    session, card, withdraw, confirm = _session()
    stale_cancel = _Element(text="Cancel")
    stale_dialog = _Element()
    stale_dialog.locator = Mock(return_value=_Collection(stale_cancel))
    active_dialog = _Element(child=confirm)
    session.page.locator.side_effect = [
        _Collection(stale_dialog),
        _Collection(active_dialog),
    ]
    find_card.side_effect = [card, None]
    card_match.return_value = _match()

    result = withdraw_sent_invitation_by_public_identifier(session, "alice")

    assert result == WithdrawalResult.WITHDRAWN
    assert stale_cancel.clicked
    assert withdraw.clicked
    assert confirm.clicked


@patch("linkedin.actions.invitations._card_match")
@patch("linkedin.actions.invitations._find_sent_card")
def test_date_withdrawal_tolerates_detached_leftover_dialog(find_card, card_match):
    session, card, withdraw, confirm = _session()
    stale_cancel = _Element(text="Cancel")
    stale_cancel.click = Mock(side_effect=PlaywrightTimeoutError("detached"))
    stale_dialog = _Element()
    stale_dialog.locator = Mock(return_value=_Collection(stale_cancel))
    active_dialog = _Element(child=confirm)
    session.page.locator.side_effect = [
        _Collection(stale_dialog),
        _Collection(active_dialog),
    ]
    find_card.side_effect = [card, None]
    card_match.return_value = _match()

    result = withdraw_sent_invitation_by_public_identifier(session, "alice")

    assert result == WithdrawalResult.WITHDRAWN
    session.page.keyboard.press.assert_called_once_with("Escape")


@patch("linkedin.actions.invitations._card_match")
@patch("linkedin.actions.invitations._find_sent_card")
def test_date_withdrawal_wraps_browser_click_timeout(find_card, card_match):
    session, card, withdraw, _ = _session()
    session.page.locator.side_effect = lambda _selector: _Collection()
    withdraw.click = Mock(side_effect=PlaywrightTimeoutError("blocked"))
    find_card.return_value = card
    card_match.return_value = _match()

    with pytest.raises(InvitationWithdrawalError, match="Withdraw click failed"):
        withdraw_sent_invitation_by_public_identifier(session, "alice")


@patch("linkedin.actions.invitations._card_match")
@patch("linkedin.actions.invitations._find_sent_card")
def test_withdraws_exact_sent_card_and_verifies_disappearance(find_card, card_match):
    session, card, withdraw, confirm = _session()
    find_card.side_effect = [card, None]
    card_match.return_value = _match()

    result = withdraw_sent_invitation(session, _target())

    assert result == WithdrawalResult.WITHDRAWN
    assert withdraw.clicked
    assert confirm.clicked
    card.locator.assert_called_once_with(SENT_WITHDRAW_SELECTOR)


@patch("linkedin.actions.invitations._find_sent_card", return_value=None)
def test_missing_sent_card_is_not_recorded_as_withdrawal(_find_card):
    session, _, withdraw, confirm = _session()

    result = withdraw_sent_invitation(session, _target())

    assert result == WithdrawalResult.NOT_PENDING
    assert not withdraw.clicked
    assert not confirm.clicked


@patch("linkedin.actions.invitations._card_match")
@patch("linkedin.actions.invitations._find_sent_card")
def test_refuses_name_mismatch_on_exact_profile_card(find_card, card_match):
    session, card, withdraw, confirm = _session()
    find_card.return_value = card
    card_match.return_value = _match(displayed_name="Someone Else")

    with pytest.raises(InvitationWithdrawalError, match="does not match"):
        withdraw_sent_invitation(session, _target())

    assert not withdraw.clicked
    assert not confirm.clicked


@patch("linkedin.actions.invitations._card_match")
@patch("linkedin.actions.invitations._find_sent_card")
def test_requires_unambiguous_confirmation_control(find_card, card_match):
    session, card, withdraw, _ = _session()
    find_card.return_value = card
    card_match.return_value = _match()
    dialog = _Element(child=_Element(text="Withdraw all"))
    session.page.locator.side_effect = (
        lambda selector: _Collection(dialog)
        if selector == VISIBLE_DIALOG_SELECTOR
        else _Collection()
    )

    with pytest.raises(InvitationWithdrawalError, match="unambiguous Withdraw"):
        withdraw_sent_invitation(session, _target())

    assert withdraw.clicked
    session.page.keyboard.press.assert_called_once_with("Escape")


@patch("linkedin.actions.invitations._scroll_state")
@patch("linkedin.actions.invitations._reported_invitation_total", return_value=100)
@patch("linkedin.actions.invitations._collect_target_matches")
def test_scan_uses_wheel_scroll_until_exact_target_is_found(
    collect_matches,
    _reported_total,
    scroll_state,
):
    target = _target()

    def collect(_page, *, targets, matches):
        if collect.calls:
            matches["alice"] = _match()
            return 20
        collect.calls += 1
        return 10

    collect.calls = 0
    collect_matches.side_effect = collect
    scroll_state.return_value = (0, 1000, 500)
    main = MagicMock()
    main.count.return_value = 1
    main.is_visible.return_value = True
    page = MagicMock()
    page.url = SENT_INVITATIONS_URL
    page.locator.return_value.first = main
    session = MagicMock(page=page)

    scan = scan_sent_invitations(session, [target])

    assert scan.matches == (_match(),)
    assert scan.scroll_rounds == 1
    page.mouse.wheel.assert_called_once()
    main.hover.assert_called_once()


@patch("linkedin.actions.invitations._reported_invitation_total", return_value=10)
@patch("linkedin.actions.invitations._collect_target_matches", return_value=10)
def test_scan_reaches_end_without_treating_absent_target_as_pending(
    _collect_matches,
    _reported_total,
):
    main = MagicMock()
    main.count.return_value = 1
    main.is_visible.return_value = True
    page = MagicMock()
    page.url = SENT_INVITATIONS_URL
    page.locator.return_value.first = main
    session = MagicMock(page=page)

    scan = scan_sent_invitations(session, [_target()])

    assert scan.matches == ()
    assert scan.reached_end
    page.mouse.wheel.assert_not_called()


@patch("linkedin.actions.invitations._oldest_visible_sent_age_days", return_value=62)
@patch("linkedin.actions.invitations._scroll_state", return_value=(0, 1000, 500))
@patch("linkedin.actions.invitations._reported_invitation_total", return_value=1000)
@patch("linkedin.actions.invitations._collect_target_matches", return_value=120)
def test_scan_stops_at_approximate_timeline_depth(
    _collect_matches,
    _reported_total,
    _scroll_state,
    _oldest_age,
):
    main = MagicMock()
    main.count.return_value = 1
    main.is_visible.return_value = True
    page = MagicMock()
    page.url = SENT_INVITATIONS_URL
    page.locator.return_value.first = main
    session = MagicMock(page=page)

    scan = scan_sent_invitations(
        session,
        [_target()],
        approximate_max_age_days=60,
    )

    assert scan.matches == ()
    assert scan.reached_timeline_depth
    assert scan.oldest_visible_days == 62
    page.mouse.wheel.assert_not_called()


@patch("linkedin.actions.invitations._oldest_visible_sent_age_days", return_value=62)
@patch("linkedin.actions.invitations._scroll_state", return_value=(0, 1000, 500))
@patch("linkedin.actions.invitations._reported_invitation_total", return_value=1000)
@patch("linkedin.actions.invitations._collect_age_matches")
def test_age_scan_stops_after_withdrawal_limit_matches(
    collect_matches,
    _reported_total,
    _scroll_state,
    _oldest_age,
):
    def collect(_page, *, matches, min_age_days, max_age_days=None, match_limit=None):
        assert min_age_days == 58
        assert max_age_days is None
        assert match_limit == 1
        matches["alice"] = _match()
        return 25

    collect_matches.side_effect = collect
    main = MagicMock()
    main.count.return_value = 1
    main.is_visible.return_value = True
    page = MagicMock()
    page.url = SENT_INVITATIONS_URL
    page.locator.return_value.first = main
    session = MagicMock(page=page)

    scan = scan_sent_invitations_by_age(
        session,
        min_age_days=58,
        match_limit=1,
    )

    assert scan.matches == (_match(),)
    assert scan.scroll_rounds == 0
    page.mouse.wheel.assert_not_called()


@patch("linkedin.actions.invitations._oldest_visible_sent_age_days", return_value=30)
@patch("linkedin.actions.invitations._scroll_state", return_value=(500, 1000, 500))
@patch("linkedin.actions.invitations._reported_invitation_total", return_value=10)
@patch("linkedin.actions.invitations._collect_age_matches", return_value=10)
def test_age_scan_ignores_underreported_total_and_waits_for_stagnant_end(
    _collect_matches,
    _reported_total,
    _scroll_state,
    _oldest_age,
):
    main = MagicMock()
    main.count.return_value = 1
    main.is_visible.return_value = True
    page = MagicMock()
    page.url = SENT_INVITATIONS_URL
    page.locator.return_value.first = main
    session = MagicMock(page=page)

    scan = scan_sent_invitations_by_age(
        session,
        min_age_days=60,
    )

    assert scan.matches == ()
    assert scan.reached_end
    assert scan.scroll_rounds == 5
    assert page.mouse.wheel.call_count == 5
