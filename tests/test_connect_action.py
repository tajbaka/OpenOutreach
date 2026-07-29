from unittest.mock import Mock, patch

import pytest

from linkedin.actions.connect import (
    ExistingPendingInvite,
    SELECTORS,
    _click_connect_option,
    _click_more_button,
    _click_with_note,
    _click_without_note,
    _connect_via_more,
    _custom_invite_vanity_name,
    _direct_invite_option,
    _direct_invite_options,
    _invite_surface_visible,
    _pending_surface_visible,
    _pending_invite_visible_before_missing_alert,
    _pending_invite_surface_visible,
    _profile_public_identifier,
    _resolve_dropdown_clickable,
    _targets_current_profile,
)
from linkedin.enums import ProfileState
from linkedin.exceptions import SkipProfile


class _Locator:
    def __init__(self, count=0, *, aria_label="", text="", href="", visible=True):
        self._count = count
        self.aria_label = aria_label
        self.text = text
        self.href = href
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
        if name == "href":
            return self.href
        return None

    def inner_text(self):
        return self.text

    def click(self, *args, **kwargs):
        self.clicked = True

    def locator(self, selector):
        return _Locator(count=0)


class _Candidate:
    def __init__(self, *, href="", aria_label="", text="", visible=True):
        self.href = href
        self.aria_label = aria_label
        self.text = text
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

    def inner_text(self):
        return self.text

    def evaluate(self, script):
        assert script == "el => el.click()"
        self.js_clicked = True


class _RoleButtonCandidate(_Candidate):
    @property
    def first(self):
        return self

    def count(self):
        return 1

    def element_handle(self, timeout):
        handle = Mock()
        handle.evaluate.return_value = True
        return handle

    def locator(self, selector):
        if selector == 'xpath=ancestor::*[@role="button"][1]':
            return self
        return _Locator(count=0)

    def get_attribute(self, name):
        if name == "role":
            return "button"
        return super().get_attribute(name)


class _ClickBlockedCandidate(_Candidate):
    def click(self, *args, **kwargs):
        raise RuntimeError("interop-outlet intercepts pointer events")


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


class _NoMoreTopCard:
    def locator(self, selector):
        assert selector == SELECTORS["more_button"]
        return _Candidates([])


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


def _session_with_open_pending_menu():
    pending = _Locator(
        count=1,
        aria_label="Pending, click to withdraw invitation sent to Ayodeji Owonibi",
    )
    page = Mock()
    page.url = "https://www.linkedin.com/in/ayo-owonibi-aso5/"

    def locator(selector):
        if selector == SELECTORS["pending_invite_surface"]:
            return pending
        return _Locator(count=0)

    page.locator.side_effect = locator
    session = Mock(page=page)
    return session


def _session_with_pending_menuitem(*, href="", aria_label="", text="Pending"):
    pending = _Locator(count=1, aria_label=aria_label, text=text, href=href)
    page = Mock()

    def locator(selector):
        if selector == SELECTORS["pending_menuitem"]:
            return pending
        return _Locator(count=0)

    page.locator.side_effect = locator
    session = Mock(page=page)
    return session


def _session_with_page_level_send_button_only():
    page = Mock()
    page.url = "https://www.linkedin.com/in/maytheforce/"

    def locator(selector):
        if selector == SELECTORS["send_invitation"]:
            return _Locator(count=1, text="Send")
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


def test_profile_public_identifier_from_profile_url():
    assert (
        _profile_public_identifier("https://www.linkedin.com/in/kristinekonrad/")
        == "kristinekonrad"
    )


def test_pending_menuitem_requires_current_profile_identity():
    generic_pending = _session_with_pending_menuitem(text="Pending")
    matching_href = _session_with_pending_menuitem(
        href="https://www.linkedin.com/in/kristinekonrad/",
        text="Pending",
    )
    matching_name = _session_with_pending_menuitem(
        aria_label="Pending, click to withdraw invitation sent to Kristine Konrad",
    )

    assert not _pending_surface_visible(
        generic_pending,
        public_identifier="kristinekonrad",
        full_name="kristinekonrad",
    )
    assert _pending_surface_visible(
        matching_href,
        public_identifier="kristinekonrad",
        full_name="kristinekonrad",
    )
    assert _pending_surface_visible(
        matching_name,
        public_identifier="kristinekonrad",
        full_name="Kristine Konrad",
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


def test_direct_invite_options_keep_weak_connect_candidates_last():
    weak = _Candidate(text="Connect over call!")
    exact = _Candidate(
        href="/preload/custom-invite/?vanityName=tiffany-hafez-m-a-7100433b"
    )

    options = _direct_invite_options(
        _TopCard([weak, exact]),
        public_identifier="tiffany-hafez-m-a-7100433b",
        full_name="Tiffany Hafez",
    )

    assert options == [exact, weak]


def test_invite_surface_requires_route_modal_or_pending_not_page_level_send():
    session = _session_with_page_level_send_button_only()

    assert not _invite_surface_visible(session)


def test_custom_invite_connect_option_uses_js_click():
    target = _Candidate(
        href="/preload/custom-invite/?vanityName=cushman"
    )

    _click_connect_option(target)

    assert target.js_clicked is True
    assert target.clicked is False


def test_more_button_click_falls_back_to_js_when_overlay_intercepts():
    more = _ClickBlockedCandidate()

    assert _click_more_button(more)
    assert more.js_clicked is True


def test_dropdown_connect_accepts_div_role_button():
    candidate = _RoleButtonCandidate(
        aria_label="Invite Jake Martens to connect",
        text="Connect",
    )

    clickable = _resolve_dropdown_clickable(
        Mock(),
        candidate,
        public_identifier="jake-martens",
        full_name="Jake Martens",
    )

    assert clickable is candidate


def test_pending_invite_surface_matches_current_lead_name():
    session = _session_with_pending_invite_surface()

    assert _pending_invite_surface_visible(session, full_name="Vincent Lu")
    assert not _pending_invite_surface_visible(session, full_name="Tiffany Hafez")
    assert not _pending_invite_surface_visible(session, full_name="vincent-lu-23974233")


def test_missing_alert_guard_detects_open_pending_invite_surface():
    session = _session_with_open_pending_menu()

    assert _pending_invite_visible_before_missing_alert(
        session,
        full_name="Ayodeji Owonibi",
    )


@patch("linkedin.actions.connect._record_connect_issue")
@patch("linkedin.actions.connect.find_top_card")
def test_connect_via_more_returns_pending_before_missing_button_issue_when_more_missing(find_top_card, record_issue):
    session = _session_with_open_pending_menu()
    find_top_card.return_value = _NoMoreTopCard()

    status = _connect_via_more(session, "ayo-owonibi-aso5", "Ayodeji Owonibi")

    assert status == ProfileState.PENDING
    record_issue.assert_not_called()


@patch("linkedin.actions.connect._pending_invite_visible_before_missing_alert", return_value=False)
@patch("linkedin.actions.connect._record_connect_issue")
@patch("linkedin.actions.connect.find_top_card")
def test_connect_via_more_logs_known_target_not_query_string(
    find_top_card,
    record_issue,
    _pending_guard,
):
    session = Mock()
    session.page.url = "https://www.linkedin.com/in/jake-martens/?skipRedirect=true"
    find_top_card.return_value = _NoMoreTopCard()

    status = _connect_via_more(session, "jake-martens", "Jake Martens")

    assert status is False
    record_issue.assert_called_once_with(
        session,
        "jake-martens",
        "connect_button_missing",
        "No More button available for Connect fallback",
        browser_url=session.page.url,
    )


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
