from types import SimpleNamespace

from linkedin.actions.message import (
    DIRECT_COMPOSER_INPUT_SELECTOR,
    DIRECT_COMPOSER_SELECTOR,
    DIRECT_COMPOSER_SEND_SELECTOR,
    DIRECT_MEDIA_ATTACHMENT_READY_SELECTOR,
    DIRECT_MEDIA_FILE_INPUT_SELECTOR,
    DIRECT_MEDIA_UPLOAD_BUSY_SELECTOR,
    DIRECT_MEDIA_UPLOAD_ERROR_SELECTOR,
    DirectMessageOutcome,
    MessageSubmissionAborted,
    send_direct_message_once,
)
from linkedin.message_media import LinkedInMediaAsset, LinkedInMediaKind


class _Locator:
    def __init__(
        self,
        *,
        page=None,
        role="generic",
        on_click=None,
        index=0,
    ):
        self._page = page
        self._role = role
        self._on_click = on_click
        self._index = index

    @property
    def first(self):
        return self.nth(0)

    def count(self):
        if self._role == "composers":
            return self._page.composer_count
        if self._role == "file":
            return len(self._page.file_accepts) if self._page.has_file_input else 0
        if self._role == "attachment-ready":
            return int(self._page.attachment_ready)
        if self._role == "upload-busy":
            return int(self._page.upload_busy)
        if self._role == "upload-error":
            return int(self._page.upload_error)
        return 1

    def nth(self, index):
        role = "composer" if self._role == "composers" else self._role
        return _Locator(
            page=self._page,
            role=role,
            on_click=self._on_click,
            index=index,
        )

    def locator(self, selector):
        assert self._role == "composer"
        if selector == DIRECT_COMPOSER_INPUT_SELECTOR:
            role = "editor"
        elif selector == DIRECT_COMPOSER_SEND_SELECTOR:
            role = "send"
        elif selector == DIRECT_MEDIA_FILE_INPUT_SELECTOR:
            role = "file"
        elif selector == DIRECT_MEDIA_ATTACHMENT_READY_SELECTOR:
            role = "attachment-ready"
        elif selector == DIRECT_MEDIA_UPLOAD_BUSY_SELECTOR:
            role = "upload-busy"
        elif selector == DIRECT_MEDIA_UPLOAD_ERROR_SELECTOR:
            role = "upload-error"
        else:
            role = "generic"
        return _Locator(
            page=self._page,
            role=role,
            on_click=self._on_click,
            index=self._index,
        )

    def click(self, **_kwargs):
        if self._on_click is not None:
            self._on_click()

    def input_value(self, **_kwargs):
        if not self._page.selected_filename:
            return ""
        return f"C:\\fakepath\\{self._page.selected_filename}"

    def get_attribute(self, name, **_kwargs):
        if self._role == "file" and name == "accept":
            return self._page.file_accepts[self._index]
        return None

    def is_enabled(self, **_kwargs):
        return self._page.send_enabled

    def is_visible(self, **_kwargs):
        return True

    def set_input_files(self, path):
        self._page.selected_file_input_index = self._index
        self._page.selected_filename = path.replace("\\", "/").rsplit("/", 1)[-1]
        self._page.events.append("attach")


class _Page:
    def __init__(
        self,
        *,
        on_click=None,
        has_file_input=True,
        attachment_ready=True,
        upload_busy=False,
        upload_error=False,
        send_enabled=True,
        composer_count=1,
        file_accept="image/*,video/*",
        events=None,
    ):
        self.on_click = on_click
        self.has_file_input = has_file_input
        self.attachment_ready = attachment_ready
        self.upload_busy = upload_busy
        self.upload_error = upload_error
        self.send_enabled = send_enabled
        self.composer_count = composer_count
        self.file_accepts = (
            [file_accept]
            if isinstance(file_accept, str)
            else list(file_accept)
        )
        self.selected_file_input_index = None
        self.selected_filename = ""
        self.events = events if events is not None else []

    def goto(self, _url):
        return None

    def locator(self, selector):
        if selector == DIRECT_COMPOSER_SELECTOR:
            return _Locator(page=self, role="composers", on_click=self.on_click)
        return _Locator(page=self)

    def wait_for_timeout(self, _milliseconds):
        return None


def _session(*, page=None, on_click=None):
    return SimpleNamespace(
        page=page or _Page(on_click=on_click),
        wait=lambda *_args, **_kwargs: None,
    )


def _media(tmp_path):
    path = tmp_path / "overview.mp4"
    path.write_bytes(b"test-video")
    return LinkedInMediaAsset(
        reference="overview.mp4",
        path=path,
        kind=LinkedInMediaKind.VIDEO,
        mime_type="video/mp4",
        size_bytes=path.stat().st_size,
        sha256="0" * 64,
    )


def _gif_media(tmp_path):
    path = tmp_path / "overview.gif"
    path.write_bytes(b"GIF89a")
    return LinkedInMediaAsset(
        reference="overview.gif",
        path=path,
        kind=LinkedInMediaKind.GIF,
        mime_type="image/gif",
        size_bytes=path.stat().st_size,
        sha256="0" * 64,
    )


def _patch_prepare(monkeypatch, *, confirmed=True):
    monkeypatch.setattr("linkedin.api.messaging.encode_urn", lambda value: value)
    monkeypatch.setattr("linkedin.actions.message.goto_page", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("linkedin.actions.message.human_type", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "linkedin.actions.message._direct_message_submission_confirmed",
        lambda *_args, **_kwargs: confirmed,
    )


def test_single_route_callback_runs_before_click(monkeypatch):
    _patch_prepare(monkeypatch)
    state = {"callback": False, "click": False}
    encoded = []
    monkeypatch.setattr(
        "linkedin.db.leads.resolve_urn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("frozen-URN send must not resolve a mutable Lead"),
        ),
    )
    monkeypatch.setattr(
        "linkedin.api.messaging.encode_urn",
        lambda value: encoded.append(value) or value,
    )

    def callback():
        state["callback"] = True

    def click():
        assert state["callback"] is True
        state["click"] = True

    result = send_direct_message_once(
        _session(on_click=click),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="https://www.linkedin.com/in/alice/",
        on_submit_attempt=callback,
    )

    assert result.outcome == DirectMessageOutcome.SENT
    assert state == {"callback": True, "click": True}
    assert encoded == ["urn:li:fsd_profile:ALICE"]


def test_callback_abort_is_definitely_pre_submit(monkeypatch):
    _patch_prepare(monkeypatch)
    clicked = []

    def abort():
        raise MessageSubmissionAborted("reply arrived")

    result = send_direct_message_once(
        _session(on_click=lambda: clicked.append(True)),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="https://www.linkedin.com/in/alice/",
        on_submit_attempt=abort,
    )

    assert result.outcome == DirectMessageOutcome.PRE_SUBMIT_FAILED
    assert result.detail == "reply arrived"
    assert clicked == []


def test_click_error_is_unclear_and_never_falls_back(monkeypatch):
    _patch_prepare(monkeypatch)

    def click():
        raise TimeoutError("LinkedIn response timed out")

    result = send_direct_message_once(
        _session(on_click=click),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="https://www.linkedin.com/in/alice/",
        on_submit_attempt=lambda: None,
    )

    assert result.outcome == DirectMessageOutcome.UNCLEAR


def test_missing_post_click_confirmation_is_unclear(monkeypatch):
    _patch_prepare(monkeypatch, confirmed=False)

    result = send_direct_message_once(
        _session(),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="https://www.linkedin.com/in/alice/",
        on_submit_attempt=lambda: None,
    )

    assert result.outcome == DirectMessageOutcome.UNCLEAR


def test_invalid_member_urn_fails_before_navigation_or_callback(monkeypatch):
    navigated = []
    callback = []
    monkeypatch.setattr(
        "linkedin.actions.message.goto_page",
        lambda *_args, **_kwargs: navigated.append(True),
    )

    result = send_direct_message_once(
        _session(),
        "https://www.linkedin.com/in/alice/",
        "Hello Alice",
        recipient_label="Alice",
        on_submit_attempt=lambda: callback.append(True),
    )

    assert result.outcome == DirectMessageOutcome.PRE_SUBMIT_FAILED
    assert "exact fsd_profile" in result.detail
    assert navigated == []
    assert callback == []


def test_media_upload_finishes_before_typing_callback_and_only_click(
    monkeypatch,
    tmp_path,
):
    _patch_prepare(monkeypatch)
    events = []
    page = _Page(on_click=lambda: events.append("click"), events=events)
    monkeypatch.setattr(
        "linkedin.actions.message.human_type",
        lambda *_args, **_kwargs: events.append("type"),
    )

    result = send_direct_message_once(
        _session(page=page),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="Alice",
        on_submit_attempt=lambda: events.append("callback"),
        media=_media(tmp_path),
    )

    assert result.outcome == DirectMessageOutcome.SENT
    assert events == ["attach", "type", "callback", "click"]


def test_media_upload_timeout_is_definitely_pre_submit(
    monkeypatch,
    tmp_path,
):
    _patch_prepare(monkeypatch)
    monkeypatch.setattr(
        "linkedin.actions.message.DIRECT_MEDIA_UPLOAD_TIMEOUT_MS",
        500,
    )
    events = []
    page = _Page(
        on_click=lambda: events.append("click"),
        attachment_ready=False,
        send_enabled=False,
        events=events,
    )
    monkeypatch.setattr(
        "linkedin.actions.message.human_type",
        lambda *_args, **_kwargs: events.append("type"),
    )

    result = send_direct_message_once(
        _session(page=page),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="Alice",
        on_submit_attempt=lambda: events.append("callback"),
        media=_media(tmp_path),
    )

    assert result.outcome == DirectMessageOutcome.PRE_SUBMIT_FAILED
    assert result.detail == "LinkedIn did not finish attaching overview.mp4"
    assert events == ["attach"]


def test_media_missing_file_input_is_definitely_pre_submit(monkeypatch, tmp_path):
    _patch_prepare(monkeypatch)
    events = []
    page = _Page(
        on_click=lambda: events.append("click"),
        has_file_input=False,
        events=events,
    )
    monkeypatch.setattr(
        "linkedin.actions.message.human_type",
        lambda *_args, **_kwargs: events.append("type"),
    )

    result = send_direct_message_once(
        _session(page=page),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="Alice",
        on_submit_attempt=lambda: events.append("callback"),
        media=_media(tmp_path),
    )

    assert result.outcome == DirectMessageOutcome.PRE_SUBMIT_FAILED
    assert "media file input" in result.detail
    assert events == []


def test_media_send_readiness_timeout_is_definitely_pre_submit(
    monkeypatch,
    tmp_path,
):
    _patch_prepare(monkeypatch)
    monkeypatch.setattr(
        "linkedin.actions.message.DIRECT_MEDIA_SEND_READY_TIMEOUT_MS",
        500,
    )
    events = []
    page = _Page(
        on_click=lambda: events.append("click"),
        attachment_ready=True,
        send_enabled=False,
        events=events,
    )
    monkeypatch.setattr(
        "linkedin.actions.message.human_type",
        lambda *_args, **_kwargs: events.append("type"),
    )

    result = send_direct_message_once(
        _session(page=page),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="Alice",
        on_submit_attempt=lambda: events.append("callback"),
        media=_media(tmp_path),
    )

    assert result.outcome == DirectMessageOutcome.PRE_SUBMIT_FAILED
    assert result.detail == (
        "LinkedIn Send did not retain overview.mp4 in the intended composer"
    )
    assert events == ["attach", "type"]


def test_media_callback_abort_does_not_click(monkeypatch, tmp_path):
    _patch_prepare(monkeypatch)
    events = []
    page = _Page(on_click=lambda: events.append("click"), events=events)
    monkeypatch.setattr(
        "linkedin.actions.message.human_type",
        lambda *_args, **_kwargs: events.append("type"),
    )

    def abort():
        events.append("callback")
        raise MessageSubmissionAborted("reply arrived during upload")

    result = send_direct_message_once(
        _session(page=page),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="Alice",
        on_submit_attempt=abort,
        media=_media(tmp_path),
    )

    assert result.outcome == DirectMessageOutcome.PRE_SUBMIT_FAILED
    assert result.detail == "reply arrived during upload"
    assert events == ["attach", "type", "callback"]


def test_media_click_error_is_unclear(monkeypatch, tmp_path):
    _patch_prepare(monkeypatch)
    page = _Page(
        on_click=lambda: (_ for _ in ()).throw(TimeoutError("click timed out")),
    )

    result = send_direct_message_once(
        _session(page=page),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="Alice",
        on_submit_attempt=lambda: None,
        media=_media(tmp_path),
    )

    assert result.outcome == DirectMessageOutcome.UNCLEAR


def test_multiple_visible_composers_fail_before_any_action(monkeypatch):
    _patch_prepare(monkeypatch)
    events = []
    page = _Page(
        on_click=lambda: events.append("click"),
        composer_count=2,
        events=events,
    )
    monkeypatch.setattr(
        "linkedin.actions.message.human_type",
        lambda *_args, **_kwargs: events.append("type"),
    )

    result = send_direct_message_once(
        _session(page=page),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="Alice",
        on_submit_attempt=lambda: events.append("callback"),
    )

    assert result.outcome == DirectMessageOutcome.PRE_SUBMIT_FAILED
    assert "exactly one usable composer" in result.detail
    assert events == []


def test_media_dropped_after_typing_never_reaches_submit_callback(
    monkeypatch,
    tmp_path,
):
    _patch_prepare(monkeypatch)
    monkeypatch.setattr(
        "linkedin.actions.message.DIRECT_MEDIA_SEND_READY_TIMEOUT_MS",
        500,
    )
    events = []
    page = _Page(on_click=lambda: events.append("click"), events=events)

    def type_then_drop(*_args, **_kwargs):
        events.append("type")
        page.attachment_ready = False

    monkeypatch.setattr("linkedin.actions.message.human_type", type_then_drop)

    result = send_direct_message_once(
        _session(page=page),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="Alice",
        on_submit_attempt=lambda: events.append("callback"),
        media=_media(tmp_path),
    )

    assert result.outcome == DirectMessageOutcome.PRE_SUBMIT_FAILED
    assert "did not retain overview.mp4" in result.detail
    assert events == ["attach", "type"]


def test_media_dropped_after_boundary_never_clicks_and_is_unclear(
    monkeypatch,
    tmp_path,
):
    _patch_prepare(monkeypatch)
    events = []
    page = _Page(on_click=lambda: events.append("click"), events=events)

    def callback_then_drop():
        events.append("callback")
        page.attachment_ready = False

    monkeypatch.setattr(
        "linkedin.actions.message.human_type",
        lambda *_args, **_kwargs: events.append("type"),
    )

    result = send_direct_message_once(
        _session(page=page),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="Alice",
        on_submit_attempt=callback_then_drop,
        media=_media(tmp_path),
    )

    assert result.outcome == DirectMessageOutcome.UNCLEAR
    assert "no click was attempted" in result.detail
    assert events == ["attach", "type", "callback"]


def test_explicit_media_upload_error_fails_before_typing(monkeypatch, tmp_path):
    _patch_prepare(monkeypatch)
    monkeypatch.setattr(
        "linkedin.actions.message.DIRECT_MEDIA_UPLOAD_TIMEOUT_MS",
        500,
    )
    events = []
    page = _Page(
        upload_error=True,
        on_click=lambda: events.append("click"),
        events=events,
    )
    monkeypatch.setattr(
        "linkedin.actions.message.human_type",
        lambda *_args, **_kwargs: events.append("type"),
    )

    result = send_direct_message_once(
        _session(page=page),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="Alice",
        on_submit_attempt=lambda: events.append("callback"),
        media=_media(tmp_path),
    )

    assert result.outcome == DirectMessageOutcome.PRE_SUBMIT_FAILED
    assert events == ["attach"]


def test_extension_based_accept_list_selects_matching_media_input(
    monkeypatch,
    tmp_path,
):
    _patch_prepare(monkeypatch)
    events = []
    page = _Page(
        file_accept=".jpg, .gif, .mp4",
        on_click=lambda: events.append("click"),
        events=events,
    )
    monkeypatch.setattr(
        "linkedin.actions.message.human_type",
        lambda *_args, **_kwargs: events.append("type"),
    )

    result = send_direct_message_once(
        _session(page=page),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="Alice",
        on_submit_attempt=lambda: events.append("callback"),
        media=_media(tmp_path),
    )

    assert result.outcome == DirectMessageOutcome.SENT
    assert events == ["attach", "type", "callback", "click"]


def test_gif_prefers_narrow_image_input_over_broad_file_input(
    monkeypatch,
    tmp_path,
):
    _patch_prepare(monkeypatch)
    events = []
    page = _Page(
        file_accept=(
            "image/*",
            "image/*,.ai,.psd,.pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.mov,.mp4",
        ),
        on_click=lambda: events.append("click"),
        events=events,
    )
    monkeypatch.setattr(
        "linkedin.actions.message.human_type",
        lambda *_args, **_kwargs: events.append("type"),
    )

    result = send_direct_message_once(
        _session(page=page),
        "urn:li:fsd_profile:ALICE",
        "Hello Alice",
        recipient_label="Alice",
        on_submit_attempt=lambda: events.append("callback"),
        media=_gif_media(tmp_path),
    )

    assert result.outcome == DirectMessageOutcome.SENT
    assert page.selected_file_input_index == 0
    assert events == ["attach", "type", "callback", "click"]
