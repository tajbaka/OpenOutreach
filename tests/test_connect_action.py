from unittest.mock import Mock, patch

import pytest

from linkedin.actions.connect import (
    SELECTORS,
    _click_with_note,
    _click_without_note,
    _custom_invite_vanity_name,
    _direct_invite_option,
    _targets_current_profile,
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


class _Candidate:
    def __init__(self, *, href="", aria_label="", visible=True):
        self.href = href
        self.aria_label = aria_label
        self.visible = visible
        self.clicked = False

    def is_visible(self):
        return self.visible

    def get_attribute(self, name):
        if name == "href":
            return self.href
        if name == "aria-label":
            return self.aria_label
        return None

    def click(self, *args, **kwargs):
        self.clicked = True


class _Candidates:
    def __init__(self, candidates):
        self._candidates = candidates

    def count(self):
        return len(self._candidates)

    def nth(self, idx):
        return self._candidates[idx]


class _TopCard:
    def __init__(self, candidates):
        self.candidates = _Candidates(candidates)

    def locator(self, selector):
        assert selector == SELECTORS["invite_to_connect"]
        return self.candidates


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


def test_invite_selector_supports_current_profile_custom_invite_anchor():
    selector = SELECTORS["invite_to_connect"]

    assert 'a[href*="/preload/custom-invite/"]:visible' in selector


def test_custom_invite_vanity_name_must_match_current_profile():
    tiffany = _Candidate(
        href="/preload/custom-invite/?vanityName=tiffany-hafez-m-a-7100433b"
    )
    recommendation = _Candidate(
        href="/preload/custom-invite/?vanityName=vamsee-krishna-metlapalli"
    )

    assert _custom_invite_vanity_name(tiffany.href) == "tiffany-hafez-m-a-7100433b"
    assert _targets_current_profile(
        tiffany,
        public_identifier="tiffany-hafez-m-a-7100433b",
        full_name="Tiffany Hafez",
    )
    assert not _targets_current_profile(
        recommendation,
        public_identifier="bryanhildebrandt",
        full_name="Bryan Hildebrandt",
    )


def test_direct_invite_option_ignores_recommendation_connects():
    recommendation = _Candidate(
        href="/preload/custom-invite/?vanityName=vamsee-krishna-metlapalli"
    )
    target = _Candidate(
        href="/preload/custom-invite/?vanityName=tiffany-hafez-m-a-7100433b"
    )

    option = _direct_invite_option(
        _TopCard([recommendation, target]),
        public_identifier="tiffany-hafez-m-a-7100433b",
        full_name="Tiffany Hafez",
    )

    assert option is target


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
