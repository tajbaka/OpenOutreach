# linkedin/actions/message.py
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Callable
from typing import Dict, Any

from playwright.sync_api import Error as PlaywrightError
from linkedin.browser.nav import goto_page, human_type

if TYPE_CHECKING:
    from linkedin.message_media import LinkedInMediaAsset

logger = logging.getLogger(__name__)

LINKEDIN_MESSAGING_URL = "https://www.linkedin.com/messaging/thread/new/"
LINKEDIN_MESSAGING_THREAD_URL = "https://www.linkedin.com/messaging/thread/"

SELECTORS = {
    "message_button": 'button[aria-label*="Message"]:visible',
    "overflow_action": 'button[id$="profile-overflow-action"]:visible',
    "message_option": 'div[aria-label$="to message"]:visible',
    "message_input": 'div[class*="msg-form__contenteditable"]:visible',
    "send_button": 'button[type="submit"][class*="msg-form"]:visible',
    "connections_input": 'input[class^="msg-connections"]',
    "search_result_row": 'div[class*="msg-connections-typeahead__search-result-row"]',
    "compose_input": 'div[class^="msg-form__contenteditable"]',
    "compose_send": 'button[class^="msg-form__send-button"]',
}

DIRECT_COMPOSER_SELECTOR = 'form[class*="msg-form"]:visible'
DIRECT_COMPOSER_INPUT_SELECTOR = 'div[class*="msg-form__contenteditable"]:visible'
DIRECT_COMPOSER_SEND_SELECTOR = (
    'button[type="submit"][class*="msg-form"]:visible, '
    'button[class^="msg-form__send-button"]:visible'
)
DIRECT_MEDIA_FILE_INPUT_SELECTOR = 'input[type="file"]'
DIRECT_MEDIA_ATTACHMENT_READY_SELECTOR = (
    '[class*="msg-form__attachment-preview"]:visible, '
    '[class*="msg-form__attachment-list-item"]:visible, '
    '[class*="msg-form__upload-data"]:visible, '
    'button[aria-label*="Remove attachment" i]:visible, '
    'button[aria-label*="Remove file" i]:visible'
)
DIRECT_MEDIA_UPLOAD_BUSY_SELECTOR = (
    '[class*="uploading"]:visible, '
    '[class*="upload-progress"]:visible, '
    '[role="progressbar"]:visible, '
    '[aria-label*="Uploading" i]:visible'
)
DIRECT_MEDIA_UPLOAD_ERROR_SELECTOR = (
    '[class*="upload-error"]:visible, '
    '[class*="attachment-error"]:visible, '
    '[aria-label*="upload failed" i]:visible, '
    '[aria-label*="could not upload" i]:visible'
)
DIRECT_MEDIA_UPLOAD_TIMEOUT_MS = 90_000
DIRECT_MEDIA_SEND_READY_TIMEOUT_MS = 15_000
DIRECT_MEDIA_POLL_INTERVAL_MS = 250


class MessageSendError(RuntimeError):
    """Raised when a caller needs the concrete UI/API send failure reason."""


class MessageSubmissionAborted(RuntimeError):
    """Raised by a pre-click callback when a persisted guard stops the send."""


class DirectMessageOutcome(StrEnum):
    SENT = "sent"
    PRE_SUBMIT_FAILED = "pre_submit_failed"
    UNCLEAR = "unclear"


@dataclass(frozen=True)
class DirectMessageResult:
    outcome: DirectMessageOutcome
    detail: str = ""


def _locator_has_visible_match(locator) -> bool:
    """Return whether a potentially multi-element locator has a visible match."""
    for index in range(locator.count()):
        if locator.nth(index).is_visible(timeout=500):
            return True
    return False


def _resolve_direct_composer(page):
    """Return the sole visible direct-route composer, or fail closed."""
    composers = page.locator(DIRECT_COMPOSER_SELECTOR)
    if composers.count() != 1:
        return None
    composer = composers.first
    if (
        composer.locator(DIRECT_COMPOSER_INPUT_SELECTOR).count() != 1
        or composer.locator(DIRECT_COMPOSER_SEND_SELECTOR).count() != 1
    ):
        return None
    return composer


def _select_direct_media_input(composer, media: "LinkedInMediaAsset"):
    """Choose one unambiguous file control compatible with the frozen MIME."""
    inputs = composer.locator(DIRECT_MEDIA_FILE_INPUT_SELECTOR)
    ranked: list[tuple[tuple[int, int], int]] = []
    media_family = media.mime_type.split("/", 1)[0]
    media_suffix = media.path.suffix.casefold()
    for index in range(inputs.count()):
        candidate = inputs.nth(index)
        accept = (candidate.get_attribute("accept", timeout=500) or "").casefold()
        accept_tokens = {
            token.strip()
            for token in accept.split(",")
            if token.strip()
        }
        if media.mime_type.casefold() in accept_tokens:
            score = 4
        elif media_suffix in accept_tokens:
            score = 3
        elif f"{media_family}/*" in accept or media_family in accept:
            score = 2
        elif not accept or "*/*" in accept:
            score = 1
        else:
            continue
        # When two controls accept the same family, prefer the narrower one.
        # LinkedIn currently exposes both an image-only input and a broad file
        # input that also includes image/*; a GIF must bind to the former,
        # while MP4 matches only the latter by exact suffix.
        ranked.append(((score, -len(accept_tokens)), index))
    if not ranked:
        return None
    best_score = max(score for score, _index in ranked)
    best = [index for score, index in ranked if score == best_score]
    if len(best) != 1:
        return None
    return inputs.nth(best[0])


def _direct_media_input_has_file(file_input, media: "LinkedInMediaAsset") -> bool:
    """Use the browser's selected filename as attachment evidence when present."""
    value = (file_input.input_value(timeout=500) or "").replace("\\", "/")
    return value.rsplit("/", 1)[-1] == media.path.name


def _direct_media_attachment_ready(
    composer,
    file_input,
    media: "LinkedInMediaAsset",
    *,
    require_visible_preview: bool = False,
) -> bool:
    """Check that LinkedIn accepted the asset and is not visibly uploading it."""
    if _locator_has_visible_match(
        composer.locator(DIRECT_MEDIA_UPLOAD_ERROR_SELECTOR),
    ):
        return False
    if _locator_has_visible_match(composer.locator(DIRECT_MEDIA_UPLOAD_BUSY_SELECTOR)):
        return False

    preview_visible = _locator_has_visible_match(
        composer.locator(DIRECT_MEDIA_ATTACHMENT_READY_SELECTOR),
    )
    if preview_visible:
        return True
    if require_visible_preview:
        return False

    # LinkedIn has shipped composer variants without a filename-bearing preview.
    # In those variants the selected file plus an enabled media-only Send button
    # is the strongest available upload-ready signal before typing the body.
    if not _direct_media_input_has_file(file_input, media):
        return False
    return composer.locator(DIRECT_COMPOSER_SEND_SELECTOR).first.is_enabled(timeout=500)


def _wait_for_direct_media_attachment(
    page,
    composer,
    file_input,
    media: "LinkedInMediaAsset",
) -> bool:
    """Poll LinkedIn's composer state until the attachment is stably ready."""
    attempts = max(
        1,
        DIRECT_MEDIA_UPLOAD_TIMEOUT_MS // DIRECT_MEDIA_POLL_INTERVAL_MS,
    )
    consecutive_ready = 0
    for attempt in range(attempts):
        if _direct_media_attachment_ready(composer, file_input, media):
            consecutive_ready += 1
            if consecutive_ready >= 2:
                return True
        else:
            consecutive_ready = 0
        if attempt + 1 < attempts:
            page.wait_for_timeout(DIRECT_MEDIA_POLL_INTERVAL_MS)
    return False


def _wait_for_direct_send_enabled(
    page,
    composer,
    *,
    file_input=None,
    media: "LinkedInMediaAsset | None" = None,
) -> bool:
    """Poll the same composer and re-prove its attachment before submission."""
    attempts = max(
        1,
        DIRECT_MEDIA_SEND_READY_TIMEOUT_MS // DIRECT_MEDIA_POLL_INTERVAL_MS,
    )
    send_button = composer.locator(DIRECT_COMPOSER_SEND_SELECTOR).first
    for attempt in range(attempts):
        media_ready = media is None or _direct_media_attachment_ready(
            composer,
            file_input,
            media,
            require_visible_preview=True,
        )
        if media_ready and send_button.is_enabled(timeout=500):
            return True
        if attempt + 1 < attempts:
            page.wait_for_timeout(DIRECT_MEDIA_POLL_INTERVAL_MS)
    return False


def _direct_message_submission_confirmed(page, composer, message: str) -> bool:
    """Confirm the direct composer cleared or the exact body rendered."""
    event_selector = "div.msg-s-event-listitem, li.msg-s-message-list__event"
    for _attempt in range(12):
        try:
            editor = composer.locator(DIRECT_COMPOSER_INPUT_SELECTOR).first
            if not (editor.inner_text(timeout=500) or "").strip():
                return True
        except PlaywrightError:
            pass

        try:
            events = page.locator(event_selector)
            start = max(events.count() - 8, 0)
            for index in range(start, events.count()):
                if message.strip() in (events.nth(index).inner_text(timeout=500) or ""):
                    return True
        except PlaywrightError:
            pass
        page.wait_for_timeout(250)
    return False


def send_direct_message_once(
    session,
    member_urn: str,
    message: str,
    *,
    recipient_label: str,
    on_submit_attempt: Callable[[], None],
    media: "LinkedInMediaAsset | None" = None,
) -> DirectMessageResult:
    """Send to one already-reviewed member URN through one compose route.

    For a media send, the already-validated asset is attached and its composer
    state is polled before typing. The callback runs after upload and typing,
    immediately before the only send click. Once it returns successfully, any
    Playwright error or missing confirmation is classified as ``unclear``
    because the click may have reached LinkedIn. No mutable Lead lookup, popup
    route, or Voyager API fallback is attempted.
    """
    from linkedin.api.messaging import encode_urn
    from linkedin.member_identity import normalize_member_urn, valid_member_urn

    target_urn = normalize_member_urn(member_urn)
    target_label = (recipient_label or "").strip() or target_urn
    if not valid_member_urn(target_urn):
        return DirectMessageResult(
            DirectMessageOutcome.PRE_SUBMIT_FAILED,
            "Refusing direct message without an exact fsd_profile member URN",
        )
    submission_boundary_crossed = False
    try:
        direct_url = f"{LINKEDIN_MESSAGING_URL}?recipient={encode_urn(target_urn)}"
        goto_page(
            session,
            action=lambda: session.page.goto(direct_url),
            expected_url_pattern="/messaging",
            timeout=30_000,
            error_message="Error opening direct compose",
        )
        session.wait(0.5, 1.2)

        composer = _resolve_direct_composer(session.page)
        if composer is None:
            return DirectMessageResult(
                DirectMessageOutcome.PRE_SUBMIT_FAILED,
                "LinkedIn direct route did not expose exactly one usable composer",
            )

        file_input = None
        if media is not None:
            file_input = _select_direct_media_input(composer, media)
            if file_input is None:
                return DirectMessageResult(
                    DirectMessageOutcome.PRE_SUBMIT_FAILED,
                    "LinkedIn direct composer did not expose one compatible media file input",
                )
            file_input.set_input_files(str(media.path))
            if not _wait_for_direct_media_attachment(
                session.page,
                composer,
                file_input,
                media,
            ):
                return DirectMessageResult(
                    DirectMessageOutcome.PRE_SUBMIT_FAILED,
                    f"LinkedIn did not finish attaching {media.reference}",
                )

        human_type(composer.locator(DIRECT_COMPOSER_INPUT_SELECTOR), message)
        session.wait(0.4, 0.9)

        if not _wait_for_direct_send_enabled(
            session.page,
            composer,
            file_input=file_input,
            media=media,
        ):
            return DirectMessageResult(
                DirectMessageOutcome.PRE_SUBMIT_FAILED,
                (
                    f"LinkedIn Send did not retain {media.reference} in the intended composer"
                    if media is not None
                    else "LinkedIn Send did not become ready in the intended composer"
                ),
            )

        on_submit_attempt()
        submission_boundary_crossed = True
        if media is not None and not _direct_media_attachment_ready(
            composer,
            file_input,
            media,
            require_visible_preview=True,
        ):
            return DirectMessageResult(
                DirectMessageOutcome.UNCLEAR,
                "Submit boundary committed but the intended composer no longer proved "
                f"attachment {media.reference}; no click was attempted",
            )
        composer.locator(DIRECT_COMPOSER_SEND_SELECTOR).click(delay=200)
        if not _direct_message_submission_confirmed(session.page, composer, message):
            return DirectMessageResult(
                DirectMessageOutcome.UNCLEAR,
                "Send click occurred but LinkedIn did not confirm the message",
            )
        logger.info(
            "Message sent to %s (single direct thread route)",
            target_label,
        )
        return DirectMessageResult(DirectMessageOutcome.SENT)
    except MessageSubmissionAborted as exc:
        return DirectMessageResult(
            DirectMessageOutcome.PRE_SUBMIT_FAILED,
            str(exc),
        )
    except (PlaywrightError, TimeoutError) as exc:
        outcome = (
            DirectMessageOutcome.UNCLEAR
            if submission_boundary_crossed
            else DirectMessageOutcome.PRE_SUBMIT_FAILED
        )
        return DirectMessageResult(outcome, str(exc))


def send_raw_message(
    session,
    profile: Dict[str, Any],
    message: str,
    *,
    deal_id: int | None = None,
    sequence_name: str = "",
    step_index: int | None = None,
    operator: str = "",
    external_id_kind: str = "daemon-send",
    prefer_direct: bool = False,
    allow_api_fallback: bool = True,
    raise_on_failure: bool = False,
) -> bool:
    """Send an arbitrary message to a profile and persist it. Returns True if sent."""
    from linkedin.db.chat import save_chat_message

    public_identifier = profile.get("public_identifier")

    if prefer_direct:
        sent = _send_message(
            session,
            profile,
            message,
            raise_on_failure=raise_on_failure and not allow_api_fallback,
        )
        if not sent and allow_api_fallback:
            sent = _send_message_via_api(session, profile, message)
    else:
        sent = _send_msg_pop_up(session, profile, message) or _send_message(
            session, profile, message,
        )
        if not sent and allow_api_fallback:
            sent = _send_message_via_api(session, profile, message)
    if not sent:
        logger.error("All send methods failed for %s", public_identifier)
        if raise_on_failure:
            raise MessageSendError(f"All send methods failed for {public_identifier}")
        return False

    save_chat_message(
        session,
        public_identifier,
        message,
        deal_id=deal_id,
        sequence_name=sequence_name,
        step_index=step_index,
        operator=operator,
        external_id_kind=external_id_kind,
    )
    logger.info("Message sent to %s: %s", public_identifier, message)
    return True



def _send_msg_pop_up(session: "AccountSession", profile: Dict[str, Any], message: str) -> bool:
    session.wait()
    page = session.page
    public_identifier = profile.get("public_identifier")

    try:
        direct = page.locator(SELECTORS["message_button"])
        if direct.count() > 0:
            direct.first.click()
            logger.debug("Opened Message popup (direct button)")
        else:
            more = page.locator(SELECTORS["overflow_action"]).first
            more.click()
            session.wait()
            msg_option = page.locator(SELECTORS["message_option"]).first
            msg_option.click()
            logger.debug("Opened Message via More → Message")

        session.wait()

        input_area = page.locator(SELECTORS["message_input"]).first

        try:
            input_area.fill(message, timeout=10000)
            logger.debug("Message typed cleanly")
        except Exception:
            logger.debug("fill() failed → using clipboard paste")
            input_area.click()
            page.evaluate(f"() => navigator.clipboard.writeText({json.dumps(message)})")
            session.wait()
            input_area.press("ControlOrMeta+V")
            session.wait()

        send_btn = page.locator(SELECTORS["send_button"]).first
        send_btn.click(force=True)
        session.wait(4, 5)

        page.keyboard.press("Escape")
        session.wait()

        logger.info("Message sent to %s", public_identifier)
        return True

    except (PlaywrightError, TimeoutError) as e:
        logger.error("Failed to send message to %s → %s", public_identifier, e)
        return False


def _send_message(
    session: "AccountSession",
    profile: Dict[str, Any],
    message: str,
    *,
    raise_on_failure: bool = False,
) -> bool:
    """Compose-and-send via URN-keyed direct thread URL.

    Pre-2026-05-12 behavior: navigate to `/messaging/thread/new/` (no
    recipient), type the lead's full name into the connections search
    input, wait, scroll-into-view, click the first search result, then
    type the message and Send. That's three extra UI steps a human
    doesn't take when they're already on the lead's profile — operator
    flagged it as bot-like (Dustin Rich incident, 2026-05-12).

    New behavior: resolve the lead's URN, navigate directly to
    `messaging/thread/new/?recipient=<URN>` which puts us straight into
    the compose with the recipient already attached. No search, no
    scroll, no result click. Then human_type the message + Send.
    """
    from linkedin.api.messaging import encode_urn
    from linkedin.db.leads import resolve_urn

    public_identifier = profile.get("public_identifier")
    try:
        target_urn = resolve_urn(public_identifier, session=session)
        if not target_urn:
            error = f"Direct-thread send for {public_identifier} failed: could not resolve URN"
            logger.error(error)
            if raise_on_failure:
                raise MessageSendError(error)
            return False

        direct_url = f"{LINKEDIN_MESSAGING_URL}?recipient={encode_urn(target_urn)}"
        goto_page(
            session,
            action=lambda: session.page.goto(direct_url),
            expected_url_pattern="/messaging",
            timeout=30_000,
            error_message="Error opening direct compose",
        )
        session.wait(0.5, 1.2)

        # human_type is multi-line safe (\n → Shift+Enter, not Enter) and
        # uses conf.HUMAN_TYPE_MIN/MAX_DELAY_MS for human cadence.
        human_type(session.page.locator(SELECTORS["compose_input"]), message)
        session.wait(0.4, 0.9)

        session.page.locator(SELECTORS["compose_send"]).click(delay=200)
        session.wait(0.5, 1)
        logger.info("Message sent to %s (direct thread, URN-keyed)", public_identifier)
        return True
    except (PlaywrightError, TimeoutError) as e:
        error = f"Direct-thread send for {public_identifier} failed: {e}"
        logger.error(error)
        if raise_on_failure:
            raise MessageSendError(error) from e
        return False


def _send_message_via_api(
    session: "AccountSession",
    profile: Dict[str, Any],
    message: str,
    file_attachments: list[dict] | None = None,
) -> bool:
    """Last-resort fallback: send via Voyager Messaging API."""
    from linkedin.api.client import PlaywrightLinkedinAPI
    from linkedin.api.messaging import send_message
    from linkedin.db.leads import resolve_urn
    from linkedin.actions.conversations import find_conversation_urn, find_conversation_urn_via_navigation

    public_identifier = profile.get("public_identifier")

    target_urn = resolve_urn(public_identifier, session=session)
    if not target_urn:
        logger.error("API send failed for %s → could not resolve URN", public_identifier)
        return False

    api = PlaywrightLinkedinAPI(session=session)

    conversation_urn = find_conversation_urn(api, target_urn)
    if not conversation_urn:
        conversation_urn = find_conversation_urn_via_navigation(session, target_urn)
    if not conversation_urn:
        logger.error("API send failed for %s → no conversation found", public_identifier)
        return False

    try:
        send_message(api, conversation_urn, message, file_attachments=file_attachments)
        logger.info("Message sent to %s (API%s)", public_identifier, " + media" if file_attachments else "")
        return True
    except Exception as e:
        logger.error("API send failed for %s → %s", public_identifier, e)
        return False


if __name__ == "__main__":
    import os
    import argparse

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")

    import django
    django.setup()

    from linkedin.conf import get_first_active_profile_handle
    from linkedin.browser.registry import get_or_create_session

    logging.basicConfig(level=logging.DEBUG, format="[%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Send a LinkedIn message")
    parser.add_argument("--handle", default=None, help="LinkedIn handle (default: first active profile)")
    parser.add_argument("--profile", required=True, help="Public identifier of the target profile")
    parser.add_argument("--message", required=True, help="Message text to send")
    args = parser.parse_args()

    handle = args.handle or get_first_active_profile_handle()
    if not handle:
        print("No active LinkedInProfile found and no --handle provided.")
        raise SystemExit(1)

    test_profile = {
        "url": f"https://www.linkedin.com/in/{args.profile}/",
        "public_identifier": args.profile,
    }

    session = get_or_create_session(handle=handle)
    session.campaign = session.campaigns.first()
    session.ensure_browser()
    print(f"Sending message as @{handle} → {args.profile}")

    send_raw_message(session=session, profile=test_profile, message=args.message)
