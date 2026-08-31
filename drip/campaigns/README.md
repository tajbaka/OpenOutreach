# Drip campaign manifests

This directory is the reviewed source location for versioned drip campaign JSON manifests. It intentionally contains no production campaign manifest yet. Adding code support did not publish a campaign, enroll Leads, schedule reconciliation, or complete a live pilot.

Use the dry-run-first workflow from the repository root:

```bash
.venv/bin/python manage.py validate_drip_campaign drip/campaigns/<campaign>.json
.venv/bin/python manage.py publish_drip_campaign drip/campaigns/<campaign>.json
.venv/bin/python manage.py publish_drip_campaign drip/campaigns/<campaign>.json --apply
.venv/bin/python manage.py plan_drip_enrollments <campaign-key> \
  --operator <canonical-operator> \
  --lead-id <exact-lead-id> \
  --output artifacts/drip/<campaign>-review.json
.venv/bin/python manage.py enroll_drip_campaign <campaign-key> \
  --plan artifacts/drip/<campaign>-review.json \
  --reviewed-by <reviewer>
.venv/bin/python manage.py enroll_drip_campaign <campaign-key> \
  --plan artifacts/drip/<campaign>-review.json \
  --reviewed-by <reviewer> \
  --apply
.venv/bin/python manage.py reconcile_drips --campaign <campaign-key>
.venv/bin/python manage.py reconcile_drips --campaign <campaign-key> --apply
.venv/bin/python manage.py resolve_drip_reference oo_<22-char-token>
```

The plan command requires explicit Lead IDs and creates a new private artifact; it never performs ICP-wide enrollment. Review that artifact before apply. Use `review_drip_handoff --lane-id <id> --not-applicable --reviewed-by <reviewer>` only when the current sequence truly never ran; persisted current or legacy outbound evidence makes that attestation fail closed.

A manifest contains one ordered theme list per canonical ICP. Every theme has a shared `intent`, the same canonical sender set, and one or both independent `linkedin`/`gmail` renditions. `delay_days` is channel-local. Gmail step 0 requires the lane subject; later Gmail steps omit it or repeat it exactly because the lane stays in one thread.

New publications use schema version 3. A Gmail step may declare at most one reviewed first-party tracked link:

```json
{
  "delay_days": 1,
  "subject": "A clearer way to see the gap",
  "body": "Thought this view might be useful: {tracked_link}",
  "link": {
    "key": "fedramp_automation",
    "url": "https://boundera.io/fedramp-automation"
  }
}
```

`link` is Gmail-only and requires exactly one `{tracked_link}` placeholder in that step's body. The URL must be an exact `https://boundera.io` or `https://www.boundera.io` destination without credentials, an explicit port, or an existing `ref`. Reconciliation replaces the placeholder once, stores an immutable random `oo_` reference mapped to the exact delivery, and freezes the resulting plain-text URL before Task creation. Raw URLs remain untracked and are never rewritten. See `docs/drip-campaign-implementation-plan.md` for the complete schema and runtime contract.
