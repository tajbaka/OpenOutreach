#!/usr/bin/env python
"""OpenOutreach management entrypoint.

Usage:
    python manage.py              # run only the LinkedIn daemon child/diagnostic
    python manage.py runserver    # Django Admin at http://localhost:8000/admin/
    python manage.py migrate      # run Django migrations
    python manage.py createsuperuser
"""
import logging
import os
from pathlib import Path
import sys
import warnings

# langchain-openai stores a Pydantic model in a dict-typed field, triggering
# a harmless serialization warning on every structured-output call.
warnings.filterwarnings("ignore", message="Pydantic serializer warnings")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")

import django
django.setup()

from linkedin.management.setup_crm import setup_crm

logging.getLogger().handlers.clear()
logging.basicConfig(
    level=logging.DEBUG,
    format="%(message)s",
)

# Suppress noisy third-party loggers
for _name in ("urllib3", "httpx", "langchain", "openai", "playwright",
              "httpcore", "fastembed", "huggingface_hub", "filelock"):
    logging.getLogger(_name).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _verify_sender_templates(running_profile):
    """Fail fast if the daemon's own LinkedIn account has no template block.

    Outbound templates in `linkedin/icp_messages.json` are keyed by sender
    (operator handle). Without a block for the account this daemon runs
    as, follow-up DMs raise `SheetsError` and re-enqueue every 24h
    forever, and connection notes silently go note-less. Catch it here
    with one clear message instead of a slow per-lead failure. Other
    active accounts only warn — they are a different daemon's problem,
    not a reason to block this one.
    """
    from linkedin.icp_outbound import known_senders, missing_sender_block
    from linkedin.models import LinkedInProfile

    missing = missing_sender_block(running_profile.linkedin_username)
    if missing:
        logger.error(
            "LinkedIn account %r resolves to sender %r, which has no template "
            "block in linkedin/icp_messages.json (known: %s). Add a block for "
            "%r (and, ideally, an alias in linkedin/operators.py) before "
            "running the daemon.",
            running_profile.linkedin_username, missing,
            sorted(known_senders()), missing,
        )
        sys.exit(1)

    for prof in LinkedInProfile.objects.filter(active=True).exclude(
        pk=running_profile.pk
    ):
        other = missing_sender_block(prof.linkedin_username)
        if other:
            logger.warning(
                "Active LinkedIn account %r (sender %r) has no template block "
                "in linkedin/icp_messages.json — its daemon will crash-loop "
                "follow-ups until a block is added.",
                prof.linkedin_username, other,
            )


def _run_daemon():
    from linkedin.api.newsletter import ensure_newsletter_subscription
    from linkedin.daemon import run_daemon
    from linkedin.db.urls import public_id_to_url
    from linkedin.setup.self_profile import ensure_self_profile
    from linkedin.setup.gdpr import apply_gdpr_newsletter_override
    from linkedin.onboarding import ensure_onboarding
    from linkedin.browser.registry import get_or_create_session

    ensure_onboarding()

    from linkedin.conf import LLM_API_KEY, get_daemon_handle

    if not LLM_API_KEY:
        logger.error("LLM_API_KEY is required. Set it in .env or environment.")
        sys.exit(1)

    handle = get_daemon_handle()
    if handle is None:
        logger.error(
            "No LinkedIn account to run as. Set LINKEDIN_USERNAME in .env to "
            "match an existing LinkedInProfile row, or activate one via the "
            "Django Admin."
        )
        sys.exit(1)

    session = get_or_create_session(handle=handle)

    # Set default campaign (first non-freemium, or first available) for startup tasks
    first_campaign = session.campaigns.filter(is_freemium=False).first() or session.campaigns.first()
    if first_campaign is None:
        logger.error("No campaigns found for this user.")
        sys.exit(1)
    session.campaign = first_campaign

    # Outbound templates are keyed by sender — verify this account (and
    # warn on any other active account) before launching a browser.
    _verify_sender_templates(session.linkedin_profile)

    session.ensure_browser()
    profile = ensure_self_profile(session)

    if not session.linkedin_profile.newsletter_processed:
        country_code = profile.get("country_code") if profile else None
        apply_gdpr_newsletter_override(session, country_code)
        linkedin_url = public_id_to_url(profile["public_identifier"]) if profile else None
        ensure_newsletter_subscription(session, linkedin_url=linkedin_url)
        session.linkedin_profile.newsletter_processed = True
        session.linkedin_profile.save(update_fields=["newsletter_processed"])

    run_daemon(session)


def _ensure_db():
    from django.core.management import call_command
    call_command("migrate", "--no-input", verbosity=0)
    setup_crm()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # No arguments → run the daemon. Top-level Exception goes to Slack
        # before re-raising so an operator sees the crash even when the
        # process logs scroll off. KeyboardInterrupt / SystemExit pass
        # through untouched.
        #
        # Startup integrity checks run first, before migrations: the update
        # check may exit so the process restarts on freshly pulled code.
        from linkedin.version_check import check_for_updates
        from linkedin.env_check import check_env_vars

        if os.environ.get("OPENOUTREACH_SUPERVISED") != "1":
            check_for_updates()
        check_env_vars()

        from linkedin.conf import ROOT_DIR
        from linkedin.notifications.slack import notify_on_error
        from linkedin.single_instance import (
            SingleInstanceGuard,
            matches_top_level_manage_daemon,
        )

        daemon_guard = SingleInstanceGuard(
            pidfile=Path("data") / "daemon.pid",
            marker="manage.py daemon",
            logger=logger,
            match_process=lambda proc: matches_top_level_manage_daemon(
                proc,
                root_dir=ROOT_DIR,
            ),
        )
        daemon_guard.acquire()
        try:
            _ensure_db()
            with notify_on_error("daemon:startup"):
                _run_daemon()
        finally:
            daemon_guard.release()
    else:
        # Auto-migrate before starting the admin server
        if sys.argv[1] == "runserver":
            _ensure_db()
        # Django management command (runserver, migrate, createsuperuser, etc.)
        from django.core.management import execute_from_command_line
        execute_from_command_line(sys.argv)
