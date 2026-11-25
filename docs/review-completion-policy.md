# Current review completion and advisory holds

User policy, 2026-11-22: uncertainty about a historical item is an item-level
hold, not automatically a blocker for the entire Actions, Active Accounts and
Partnerships/Referrals run. This policy supersedes older wording that makes any
unresolved historical outcome prevent operational completion.

## Unavailable exact references and recovery permissions (2026-11-23)

An exact required Gmail History reference returning 404 is a source limitation
for the decisions depending on that reference. After complete fresh discovery,
call `recover_history_additions` with an explicit `unavailable_required=[]` list.
The helper checks the exact thread too, returns its available full messages for
review, and records each unavailable required reference in that private list.
Preserve the affected item and existing decision; never infer fulfillment,
deletion, irrelevance or identity. Keep these holds separate from optional
`gmail_history_skipped`, with aggregate counts and reasons in the receipt.
Review all returned available messages and finish independent work. Authentication,
quota, service errors, invalid mailbox/ID matches and incomplete discovery remain
real blockers. Required-reference holds do not advance the full-review watermark.
For current completion, `actionable_pending=0` means no unheld eligible work is
left; report unavailable held decisions separately, not as completed decisions.

The recovery launcher's fresh automation-idle attestation plus its exclusive
responder ownership and the canonical publisher lock are the concurrency gates.
Do not require system-wide `Get-CimInstance Win32_Process` enumeration. A denied
optional process query does not establish an active competing review; actual
evidence of an active owner still stops duplicate execution. Final locked precheck,
operator-edit reconciliation and readback remain mandatory.

Do not open a previous sandbox session's temporary transcript cache. Fetch needed
exact transcripts again through the existing authorized Granola helper into a new
private temporary directory writable by this worker. Never copy a denied cache
through host tools or another agent. Unavailable historical transcripts become
explicit affected-item holds; unread available relevant transcripts must still be
reviewed. An unchanged discovery index alone does not prove transcript review.
Continue productive source review in checkpointed segments when needed.

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

## Scheduled receipt lifecycle

Save the new run's receipt at launch and throughout source review. Use
`crm.review_receipt.review_checkpoint(receipt, phase=...)` at checkpoints.
The receipt keeps `status="in_progress"` and `completed_at=null` during discovery,
paced retrieval and other unfinished work. Put the milestone in
`checkpoint_phase` and include the aggregate required and verified source
counts as reads progress. Record actual failures with a blocked receipt.

After current work is complete, acquire the existing publisher lock, reconcile
all six tabs and verify protected data, IDs, sorting and row heights. Save a NEW
receipt using actual timestamps. Never rewrite an old blocked receipt as success.
For a completed run retaining uncertainty, use the shared builder:

```python
from crm.review_receipt import complete_with_holds

receipt = complete_with_holds(
    receipt,
    checks=completion_checks,
    verification=locked_readback_results,
    historical_holds=advisory_ledger["holds_count"],
    historical_outcomes=advisory_ledger["historical_outcomes_count"],
    unavailable_required_references=len(unavailable_required),
)
# Save the returned receipt to this run's private receipt.json.
```

Supply the actual recorded results. The readback results must confirm
`publisher_lock_held`, `passed` and `all_six_tabs_verified`. The generated
fields include:

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
  "advisories": {
    "retained": true,
    "holds": 12,
    "historical_holds": 9,
    "historical_outcomes": 28,
    "unavailable_required_references": 3
  }
}
```

Calculate the counts from this run's saved evidence. The monitor requires
`advisories.holds`, the sum of historical holds and unavailable required
references. Historical outcomes and optional Gmail History skips are separate.
Include started_at, completed_at, the private advisory evidence path, source
coverage and normal write/readback evidence. Set current_review_complete only
after the checks actually pass. Report operational completion and advisory counts
separately. full_review_complete and the full historical watermark remain false/
unchanged until that historical scope is truly resolved. The latest verified
operational receipt can checkpoint current discovery without declaring historical
holds resolved. No daemon, sender, campaign or database changes are authorized.
