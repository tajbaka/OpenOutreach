"""Canonical Boundera booking links used in sales and relationship workflows."""
from __future__ import annotations

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
