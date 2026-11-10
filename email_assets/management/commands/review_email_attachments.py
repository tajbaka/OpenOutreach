"""Show which file each recipient will get on which email step of a campaign."""
import csv
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from email_assets.review import OK_STATUSES, review_campaign_attachments


class Command(BaseCommand):
    help = (
        "Read-only pre-send review of campaign email attachments: per recipient "
        "and step, the asset, its approved hash and whether it will send or be "
        "held (and why). --check-files also confirms each approved file is "
        "still readable and unchanged at the provider."
    )

    def add_arguments(self, parser):
        parser.add_argument("--campaign", type=int, required=True)
        parser.add_argument("--check-files", action="store_true")
        parser.add_argument("--output", default="", help="Write all rows to a .json or .csv file.")
        parser.add_argument("--limit", type=int, default=50, help="Rows to print (0 = all).")
        parser.add_argument("--problems", action="store_true", help="Print only rows that would be held.")

    def handle(self, *args, **options):
        try:
            report = review_campaign_attachments(
                campaign_id=options["campaign"],
                check_files=options["check_files"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from None

        self.stdout.write(
            f"Campaign {report.campaign_id}: {report.campaign_name} ({report.campaign_status})"
        )
        if not report.steps:
            self.stdout.write("No email step in this campaign declares an attachment.")
        for step in report.steps:
            self.stdout.write(
                f"  {step['step_key']} (step {step['step_index']}, version "
                f"{step['message_version_id']}): {', '.join(step['attachments'])}"
            )
        counts = report.counts
        if counts:
            self.stdout.write(
                "Rows by status: " + ", ".join(f"{status}={count}" for status, count in counts.items())
            )
        rows = report.rows
        if options["problems"]:
            rows = [row for row in rows if row.status not in OK_STATUSES]
        limit = options["limit"]
        shown = rows if limit <= 0 else rows[:limit]
        for row in shown:
            target = f"{row.asset_key} {row.filename} {row.sha256[:12]}" if row.asset_key else "-"
            line = (
                f"Deal {row.deal_id:<7} {row.lead_name[:28]:<28} {row.company[:24]:<24} "
                f"{row.step_key:<10} {row.delivery_status:<11} {row.status:<22} {target}"
            )
            if row.reason:
                line += f"  | {row.reason}"
            self.stdout.write(line)
        if len(shown) < len(rows):
            self.stdout.write(f"... {len(rows) - len(shown)} more row(s); use --limit 0 or --output.")
        for note in report.notes:
            self.stdout.write(f"Note: {note}")

        if options["output"]:
            target = Path(options["output"]).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.suffix.lower() == ".csv":
                with target.open("w", newline="", encoding="utf-8") as handle:
                    fields = list(report.rows[0].as_dict()) if report.rows else ["status"]
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    for row in report.rows:
                        writer.writerow(row.as_dict())
            else:
                target.write_text(json.dumps(report.as_dict(), indent=2, default=str) + "\n", encoding="utf-8")
            self.stdout.write(f"Wrote {len(report.rows)} row(s) to {target}")
