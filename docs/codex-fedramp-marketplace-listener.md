# Codex FedRAMP Marketplace Listener

This workflow watches the official FedRAMP marketplace JSON repository for the two GTM signals that should create immediate account research:

- a legacy product newly entering FedRAMP Ready, routed to `Rev5 Ready`
- a Program-path product newly entering Initial Implementation, routed to `20x Pipeline`

The collector performs deterministic source validation, snapshot comparison, and event deduplication. Codex reviews only the new target transitions, adds concise context and a recommended action, and the apply command posts approved high-priority alerts to the same `SLACK_HIGH_SIGNAL_URL` used by LinkedIn feed analysis.

## Source data

- Changelog: `https://raw.githubusercontent.com/FedRAMP/marketplace-fedramp-gov-data/main/fedramp-status-changelog.json`
- Full snapshot: `https://raw.githubusercontent.com/FedRAMP/marketplace-fedramp-gov-data/main/data.json`
- Repository: `https://github.com/FedRAMP/marketplace-fedramp-gov-data`

The changelog is authoritative for transition events. The snapshot is a second detection path for a product whose current status changes to `FedRAMP Ready` without a new changelog row.

## Machine setup

The periodic runner needs the OpenOutreach checkout, its Python environment, database access, and the existing high-signal Slack webhook:

```dotenv
DATABASE_URL=postgresql://...
SLACK_HIGH_SIGNAL_URL=https://hooks.slack.com/services/...
```

The default source URLs and 45-second fetch timeout work without additional configuration. Optional overrides are:

```dotenv
FEDRAMP_MARKETPLACE_CHANGELOG_URL=https://raw.githubusercontent.com/FedRAMP/marketplace-fedramp-gov-data/main/fedramp-status-changelog.json
FEDRAMP_MARKETPLACE_DATA_URL=https://raw.githubusercontent.com/FedRAMP/marketplace-fedramp-gov-data/main/data.json
FEDRAMP_MARKETPLACE_FETCH_TIMEOUT_SECONDS=45
```

After pulling the code on the runner:

```bash
.venv/bin/python manage.py migrate
```

Use the same shared `DATABASE_URL` on every runner. The source baseline, unique event keys, Codex analysis, and Slack notification timestamps live in the database, so moving the periodic workflow to another machine does not reset the listener or repost old signals.

## First run

Safe baseline with no historical alert flood:

```bash
.venv/bin/python manage.py collect_fedramp_marketplace
```

To intentionally seed recent transitions on the first run:

```bash
.venv/bin/python manage.py collect_fedramp_marketplace --lookback-days 7
```

Do not use a broad lookback casually. Historical `FRR` rows include hundreds of old Ready transitions.

## Periodic Codex workflow

Run the collection after the marketplace repository's daily update window, ideally once daily after 5:00 AM America/New_York.

```bash
.venv/bin/python manage.py collect_fedramp_marketplace
.venv/bin/python manage.py analyze_fedramp_marketplace \
  --output artifacts/marketplace/codex-review.json
```

Codex then reads:

```text
artifacts/marketplace/codex-review.json
```

If `signals` is empty, stop successfully. Otherwise, Codex writes:

```text
artifacts/marketplace/codex-decisions.json
```

Decision shape:

```json
{
  "decisions": [
    {
      "signal_id": 123,
      "is_relevant": true,
      "should_alert": true,
      "priority": "urgent",
      "relevance_reason": "Acme entered Initial Implementation on the Program path, making this a fresh 20x build motion.",
      "suggested_action": "Add Acme to the 20x Pipeline account list and identify the security, compliance, or technical founder owner."
    }
  ]
}
```

Allowed priorities are `none`, `low`, `medium`, `high`, and `urgent`. Slack posting requires all of the following:

- `is_relevant` is true
- `should_alert` is true
- priority is `high` or `urgent`
- `relevance_reason` is non-empty
- the signal has not already been Slack-notified

Apply the decisions:

```bash
.venv/bin/python manage.py analyze_fedramp_marketplace \
  --apply-json artifacts/marketplace/codex-decisions.json
```

After every scheduled run, post exactly one rollup to the regular ops Slack
channel. Use `--status success` after a reviewed/apply run, `--status empty`
when the queue is empty, and `--status failed` when collection, export, review,
or apply fails:

```bash
.venv/bin/python manage.py notify_fedramp_marketplace_status \
  --status success \
  --new-source-entries 0 \
  --target-transitions 0 \
  --reviewed-decisions 0 \
  --slack-alerts 0 \
  --detail "Daily listener completed."
```

This status command posts to `SLACK_WEBHOOK_URL`; Marketplace signal alerts
remain isolated on the workflow-scoped `SLACK_HIGH_SIGNAL_URL`.

Use `--no-slack` to test decision validation and persistence without notifying the channel.

## Prompt for a scheduled Codex task

Use this as the scheduled task instruction on the other machine:

```text
In /absolute/path/to/OpenOutreach, run the FedRAMP marketplace listener workflow in docs/codex-fedramp-marketplace-listener.md. Collect the official sources, export artifacts/marketplace/codex-review.json, and inspect every signal. Write artifacts/marketplace/codex-decisions.json using the queue schema. Treat a real external Program-path Initial Implementation entrant as urgent and a real external Rev5 Ready entrant as high priority. Exclude Boundera itself, duplicates, test data, and clearly noncommercial government-only services. Do not invent facts; use the marketplace context and CRM matches, and recommend research when context is missing. Apply the decisions so approved signals post to the configured high-signal Slack channel. Report collection, decision, and Slack alert counts. If the queue is empty, report that and stop successfully.
```

## Safety and diagnostics

- `--dry-run` fetches and computes differences without changing the database.
- Collection never posts to Slack. Only Codex apply can post.
- The first normal run creates a baseline and emits no historical signals.
- Repeated collection is idempotent through changelog IDs and unique signal keys.
- Repeated apply does not repost a signal after `slack_notified_at` is set.
- A malformed source schema or failed fetch exits nonzero instead of silently replacing the baseline.
- The Django Admin exposes both marketplace source states and signals for inspection.
