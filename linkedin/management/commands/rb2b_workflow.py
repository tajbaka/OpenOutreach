"""Portable Windows Codex bridge. Never opens a browser and never sends.

Qualified visitors are queued for the existing Arian daemon, which performs
the live LinkedIn checks and the single connection request.
"""
import json
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from linkedin import rb2b
from linkedin.models import DaemonHeartbeat, RB2BVisitor, Task


class Command(BaseCommand):
    help = "Review RB2B intake, register CRM contacts and queue worker connection requests. Preview by default."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["export", "review", "notify", "migrate-discovery", "preflight"])
        parser.add_argument("--id", type=int)
        parser.add_argument("--input", type=Path)
        parser.add_argument("--output", type=Path)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        action = opts["action"]
        data = json.loads(opts["input"].read_text(encoding="utf-8")) if opts["input"] else {}
        if action == "review" and (not opts["id"] or not opts["input"]):
            raise CommandError("--id and --input are required")
        if action == "notify" and not opts["apply"]:
            raise CommandError("notify requires --apply; it only retries Slack delivery, never invitations")
        try:
            with transaction.atomic():
                if action == "export":
                    result = rb2b.export_queue()
                elif action == "preflight":
                    result = preflight()
                elif action == "migrate-discovery":
                    result = rb2b.migrate_discovery()
                elif action == "notify":
                    result = {"delivered": rb2b.flush_notifications()}
                else:
                    row = rb2b.review(opts["id"], data)
                    task = Task.objects.filter(task_type=Task.TaskType.RB2B_CONNECT, status=Task.Status.PENDING,
                                               payload__visitor_id=row.pk).first()
                    result = {"id": row.pk, "status": row.status, "lead_id": row.lead_id, "note": row.note,
                              "connect_task_id": task.pk if task and opts["apply"] else None}
                if not opts["apply"] and action not in {"export", "preflight"}:
                    transaction.set_rollback(True)
            response = {"applied": bool(opts["apply"]), "result": result}
        except rb2b.RB2BHeld as exc:
            # Persist the actionable hold notification only after the attempted
            # mutation has rolled back.
            if opts["apply"] and opts["id"]:
                row = RB2BVisitor.objects.get(pk=opts["id"])
                rb2b._notify(row, "held", str(exc))
                rb2b.flush_notifications()
            raise CommandError(str(exc)) from None
        except Exception as exc:
            # A DB outage cannot write an outbox row. Post only a safe class
            # name to the configured RB2B channel; never raw exceptions/inputs.
            import os
            from linkedin.notifications.slack import _slack_api
            channel = os.getenv("RB2B_SLACK_CHANNEL_ID", "")
            if opts["apply"] and channel:
                _slack_api("chat.postMessage", {"channel": channel,
                    "text": f"RB2B {action} failed ({type(exc).__name__}). Work needs review; nothing was sent."}, "rb2b-workflow-failure")
            raise CommandError(f"RB2B {action} failed ({type(exc).__name__}); inspect private evidence before retrying.") from None
        if opts["apply"] and action == "review":
            # Holds post to #rb2b-v2 now; the worker posts send outcomes.
            rb2b.flush_notifications()
        text = json.dumps(response, indent=2, ensure_ascii=False, default=str) + "\n"
        if opts["output"]:
            opts["output"].parent.mkdir(parents=True, exist_ok=True)
            opts["output"].write_text(text, encoding="utf-8")
            opts["output"].chmod(0o600)
            self.stdout.write(f"Saved private receipt: {opts['output']}")
        else:
            self.stdout.write(text)


def preflight():
    """Whether queued work will actually be sent: owner, live worker, config."""
    profile = rb2b.owner()
    beat = DaemonHeartbeat.objects.filter(sender=rb2b.SENDER).first()
    fresh = bool(beat and beat.last_alive and timezone.now() - beat.last_alive <= timedelta(minutes=5))
    state = beat.rb2b_coordination if beat else {}
    return {
        "sender": rb2b.SENDER,
        "profile_id": profile.pk,
        "worker_alive": fresh,
        "worker_connect_ready": fresh and state.get("protocol") == rb2b.PROTOCOL and state.get("connect_ready") is True,
        "can_connect_now": profile.can_execute("connect"),
        "pending_connect_tasks": Task.objects.filter(task_type=Task.TaskType.RB2B_CONNECT,
                                                     status=Task.Status.PENDING).count(),
    }
