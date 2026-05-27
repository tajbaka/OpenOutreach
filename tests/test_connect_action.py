from unittest.mock import Mock, patch

import pytest

from linkedin.actions.connect import (
    SELECTORS,
    _click_with_note,
    _click_without_note,
)
from linkedin.exceptions import SkipProfile


class _Locator:
    def __init__(self, count=0):
        self._count = count
        self.clicked = False

    def count(self):
        return self._count

    @property
    def first(self):
        return self

    def click(self, *args, **kwargs):
        self.clicked = True


def _session_with_email_required_dialog():
    prompt = _Locator(count=1)
    cancel = _Locator(count=1)
    page = Mock()
    page.url = "https://www.linkedin.com/in/jason-example/"

    def locator(selector):
        if selector == SELECTORS["email_required_prompt"]:
            return prompt
        if selector == SELECTORS["invite_cancel"]:
            return cancel
        return _Locator(count=0)

    page.locator.side_effect = locator
    session = Mock(page=page)
    return session, cancel


@patch("linkedin.actions.connect._wait_for_invite_surface", return_value=True)
def test_click_with_note_skips_email_required_invite(_wait):
    session, cancel = _session_with_email_required_dialog()

    with pytest.raises(SkipProfile, match="requires this member's email"):
        _click_with_note(session, "Hi Jason, would love to connect.")

    assert cancel.clicked is True
    session.wait.assert_called()


@patch("linkedin.actions.connect._wait_for_invite_surface", return_value=True)
def test_click_without_note_skips_email_required_invite(_wait):
    session, cancel = _session_with_email_required_dialog()

    with pytest.raises(SkipProfile, match="requires this member's email"):
        _click_without_note(session)

    assert cancel.clicked is True
    session.wait.assert_called()
