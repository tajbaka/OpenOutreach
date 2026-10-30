"""Portable Windows Codex bridge. No browser launch and no implicit send."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from linkedin import rb2b
from linkedin.models import RB2BVisitor


class Command(BaseCommand):
    help = "Review RB2B intake, register CRM contacts, reserve IAB sends and record outcomes. Preview by default."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["export", "review", "begin", "finish", "replies", "notify", "migrate-discovery", "preflight"])
        parser.add_argument("--id", type=int)
        parser.add_argument("--input", type=Path)
        parser.add_argument("--output", type=Path)
        parser.add_argument("--token")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        action = opts["action"]
        data = json.loads(opts["input"].read_text(encoding="utf-8")) if opts["input"] else {}
        if action in {"review", "begin", "finish", "replies"} and (not opts["id"] or not opts["input"]):
            raise CommandError("--id and --input are required")
        if action == "notify" and not opts["apply"]:
            raise CommandError("notify requires --apply; it only retries Slack delivery, never invitations")
        try:
            with transaction.atomic():
                if action == "export":
                    result = rb2b.export_queue()
                elif action == "preflight":
                    profile = rb2b.owner()
                    rb2b.worker_ready()
                    result = {"sender": "Arian", "profile_id": profile.pk, "coordination_ready": True,
                              "can_connect": profile.can_execute("connect")}
                elif action == "migrate-discovery":
                    result = rb2b.migrate_discovery()
                elif action == "notify":
                    result = {"delivered": rb2b.flush_notifications()}
                elif action == "replies":
                    result = {"new_messages": rb2b.ingest_replies(opts["id"], data)}
                else:
                    if action == "review":
                        row = rb2b.review(opts["id"], data)
                    elif action == "begin":
                        row = rb2b.begin(opts["id"], data)
                    else:
                        row = rb2b.finish(opts["id"], opts["token"], data)
                    result = {"id": row.pk, "status": row.status, "lead_id": row.lead_id, "note": row.note,
                              "attempt_token": str(row.attempt_token) if opts["apply"] and row.attempt_token else None}
                if not opts["apply"] and action not in {"export", "preflight"}:
                    transaction.set_rollback(True)
            response = {"applied": bool(opts["apply"]), "result": result}
        except rb2b.RB2BHeld as exc:
            # Persist the actionable hold notification only after the attempted
            # mutation has rolled back. No failed begin consumes a send budget.
            if opts["apply"] and opts["id"]:
                row = RB2BVisitor.objects.get(pk=opts["id"])
                rb2b._notify(row, "held", str(exc))
            raise CommandError(str(exc)) from None
        except Exception as exc:
            # A DB outage cannot write an outbox row. Post only a safe class
            # name to the configured RB2B channel; never raw exceptions/inputs.
            import os
            from linkedin.notifications.slack import _slack_api
            channel = os.getenv("RB2B_SLACK_CHANNEL_ID", "")
            if opts["apply"] and channel:
                _slack_api("chat.postMessage", {"channel": channel,
                    "text": f"RB2B {action} failed ({type(exc).__name__}). Work needs review; no automatic send retry."}, "rb2b-workflow-failure")
            raise CommandError(f"RB2B {action} failed ({type(exc).__name__}); inspect private evidence before retrying.") from None
        text = json.dumps(response, indent=2, ensure_ascii=False, default=str) + "\n"
        if opts["output"]:
            opts["output"].parent.mkdir(parents=True, exist_ok=True)
            opts["output"].write_text(text, encoding="utf-8")
            opts["output"].chmod(0o600)
            self.stdout.write(f"Saved private receipt: {opts['output']}")
        else:
            self.stdout.write(text)
