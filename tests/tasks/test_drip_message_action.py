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
    monkeypatch.setattr("linkedin.db.leads.resolve_urn", lambda *_args, **_kwargs: "urn:li:fsd_profile:1")
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

    def callback():
        state["callback"] = True

    def click():
        assert state["callback"] is True
        state["click"] = True

    result = send_direct_message_once(
        _session(on_click=click),
        {"public_identifier": "alice"},
        "Hello Alice",
        on_submit_attempt=callback,
    )

    assert result.outcome == DirectMessageOutcome.SENT
    assert state == {"callback": True, "click": True}


def test_callback_abort_is_definitely_pre_submit(monkeypatch):
    _patch_prepare(monkeypatch)
    clicked = []

    def abort():
        raise MessageSubmissionAborted("reply arrived")

    result = send_direct_message_once(
        _session(on_click=lambda: clicked.append(True)),
        {"public_identifier": "alice"},
        "Hello Alice",
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
        {"public_identifier": "alice"},
        "Hello Alice",
        on_submit_attempt=lambda: None,
    )

    assert result.outcome == DirectMessageOutcome.UNCLEAR


def test_missing_post_click_confirmation_is_unclear(monkeypatch):
    _patch_prepare(monkeypatch, confirmed=False)

    result = send_direct_message_once(
        _session(),
        {"public_identifier": "alice"},
        "Hello Alice",
        on_submit_attempt=lambda: None,
    )

    assert result.outcome == DirectMessageOutcome.UNCLEAR
