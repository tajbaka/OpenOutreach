"""Prepare/apply Codex review decisions for collected LinkedIn feed posts."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = (
        "Export collected LinkedIn feed posts for Codex review, or apply a "
        "Codex-produced decision JSON file. This command does not call an LLM."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, help="Optional cap for manual/debug exports.")
        parser.add_argument("--since-days", type=int, help="Only include posts seen in the last N days.")
        parser.add_argument("--reanalyze", action="store_true", help="Include posts that already have analysis.")
        parser.add_argument("--output", help="Write review queue JSON to this path instead of stdout.")
        parser.add_argument("--apply-json", help="Apply Codex decision JSON from this path.")
        parser.add_argument("--no-slack", action="store_true", help="When applying decisions, do not send Slack alerts.")

    def handle(self, *args, **opts):
        from linkedin.feed_analysis import (
            load_decisions,
            mark_feed_post_slack_notified,
            save_feed_post_analysis,
            serialize_posts_for_codex,
            should_notify_feed_post,
        )
        from linkedin.models import LinkedInFeedPost
        from linkedin.notifications.slack import notify_feed_intent_signal

        if opts.get("apply_json"):
            decisions = load_decisions(opts["apply_json"])
            applied = 0
            alerts = 0
            for post_id, result in decisions:
                post = LinkedInFeedPost.objects.get(pk=post_id)
                save_feed_post_analysis(post, result)
                applied += 1
                if result.should_alert and should_notify_feed_post(post) and not opts["no_slack"]:
                    if notify_feed_intent_signal(post=post):
                        mark_feed_post_slack_notified(post)
                        alerts += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"Applied {applied} Codex feed decisions; Slack alerts sent: {alerts}.",
                ),
            )
            return

        limit = opts.get("limit")
        if limit is not None and limit <= 0:
            raise CommandError("--limit must be positive.")

        qs = LinkedInFeedPost.objects.prefetch_related("observations").order_by("-last_seen_at")
        if not opts["reanalyze"]:
            qs = qs.filter(analyzed_at__isnull=True)
        if opts.get("since_days") is not None:
            if opts["since_days"] <= 0:
                raise CommandError("--since-days must be positive.")
            qs = qs.filter(last_seen_at__gte=timezone.now() - timedelta(days=opts["since_days"]))

        posts = list(qs[:limit]) if limit is not None else list(qs)
        payload = serialize_posts_for_codex(posts)
        encoded = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        if opts.get("output"):
            path = Path(opts["output"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(encoded)
            self.stdout.write(f"Wrote Codex feed review queue to {path}")
        else:
            self.stdout.write(encoded)
