# Gmail Post-Accept Sequence

This is the current operating spec for Gmail sequencing in OpenOutreach.

## Trigger

Gmail sequencing is scheduled when a lead reaches `CONNECTED`, not after the
LinkedIn follow-up sequence finishes. The scheduling hook is
`gmail.handoff.maybe_schedule_gmail_sequence`, called from:

- `linkedin/tasks/connect.py`
- `linkedin/tasks/sweep_connections.py`
- `linkedin/management/commands/enqueue_no_reply_followups.py`

If `ENABLE_GMAIL_SEQUENCE=false`, the hook no-ops.

## Timeline

LinkedIn and Gmail steps each use `delay_hours`, interpreted as an offset from
`Deal.connected_at`, then normalized into configured active hours/rest days.

Example:

```json
[
  {"delay_hours": 0.33, "subject": "FedRAMP 20x path at {company_name}", "body": "..."},
  {"delay_hours": 192, "subject": "Worth comparing 20x vs Rev 5?", "body": "..."}
]
```

That means Gmail step 0 can run about 20 minutes after connection acceptance even if
the next LinkedIn step is not due until later.

## Cadence Policy

The default post-accept cadence is:

- Day 0: LinkedIn follow-up 1, immediately after connection acceptance.
- +0.33 hours: Gmail follow-up 1, a same-day cross-channel touch without
  making the copy depend on LinkedIn having sent successfully.
- Day 4: LinkedIn follow-up 2, staying inside the first business week.
- Day 8: Gmail follow-up 2, a final lower-friction nudge before the automated
  sequence stops.

Future LinkedIn steps added from Sheets default to 96-hour spacing. Future
Gmail steps default to 0.33h, 192h, then weekly after that. This keeps the
first email close to the first LinkedIn follow-up while preserving independent,
standalone copy in case either lane fails open.

## Fail-Open Behavior

Channels are intentionally independent:

- If Gmail enrichment or send fails, LinkedIn follow-up tasks still run.
- If a LinkedIn follow-up send fails and retries, Gmail tasks can still run.
- If Gmail scheduling itself fails while LinkedIn is processing an accept, the
  exception is logged and swallowed; the LinkedIn path continues unchanged.
- Both lanes stop when the lead replies on LinkedIn or Gmail, gets a meeting,
  is disqualified, or matches suppression.

Because of this, every LinkedIn and Gmail template must stand alone. Avoid copy
that depends on another channel having succeeded, such as "as I mentioned in my
email" or "following up on my LinkedIn note." Prefer local wording like "quick
follow-up on FedRAMP 20x" or "curious how your team is thinking about 20x vs
Rev 5."

## Templates

LinkedIn copy lives in `linkedin/icp_messages.json`.

Gmail copy lives in `gmail/icp_emails.json`.

Gmail routing uses the lead's canonical ICP bucket (`Lead.icp`) through
`resolve_icp()`. If `Lead.icp` is blank, the legacy deterministic classifier
backfills it before template lookup. This matters for buckets like `Channel`,
which should not be reclassified as generic CSP copy.

Allowed Gmail placeholders are:

- `{first_name}`
- `{last_name}`
- `{company_name}`
- `{my_name}`
- `{our_company_name}`
- `{our_website_url}`

`steps()` validates subject/body placeholders against that allowlist before any
email is rendered. `render_for_icp()` also sanitizes unknown company sentinels
such as `Unknown Company` to `your team`.

The Google Sheets sync renders both into the ICP Messages tabs using interleaved
columns:

- `Followup Message N`
- `Email Subject N`
- `Email Body N`

`sync_icp_messages --push` writes JSON to Sheets. `--pull` reads Sheets back
into both JSON files while preserving existing `delay_hours`; Sheets is the copy
surface, not the cadence editor. If a row has email columns but the subject/body
cells are blank, that sender/ICP's Gmail block is saved as an empty list and the
Gmail lane is disabled for that bucket.

Before running a newly pulled Gmail sequence, validate the JSON:

```bash
.venv/bin/python manage.py validate_gmail_templates
```

## Sender Coverage

Only operators with a Gmail mapping in `gmail/auth.py` can schedule Gmail sends.
Operators without a mapping are skipped cleanly.

Missing, blank, or incomplete Gmail copy for a sender/ICP disables only that
Gmail lane. It must not alter LinkedIn state, LinkedIn scheduling, or LinkedIn
send behavior.

Current Gmail templates are populated for Arian, Athena, Leili, and Eddy. Chuka
is intentionally skipped unless a Gmail mapping and templates are added.
