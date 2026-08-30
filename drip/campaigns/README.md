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
```

The plan command requires explicit Lead IDs and creates a new private artifact; it never performs ICP-wide enrollment. Review that artifact before apply. Use `review_drip_handoff --lane-id <id> --not-applicable --reviewed-by <reviewer>` only when the current sequence truly never ran; persisted current or legacy outbound evidence makes that attestation fail closed.

A manifest contains one ordered theme list per canonical ICP. Every theme has a shared `intent`, the same canonical sender set, and one or both independent `linkedin`/`gmail` renditions. `delay_days` is channel-local. Gmail step 0 requires the lane subject; later Gmail steps omit it or repeat it exactly because the lane stays in one thread. See `docs/drip-campaign-implementation-plan.md` for the complete schema and runtime contract.
