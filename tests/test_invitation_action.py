from unittest.mock import Mock, patch

import pytest

from linkedin.actions.invitations import (
    PENDING_WITHDRAW_SELECTOR,
    VISIBLE_DIALOG_SELECTOR,
    WITHDRAW_CONFIRM_SELECTOR,
    WithdrawalResult,
    withdraw_pending_invitation,
)
from linkedin.enums import ProfileState
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
        assert selector == WITHDRAW_CONFIRM_SELECTOR
        return _Collection(self.child) if self.child else _Collection()


def _session(*, current_url="https://www.linkedin.com/in/alice/"):
    confirm = _Element(text="Withdraw")
    dialog = _Element(child=confirm)
    pending = _Element(aria="Pending, click to withdraw invitation sent to Alice")
    top_card = Mock()
    top_card.locator.return_value = _Collection(pending)
    page = Mock()
    page.url = current_url
    page.locator.side_effect = (
        lambda selector: _Collection(dialog)
        if selector == VISIBLE_DIALOG_SELECTOR
        else _Collection()
    )
    session = Mock(page=page)
    return session, top_card, pending, confirm


@patch("linkedin.actions.invitations.find_top_card")
@patch("linkedin.actions.invitations.get_connection_status")
def test_withdraws_from_exact_profile_and_verifies_final_state(mock_status, mock_top):
    session, top_card, pending, confirm = _session()
    mock_top.return_value = top_card
    mock_status.side_effect = [ProfileState.PENDING, ProfileState.QUALIFIED]

    result = withdraw_pending_invitation(
        session,
        {"public_identifier": "alice", "url": session.page.url},
    )

    assert result == WithdrawalResult.WITHDRAWN
    assert pending.clicked
    assert confirm.clicked
    top_card.locator.assert_called_once_with(PENDING_WITHDRAW_SELECTOR)


@patch("linkedin.actions.invitations.find_top_card")
@patch("linkedin.actions.invitations.get_connection_status")
def test_refuses_visible_different_profile(mock_status, mock_top):
    session, _, _, _ = _session(
        current_url="https://www.linkedin.com/in/not-alice/",
    )
    mock_status.return_value = ProfileState.PENDING

    with pytest.raises(InvitationWithdrawalError, match="visible profile"):
        withdraw_pending_invitation(
            session,
            {"public_identifier": "alice", "url": "https://www.linkedin.com/in/alice/"},
        )

    mock_top.assert_not_called()


@patch("linkedin.actions.invitations.find_top_card")
@patch("linkedin.actions.invitations.get_connection_status")
def test_requires_explicit_pending_withdraw_surface(mock_status, mock_top):
    session, top_card, _, _ = _session()
    top_card.locator.return_value = _Collection()
    mock_top.return_value = top_card
    mock_status.return_value = ProfileState.PENDING

    with pytest.raises(InvitationWithdrawalError, match="no explicit withdrawal surface"):
        withdraw_pending_invitation(
            session,
            {"public_identifier": "alice", "url": session.page.url},
        )


@patch("linkedin.actions.invitations.find_top_card")
@patch("linkedin.actions.invitations.get_connection_status")
def test_requires_unambiguous_confirmation_button(mock_status, mock_top):
    session, top_card, _, _ = _session()
    mock_top.return_value = top_card
    mock_status.return_value = ProfileState.PENDING
    dialog = _Element(child=_Element(text="Withdraw all"))
    session.page.locator.side_effect = (
        lambda selector: _Collection(dialog)
        if selector == VISIBLE_DIALOG_SELECTOR
        else _Collection()
    )

    with pytest.raises(InvitationWithdrawalError, match="unambiguous Withdraw"):
        withdraw_pending_invitation(
            session,
            {"public_identifier": "alice", "url": session.page.url},
        )


@patch("linkedin.actions.invitations.get_connection_status")
def test_last_second_acceptance_wins(mock_status):
    session, _, pending, confirm = _session()
    mock_status.return_value = ProfileState.CONNECTED

    result = withdraw_pending_invitation(
        session,
        {"public_identifier": "alice", "url": session.page.url},
    )

    assert result == WithdrawalResult.CONNECTED
    assert not pending.clicked
    assert not confirm.clicked


@patch("linkedin.actions.invitations.find_top_card")
@patch("linkedin.actions.invitations.get_connection_status")
def test_pending_after_confirmation_is_not_recorded_as_success(mock_status, mock_top):
    session, top_card, _, _ = _session()
    mock_top.return_value = top_card
    mock_status.side_effect = [ProfileState.PENDING, ProfileState.PENDING]

    with pytest.raises(InvitationWithdrawalError, match="still appears pending"):
        withdraw_pending_invitation(
            session,
            {"public_identifier": "alice", "url": session.page.url},
        )
