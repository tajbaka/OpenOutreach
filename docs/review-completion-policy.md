# Current review completion and advisory holds

User policy, 2026-11-22: uncertainty about a historical item is an item-level
hold, not automatically a blocker for the entire Actions, Active Accounts and
Partnerships/Referrals run. This policy supersedes older wording that makes any
unresolved historical outcome prevent operational completion.

Perform fresh discovery, read every available required source, review current
items and finish all independent authorized work. Preserve uncertain identities,
unverified fulfillment, missing original recordings and historical provenance in
a private advisory ledger. Each held item needs its evidence, reason and next
investigation action. Do not invent a match, mark an obligation fulfilled, or
write an uncertain decision to the sheets. Revisit a hold when relevant evidence
changes; unchanged historical uncertainty does not require repeating the whole
backlog or asking the user to approve an otherwise safe run.

Actual shared failures still block completion: failed required discovery/read,
unread available required material, unprocessed actionable work, unsafe concurrent
edits, failed sheet verification, or an uncertain write. A missing historical
recording with a recorded hold is distinct from skipping an available transcript.

After current work is complete, acquire the existing publisher lock, reconcile
all six tabs and verify protected data, IDs, sorting and row heights. Save a NEW
receipt using actual timestamps. Never rewrite an old blocked receipt as success.
For a completed run retaining uncertainty, record:

```json
{
  "status": "completed_with_holds",
  "completion_scope": "available_current_evidence",
  "current_review_complete": true,
  "full_review_complete": false,
  "completion_checks": {
    "fresh_discovery_complete": true,
    "available_sources_reviewed": true,
    "independent_work_complete": true,
    "locked_readback_verified": true,
    "actionable_pending": 0,
    "uncertain_writes": 0,
    "blocking_failures": 0
  },
  "advisories": { "retained": true, "holds": 9, "historical_outcomes": 28 }
}
```

Counts above are illustrative; calculate them from this run's saved evidence.
Include started_at, completed_at, the private advisory evidence path, source
coverage and normal write/readback evidence. Set current_review_complete only
after the checks actually pass. Report operational completion and advisory counts
separately. full_review_complete and the full historical watermark remain false/
unchanged until that historical scope is truly resolved. The latest verified
operational receipt can checkpoint current discovery without declaring historical
holds resolved. No daemon, sender, campaign or database changes are authorized.
