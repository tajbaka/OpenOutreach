from types import SimpleNamespace

from linkedin.actions.message import (
    DirectMessageOutcome,
    MessageSubmissionAborted,
    send_direct_message_once,
)


class _Locator:
    def __init__(self, *, on_click=None):
        self._on_click = on_click

    def click(self, **_kwargs):
        if self._on_click is not None:
            self._on_click()


class _Page:
    def __init__(self, *, on_click=None):
        self.on_click = on_click

    def goto(self, _url):
        return None

    def locator(self, selector):
        if "send-button" in selector:
            return _Locator(on_click=self.on_click)
        return _Locator()


def _session(*, on_click=None):
    return SimpleNamespace(
        page=_Page(on_click=on_click),
        wait=lambda *_args, **_kwargs: None,
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
