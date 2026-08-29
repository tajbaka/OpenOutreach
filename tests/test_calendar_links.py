from linkedin.calendar_links import (
    ARIAN_CALENDAR_LINKS,
    ARIAN_DEEP_DIVE_URL,
    ARIAN_GENERAL_CALL_URL,
    ARIAN_INTRO_CALL_URL,
    ARIAN_NEXT_STEPS_URL,
    ARIAN_QUICK_CHAT_URL,
)


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
