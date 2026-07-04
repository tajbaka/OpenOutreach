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
# Per-keystroke delay bounds for `human_type`. Real humans average
# 30-50 WPM (~200-300ms/keystroke) up to 70-90 WPM (~80-120ms/keystroke)
# in bursts. Bumped from 50-200ms to 80-250ms on 2026-05-12 after
# operator flagged sends were "way too fast and seems like a bot".
# Sleep delays per keystroke are randomly sampled inside this band,
# so the timing distribution matches a human typing with natural
# pauses and bursts rather than a uniform machine cadence.
HUMAN_TYPE_MIN_DELAY_MS = 80
HUMAN_TYPE_MAX_DELAY_MS = 250
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
CONNECT_LOW_POOL_THRESHOLD = int(os.getenv("CONNECT_LOW_POOL_THRESHOLD", "100"))
ENABLE_FREEMIUM_CAMPAIGN = os.getenv("ENABLE_FREEMIUM_CAMPAIGN", "false").strip().lower() in {
    "1", "true", "yes", "on",
}
# When enabled, connect/follow-up pacing can accelerate beyond the full-window
# average if a sender is behind pace for the current active-hours window.
# Default OFF so existing rollout behavior stays unchanged unless explicitly
# opted into via env.
ENABLE_PACING_CATCH_UP = os.getenv("ENABLE_PACING_CATCH_UP", "false").strip().lower() in {
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
# Master kill-switch for all follow-up messaging. When false, the daemon
# stops at the connect step: connection invites still go out, but no
# post-accept message and no follow-up agent ever runs. Existing pending
# follow_up tasks become no-ops.
ENABLE_FOLLOW_UP = os.getenv("ENABLE_FOLLOW_UP", "true").strip().lower() in {
    "1", "true", "yes", "on",
}

# Post-accept browserless Gmail lane. Default OFF; when on, accepted LinkedIn
# connections can enqueue email enrichment or a pending gmail_follow_up task on
# their own delay_hours timeline, independent from LinkedIn follow-up completion.
ENABLE_GMAIL_SEQUENCE = os.getenv("ENABLE_GMAIL_SEQUENCE", "false").strip().lower() in {
    "1", "true", "yes", "on",
}

# Kill-switch for the connect lane (sending new connection invites).
# When false, `handle_connect` is a no-op and `enqueue_connect` skips —
# the daemon stops adding to the network while still processing
# `follow_up` and `sweep_connections` tasks. Pair with
# `ENABLE_FOLLOW_UP=true` + `enqueue_no_reply_followups` to test the
# follow-up path against existing CONNECTED leads without growing the
# network further. Default true so nothing breaks for operators not
# using this gate.
ENABLE_CONNECT = os.getenv("ENABLE_CONNECT", "true").strip().lower() in {
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

# Slack incoming-webhook URLs. Get one at:
#   https://api.slack.com/messaging/webhooks
#
# SLACK_WEBHOOK_URL is the "ops" channel — workflow crashes (notify_error)
# and newly accepted connection invites (notify_connection_accepted).
# Empty disables those notifications.
#
# SLACK_REPLIES_WEBHOOK_URL is the "replies" channel — inbound DM detections
# from the realtime listener (notify_message_received) and phone-enrichment
# results (notify_phone_enriched). Empty disables those notifications.
# SLACK_BOT_TOKEN is the xoxb token used for Slack interactivity that needs
# Web API calls, e.g. opening manual-reply modals and updating messages.
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()
SLACK_REPLIES_WEBHOOK_URL = os.getenv("SLACK_REPLIES_WEBHOOK_URL", "").strip()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
MANUAL_REPLY_POLL_SECONDS = int(os.getenv("MANUAL_REPLY_POLL_SECONDS", "60"))

# Google Sheets CRM sync. GOOGLE_SHEETS_ID is the spreadsheet id from the
# URL (https://docs.google.com/spreadsheets/d/<id>/edit). CREDENTIALS_PATH
# points at a service-account JSON key file with Editor access shared on
# the sheet. Empty either disables the sync (safe no-op).
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID", "").strip()
GOOGLE_SHEETS_CREDENTIALS_PATH = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "").strip()
GOOGLE_SHEETS_TAB_NAME = os.getenv("GOOGLE_SHEETS_TAB_NAME", "People").strip()

# Gmail "self" address set is derived at runtime from the connected
# Gmail account itself (Gmail Profile API + Send-As aliases) rather than
# from an env-var list. Removes the brittle "remember every alias" config —
# see `linkedin/notifications/gmail_threads.py:persist_gmail_threads` for the
# new `self_emails` parameter that callers pass in. Was previously
# HOST_EMAIL + TEAM_EMAILS env vars; removed 2026-05-12.

# Our product identity — surfaced into outreach drafts via the ICP Templates
# tab's `{our_company_name}` / `{our_website_url}` substitution tokens.
# Centralizing here means renaming the product (or changing the URL) is a
# one-line .env edit, not a sweep across every template cell. Templates that
# pre-date this can keep hard-coded values; new templates should use the
# substitution tokens.
OUR_COMPANY_NAME = os.getenv("OUR_COMPANY_NAME", "").strip()
OUR_WEBSITE_URL = os.getenv("OUR_WEBSITE_URL", "").strip()

# Kill-switch for the connect lane's auto-discovery + LLM qualification
# fallback. When false, the daemon only connects to leads already in
# READY_TO_CONNECT — no LinkedIn search, no LLM qualifier, no auto-
# promotion of QUALIFIED leads. Set when working from a curated seed
# list and you don't want the bot finding/messaging anyone else.
ENABLE_AUTO_DISCOVERY = os.getenv("ENABLE_AUTO_DISCOVERY", "true").strip().lower() in {
    "1", "true", "yes", "on",
}

# Kill-switch for the realtime inbound-message listener. When true, the
# daemon opens a second browser tab on /messaging/ and observes LinkedIn's
# own realtime SSE stream via CDP, persisting + Slack-notifying inbound
# DMs within seconds. Default OFF — the listener is an enhancement; with
# it disabled the daemon behaves exactly as before (polling only).
# Mirrors the existing ENABLE_* gates.
ENABLE_REALTIME_LISTENER = os.getenv("ENABLE_REALTIME_LISTENER", "false").strip().lower() in {
    "1", "true", "yes", "on",
}

# Kill-switch for daily LinkedIn home-feed collection. The outer
# daemon_supervisor.py wakes the collector; the collector connects to this
# daemon browser over CDP and opens its own /feed/ tab.
ENABLE_LINKEDIN_FEED_COLLECTOR = os.getenv(
    "ENABLE_LINKEDIN_FEED_COLLECTOR", "false",
).strip().lower() in {"1", "true", "yes", "on"}

# Startup catch-up threshold. If the listener heartbeat is older than this
# many minutes when the daemon boots, the catch-up surfaces the gap (prompt
# on a TTY, WARNING log when headless). A quick restart stays below it.
LISTENER_CATCHUP_GAP_MINUTES = int(os.getenv("LISTENER_CATCHUP_GAP_MINUTES") or 30)

# Granularity of the daemon's chunked Playwright-pumping idle wait. The wait
# loops in slices of this many seconds so CDP callbacks fire promptly and
# the heartbeat file is refreshed each slice.
LISTENER_PUMP_SLICE_SECONDS = int(os.getenv("LISTENER_PUMP_SLICE_SECONDS") or 30)

# Listener schedule is independent from outbound active hours. Defaults keep
# the inbound watcher available all day, every day, while connect/follow-up
# automation remains governed by ACTIVE_* above.
LISTENER_ACTIVE_START_HOUR = int(os.getenv("LISTENER_ACTIVE_START_HOUR", "0"))
LISTENER_ACTIVE_END_HOUR = int(os.getenv("LISTENER_ACTIVE_END_HOUR", "24"))
LISTENER_REST_DAYS = tuple(
    int(day.strip())
    for day in os.getenv("LISTENER_REST_DAYS", "").split(",")
    if day.strip()
)

# Fixed CDP port the daemon exposes on its Chromium (`--remote-debugging-port`).
# Localhost-only. The daemon opens it when realtime listening or feed
# collection needs a sibling process to attach to the shared browser context.
LISTENER_CDP_PORT = int(os.getenv("LISTENER_CDP_PORT") or 9222)

# Daily feed collector schedule/tuning. Time is local to
# LINKEDIN_FEED_COLLECTION_TIMEZONE; the default is 5:00 PM Toronto time.
LINKEDIN_FEED_COLLECTION_HOUR = int(os.getenv("LINKEDIN_FEED_COLLECTION_HOUR") or 17)
LINKEDIN_FEED_COLLECTION_MINUTE = int(os.getenv("LINKEDIN_FEED_COLLECTION_MINUTE") or 0)
LINKEDIN_FEED_COLLECTION_TIMEZONE = os.getenv(
    "LINKEDIN_FEED_COLLECTION_TIMEZONE", "America/Toronto",
)
LINKEDIN_FEED_COLLECTION_RETRY_MINUTES = int(
    os.getenv("LINKEDIN_FEED_COLLECTION_RETRY_MINUTES") or 60,
)
LINKEDIN_FEED_COLLECTION_CUTOFF_OVERLAP_MINUTES = int(
    os.getenv("LINKEDIN_FEED_COLLECTION_CUTOFF_OVERLAP_MINUTES") or 1,
)
LINKEDIN_FEED_COLLECTION_MAX_POSTS = int(
    os.getenv("LINKEDIN_FEED_COLLECTION_MAX_POSTS") or 200,
)
LINKEDIN_FEED_COLLECTION_STOP_AFTER_SEEN = int(
    os.getenv("LINKEDIN_FEED_COLLECTION_STOP_AFTER_SEEN") or 15,
)
LINKEDIN_FEED_COLLECTION_SCROLL_PAUSE_SECONDS = float(
    os.getenv("LINKEDIN_FEED_COLLECTION_SCROLL_PAUSE_SECONDS") or 2,
)

# ----------------------------------------------------------------------
# Phone enrichment (multi-provider waterfall — see linkedin/enrichment/)
# ----------------------------------------------------------------------
# Gates ONLY the realtime listener's auto-enqueue of an enrich_phone task on
# every inbound reply. Default OFF — the operator triggers enrichment on
# demand from the Slack select menu instead (see api/slack_enrich.py). The
# EnrichmentWorker itself is NOT gated by this: it always runs, because the
# select menu is always present so enrichment must always be processable.
ENABLE_AUTO_PHONE_ENRICHMENT = os.getenv(
    "ENABLE_AUTO_PHONE_ENRICHMENT", "false",
).strip().lower() in {"1", "true", "yes", "on"}

# Hard cap on a single BetterContact submit→poll cycle. Past this the provider
# returns API_FAILURE and the waterfall fails over to the next provider.
ENRICHMENT_MAX_DURATION_SECONDS = int(os.getenv("ENRICHMENT_MAX_DURATION_SECONDS") or 600)

# Per-request timeout for every enrichment HTTP call.
ENRICHMENT_HTTP_TIMEOUT_SECONDS = int(os.getenv("ENRICHMENT_HTTP_TIMEOUT_SECONDS") or 5)

# Delay between BetterContact async-result polls.
BETTERCONTACT_POLL_INTERVAL_SECONDS = int(os.getenv("BETTERCONTACT_POLL_INTERVAL_SECONDS") or 15)

# How long the daemon waits between checks when its outbound queue is empty
# but the enrichment worker still has tasks in flight — keeps the daemon from
# exiting mid-enrichment (see the queue-empty guard in run_daemon).
ENRICHMENT_WAIT_POLL_SECONDS = int(os.getenv("ENRICHMENT_WAIT_POLL_SECONDS") or 30)

# Provider API keys. Empty disables that provider (it returns API_FAILURE so
# the waterfall fails over). Read here as constants — mirrors LLM_API_KEY —
# so provider modules never call os.getenv directly.
BETTERCONTACT_API_KEY = os.getenv("BETTERCONTACT_API_KEY", "").strip()
LEADMAGIC_API_KEY = os.getenv("LEADMAGIC_API_KEY", "").strip()
PROSPEO_API_KEY = os.getenv("PROSPEO_API_KEY", "").strip()

# ----------------------------------------------------------------------
# Node monitoring (peer-node liveness + degraded-state — see linkedin/monitoring/)
# ----------------------------------------------------------------------
# Peer monitoring needs no third-party service: each daemon is a "node"
# that stamps its DaemonHeartbeat row and watches the other nodes' rows,
# Slack-alerting (ops channel) when a peer goes silent. Coverage needs
# >=2 daemons running — a lone daemon has no peer to watch it.
ENABLE_NODE_MONITOR = os.getenv("ENABLE_NODE_MONITOR", "true").strip().lower() in {
    "1", "true", "yes", "on",
}

# Cadence of the NodeMonitor background thread: how often it re-stamps its
# own heartbeat and scans peers. Runs in its own thread, so it keeps
# beating through the daemon's off-hours sleeps.
MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS") or 300)

# A peer whose heartbeat is older than this is considered down. Must be
# comfortably larger than MONITOR_INTERVAL_SECONDS to absorb one missed beat.
PEER_STALE_MINUTES = int(os.getenv("PEER_STALE_MINUTES") or 15)

# Re-alert cooldown for a persistent problem (peer-down and in-daemon
# degraded conditions alike): alert once, then stay quiet this many hours
# even if still broken, so a long outage spams once, not every cycle.
DEGRADED_REALERT_HOURS = int(os.getenv("DEGRADED_REALERT_HOURS") or 6)

# Consecutive task failures (in-process streak) before the daemon flags
# itself degraded — "alive but every task is failing".
TASK_FAILURE_STREAK_THRESHOLD = int(os.getenv("TASK_FAILURE_STREAK_THRESHOLD") or 5)

# Canonical sender handles that should be making outbound progress on workdays
# (e.g. "Arian,Chuka,Leili"). Empty means infer expected senders from active
# LinkedIn profiles that own active campaigns.
EXPECTED_OUTBOUND_SENDERS = tuple(
    sender.strip()
    for sender in os.getenv("EXPECTED_OUTBOUND_SENDERS", "").split(",")
    if sender.strip()
)

# Activity health: after this many minutes from active-day start, expected
# senders should have at least one outbound ActionLog row for the day.
SENDER_ACTIVITY_GRACE_MINUTES = int(os.getenv("SENDER_ACTIVITY_GRACE_MINUTES") or 60)

# Activity health: a sender with due outbound work and no successful ActionLog
# in this many minutes is considered stuck even when its heartbeat is fresh.
SENDER_ACTIVITY_STALE_MINUTES = int(os.getenv("SENDER_ACTIVITY_STALE_MINUTES") or 90)

# Post-accept follow-up message content lives in `linkedin/icp_messages.json`
# (rigid templates keyed `{sender: {icp: {channel: [...]}}}`, `{first_name}`
# substitution only). The daemon's `linkedin.tasks.follow_up.handle_follow_up`
# resolves the sending operator + the lead's ROLE via
# `linkedin.icp_outbound.classify_role()` and pulls the matching template.
# Previously this section held `POST_ACCEPT_VIDEO_LINK` /
# `POST_ACCEPT_MESSAGE_TEMPLATE` / `FOLLOW_UP_MEDIA_PATH` env vars +
# a parallel LLM-agent fallback (`linkedin.agents.follow_up`); both were
# collapsed into the single ICP-template path on 2026-05-12.

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


def get_daemon_handle() -> str | None:
    """Resolve which LinkedInProfile the daemon should run as.

    Precedence:
      1. `LINKEDIN_USERNAME` env var matches an existing row → return it.
      2. `LINKEDIN_USERNAME` set but NO row matches AND `LINKEDIN_PASSWORD`
         is also set → auto-materialize a User + LinkedInProfile from env
         without claiming any Campaigns. Next boot finds the row directly.
      3. `LINKEDIN_USERNAME` set but NO row matches AND no
         `LINKEDIN_PASSWORD` → refuse to start (return None). The daemon's
         caller exits with a clear message rather than guessing.
      4. `LINKEDIN_USERNAME` UNSET → fall back to the
         `LinkedInProfile.active=True` row (legacy single-account setups).

    Returns the Django `User.username` (the "handle" the rest of the
    daemon's registry / session machinery keys on), not the LinkedIn
    username. Cookies live on disk at
    `data/cookies-<safe_linkedin_username>.json` (per-username, not
    per-row), so flipping between two configured accounts via .env
    doesn't force a re-login as long as each has a cached cookie file.

    Why the silent-fallback path was removed (2026-05-12, Chuka footgun):
    operator flipped `LINKEDIN_USERNAME` to switch the daemon from Arian
    to Chuka, hit a missing-row situation, and the daemon quietly came
    up as Arian (the only `active=True` row). The session went out under
    the wrong identity until startup logs were re-read. Now: if env var
    is set, either the row resolves, the row gets materialized, or the
    daemon refuses to start. No silent identity swap.
    """
    import logging
    logger = logging.getLogger(__name__)

    from linkedin.models import LinkedInProfile

    env_username = os.getenv("LINKEDIN_USERNAME", "").strip()
    if env_username:
        match = (
            LinkedInProfile.objects
            .filter(linkedin_username__iexact=env_username)
            .select_related("user")
            .first()
        )
        if match:
            return match.user.username

        env_password = os.getenv("LINKEDIN_PASSWORD", "").strip()
        if not env_password:
            logger.error(
                "LINKEDIN_USERNAME=%r has no matching LinkedInProfile row "
                "and LINKEDIN_PASSWORD is unset — cannot auto-create. "
                "Either add a row via Django Admin or set "
                "LINKEDIN_PASSWORD in .env so the daemon can materialize "
                "the row itself.",
                env_username,
            )
            return None

        return _materialize_profile_from_env(env_username, env_password)

    return get_first_active_profile_handle()


def _materialize_profile_from_env(linkedin_username: str, linkedin_password: str) -> str:
    """Create User + LinkedInProfile from env credentials. Used by
    get_daemon_handle when LINKEDIN_USERNAME points at an account that
    doesn't have a DB row yet.

    First-run onboarding for a new operator without making them touch
    Django Admin — matches the UX of the backfill scripts where editing
    .env is the only thing the operator does to switch accounts.

    `legal_accepted=True` and `newsletter_processed=True` are stamped to
    skip the interactive prompts in `ensure_onboarding` — the operator
    accepted these implicitly by configuring the env vars, and an
    interactive prompt would block an unattended daemon boot.
    """
    import logging
    logger = logging.getLogger(__name__)
    from django.contrib.auth.models import User
    from linkedin.models import LinkedInProfile

    # Django username = local-part of the email with dots stripped.
    # The registry / session machinery keys on this string; making it
    # deterministic from the linkedin email means the same env value
    # always resolves to the same handle across machines.
    handle = linkedin_username.split("@", 1)[0].replace(".", "")

    user, user_created = User.objects.get_or_create(
        username=handle,
        defaults={"email": linkedin_username},
    )
    LinkedInProfile.objects.create(
        user=user,
        linkedin_username=linkedin_username,
        linkedin_password=linkedin_password,
        active=True,
        legal_accepted=True,
        newsletter_processed=True,
    )

    logger.info(
        "Auto-materialized LinkedInProfile for %s (handle=%s, user_created=%s). "
        "No campaigns were auto-assigned.",
        linkedin_username, handle, user_created,
    )
    return handle
