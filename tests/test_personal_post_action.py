"""UI logic for the personal post worker against a small fake page (no browser)."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError

from linkedin.actions import personal_post as action
from linkedin.exceptions import (
    PostAttachmentError,
    PostCommentaryMismatchError,
    PostComposerError,
    PostControlNotFoundError,
    PostLoginWallError,
    PostRejectedError,
    PostSendError,
    PostUncertainError,
    PostUnverifiedError,
)

NEW_URL = "https://www.linkedin.com/feed/update/urn:li:share:555/"


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def type(self, text, delay=None):
        self.page.focused.receive(text)

    def press(self, key):
        assert key == "Shift+Enter", "Only soft line breaks may be pressed in the composer"
        self.page.focused.receive("\n")


class FakeElement:
    def __init__(self, page, *, text="", label=None, visible=True, enabled=True, excluded=False, on_click=None,
                 value=None, mangle=None, inner=""):
        self.page = page
        self.text = text
        self.attrs = {"aria-label": label} if label is not None else {}
        self.visible = visible
        self.enabled = enabled
        self.excluded = excluded
        self.on_click = on_click
        self.value = value
        self.mangle = mangle or (lambda text: text)
        self.inner = inner
        self.children = {}
        self.clicks = 0

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def get_attribute(self, name):
        return self.attrs.get(name)

    def inner_text(self):
        return self.inner or self.text

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

    def fill(self, text):
        self.value = text

    def input_value(self):
        return self.value or ""

    def evaluate(self, script, arg=None):
        if script is action._ELEMENT_CONTEXT_JS:
            assert ":has-text(" not in arg[1], "Native closest cannot parse Playwright selectors"
            return {"excluded": self.excluded, "nested": False}
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


class FakeChooser:
    def __init__(self, page):
        self.page = page
        self.value = SimpleNamespace(set_files=self.set_files)

    def set_files(self, path):
        self.page.events.append(("set_files", str(path)))
        self.page.chosen.append(Path(path))
        if self.page.on_files is not None:
            self.page.on_files()


class FakePage:
    def __init__(self, url, events):
        self.url = url
        self.events = events
        self.elements = {}
        self.blocked = False
        self.toasts = []
        self.cards = []
        self.focused = None
        self.chosen = []
        self.on_files = None
        self.keyboard = FakeKeyboard(self)

    def locator(self, selector):
        return FakeLocator(self.elements.get(selector, []))

    def evaluate(self, script, arg=None):
        if script is action._BLOCKED_DOCUMENT_JS:
            return self.blocked
        if script is action._TOASTS_JS:
            return self.toasts
        if script is action._ACTIVITY_POSTS_JS:
            return self.cards
        raise AssertionError("Unexpected page script")

    @contextmanager
    def expect_file_chooser(self, timeout=None):
        yield FakeChooser(self)

    def wait_for_timeout(self, ms):
        pass

    def reload(self, **kwargs):
        self.events.append(("reload", self.url))


class Scenario:
    """The member's feed with a composer, and their activity page."""

    def __init__(self, tmp_path):
        self.events = []
        self.feed = FakePage(action.FEED_URL, self.events)
        self.activity = FakePage("https://www.linkedin.com/in/qa-member/recent-activity/all/", self.events)
        self.activity_after_click = []
        self.toasts_after_click = [{"type": "success", "text": "post successful. view post", "hrefs": [NEW_URL]}]
        self.start = FakeElement(self.feed, text="Start a post", on_click=self.open_composer)
        self.feed.elements[action.START_POST_SELECTOR] = [self.start]
        self.dialog = FakeElement(self.feed)
        self.editor = FakeElement(self.feed, value="")
        self.post_button = FakeElement(self.feed, text="Post", on_click=self.after_final_click)
        self.dialog.children[action.EDITOR_SELECTOR] = [self.editor]
        self.dialog.children[action.POST_BUTTON_SELECTOR] = [self.post_button]
        # Image and document controls in the composer.
        self.media_button = FakeElement(self.feed, label="Add media")
        self.dialog.children[action.IMAGE_BUTTON_SELECTOR] = [self.media_button]
        self.document_button = FakeElement(self.feed, label="Add a document", on_click=self.open_document_panel)
        self.dialog.children[action.DOCUMENT_BUTTON_SELECTOR] = [self.document_button]
        self.choose_file = FakeElement(self.feed, text="Choose file")
        self.title_field = FakeElement(self.feed, label="Document title", value="")
        self.next_button = FakeElement(self.feed, text="Next", on_click=self.finish_image_editor)
        self.done_button = FakeElement(self.feed, text="Done", on_click=self.finish_document)
        self.image_preview = FakeElement(self.feed)
        self.file = tmp_path / "file.bin"
        self.file.write_bytes(b"approved")
        self.opened = []
        self.closed = []

    # Page reactions
    def open_composer(self):
        self.feed.elements[action.DIALOG_SELECTOR] = [self.dialog]

    def open_document_panel(self):
        self.feed.elements[action.CHOOSE_FILE_SELECTOR] = [self.choose_file]
        self.feed.on_files = self.show_title_field

    def show_title_field(self):
        self.feed.elements[action.TITLE_INPUT_SELECTOR] = [self.title_field]
        self.feed.elements[action.EDITOR_STEP_SELECTOR] = [self.done_button]

    def show_image_editor(self):
        self.feed.elements[action.EDITOR_STEP_SELECTOR] = [self.next_button]

    def finish_image_editor(self):
        self.feed.elements[action.EDITOR_STEP_SELECTOR] = []
        self.dialog.children[action.IMAGE_PREVIEW_SELECTOR] = [self.image_preview]

    def finish_document(self):
        self.feed.elements[action.EDITOR_STEP_SELECTOR] = []
        self.dialog.inner = f"Your post\n{self.title_field.value}\n10 pages"

    def after_final_click(self):
        self.feed.toasts = self.toasts_after_click
        self.activity.cards = self.activity_after_click

    def on_click_attempt(self):
        self.events.append(("click_attempt_recorded", None))

    @contextmanager
    def open_page(self, url):
        assert url in (action.FEED_URL, action.ACTIVITY_URL)
        page = self.feed if url == action.FEED_URL else self.activity
        self.opened.append(url)
        try:
            yield page
        finally:
            self.closed.append(url)

    def run(self, commentary="", media=None):
        return action.perform_post(self.open_page, commentary=commentary, media=media, on_click_attempt=self.on_click_attempt)

    def image(self):
        self.feed.on_files = self.show_image_editor
        return {"kind": "image", "path": self.file, "title": ""}

    def document(self, title="Quick 411 on VDR Response"):
        return {"kind": "document", "path": self.file, "title": title}

    def final_click_index(self):
        return self.events.index(("click_attempt_recorded", None))


def test_text_post_types_exact_text_then_posts_once(tmp_path):
    s = Scenario(tmp_path)
    text = "FedRAMP 20x moves fast.\n\nHere is what changed this quarter."
    assert s.run(text) == NEW_URL
    assert s.editor.value == text
    assert s.post_button.clicks == 1 and s.start.clicks == 1
    assert s.events[s.final_click_index() + 1] == ("click", "Post")
    assert s.opened == [action.ACTIVITY_URL, action.FEED_URL] and s.closed == s.opened
    assert s.feed.chosen == [], "a text post attaches nothing"


def test_image_post_attaches_the_verified_file_steps_through_the_editor_and_posts(tmp_path):
    s = Scenario(tmp_path)
    assert s.run("With a picture.", s.image()) == NEW_URL
    assert s.feed.chosen == [s.file]
    assert s.next_button.clicks == 1
    assert s.editor.value == "With a picture."
    order = [e for e in s.events if e[0] in ("set_files", "click_attempt_recorded")]
    assert order == [("set_files", str(s.file)), ("click_attempt_recorded", None)], "attached before the final click"


def test_document_post_attaches_the_pdf_types_the_exact_title_and_posts(tmp_path):
    s = Scenario(tmp_path)
    assert s.run("Slides below.", s.document()) == NEW_URL
    assert s.feed.chosen == [s.file]
    assert s.title_field.value == "Quick 411 on VDR Response"
    assert s.done_button.clicks == 1 and s.post_button.clicks == 1


def test_document_control_behind_more_is_found(tmp_path):
    s = Scenario(tmp_path)
    s.dialog.children[action.DOCUMENT_BUTTON_SELECTOR] = []
    more = FakeElement(s.feed, label="More", on_click=lambda: s.feed.elements.__setitem__(action.DOCUMENT_BUTTON_SELECTOR, [s.document_button]))
    s.dialog.children[action.MORE_BUTTON_SELECTOR] = [more]
    assert s.run("Slides below.", s.document()) == NEW_URL
    assert more.clicks == 1 and s.document_button.clicks == 1


def test_a_prefilled_document_title_is_replaced_by_the_exact_title(tmp_path):
    s = Scenario(tmp_path)
    s.title_field.value = "carousel.pdf"
    assert s.run("Slides.", s.document()) == NEW_URL
    assert s.title_field.value == "Quick 411 on VDR Response"


def test_a_mangled_document_title_fails_before_posting(tmp_path):
    s = Scenario(tmp_path)
    s.title_field.mangle = lambda text: text.upper()
    with pytest.raises(PostAttachmentError) as raised:
        s.run("Slides.", s.document())
    assert not isinstance(raised.value, PostUncertainError)
    assert s.post_button.clicks == 0 and ("click_attempt_recorded", None) not in s.events


def test_an_attachment_that_never_appears_fails_before_posting(tmp_path):
    s = Scenario(tmp_path)
    s.next_button.on_click = lambda: None  # the editor never returns a preview
    with pytest.raises(PostAttachmentError):
        s.run("With a picture.", s.image())
    assert s.post_button.clicks == 0


def test_missing_media_control_fails_before_posting(tmp_path):
    s = Scenario(tmp_path)
    s.dialog.children[action.IMAGE_BUTTON_SELECTOR] = []
    with pytest.raises(PostAttachmentError):
        s.run("With a picture.", s.image())
    assert s.post_button.clicks == 0


def test_text_mismatch_fails_before_any_final_click(tmp_path):
    s = Scenario(tmp_path)
    s.editor.mangle = lambda text: text.replace("20x", "20X")
    with pytest.raises(PostCommentaryMismatchError) as raised:
        s.run("FedRAMP 20x moves fast.")
    assert not isinstance(raised.value, PostUncertainError)
    assert s.post_button.clicks == 0


@pytest.mark.parametrize("draft", ["text", "image"])
def test_a_restored_draft_is_refused_and_never_posted(tmp_path, draft):
    s = Scenario(tmp_path)
    if draft == "text":
        s.editor.value = "restored draft"
    else:
        s.dialog.children[action.IMAGE_PREVIEW_SELECTOR] = [s.image_preview]
    with pytest.raises(PostComposerError):
        s.run("Approved text.")
    assert s.post_button.clicks == 0


def test_missing_or_duplicate_start_controls_fail_before_anything(tmp_path):
    s = Scenario(tmp_path)
    s.feed.elements[action.START_POST_SELECTOR] = []
    with pytest.raises(PostControlNotFoundError):
        s.run("Text.")
    s.feed.elements[action.START_POST_SELECTOR] = [s.start, FakeElement(s.feed, text="Start a post")]
    with pytest.raises(PostControlNotFoundError):
        s.run("Text.")
    assert s.start.clicks == 0


def test_login_wall_fails_without_clicking(tmp_path):
    s = Scenario(tmp_path)
    s.feed.url = "https://www.linkedin.com/checkpoint/challenge/x"
    with pytest.raises(PostLoginWallError) as raised:
        s.run("Text.")
    assert not isinstance(raised.value, PostUncertainError)
    assert s.start.clicks == 0 and s.closed == s.opened


def test_checkpoint_document_fails_without_clicking(tmp_path):
    s = Scenario(tmp_path)
    s.feed.blocked = True
    with pytest.raises(action.PostCheckpointError):
        s.run("Text.")
    assert s.start.clicks == 0


def test_browser_error_before_the_final_click_is_a_plain_failure(tmp_path):
    s = Scenario(tmp_path)

    def broken():
        raise PlaywrightError("Target closed")

    s.start.on_click = broken
    with pytest.raises(PostSendError) as raised:
        s.run("Text.")
    assert not isinstance(raised.value, PostUncertainError)


def test_browser_error_after_the_final_click_is_uncertain(tmp_path):
    s = Scenario(tmp_path)

    def broken():
        raise PlaywrightError("Target closed")

    s.post_button.on_click = broken
    with pytest.raises(PostUncertainError) as raised:
        s.run("Text.")
    assert raised.value.error_type == "Error"


def test_failed_attempt_record_prevents_the_final_click(tmp_path):
    s = Scenario(tmp_path)

    def refuse():
        raise PostSendError("row changed", error_type="PostStateChangedError")

    s.on_click_attempt = refuse
    with pytest.raises(PostSendError) as raised:
        s.run("Text.")
    assert not isinstance(raised.value, PostUncertainError)
    assert s.post_button.clicks == 0


def test_error_toast_after_click_is_uncertain(tmp_path):
    s = Scenario(tmp_path)
    s.toasts_after_click = [{"type": "error", "text": "something went wrong", "hrefs": []}]
    with pytest.raises(PostRejectedError) as raised:
        s.run("Text.")
    assert isinstance(raised.value, PostUncertainError)


def test_new_activity_card_with_the_posts_own_text_is_proof_when_no_toast_link(tmp_path):
    s = Scenario(tmp_path)
    s.toasts_after_click = []
    s.activity.cards = [{"urn": "urn:li:activity:400", "text": "An older post"}]
    s.activity_after_click = [
        {"urn": "urn:li:activity:555", "text": "Arian Tavakoli • Now FedRAMP 20x moves fast. Here is what changed."},
        {"urn": "urn:li:activity:556", "text": "Arian liked a post about something else"},
        {"urn": "urn:li:activity:400", "text": "An older post"},
    ]
    assert s.run("FedRAMP 20x moves fast.\nHere is what changed.") == "https://www.linkedin.com/feed/update/urn:li:activity:555/"


def test_no_matching_new_card_is_uncertain_never_sent(tmp_path):
    s = Scenario(tmp_path)
    s.toasts_after_click = []
    s.activity_after_click = [{"urn": "urn:li:activity:556", "text": "Arian liked a post about something else"}]
    with pytest.raises(PostUnverifiedError):
        s.run("FedRAMP 20x moves fast.")
    assert ("reload", s.activity.url) in s.events and s.post_button.clicks == 1


def test_a_document_without_text_is_matched_by_its_title(tmp_path):
    s = Scenario(tmp_path)
    s.toasts_after_click = []
    s.activity_after_click = [{"urn": "urn:li:activity:557", "text": "Quick 411 on VDR Response 10 pages"}]
    assert s.run("", s.document()) == "https://www.linkedin.com/feed/update/urn:li:activity:557/"


def test_toast_links_off_linkedin_are_not_proof():
    assert action._urn_from_href("https://evil.example/feed/update/urn:li:activity:5/") is None


def test_two_document_controls_in_the_composer_are_refused(tmp_path):
    s = Scenario(tmp_path)
    s.dialog.children[action.DOCUMENT_BUTTON_SELECTOR] = [s.document_button, FakeElement(s.feed, label="Add a document")]
    with pytest.raises(PostAttachmentError):
        s.run("Slides.", s.document())
    assert s.document_button.clicks == 0 and s.post_button.clicks == 0
