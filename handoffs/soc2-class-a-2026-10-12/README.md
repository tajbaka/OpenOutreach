# Arian SOC 2 Class A campaign handoff

This is an explicitly authorized cross-machine transfer, prepared October 13,
2026. The user approved creating and activating Arian's separate campaign,
concurrent operation, all five messages, and **post-acceptance** email. Setup
stalled off the database network. Finish it on the connected machine, then remove
this temporary transfer folder and push the cleanup to `main`.

The repository is public. The candidate CSV, seed IDs, personalized previews and
private setup state are encrypted with Fernet authenticated encryption. The key
is supplied separately in the user's prompt; never commit it or decrypted data.
Deleting the bundle in a later commit does not erase its Git history.

## Exact scope

| Field | Approved value |
| --- | --- |
| Campaign name | `SOC2 Class A \| Small-company search \| Arian \| 2026-10-12` |
| Program key | `soc2-class-a-arian-2026-10-12` |
| Audience key | `soc2-class-a-federal-prospects` |
| Owner username | `ariantajbakh` |
| Canonical operator | `Arian` |
| Gmail start mode | `post_acceptance` |
| Initial creation status | `disabled` pending independent readback |
| Intended final status | `active` |
| Candidate maximum | 61, before fresh live checks |
| Excluded from transfer/enrollment | 40 held profiles from the 101-profile export |
| Program content hash | `0df8a9e866d3f355c237db6dbd6b9213a98882db154eae975a8386b65f62eada` |

These are reviewed outreach candidates, not a claim that every company has a
current qualifying SOC 2 Type II report or confirmed headcount of 100 or fewer.
Some source size bands were unconfirmed. Do not expand the seed list or re-add
the held profiles. This transfer excludes unrelated CRM histories and credentials.

## 1. Pull and decrypt locally

Read current `AGENTS.md` and `ARCHITECTURE.md`. Inspect Git state and pull `main`
without discarding local work. Use the repository virtual environment, never
system Python; on Windows its executable may be `.venv/Scripts/python.exe`.

`cryptography` is the only optional decryption dependency. If unavailable,
install `requirements/campaign-handoff.txt` into that virtual environment. This
is not an instruction to change production worker dependencies.

Save the separately supplied key only in an ignored file, for example
`artifacts/leads/soc2-class-a-2026-10-12/handoff-key.txt`. Do not put the key in
tracked files, logs, or a command-line argument. Run:

```sh
.venv/bin/python handoffs/soc2-class-a-2026-10-12/unpack.py --key-file artifacts/leads/soc2-class-a-2026-10-12/handoff-key.txt
```

The fixed output is `artifacts/leads/soc2-class-a-2026-10-12/handoff/`, containing:

- `campaign-candidates.csv`: 61 positive-inclusion candidates, unique public URLs.
- `campaign-definition.json`: exact sender, post-acceptance timing and disabled configuration.
- `campaign-plan.json`: approved scope, messages, hashes and cadence; no held-row histories.
- `message-previews.json`: 305 previously validated personalized messages.
- `creation-status.json`: prior attempt's **unverified** outcome.

The helper only authenticates/decrypts files. It never imports, accesses the DB,
activates a campaign, sends a message or requests a restart. It refuses to replace
different existing files. Confirm the CSV identities exactly match the definition
and preview keys, and validate the canonical JSON program hash before DB writes.

## 2. Reconcile first; never blindly retry creation

The October 12 disabled-only creation process was terminated after DB timeouts.
No activation or sender restart was attempted. Missing receipts do **not** prove
that its transaction rolled back. Use bounded DB connection/query timeouts and
query the **exact campaign name** before any writes.

- No match: perform fresh checks, then create once, initially disabled.
- Exactly one match: inspect its owner, status, program/version/hash, seed IDs,
  Deals, enrollments, deliveries and Tasks. Reuse an exact, complete disabled
  result; do not duplicate it or rerun a broad importer over it.
- Already active with the exact intended setup: reconcile actual state/history;
  do not reset status, schedules, deliveries or already-started outreach.
- Duplicate names, unexpected partial state, wrong owner/copy, identity conflicts,
  or unclear sends: preserve evidence and stop the affected operation. Never
  delete/recreate records to make the handoff pass.

`import_campaign` is an upsert and can overwrite an existing campaign's fields;
do not invoke it without this reconciliation. Do not infer absence from a timeout.
Suppress diagnostic-only Slack transport in the diagnostic process, not persisted
configuration. Never log raw secrets, message histories, or unrelated CRM data.

## 3. Fresh eligibility and controlled creation

Use existing models/services and their normal outbound ownership locks. Re-read
their current implementations rather than copying a historical one-off repair.
Confirm the unique active sender resolves to Arian / `ariantajbakh`, with no
emergency stop. Never clear a stop or change sender limits/active hours.

For each included identity, check canonical URL/public identifier, normalized
name/company collisions, existing CRM classification/dependencies, disqualification,
suppression, prior LinkedIn/Gmail conversation or invitation history, active
campaign/drip ownership, meetings, withdrawals, replies/stops, and outstanding
Tasks/deliveries. Distinguish the exact campaign's legitimate staged records from
unrelated prior work. The 61 candidates were new identities at the original
preflight; this is not a guarantee they are still new.

Hold newly conflicting candidates and record why; fewer than 61 is acceptable.
Do not replace them from the held list, overwrite an existing Lead's ICP/role,
retry unclear sends, or bypass normal ownership checks. If an existing staged
record is newly ineligible, resolve it safely before activation; do not activate
the whole campaign with unsafe rows still executable.

Useful existing APIs (inspect signatures and invariants):

- `linkedin.campaign_setup.exact_campaign_owner`, `explicit_gmail_start_mode`,
  `message_program_draft`, `snapshot_campaign_program`.
- `linkedin.message_delivery.ensure_message_enrollment`, `list_message_steps`,
  `render_message`, `get_or_create_delivery`.

Publish/snapshot **only** the dedicated program from `linkedin/icp_messages.json`
and `gmail/icp_emails.json`. Preserve all older programs. For a new eligible
identity, set its approved CSV name/company, canonical public URL/identifier and
`icp=soc2-class-a-federal-prospects`; leave email and reviewed role blank rather
than inventing them. The current connect path still resolves `Lead.icp` before
validating the frozen enrollment, so a mismatched Lead ICP is not harmless.

Within a guarded atomic setup, create only the exact disabled campaign, eligible
Leads, `Ready to Connect` Deals, Arian enrollments frozen to its version/audience,
and planned `linkedin_connect` step 0 deliveries. Do not manually prequeue
follow-ups, Gmail enrichment, email 1 or email 2. Prevent concurrent duplicate
creation and recheck ownership under the existing Lead-first locks.

## 4. Independent readback, activation and pickup

After commit, start an independent readback. Verify exact owner/mode/version/hash,
identity set/count, enrollment operator/audience, and saved connection delivery
copy. Render all five steps for every eligible enrollment with the production
renderer and compare subject/body against the decrypted approved previews;
check frozen media is empty and no placeholders remain. Verify expected dates
and that no unrelated campaign/configuration was changed. Persist private receipts
with IDs, hashes, counts, holds and timestamps under the ignored artifact tree.

Approved cadence, subject to existing reply/meeting/suppression guards:

- LinkedIn follow-up 1 after acceptance and normal pacing; follow-up 2 at actual
  follow-up-1 send +72 hours.
- Email 1 at acceptance +0.33 hours when an address is available; email 2 at
  actual email-1 send +192 hours. Never start before acceptance.

Only after successful readback and a fresh eligibility/stop check, activate this
campaign. Concurrent operation with Arian's existing campaigns is authorized;
leave their statuses, schedules, copy, limits and global email settings untouched.
Ensure the real worker has this commit. If its active campaign snapshot requires
refresh, use the existing controlled Arian-only restart request:

```sh
.venv/bin/python manage.py request_sender_restart --operator Arian --apply
```

Do not launch a second sender/LinkedIn daemon or restart other senders. Confirm
supervisor pickup using fresh heartbeat/restart state and campaign-scoped work
evidence, allowing ordinary active hours/caps to defer invitations. Report separately
what was created, activated, picked up and actually sent; Git push is not runtime
verification. If a blocker prevents activation or pickup, retain the recoverable
disabled state/receipts and report the exact remaining action without bypassing it.

## 5. Remove temporary transfer and push cleanup

After the receiving machine has verified its private decrypted files and saved
setup/reconciliation receipts, delete only the tracked
`handoffs/soc2-class-a-2026-10-12/` folder and
`requirements/campaign-handoff.txt`. Preserve canonical program JSON, private
receipts, other campaigns, unrelated local changes, and user files. The decrypted
artifacts stay ignored. Do not force-add them or the key. Remove the ignored key
once it is no longer needed; the decrypted receipt files are the local recovery copy.

Update `AGENTS.md` and `ARCHITECTURE.md` with the actual campaign ID, eligible/held
counts, version/hash, activation/pickup result and receipt location. If setup remains
blocked, record that accurately instead of claiming launch. Verify the staged diff
contains only this cleanup and accurate documentation, then commit with one line
and no Co-Authored-By trailer; push to `main`. Fetch/integrate concurrent changes
without force-pushing or discarding other work. Confirm the remote commit and report
the final state. This cleanup removes the transfer from the current tree, not history.
