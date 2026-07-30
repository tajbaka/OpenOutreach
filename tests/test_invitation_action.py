from unittest.mock import MagicMock, Mock, patch

import pytest

from linkedin.actions.invitations import (
    CANCEL_DIALOG_SELECTOR,
    SENT_INVITATIONS_URL,
    SENT_WITHDRAW_SELECTOR,
    VISIBLE_DIALOG_SELECTOR,
    WITHDRAW_CONFIRM_SELECTOR,
    SentInvitationMatch,
    SentInvitationTarget,
    WithdrawalResult,
    names_match,
    scan_sent_invitations,
    withdraw_sent_invitation,
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

    def click(self):
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
