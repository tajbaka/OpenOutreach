# Follow-up generation workflow

This is the manual-drafting workflow for canonical CRM Actions. Eligibility,
owner routing, and queue placement come from `refresh_crm_v2`; the drafter
does not independently scan every Lead or decide who should be contacted.

The workflow writes drafts and review metadata only. It never sends Gmail or
LinkedIn messages.

## Before drafting

Keep the stored context fresh:

- `backfill_messages` is the separate LinkedIn ingestion prerequisite.
- `sync_crm_v2_context` refreshes Gmail/Gmail-delivered Gemini context,
  validated email-first contacts, and Granola.
- Google Calendar and Drive are not queried by that command; run
  `docs/data-sync-workflow.md` when their stored context may be stale.

Run and review the default no-write CRM plan, then apply it:

```bash
.venv/bin/python manage.py sync_crm_v2_context --apply
.venv/bin/python manage.py refresh_crm_v2 \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian
.venv/bin/python manage.py refresh_crm_v2 --apply --routine \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian
```

Do not draft from a queue whose refresh reported conflicts, invalid human
edits, ambiguous owners, or stale/missing context relevant to the proposed
message.

## What appears in the daily queue

Each row in the owner-filterable `Actions` tab has stable Action, Opportunity,
and target Lead IDs. A
row appears only for a genuine current action, such as:

- new inbound / Needs Response;
- overdue or due-today next action;
- preparation for a real upcoming meeting;
- unresolved post-meeting commitment;
- an explicit human next step.

Future Waiting rows do not become current work until due. Closed and ineligible
records do not enter the current queue. An exact target marked Don't send may
remain sales-relevant but is shown as outreach stopped with no send channel or
draft. Granola/Gemini context can enrich a row but cannot create eligibility.

## Export the canonical queue

```bash
.venv/bin/python manage.py generate_followups \
  --output artifacts/followups/codex-review.json
```

Useful safe scopes:

```bash
.venv/bin/python manage.py generate_followups \
  --operator Arian \
  --limit 10 \
  --output artifacts/followups/codex-review.json
```

`--refresh-crm` is a compatibility name for running
`sync_crm_v2_context --apply` followed by `refresh_crm_v2 --apply --routine`
before export. It fails closed before the v2 cutover. Because it writes, prefer
the explicit sequence when reviewing changed policy.

The queue contains one candidate per canonical Action, with:

- exact `action_id`, `opportunity_id`, ordered `lead_ids`, and
  `context_fingerprint`;
- explicit owner and action/evaluation reason;
- Opportunity stage, sales-motion step, and contact roles;
- recent stored LinkedIn/Gmail messages; and
- Granola-primary/Gemini-fallback meeting context.

Names and company strings are context only. Never use them as row identity.

## Produce decisions

Write JSON in the `schema` embedded in the exported queue. The shape is:

```json
{
  "decisions": [
    {
      "action_id": "copied UUID",
      "opportunity_id": "copied UUID",
      "lead_ids": [123],
      "context_fingerprint": "copied SHA-256",
      "recommended_next_step": "short recommendation",
      "relationship_summary": "one or two grounded sentences",
      "draft_email": "",
      "draft_linkedin": "short draft or blank",
      "needs_human_review": false,
      "review_reason": ""
    }
  ]
}
```

Copy all four identity/fingerprint fields exactly. Supply at most one of
`draft_email` and `draft_linkedin`. A review-only decision may leave both
drafts blank. If the correct contact, current commitment, or channel is
ambiguous, request human review instead of guessing.

Drafting rules:

- Answer a fresh inbound before introducing a new ask.
- Use a post-meeting commitment or promised deliverable when the Action says
  that is what is owed.
- Do not write as though an old thread is warm. An account needs current
  qualifying evidence or an explicit human next step.
- Ground feature claims in the current FedRampGPT product source or supplied
  product context. If a claim cannot be verified, remove it.
- Keep LinkedIn drafts concise; populate email only when email is the intended
  channel and the supplied context supports it.
- Do not include operator instructions inside the draft body.
- Never convert a polite decline into Closed Lost or Disqualified; flag it for
  human review.

Save the result outside git, for example:

```text
artifacts/followups/codex-decisions.json
```

## Validate, apply, and publish drafts

```bash
.venv/bin/python manage.py generate_followups \
  --apply-json artifacts/followups/codex-decisions.json
```

Apply validates the complete file atomically against a newly serialized queue.
Unknown or duplicate IDs, changed `lead_ids`, or stale fingerprints fail
closed. Existing nonblank human drafts are preserved. Valid drafts are stored
on canonical Actions and published to the corresponding stable-ID `Actions`
rows through routine CRM v2. Blank decisions are no-ops.

To store valid drafts for the next scheduled refresh without publishing now:

```bash
.venv/bin/python manage.py generate_followups \
  --apply-json artifacts/followups/codex-decisions.json \
  --no-publish
```

After publication, the operator reviews the draft, sends it manually, and
records `Handled`, `Disposition`, `Waiting until`, or other human state in the
Actions row. The next `refresh_crm_v2 --apply --routine` imports those fields
before regenerating the queue.

## Retired legacy workflow

The former Lead/name-based cohort workflow rebuilt sender tabs with
`write_followups()`. It could drop and recreate worksheets, route by inferred
sender, and deduplicate by Name. It is not compatible with the canonical CRM
safety contract and must not be scheduled.

It remains only as a bounded rollback tool behind an explicit `--legacy` flag:

```bash
.venv/bin/python manage.py generate_followups \
  --legacy \
  --output artifacts/followups/legacy-review.json

.venv/bin/python manage.py generate_followups \
  --legacy \
  --apply-json artifacts/followups/legacy-decisions.json
```

Never run legacy apply against the canonical workbook without a current
workbook backup and an explicit recovery reason. Options such as `--campaign`,
`--no-active`, `--no-sheet-read`, and `--full-review` are legacy-only and also
require `--legacy`.

## Scheduling

Schedule the two-phase `sync_crm_v2_context --apply` then
`refresh_crm_v2 --apply --routine` wrapper, not legacy followup generation. A separate
Codex drafting job may export, draft, and apply canonical decisions, but it must
stop on validation failures and must never send messages automatically.

See `docs/codex-followup-automation.md` for the machine-oriented queue/apply
contract and `docs/crm-refresh-workflow.md` for workbook safety and recovery.
