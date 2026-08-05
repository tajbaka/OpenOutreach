"""Run a bounded feed-comment-only browser session for one LinkedIn sender."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from linkedin.browser.registry import get_or_create_session
from linkedin.models import LinkedInFeedComment, LinkedInProfile, Task
from linkedin.operators import resolve_operator
from linkedin.setup.self_profile import ensure_self_profile
from linkedin.tasks.feed_comment import handle_feed_comment


class Command(BaseCommand):
    help = (
        "Run one sender-scoped feed_comment Task without claiming connect, "
        "follow-up, sweep, discovery, status, or manual-reply work."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--handle",
            required=True,
            help="Django username for the LinkedInProfile to run.",
        )

    def handle(self, *args, **options):
        try:
            profile = LinkedInProfile.objects.select_related("user").get(
                active=True,
                user__username=options["handle"],
            )
        except LinkedInProfile.DoesNotExist as exc:
            raise CommandError("No matching active LinkedInProfile found.") from exc

        operator = resolve_operator(profile.linkedin_username)
        campaign_ids = list(
            profile.user.campaigns.filter(status="active").values_list("pk", flat=True),
        )
        if Task.objects.seconds_to_next(
            operator=operator,
            campaign_ids=campaign_ids,
            task_types={Task.TaskType.FEED_COMMENT},
        ) is None:
            raise CommandError(f"No due feed_comment Task is available for {operator}.")

        session = get_or_create_session(handle=profile.user.username)
        session.campaign = (
            session.campaigns.filter(status="active").first()
            or session.campaigns.first()
        )
        if session.campaign is None:
            session.close()
            raise CommandError(f"No campaign is available for {operator}.")

        task = None
        try:
            session.ensure_browser()
            ensure_self_profile(session)
            task = Task.objects.claim_next(
                operator=operator,
                campaign_ids=campaign_ids,
                task_types={Task.TaskType.FEED_COMMENT},
            )
            if task is None:
                raise CommandError(
                    f"The due feed_comment Task for {operator} was claimed elsewhere.",
                )
            try:
                handle_feed_comment(task, session, qualifiers={})
            except Exception as exc:
                task.mark_failed(str(exc))
                raise
            task.mark_completed()
        finally:
            session.close()

        ledger = LinkedInFeedComment.objects.filter(task=task).first()
        ledger_status = ledger.status if ledger is not None else "missing"
        self.stdout.write(
            self.style.SUCCESS(
                f"Completed feed_comment Task {task.pk} for {operator}; "
                f"ledger_status={ledger_status}.",
            ),
        )
