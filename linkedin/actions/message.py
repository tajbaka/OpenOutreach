# linkedin/actions/message.py
import json
import logging
from typing import Dict, Any

from playwright.sync_api import Error as PlaywrightError
from linkedin.browser.nav import goto_page, human_type

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


def send_raw_message(
    session,
    profile: Dict[str, Any],
    message: str,
    *,
    deal_id: int | None = None,
    sequence_name: str = "",
    step_index: int | None = None,
    operator: str = "",
) -> bool:
    """Send an arbitrary message to a profile and persist it. Returns True if sent."""
    from linkedin.db.chat import save_chat_message

    public_identifier = profile.get("public_identifier")

    sent = (
        _send_msg_pop_up(session, profile, message)
        or _send_message(session, profile, message)
        or _send_message_via_api(session, profile, message)
    )
    if not sent:
        logger.error("All send methods failed for %s", public_identifier)
        return False

    save_chat_message(
        session,
        public_identifier,
        message,
        deal_id=deal_id,
        sequence_name=sequence_name,
        step_index=step_index,
        operator=operator,
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


def _send_message(session: "AccountSession", profile: Dict[str, Any], message: str) -> bool:
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
            logger.error("Direct-thread send for %s → could not resolve URN", public_identifier)
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
        logger.error("Failed to send message to %s (direct thread) → %s", public_identifier, e)
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


def send_media_message(
    session,
    profile: Dict[str, Any],
    message: str,
    media_path: str,
    *,
    deal_id: int | None = None,
    sequence_name: str = "",
    step_index: int | None = None,
    operator: str = "",
) -> bool:
    """Send a message + media attachment via the URN-keyed direct compose URL.

    Mirrors `_send_message`'s single-nav pattern: navigate once to
    `/messaging/thread/new/?recipient=<URN>` (recipient attached by URL —
    no search, no click), attach the file via the hidden file input,
    human_type the body, click Send.

    Previously this resolved the existing conversation URN (API first,
    navigation fallback) and then navigated to `/messaging/thread/<id>/`,
    which produced a visible double-nav whenever the conv URN wasn't in
    the recent-conversations cache — the fallback navigation to
    `/messaging/thread/new/?recipient=<URN>` left the page on the
    compose, then we'd immediately goto the thread URL on top of it.
    Hemang Bhatt incident, 2026-05-12. The compose URL already renders
    the file input + send button, so the conv-URN lookup wasn't earning
    its second navigation.
    """
    import time
    from linkedin.api.messaging import encode_urn
    from linkedin.db.chat import save_chat_message
    from linkedin.db.leads import resolve_urn

    public_identifier = profile.get("public_identifier")
    page = session.page

    target_urn = resolve_urn(public_identifier, session=session)
    if not target_urn:
        logger.error("Cannot resolve URN for %s — media send failed", public_identifier)
        return False

    direct_url = f"{LINKEDIN_MESSAGING_URL}?recipient={encode_urn(target_urn)}"

    try:
        goto_page(
            session,
            action=lambda: page.goto(direct_url),
            expected_url_pattern="/messaging",
            timeout=30_000,
            error_message="Error opening direct compose for media send",
        )
        session.wait()

        # Attach file via hidden file input
        file_input = page.locator('input[type="file"]')
        if file_input.count() == 0:
            logger.error("No file input found on messaging page for %s", public_identifier)
            return False

        file_input.first.set_input_files(media_path)
        logger.debug("Media attached via file input: %s", media_path)
        time.sleep(3)
        session.wait()

        # Type message if provided. human_type uses per-keystroke randomized
        # cadence (HUMAN_TYPE_MIN/MAX_DELAY_MS) and is multi-line safe
        # (\n → Shift+Enter). fill() pasted the entire body in one shot,
        # which read as bot-like in-thread (2026-05-12 operator report).
        if message:
            msg_input = page.locator(SELECTORS["message_input"])
            if msg_input.count() > 0:
                human_type(msg_input.first, message)
                session.wait()

        # Click send
        send_btn = page.locator(SELECTORS["send_button"])
        if send_btn.count() == 0:
            logger.error("Send button not found after attaching media for %s", public_identifier)
            return False

        send_btn.first.click(force=True)
        session.wait(3, 4)

        save_chat_message(
            session,
            public_identifier,
            message or "[media]",
            deal_id=deal_id,
            sequence_name=sequence_name,
            step_index=step_index,
            operator=operator,
        )
        logger.info("Media message sent to %s", public_identifier)
        return True

    except Exception as e:
        logger.error("Media send failed for %s → %s", public_identifier, e)
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
