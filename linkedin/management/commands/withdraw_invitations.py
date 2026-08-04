"""Review and explicitly withdraw a dated batch of project-sent invitations."""
from __future__ import annotations

import argparse
import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from linkedin.actions.standalone_session import StandaloneLinkedInSession
from linkedin.api.client import PlaywrightLinkedinAPI
from linkedin.conf import ACTIVE_TIMEZONE
from linkedin.exceptions import (
    AuthenticationError,
    InvitationWithdrawalError,
    InvitationWithdrawalIdentityError,
)
from linkedin.invitation_withdrawal import (
    BoundStandaloneSession,
    WithdrawalPlan,
    apply_withdrawal_batch,
    assert_no_daemon_conflict,
    build_withdrawal_plan,
    sender_advisory_lock,
)
from linkedin.operators import resolve_operator

ACCOUNTS = {
    "primary": ("LINKEDIN_USERNAME", "LINKEDIN_PASSWORD"),
    "backfill": (
        "BACKFILL_LINKEDIN_USERNAME",
        "BACKFILL_LINKEDIN_PASSWORD",
    ),
}


def _date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "--before must be an ISO date in YYYY-MM-DD format"
        ) from error


def _configured_account(account: str) -> tuple[str, str, str]:
    username_env, password_env = ACCOUNTS[account]
    username = os.getenv(username_env, "").strip()
    password = os.getenv(password_env, "")
    if not username or not password:
        raise CommandError(
            f"--account {account!r} is not configured; set "
            f"{username_env} and {password_env}"
        )
    return username, username_env, password_env


def _profile_for_operator(operator: str):
    from linkedin.models import LinkedInProfile

    matches = []
    for profile in LinkedInProfile.objects.select_related("user"):
        user = profile.user
        aliases = {
            profile.linkedin_username,
            user.username,
            user.email,
            user.first_name,
            f"{user.first_name} {user.last_name}".strip(),
        }
        if operator in {
            resolve_operator(alias)
            for alias in aliases
            if (alias or "").strip()
        }:
            matches.append(profile)
    if not matches:
        raise CommandError(
            f"No LinkedInProfile maps to configured operator {operator!r}"
        )
    if len(matches) > 1:
        ids = ", ".join(str(profile.pk) for profile in matches)
        raise CommandError(
            f"Multiple LinkedInProfile rows map to {operator!r}: {ids}"
        )
    return matches[0]


def _authenticated_display_name(session) -> str:
    api = PlaywrightLinkedinAPI(session=session)
    profile, _ = api.get_profile(public_identifier="me")
    if not profile:
        raise InvitationWithdrawalIdentityError(
            "Could not fetch the authenticated LinkedIn self-profile"
        )
    name = (
        profile.get("full_name")
        or f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    )
    if not name:
        raise InvitationWithdrawalIdentityError(
            f"Authenticated LinkedIn self-profile returned no name: {profile!r}"
        )
    return name


def _cutoff_for_date(before: date) -> datetime:
    boundary = datetime.combine(before, time.min)
    return timezone.make_aware(boundary, timezone=ZoneInfo(ACTIVE_TIMEZONE))


class Command(BaseCommand):
    help = (
        "Dry-run CRM attribution or explicitly withdraw LinkedIn invitations "
        "by visible sent-date."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--account",
            choices=sorted(ACCOUNTS),
            required=True,
            help="Configured LinkedIn account slot: primary or backfill.",
        )
        parser.add_argument(
            "--since",
            type=_date_argument,
            help=(
                "Optional inclusive invitation-sent date boundary in "
                "ACTIVE_TIMEZONE (YYYY-MM-DD)."
            ),
        )
        parser.add_argument(
            "--before",
            type=_date_argument,
            required=True,
            help=(
                "Exclusive invitation-sent date boundary in ACTIVE_TIMEZONE "
                "(YYYY-MM-DD)."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            help=(
                "Optional maximum confirmed withdrawals. Omit to withdraw every "
                "visible date-eligible invitation LinkedIn exposes before the "
                "time cap."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Perform verified LinkedIn withdrawals. Omit for DB-only dry-run.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit is not None and limit <= 0:
            raise CommandError("--limit must be greater than zero")

        account = options["account"]
        username, username_env, password_env = _configured_account(account)
        operator = resolve_operator(username)
        linkedin_profile = _profile_for_operator(operator)
        since = (
            _cutoff_for_date(options["since"])
            if options["since"] is not None
            else None
        )
        cutoff = _cutoff_for_date(options["before"])
        if since is not None and since >= cutoff:
            raise CommandError("--since must be earlier than --before")

        if not options["apply"]:
            plan = build_withdrawal_plan(
                linkedin_profile=linkedin_profile,
                operator=operator,
                since=since,
                cutoff=cutoff,
                limit=limit,
            )
            self._print_plan(plan, account=account, username=username, detailed=True)
            self.stdout.write(
                self.style.WARNING(
                    "[dry-run] no LinkedIn login, withdrawal, or database write occurred"
                )
            )
            return

        try:
            with sender_advisory_lock(operator):
                assert_no_daemon_conflict(
                    operator=operator,
                    account_username=username,
                )
                plan = build_withdrawal_plan(
                    linkedin_profile=linkedin_profile,
                    operator=operator,
                    since=since,
                    cutoff=cutoff,
                    limit=limit,
                )
                self._print_plan(
                    plan,
                    account=account,
                    username=username,
                    detailed=options["verbosity"] >= 2,
                )
                if not plan.candidates:
                    self.stdout.write("No invitations are eligible for this batch.")
                    return

                self.stdout.write(
                    self.style.WARNING(
                        "--apply enabled: starting a visible LinkedIn session"
                    )
                )
                with StandaloneLinkedInSession(
                    env_username=username_env,
                    env_password=password_env,
                    label=f"withdraw invitations ({account})",
                    use_persistent_profile=True,
                ) as browser_session:
                    authenticated_name = _authenticated_display_name(browser_session)
                    authenticated_operator = resolve_operator(authenticated_name)
                    if authenticated_operator != operator:
                        raise InvitationWithdrawalIdentityError(
                            f"Requested {operator!r}, but LinkedIn authenticated as "
                            f"{authenticated_name!r} ({authenticated_operator!r})"
                        )
                    self.stdout.write(
                        f"Authenticated LinkedIn identity: {authenticated_name!r} "
                        f"({authenticated_operator})"
                    )
                    session = BoundStandaloneSession(
                        browser_session,
                        linkedin_profile,
                    )
                    result = apply_withdrawal_batch(
                        session=session,
                        candidates=plan.candidates,
                        linkedin_profile=linkedin_profile,
                        operator=operator,
                        since=since,
                        cutoff=cutoff,
                        withdrawal_limit=limit,
                    )
        except (AuthenticationError, InvitationWithdrawalError) as error:
            raise CommandError(str(error)) from error

        self.stdout.write(
            self.style.SUCCESS(
                "Batch complete: "
                f"planned={result.planned}, accepted={result.accepted}, "
                f"withdrawn={result.withdrawn}, not_pending={result.not_pending}, "
                f"skipped={result.skipped}"
            )
        )

    def _print_plan(
        self,
        plan: WithdrawalPlan,
        *,
        account: str,
        username: str,
        detailed: bool,
    ) -> None:
        local_zone = ZoneInfo(ACTIVE_TIMEZONE)
        local_cutoff = timezone.localtime(plan.cutoff, timezone=local_zone)
        self.stdout.write(
            f"Account: {account} ({username}) -> operator={plan.operator}"
        )
        self.stdout.write(
            "Exclusive cutoff: "
            f"{local_cutoff.isoformat()} ({ACTIVE_TIMEZONE}); "
            f"only invitations sent before {local_cutoff.date().isoformat()}"
        )
        if plan.since is not None:
            local_since = timezone.localtime(
                plan.since,
                timezone=local_zone,
            )
            self.stdout.write(
                "Inclusive lower boundary: "
                f"{local_since.isoformat()} ({ACTIVE_TIMEZONE}); "
                f"only invitations sent on/after "
                f"{local_since.date().isoformat()}"
            )
        self.stdout.write(
            f"Pending scanned: {plan.pending_total}; "
            f"positively attributed: {plan.proven_total}; "
            f"eligible candidate pool: {plan.eligible_total}; "
            f"planned scan pool: {len(plan.candidates)}/all eligible; "
            f"withdrawal target: "
            f"{plan.limit if plan.limit is not None else 'all live matches'}"
        )
        if plan.exclusions:
            rendered = ", ".join(
                f"{reason}={count}" for reason, count in plan.exclusions
            )
            self.stdout.write(f"Exclusions: {rendered}")
        if plan.candidates:
            newest = timezone.localtime(
                plan.candidates[0].sent_at,
                timezone=local_zone,
            )
            oldest = timezone.localtime(
                plan.candidates[-1].sent_at,
                timezone=local_zone,
            )
            self.stdout.write(
                f"Selected date range: {oldest.isoformat()} -> {newest.isoformat()}"
            )
            if not detailed:
                self.stdout.write(
                    "Exact planned batch omitted at normal apply verbosity; "
                    "use -v 2 to print CRM candidate rows."
                )
                return
            self.stdout.write("Exact planned batch (newest before cutoff first):")
        for index, candidate in enumerate(plan.candidates, 1):
            local_sent = timezone.localtime(
                candidate.sent_at,
                timezone=local_zone,
            )
            self.stdout.write(
                f"  {index:02d}. deal={candidate.deal_id} "
                f"sent={local_sent.isoformat()} evidence={candidate.evidence} "
                f"lead={candidate.lead_name!r} campaign={candidate.campaign_name!r} "
                f"url={candidate.profile_url}"
            )
