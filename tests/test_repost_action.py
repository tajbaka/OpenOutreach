"""UI logic for the repost worker against a small fake page (no browser)."""
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from playwright.sync_api import Error as PlaywrightError

from linkedin.actions import repost as action
from linkedin.exceptions import (
    RepostCommentaryMismatchError,
    RepostComposerError,
    RepostControlNotFoundError,
    RepostLoginWallError,
    RepostOptionNotFoundError,
    RepostSendError,
    RepostUncertainError,
    RepostUnverifiedError,
)

POST_URL = "https://www.linkedin.com/feed/update/urn:li:activity:91/"
NEW_URL = "https://www.linkedin.com/feed/update/urn:li:activity:555/"


def test_owned_target_skips_a_stale_page_without_touching_other_tabs():
    browser = Mock()
    cdp = browser.new_browser_cdp_session.return_value
    cdp.send.return_value = {"targetId": "owned"}
    stale, unrelated, owned = Mock(), Mock(), Mock()
    for page in (stale, unrelated, owned):
        page.is_closed.return_value = False
    context = Mock(pages=[stale, unrelated, owned])
    browser.contexts = [context]
    unrelated_session, owned_session = Mock(), Mock()
    unrelated_session.send.return_value = {"targetInfo": {"targetId": "other"}}
    owned_session.send.return_value = {"targetInfo": {"targetId": "owned"}}
    context.new_cdp_session.side_effect = [
        PlaywrightError("Protocol error (Target.attachToTarget): No target with given id found"),
        unrelated_session, owned_session,
    ]
    with action.owned_page(browser, POST_URL) as page:
        assert page is owned
    assert [c for c in cdp.send.call_args_list if c.args[0] == 'Target.closeTarget'][0].args == (
        'Target.closeTarget', {'targetId': 'owned'},
    )
    unrelated.close.assert_not_called()
    browser.close.assert_not_called()
    unrelated_session.detach.assert_called_once()
    owned_session.detach.assert_called_once()


def test_closed_page_is_not_attached():
    page, context = Mock(), Mock()
    page.is_closed.return_value = True
    assert action._page_target_id(context, page) is None
    context.new_cdp_session.assert_not_called()


def test_unexpected_target_attach_errors_propagate():
    page, context = Mock(), Mock()
    page.is_closed.return_value = False
    context.new_cdp_session.side_effect = PlaywrightError("Unexpected protocol failure")
    with pytest.raises(PlaywrightError, match='Unexpected protocol failure'):
        action._page_target_id(context, page)


@pytest.mark.parametrize('selector', [action.REPOST_TRIGGER_SELECTOR, action.POST_BUTTON_SELECTOR])
def test_control_ancestor_filter_uses_native_css(selector):
    element = Mock()
    action._context(element, 'aside', selector)
    element.evaluate.assert_called_once_with(
        action._ELEMENT_CONTEXT_JS, ['aside', 'button, [role="button"]'],
    )


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def type(self, text, delay=None):
        self.page.focused.receive(text)

    def press(self, key):
        assert key == "Shift+Enter", "Only soft line breaks may be pressed in the composer"
        self.page.focused.receive("\n")


class FakeElement:
    def __init__(self, page, *, text="", label=None, attrs=None, visible=True, enabled=True,
                 excluded=False, nested=False, urns=(), on_click=None, value=None, mangle=None):
        self.page = page
        self.text = text
        self.attrs = dict(attrs or {})
        if label is not None:
            self.attrs["aria-label"] = label
        self.visible = visible
        self.enabled = enabled
        self.excluded = excluded
        self.nested = nested
        self.urns = list(urns)
        self.on_click = on_click
        self.value = value
        self.mangle = mangle or (lambda text: text)
        self.children = {}
        self.clicks = 0

    # Playwright Locator surface used by the action.
    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def get_attribute(self, name):
        return self.attrs.get(name)

    def inner_text(self):
        return self.text

    def click(self, delay=None):
        self.clicks += 1
        self.page.events.append(("click", self.text or self.attrs.get("aria-label", "")))
        self.page.focused = self
        if self.on_click is not None:
            self.on_click()

    def type(self, text, delay=None, timeout=None):
        self.page.focused = self
        self.receive(text)

    def receive(self, text):
        self.value = (self.value or "") + self.mangle(text)

    def evaluate(self, script, arg=None):
        if script is action._ELEMENT_CONTEXT_JS:
            assert ':has-text(' not in arg[1], 'Native closest cannot parse Playwright selectors'
            assert ':text-is(' not in arg[1], 'Native closest cannot parse Playwright selectors'
            return {"excluded": self.excluded, "nested": self.nested}
        if script is action._CARD_URNS_JS:
            return self.urns
        if script is action._EDITOR_TEXT_JS:
            return self.value or ""
        raise AssertionError("Unexpected element script")

    def locator(self, selector):
        return FakeLocator(self.children.get(selector, []))


class FakeLocator:
    def __init__(self, items):
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakePage:
    def __init__(self, url, events):
        self.url = url
        self.events = events
        self.elements = {}
        self.blocked = False
        self.toasts = []
        self.cards = []
        self.focused = None
        self.keyboard = FakeKeyboard(self)

    def locator(self, selector):
        return FakeLocator(self.elements.get(selector, []))

    def evaluate(self, script, arg=None):
        if script is action._BLOCKED_DOCUMENT_JS:
            return self.blocked
        if script is action._TOASTS_JS:
            return self.toasts
        if script is action._ACTIVITY_CARDS_JS:
            return self.cards
        raise AssertionError("Unexpected page script")

    def wait_for_timeout(self, ms):
        pass

    def reload(self, **kwargs):
        self.events.append(("reload", self.url))


class Scenario:
    """A post page with a Repost menu, and the member's activity page."""

    def __init__(self, *, commentary_supported=True):
        self.events = []
        self.post = FakePage(POST_URL, self.events)
        self.activity = FakePage("https://www.linkedin.com/in/qa-member/recent-activity/all/", self.events)
        self.activity_after_click = []
        self.post_toasts_after_click = [{
            "type": "success", "text": "repost successful. view post", "hrefs": [NEW_URL],
        }]
        self.trigger = FakeElement(self.post, text="Repost", label="Repost", attrs={"aria-haspopup": "true"},
                                   on_click=self.open_menu)
        self.post.elements[action.REPOST_TRIGGER_SELECTOR] = [self.trigger]
        self.plain = FakeElement(self.post, text="Repost Instantly bring Boundera's post to others' feeds",
                                 on_click=self.after_final_click)
        self.thoughts = FakeElement(self.post, text="Repost with your thoughts Create a new post with Boundera's post attached",
                                    on_click=self.open_composer)
        self.dialog = FakeElement(self.post)
        self.editor = FakeElement(self.post, value="")
        self.post_button = FakeElement(self.post, text="Post", on_click=self.after_final_click)
        self.dialog.children[action.EDITOR_SELECTOR] = [self.editor]
        self.dialog.children[action.POST_BUTTON_SELECTOR] = [self.post_button]
        self.opened = []
        self.closed = []

    def open_menu(self):
        self.post.elements[action.MENU_ITEM_SELECTOR] = [self.plain, self.thoughts]

    def open_composer(self):
        self.post.elements[action.DIALOG_SELECTOR] = [self.dialog]

    def after_final_click(self):
        self.post.toasts = self.post_toasts_after_click
        self.activity.cards = self.activity_after_click

    def on_click_attempt(self):
        self.events.append(("click_attempt_recorded", None))

    @contextmanager
    def open_page(self, url):
        page = self.post if url == POST_URL else self.activity
        assert url in (POST_URL, action.ACTIVITY_URL)
        self.opened.append(url)
        try:
            yield page
        finally:
            self.closed.append(url)

    def run(self, commentary=""):
        return action.perform_repost(
            self.open_page, post_url=POST_URL, commentary=commentary, on_click_attempt=self.on_click_attempt,
        )

    def final_click_index(self):
        return next(i for i, event in enumerate(self.events) if event == ("click_attempt_recorded", None))


def test_plain_repost_clicks_plain_option_once_and_returns_toast_permalink():
    s = Scenario()
    assert s.run("") == NEW_URL
    assert s.plain.clicks == 1
    assert s.thoughts.clicks == 0 and s.post_button.clicks == 0
    # The attempt is persisted immediately before the final click.
    recorded = s.final_click_index()
    assert s.events[recorded + 1] == ("click", s.plain.text)
    assert s.opened == [action.ACTIVITY_URL, POST_URL]
    assert s.closed == s.opened


def test_with_thoughts_types_exact_commentary_then_posts_once():
    s = Scenario()
    commentary = "FedRAMP 20x moves fast.\n\nHere is what changed for #CSPs this quarter."
    assert s.run(commentary) == NEW_URL
    assert s.editor.value == commentary
    assert s.post_button.clicks == 1
    assert s.plain.clicks == 0
    recorded = s.final_click_index()
    assert s.events[recorded + 1] == ("click", "Post")


def test_commentary_mismatch_fails_before_any_final_click():
    s = Scenario()
    s.editor.mangle = lambda text: text.replace("20x", "20X")
    with pytest.raises(RepostCommentaryMismatchError) as raised:
        s.run("FedRAMP 20x moves fast.")
    assert not isinstance(raised.value, RepostUncertainError)
    assert s.post_button.clicks == 0
    assert ("click_attempt_recorded", None) not in s.events


def test_non_empty_composer_is_refused_before_typing():
    s = Scenario()
    s.editor.value = "restored draft"
    with pytest.raises(RepostComposerError):
        s.run("Approved commentary.")
    assert s.editor.value == "restored draft"
    assert s.post_button.clicks == 0


def test_browser_error_after_final_click_is_uncertain():
    s = Scenario()

    def broken_click():
        raise PlaywrightError("Target closed")

    s.post_button.on_click = broken_click
    with pytest.raises(RepostUncertainError) as raised:
        s.run("Approved commentary.")
    assert raised.value.error_type == "Error"
    assert ("click_attempt_recorded", None) in s.events


def test_browser_error_before_final_click_is_a_plain_failure():
    s = Scenario()

    def broken_menu():
        raise PlaywrightError("Target closed")

    s.trigger.on_click = broken_menu
    with pytest.raises(RepostSendError) as raised:
        s.run("")
    assert not isinstance(raised.value, RepostUncertainError)
    assert ("click_attempt_recorded", None) not in s.events


def test_failed_attempt_record_prevents_the_final_click():
    s = Scenario()

    def refuse():
        raise action.RepostSendError("row changed", error_type="RepostStateChangedError")

    s.on_click_attempt = refuse
    with pytest.raises(RepostSendError) as raised:
        s.run("")
    assert not isinstance(raised.value, RepostUncertainError)
    assert s.plain.clicks == 0


@pytest.mark.parametrize("wall", ["post", "activity"])
def test_login_wall_fails_without_clicking(wall):
    s = Scenario()
    page = s.post if wall == "post" else s.activity
    page.url = "https://www.linkedin.com/checkpoint/challenge/x" if wall == "post" else "https://www.linkedin.com/login?r=1"
    with pytest.raises(RepostLoginWallError) as raised:
        s.run("")
    assert not isinstance(raised.value, RepostUncertainError)
    assert s.trigger.clicks == 0 and s.plain.clicks == 0
    assert ("click_attempt_recorded", None) not in s.events
    assert s.closed == s.opened
    assert s.opened == ([action.ACTIVITY_URL, POST_URL] if wall == "post" else [action.ACTIVITY_URL])


def test_checkpoint_document_fails_without_clicking():
    s = Scenario()
    s.post.blocked = True
    with pytest.raises(action.RepostCheckpointError):
        s.run("")
    assert s.trigger.clicks == 0


def test_ambiguous_or_count_only_repost_controls_fail_before_clicking():
    s = Scenario()
    count = FakeElement(s.post, text="12 reposts", label="12 reposts of Boundera's post")
    s.post.elements[action.REPOST_TRIGGER_SELECTOR] = [count]
    with pytest.raises(RepostControlNotFoundError):
        s.run("")
    s.post.elements[action.REPOST_TRIGGER_SELECTOR] = [s.trigger, FakeElement(s.post, text="Repost", attrs={"aria-haspopup": "true"})]
    with pytest.raises(RepostControlNotFoundError):
        s.run("")
    assert s.trigger.clicks == 0


def test_comment_repost_controls_are_ignored():
    s = Scenario()
    in_comment = FakeElement(s.post, text="Repost", attrs={"aria-haspopup": "true"}, excluded=True)
    s.post.elements[action.REPOST_TRIGGER_SELECTOR] = [in_comment, s.trigger]
    assert s.run("") == NEW_URL
    assert in_comment.clicks == 0


def test_control_detaching_while_polling_is_skipped_not_fatal():
    s = Scenario()
    detached = FakeElement(s.post, text="Repost", attrs={"aria-haspopup": "true"})

    def gone():
        raise PlaywrightError("Element is not attached to the DOM")

    detached.inner_text = gone
    detached.get_attribute = lambda name: gone()
    s.post.elements[action.REPOST_TRIGGER_SELECTOR] = [detached, s.trigger]
    assert s.run("") == NEW_URL
    assert detached.clicks == 0


def test_control_without_menu_semantics_is_never_clicked():
    s = Scenario()
    s.trigger.attrs.pop("aria-haspopup")
    with pytest.raises(RepostControlNotFoundError):
        s.run("")
    assert s.trigger.clicks == 0


def test_missing_requested_option_fails_before_final_click():
    s = Scenario()
    s.open_menu = lambda: s.post.elements.__setitem__(action.MENU_ITEM_SELECTOR, [s.thoughts])
    s.trigger.on_click = s.open_menu
    with pytest.raises(RepostOptionNotFoundError):
        s.run("")
    assert ("click_attempt_recorded", None) not in s.events


def test_sdui_menu_labels_select_the_exact_requested_repost_kind():
    s = Scenario()
    s.thoughts.text = "Repost with thoughts"
    s.plain.text = "Repost instantly"
    assert s.run("Approved commentary") == NEW_URL
    assert s.thoughts.clicks == 1
    assert s.plain.clicks == 0
    assert s.editor.value == "Approved commentary"


def test_sdui_composer_repost_button_submits_exact_commentary_once():
    s = Scenario()
    s.post.url = 'https://www.linkedin.com/sharing/compose'
    s.post_button.text = 'Repost'
    s.dialog.children[action.SDUI_COMPOSER_SELECTOR] = [FakeElement(s.post)]
    assert action._post_button(s.post, s.dialog) is s.post_button
    # The full flow enters this route only after selecting thoughts.
    s.post.url = POST_URL
    original_open = s.open_composer

    def open_sdui():
        original_open()
        s.post.url = 'https://www.linkedin.com/sharing/compose'

    s.thoughts.on_click = open_sdui
    assert s.run('Approved commentary') == NEW_URL
    assert s.editor.value == 'Approved commentary'
    assert s.post_button.clicks == 1 and s.plain.clicks == 0


@pytest.mark.parametrize('wrong_route', [True, False])
def test_repost_button_without_exact_sdui_composer_is_refused(wrong_route):
    s = Scenario()
    s.post_button.text = 'Repost'
    if wrong_route:
        s.dialog.children[action.SDUI_COMPOSER_SELECTOR] = [FakeElement(s.post)]
    else:
        s.post.url = 'https://www.linkedin.com/sharing/compose'
    with pytest.raises(RepostComposerError):
        action._post_button(s.post, s.dialog)
    assert s.post_button.clicks == 0


def test_toast_link_to_the_original_post_is_not_proof_but_new_activity_is():
    s = Scenario()
    s.post_toasts_after_click = [{"type": "success", "text": "repost successful", "hrefs": [POST_URL]}]
    s.activity.cards = [{"urn": "urn:li:activity:400", "refs": ["urn:li:activity:400", "urn:li:activity:91"]}]
    s.activity_after_click = [
        {"urn": "urn:li:activity:555", "refs": ["urn:li:activity:555", "urn:li:activity:91"]},
        {"urn": "urn:li:activity:400", "refs": ["urn:li:activity:400", "urn:li:activity:91"]},
    ]
    assert s.run("") == NEW_URL


def test_pre_existing_repost_in_activity_never_verifies_a_new_one():
    s = Scenario()
    s.post_toasts_after_click = []
    s.activity.cards = [{"urn": "urn:li:activity:400", "refs": ["urn:li:activity:91"]}]
    s.activity_after_click = list(s.activity.cards)
    with pytest.raises(RepostUnverifiedError):
        s.run("")
    assert ("reload", s.activity.url) in s.events


def test_success_without_any_verifiable_link_is_uncertain_not_sent():
    s = Scenario()
    s.activity.url = "https://www.linkedin.com/in/me/"  # not an activity page: no fallback proof
    s.post_toasts_after_click = [{"type": "success", "text": "repost successful", "hrefs": []}]
    with pytest.raises(RepostUnverifiedError):
        s.run("")
    assert s.plain.clicks == 1


def test_error_toast_after_click_is_uncertain():
    s = Scenario()
    s.post_toasts_after_click = [{"type": "error", "text": "something went wrong", "hrefs": []}]
    with pytest.raises(action.RepostRejectedError) as raised:
        s.run("")
    assert isinstance(raised.value, RepostUncertainError)


def test_multiline_commentary_uses_soft_line_breaks_only():
    s = Scenario()
    commentary = "Line one\nLine two\n\nLine four"
    assert s.run(commentary) == NEW_URL
    assert s.editor.value == commentary


def test_editor_text_normalization_only_covers_non_breaking_and_invisible_space():
    assert action._normalize_text("a\u00a0 b\u200b") == "a  b"
    assert action._normalize_text("Line\r\nTwo") == "Line\nTwo"


def test_toast_permalink_rejects_non_linkedin_hosts():
    assert action._urn_from_href("https://evil.example/feed/update/urn:li:activity:5/") is None
    assert action._urn_from_href("https://www.linkedin.com/feed/update/urn%3Ali%3Ashare%3A7/") == "urn:li:share:7"
