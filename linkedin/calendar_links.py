"""Canonical Boundera booking links used in sales and relationship workflows."""
from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType


ARIAN_INTRO_CALL_URL = "https://cal.com/arian-taj-hchtgz/intro-call"
ARIAN_NEXT_STEPS_URL = "https://cal.com/arian-taj-hchtgz/boundera-next-steps"
ARIAN_DEEP_DIVE_URL = "https://cal.com/arian-taj-hchtgz/boundera-deep-dive"
ARIAN_GENERAL_CALL_URL = "https://cal.com/arian-taj-hchtgz/boundera-call"
ARIAN_QUICK_CHAT_URL = "https://cal.com/arian-taj-hchtgz/quick-chat-boundera"


ARIAN_CALENDAR_LINKS = MappingProxyType(
    {
        "intro": ARIAN_INTRO_CALL_URL,
        "next_steps": ARIAN_NEXT_STEPS_URL,
        "deep_dive": ARIAN_DEEP_DIVE_URL,
        "general": ARIAN_GENERAL_CALL_URL,
        "quick_chat": ARIAN_QUICK_CHAT_URL,
    }
)


@dataclass(frozen=True)
class CalendarEvent:
    name: str
    duration_minutes: int
    purpose: str
    url: str


# Verified in Arian's signed-in Cal.com event-types page on 2026-09-17.
# These are booking durations, not claims about real-time availability.
ARIAN_CALENDAR_EVENTS = MappingProxyType({
    "intro": CalendarEvent("Boundera Intro Call", 30, "first conversation", ARIAN_INTRO_CALL_URL),
    "next_steps": CalendarEvent("Boundera Next Steps", 30, "an established opportunity's next meeting", ARIAN_NEXT_STEPS_URL),
    "deep_dive": CalendarEvent("Boundera Deep Dive", 60, "detailed product or technical session", ARIAN_DEEP_DIVE_URL),
    "general": CalendarEvent("Boundera Call", 30, "normal call; default when only 30 minutes is specified", ARIAN_GENERAL_CALL_URL),
    "quick_chat": CalendarEvent("Quick Chat Boundera", 20, "short conversation", ARIAN_QUICK_CHAT_URL),
})


def unsupported_calendar_duration(instructions: str) -> bool:
    """Catch explicit unsupported booking durations, without parsing all prose.

    The prompt also requires exact duration/purpose matching. This guard covers
    common numeric requests and does not reinterpret arbitrary timing language.
    """
    if not re.search(r"\b(link|calendar|booking|book|schedule)\b", instructions, re.I):
        return False
    durations = re.findall(r"\b(\d+)\s*[-–]?\s*(minutes?|mins?|hours?|hrs?)\b", instructions, re.I)
    available = {event.duration_minutes for event in ARIAN_CALENDAR_EVENTS.values()}
    return any(int(number) * (60 if unit.lower().startswith("h") else 1) not in available
               for number, unit in durations)
