from linkedin.calendar_links import (
    ARIAN_CALENDAR_EVENTS,
    ARIAN_CALENDAR_LINKS,
    ARIAN_DEEP_DIVE_URL,
    ARIAN_GENERAL_CALL_URL,
    ARIAN_INTRO_CALL_URL,
    ARIAN_NEXT_STEPS_URL,
    ARIAN_QUICK_CHAT_URL,
    unsupported_calendar_duration,
)
import pytest


def test_arian_calendar_links_are_complete_and_unique():
    assert ARIAN_CALENDAR_LINKS == {
        "intro": ARIAN_INTRO_CALL_URL,
        "next_steps": ARIAN_NEXT_STEPS_URL,
        "deep_dive": ARIAN_DEEP_DIVE_URL,
        "general": ARIAN_GENERAL_CALL_URL,
        "quick_chat": ARIAN_QUICK_CHAT_URL,
    }
    assert len(set(ARIAN_CALENDAR_LINKS.values())) == 5
    assert all(url.startswith("https://cal.com/arian-taj-hchtgz/") for url in ARIAN_CALENDAR_LINKS.values())


def test_arian_calendar_link_urls():
    assert ARIAN_INTRO_CALL_URL.endswith("/intro-call")
    assert ARIAN_NEXT_STEPS_URL.endswith("/boundera-next-steps")
    assert ARIAN_DEEP_DIVE_URL.endswith("/boundera-deep-dive")
    assert ARIAN_GENERAL_CALL_URL.endswith("/boundera-call")
    assert ARIAN_QUICK_CHAT_URL.endswith("/quick-chat-boundera")


def test_verified_event_durations_and_links():
    assert {key: event.duration_minutes for key, event in ARIAN_CALENDAR_EVENTS.items()} == {
        "intro": 30, "next_steps": 30, "deep_dive": 60, "general": 30, "quick_chat": 20,
    }
    assert {key: event.url for key, event in ARIAN_CALENDAR_EVENTS.items()} == ARIAN_CALENDAR_LINKS


@pytest.mark.parametrize("text,unsupported", [
    ("Attach my meeting link for 15 mins", True),
    ("Include a 45-minute booking link", True),
    ("Include my 20-minute meeting link", False),
    ("Use the 30 min intro link", False),
    ("Send my 1 hour deep dive link", False),
    ("Keep it under 30 words", False),
    ("Ask about the 15-minute timeout", False),
    ("", False),
])
def test_explicit_unsupported_duration_guard(text, unsupported):
    assert unsupported_calendar_duration(text) is unsupported
