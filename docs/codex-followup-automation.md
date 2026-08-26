# Codex followup automation

This runbook generates drafts for canonical CRM Actions. It never sends Gmail
or LinkedIn messages. Read `docs/crm-refresh-workflow.md` first for workbook
ownership, context prerequisites, dry-run, backup, and recovery behavior.

## Safe canonical sequence

Refresh stored context, then review the CRM v2 plan:

```bash
.venv/bin/python manage.py sync_crm_v2_context --apply
.venv/bin/python manage.py refresh_crm_v2 \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian
```

After approval, apply the CRM refresh:

```bash
.venv/bin/python manage.py refresh_crm_v2 --apply --routine \
  --manual-pin StackArmor \
  --owner-override Ramp=Arian \
  --owner-override StackArmor=Arian
```

Export only explicitly owned daily Actions:

```bash
.venv/bin/python manage.py generate_followups \
  --output artifacts/followups/codex-review.json
```

Codex reads that file and writes:

```text
artifacts/followups/codex-decisions.json
```

Then validate and apply drafts:

```bash
.venv/bin/python manage.py generate_followups \
  --apply-json artifacts/followups/codex-decisions.json
```

The artifacts contain CRM context and must remain gitignored/local.

## Decision contract

Return JSON only in the `schema` embedded in the queue:

```json
{
  "decisions": [
    {
      "action_id": "copied UUID",
      "opportunity_id": "copied UUID",
      "lead_ids": [123],
      "context_fingerprint": "copied SHA-256",
      "recommended_next_step": "one short recommendation",
      "relationship_summary": "one or two grounded sentences",
      "draft_email": "",
      "draft_linkedin": "short draft or blank",
      "needs_human_review": false,
      "review_reason": ""
    }
  ]
}
```

Rules:

- Copy `action_id`, `opportunity_id`, ordered `lead_ids`, and
  `context_fingerprint` exactly. Never key by Name.
- Supply at most one of `draft_email` and `draft_linkedin`.
- Do not alter owner, stage, action status/description, contact roles, or due
  dates in the decision file.
- Flag ambiguity, missing context, contact uncertainty, or a polite decline for
  human review rather than guessing.
- Ground every product claim in supplied/current product context.
- Do not write operator instructions inside the draft body.

Apply validates the entire file atomically against a freshly serialized queue.
Unknown/duplicate IDs, changed contacts, and stale fingerprints fail closed.
Existing nonblank human drafts are preserved. Blank/review-only decisions are
no-ops. Successful drafts are stored on their canonical Actions and published
to stable-ID rows in the single `Actions` tab; no send API is called.

## Scopes and publication

Export one operator or a small diagnostic batch:

```bash
.venv/bin/python manage.py generate_followups \
  --operator Arian \
  --limit 10 \
  --output artifacts/followups/codex-review.json
```

Store valid drafts for the next scheduled CRM refresh without publishing now:

```bash
.venv/bin/python manage.py generate_followups \
  --apply-json artifacts/followups/codex-decisions.json \
  --no-publish
```

`--refresh-crm` before export is a compatibility name for
`sync_crm_v2_context --apply` followed by `refresh_crm_v2 --apply --routine`.
The old `--sync-gmail-context` compatibility flag takes the same v2 path;
`--sync-sheets` runs routine v2 publication. None can fall back to legacy
`refresh_crm`. Because these are writes, the explicit sequence is preferred.

## Scheduling

A drafting automation may:

1. run a no-write CRM dry-run and stop on safety warnings;
2. apply the CRM refresh only under the deployment's approved policy;
3. export the canonical queue;
4. write a schema-valid decision file;
5. apply the decisions; and
6. report counts and validation failures.

Never add automatic message sending. The Windows wrapper schedules context
apply followed by `refresh_crm_v2 --apply --routine`; draft generation is not
the scheduled publisher.

## Retired legacy path

The former Lead/name-based workflow rebuilt sender tabs and inferred routing
outside the canonical Action policy. It is a destructive rollback-only path and
must never be scheduled.

Every invocation requires explicit `--legacy`:

```bash
.venv/bin/python manage.py generate_followups \
  --legacy \
  --sync-gmail-context \
  --sync-sheets \
  --output artifacts/followups/legacy-review.json

.venv/bin/python manage.py generate_followups \
  --legacy \
  --apply-json artifacts/followups/legacy-decisions.json
```

Options `--campaign`, `--no-active`, `--no-sheet-read`, and `--full-review` are
legacy-only. Do not use legacy apply against the canonical workbook without a
current backup and a deliberate recovery reason.
