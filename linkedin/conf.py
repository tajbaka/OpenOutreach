# linkedin/conf.py
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------
ROOT_DIR = Path(__file__).parent.parent

PROMPTS_DIR = Path(__file__).parent / "templates" / "prompts"

DIAGNOSTICS_DIR = Path("/tmp/openoutreach-diagnostics")

ENV_FILE = ROOT_DIR / ".env"

FIXTURE_DIR = ROOT_DIR / "tests" / "fixtures"
FIXTURE_PROFILES_DIR = FIXTURE_DIR / "profiles"
FIXTURE_PAGES_DIR = FIXTURE_DIR / "pages"

MIN_DELAY = 5
MAX_DELAY = 8

# ----------------------------------------------------------------------
# Browser config
# ----------------------------------------------------------------------
BROWSER_SLOW_MO = 200
BROWSER_DEFAULT_TIMEOUT_MS = 30_000
BROWSER_LOGIN_TIMEOUT_MS = 40_000
BROWSER_NAV_TIMEOUT_MS = 10_000
HUMAN_TYPE_MIN_DELAY_MS = 50
HUMAN_TYPE_MAX_DELAY_MS = 200
VOYAGER_REQUEST_TIMEOUT_MS = 30_000

# ----------------------------------------------------------------------
# Onboarding defaults (shown to user during interactive setup)
# ----------------------------------------------------------------------
DEFAULT_CONNECT_DAILY_LIMIT = 20
DEFAULT_CONNECT_WEEKLY_LIMIT = 100
DEFAULT_FOLLOW_UP_DAILY_LIMIT = 30
MAX_TOTAL_DAILY_ACTIONS = int(os.getenv("MAX_TOTAL_DAILY_ACTIONS", "200"))

# Per-action rate-limit env overrides. When set to a positive integer these
# take precedence over the LinkedInProfile DB columns at runtime, so you can
# tune limits without touching Django Admin. Leave unset (or set to 0) to
# fall back to whatever each profile has saved in the DB.
CONNECT_DAILY_LIMIT = int(os.getenv("CONNECT_DAILY_LIMIT") or 0) or None
CONNECT_WEEKLY_LIMIT = int(os.getenv("CONNECT_WEEKLY_LIMIT") or 0) or None
FOLLOW_UP_DAILY_LIMIT = int(os.getenv("FOLLOW_UP_DAILY_LIMIT") or 0) or None
ENABLE_FREEMIUM_CAMPAIGN = os.getenv("ENABLE_FREEMIUM_CAMPAIGN", "false").strip().lower() in {
    "1", "true", "yes", "on",
}

# ----------------------------------------------------------------------
# Active-hours schedule (daemon pauses outside this window)
# Set to False to run 24/7.
# ----------------------------------------------------------------------
ENABLE_ACTIVE_HOURS = os.getenv("ENABLE_ACTIVE_HOURS", "true").strip().lower() in {
    "1", "true", "yes", "on",
}
ACTIVE_START_HOUR = int(os.getenv("ACTIVE_START_HOUR", "9"))   # inclusive, local time
ACTIVE_END_HOUR = int(os.getenv("ACTIVE_END_HOUR", "17"))     # exclusive, local time
ACTIVE_TIMEZONE = os.getenv("ACTIVE_TIMEZONE", "America/Toronto")
REST_DAYS = tuple(
    int(day.strip()) for day in os.getenv("REST_DAYS", "5,6").split(",") if day.strip()
)      # 0=Mon … 6=Sun; default Sat+Sun off

# How often to sweep the My Network → Connections page to detect accepted
# invitations in bulk (replaces per-profile check_pending visits).
CONNECTION_SWEEP_INTERVAL_HOURS = float(os.getenv("CONNECTION_SWEEP_INTERVAL_HOURS", "2"))

# ----------------------------------------------------------------------
# Campaign config (timing + ML defaults — hardcoded, no YAML)
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# Connection note templates (sent with connection requests)
# ----------------------------------------------------------------------
CONNECTION_NOTE_PERSONALIZED = os.getenv("CONNECTION_NOTE_PERSONALIZED", "").replace("\\n", "\n")
CONNECTION_NOTE_FALLBACK = os.getenv("CONNECTION_NOTE_FALLBACK", "").replace("\\n", "\n")

# Master kill-switch for all follow-up messaging. When false, the daemon
# stops at the connect step: connection invites still go out, but no
# post-accept message and no follow-up agent ever runs. Existing pending
# follow_up tasks become no-ops.
ENABLE_FOLLOW_UP = os.getenv("ENABLE_FOLLOW_UP", "true").strip().lower() in {
    "1", "true", "yes", "on",
}

# Independent kill-switch for the connection sweep (acceptance detection).
# When true, the daemon visits the Connections page every
# CONNECTION_SWEEP_INTERVAL_HOURS and transitions PENDING → CONNECTED for
# leads who accepted. Decoupled from ENABLE_FOLLOW_UP so you can detect
# accepts (and get them mirrored to Attio + Slack) WITHOUT auto-sending
# follow-up DMs. The sweep still calls enqueue_follow_up after a match,
# but that call is itself gated on ENABLE_FOLLOW_UP — so when follow-up
# is off, it's a no-op and no DM fires.
ENABLE_SWEEP_CONNECTIONS = os.getenv("ENABLE_SWEEP_CONNECTIONS", "true").strip().lower() in {
    "1", "true", "yes", "on",
}

# Slack incoming-webhook URL. When set, a notification is posted whenever
# the standalone `manage.py check_connections` command detects a newly
# accepted invite. Empty disables Slack entirely. Get one at:
#   https://api.slack.com/messaging/webhooks
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()

# Attio CRM sync. ATTIO_API_KEY scopes need record:read+write, list:read+write,
# object_configuration:read. ATTIO_SALES_LIST_ID is the UUID of the Sales list
# (parent object: companies). manage.py sync_attio mirrors Deal state into the
# Sales list's Stage column. Empty disables the sync (safe no-op).
ATTIO_API_KEY = os.getenv("ATTIO_API_KEY", "").strip()
ATTIO_SALES_LIST_ID = os.getenv("ATTIO_SALES_LIST_ID", "").strip()

# Kill-switch for the connect lane's auto-discovery + LLM qualification
# fallback. When false, the daemon only connects to leads already in
# READY_TO_CONNECT — no LinkedIn search, no LLM qualifier, no auto-
# promotion of QUALIFIED leads. Set when working from a curated seed
# list and you don't want the bot finding/messaging anyone else.
ENABLE_AUTO_DISCOVERY = os.getenv("ENABLE_AUTO_DISCOVERY", "true").strip().lower() in {
    "1", "true", "yes", "on",
}

# Path to GIF/image to attach to follow-up messages (empty = disabled).
# Relative paths are resolved from the repo root so the same .env works
# across machines without requiring identical checkout locations.
_raw_follow_up_media_path = os.getenv("FOLLOW_UP_MEDIA_PATH", "").strip()
if _raw_follow_up_media_path:
    _follow_up_media_path = Path(_raw_follow_up_media_path).expanduser()
    if not _follow_up_media_path.is_absolute():
        _follow_up_media_path = ROOT_DIR / _follow_up_media_path
    FOLLOW_UP_MEDIA_PATH = str(_follow_up_media_path)
else:
    FOLLOW_UP_MEDIA_PATH = ""

# Tracked walkthrough link sent after a connection is accepted without a reply.
# Empty = keep the generic follow-up agent behavior.
POST_ACCEPT_VIDEO_LINK = os.getenv("POST_ACCEPT_VIDEO_LINK", "")
POST_ACCEPT_MESSAGE_TEMPLATE = os.getenv(
    "POST_ACCEPT_MESSAGE_TEMPLATE",
    "Hey {first_name} - put together a 60-second walkthrough of what I mentioned. "
    "Easier to show than explain: {video_link}",
)

CAMPAIGN_CONFIG = {
    "min_action_interval": 120,
    "qualification_n_mc_samples": 100,
    "min_ready_to_connect_prob": 0.9,
    "min_positive_pool_prob": 0.20,
    "embedding_model": "BAAI/bge-small-en-v1.5",
    "connect_delay_seconds": 10,
    "connect_no_candidate_delay_seconds": 300,
}

# ----------------------------------------------------------------------
# Global OpenAI / LLM config
# ----------------------------------------------------------------------
LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_API_BASE = os.getenv("LLM_API_BASE")
AI_MODEL = os.getenv("AI_MODEL")

# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def get_first_active_profile_handle() -> str | None:
    """Return the username of the first active LinkedInProfile, or None."""
    from linkedin.models import LinkedInProfile

    profile = LinkedInProfile.objects.filter(active=True).select_related("user").first()
    return profile.user.username if profile else None
