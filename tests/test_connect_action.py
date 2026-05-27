from unittest.mock import Mock, patch

import pytest

from linkedin.actions.connect import (
    ExistingPendingInvite,
    SELECTORS,
    _click_connect_option,
    _click_with_note,
    _click_without_note,
    _custom_invite_vanity_name,
    _direct_invite_option,
    _pending_invite_surface_visible,
    _targets_current_profile,
)
from linkedin.exceptions import SkipProfile


class _Locator:
    def __init__(self, count=0, *, aria_label="", text="", visible=True):
        self._count = count
        self.aria_label = aria_label
        self.text = text
        self.visible = visible
        self.clicked = False

    def count(self):
        return self._count

    def nth(self, idx):
        return self

    @property
    def first(self):
        return self

    def is_visible(self):
        return self.visible

    def get_attribute(self, name):
        if name == "aria-label":
            return self.aria_label
        return None

    def inner_text(self):
        return self.text

    def click(self, *args, **kwargs):
        self.clicked = True


class _Candidate:
    def __init__(self, *, href="", aria_label="", visible=True):
        self.href = href
        self.aria_label = aria_label
        self.visible = visible
        self.clicked = False
        self.js_clicked = False

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

    def evaluate(self, script):
        assert script == "el => el.click()"
        self.js_clicked = True


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


def _session_with_pending_invite_surface():
    pending = _Locator(
        count=1,
        aria_label="Pending, click to withdraw invitation sent to Vincent Lu",
    )
    page = Mock()
    page.url = "https://www.linkedin.com/in/vincent-lu-23974233/"

    def locator(selector):
        if selector == SELECTORS["pending_invite_surface"]:
            return pending
        if selector == SELECTORS["email_required_prompt"]:
            return _Locator(count=0)
        if selector in (SELECTORS["note_textarea"], SELECTORS["add_note"]):
            return _Locator(count=0)
        return _Locator(count=0)

    page.locator.side_effect = locator
    session = Mock(page=page)
    return session


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


def test_custom_invite_connect_option_uses_js_click():
    target = _Candidate(
        href="/preload/custom-invite/?vanityName=cushman"
    )

    _click_connect_option(target)

    assert target.js_clicked is True
    assert target.clicked is False


def test_pending_invite_surface_matches_current_lead_name():
    session = _session_with_pending_invite_surface()

    assert _pending_invite_surface_visible(session, full_name="Vincent Lu")
    assert not _pending_invite_surface_visible(session, full_name="Tiffany Hafez")


@patch("linkedin.actions.connect._wait_for_invite_surface", return_value=True)
def test_click_with_note_raises_existing_pending_invite_from_invite_surface(_wait):
    session = _session_with_pending_invite_surface()

    with pytest.raises(ExistingPendingInvite):
        _click_with_note(session, "Hi Vincent, would love to connect.", full_name="Vincent Lu")


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
