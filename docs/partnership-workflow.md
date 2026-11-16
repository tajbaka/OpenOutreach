# Partnership tracking

The user approved this separate track on 2026-11-16. `Partnership` is an
operator-selected Stage in manual `Active Accounts`, grouped after the buyer
stages. Partners stay in that ledger so their relationship-backed contacts remain
eligible for People. A demo or sandbox does not establish a license purchase.

Use the configured FedRampGPT workbook with the repo Google Sheets, Gmail and
Granola skills. `Partnerships` is the current tracker; `Partner Referrals` is the
attributed introduction log. The old Schellman and Prescient Security tabs in
Sales Motions remain visible history: retain their IDs, complete contents and A2
pointers. The user's later 2026-11-16 instruction supersedes the initial hiding:
keep verified Partnership tabs visible at the far left after Template, restoring
visibility when needed. The [Sales Motion cleanup skill](../skills/boundera-sales-motion-cleanup/SKILL.md)
applies this stable grouping on every cleanup, preserving order within the
Partnership group and among all remaining tabs. Routine partner-tracker reviews
do not hide these historical tabs. Do not delete/rebuild them, change their 85 task statuses, mark the
partners Closed Won/Lost, or use the buyer Template for new partnership work.

## Status and scope

- `Developing`: establishing scope or a concrete collaboration; no promised
  introductions or revenue implied.
- `Active`: verified ongoing collaboration or introductions; no guaranteed
  volume, license purchase or revenue commitment implied.
- `Dormant`: explicitly paused based on a recorded decision; elapsed time or
  an unanswered email alone is insufficient.

Partner Status and Active Accounts Stage remain operator-owned in routine
reviews. Recommend a status change when warranted; do not make it automatically.
Initial classification can be written on an explicit user request. Initial scope
is Schellman, Prescient Security and the existing Samantha Cowan advisory-partner
row, whose entity is unconfirmed. Do not treat Nylas as the referred buyer.

Track referrals, seller enablement, joint marketing and collaboration separately
from purchased services. Schellman's assessment of Boundera is service work,
not Boundera revenue or a referral. Customer-reference support is not a sourced
lead. A future partner internal-use purchase requires a separate buyer opportunity.

## Trackers and evidence

`Partnerships` has one row per verified partner: status, owner, contacts, focus,
next partnership step/review, logged buyer referrals, logged partner introductions,
qualified buyers, wins, won revenue, evidence/coverage and separate service work.
Hidden Partner ID and existing Account ID anchor rows; an unbound partner is valid.
Never create database identities to fill these cells.

`Partner Referrals` records the exact referring partner, kind (`Buyer referral`
or `Partner introduction`), referred account/relationship, contact, introduction
date, outcome, qualification, opportunity amount/currency, won amount/date,
next step and evidence. Preserve hidden Referral ID, Partner ID and any verified
existing buyer Account/Opportunity IDs. A log row does not create a Lead, Deal,
Opportunity or Active Accounts row.

Count only delivered, identifiable introductions. Expected batches, speculative
volume, target lists and repeated messages about one introduction do not increase
the count. Deduplicate by exact partner/contact identity and underlying
introduction or buyer opportunity; preserve the canonical ID and all evidence.
An advisory introduction is not a buyer referral to that person's employer.

Count formulas summarize logged records, not lifetime totals. Record coverage;
zero logged does not prove zero historical referrals. Qualification requires
buyer evidence, not an introducer's enthusiasm. Keep unknown amounts blank.
Wins require verified buyer-side agreement evidence; partner NDAs, training and
sandbox access do not qualify. Attribute revenue once to the actual buyer
opportunity and partner, retaining currency. Show separate currency totals.
Won revenue is `—` when no wins are logged, or `Amount / currency unverified`
when a logged win lacks either. Never multiply expected volume by a per-tenant
quote to create forecast or booked revenue.

## Daily review

Run after Actions and Active Accounts Next step in the same daily runner.
Read the live tracker, relevant Active Accounts and Actions, exact stored
LinkedIn identities, live Arian Gmail including Sent, and full relevant Granola
transcripts. Reconcile dated evidence across channels. Missing Slack/text or
unrecorded calls are coverage limits, not proof of noncompletion.

1. Preserve operator Status, Owner, Contacts, planned dates and manual notes.
   A new operator-selected Partnership account may receive a tracker row after
   exact identity verification; leave unknown status and IDs blank.
2. Update source-backed focus, next partnership step, evidence/coverage and
   separate-service context. Retain operator context; distinguish promises from
   suggestions. Do not roll dates forward or invent periodic nudges.
3. Append/update exact verified introductions and buyer outcomes, preserving
   IDs, evidence and manual cells. An introduction already delivered is no
   longer owed merely because its recipient has not replied.
4. Preserve count formulas and verify their ranges cover the entire referral
   log; extend them when its grid grows. Reconcile won-revenue summaries by
   currency against actual buyer evidence. Retain unknowns and coverage limits.
5. Keep concrete partner commitments eligible in Actions through its own review.
   Do not duplicate or fulfill an obligation because a tracker row exists.
   This phase writes only Partnerships and Partner Referrals, not Actions or
   Active Accounts; their separate reviews retain their exact write scopes.

Back up native tabs in a new ignored receipt folder. Under `crm_refresh_lock()`,
re-read exact target rows and source activity before precise writes. Hold
ambiguous identities and concurrent edits. Read back changed values/formulas,
verify metrics and IDs, preserve unrelated tabs and manual/native properties,
and rerun the duplicate check. Retain filters, frozen panes, hidden IDs, formatting
and taller rows; apply an 84-pixel floor to material entries. Preview is read-only.

No sends, drafts, campaigns, database mutations, new logins, recurring jobs or
full People rebuild. `Partnership` is a manual workbook classification, not a
new database `Opportunity.Stage`: never silently map it onto a buyer stage or
bypass retired publisher/schema guards. Preserve all existing IDs. Contextual
reviews use the manual classification as authoritative.

## Receiving-computer rollout

The active daily runner is on Windows. Update its checkout and saved prompt for
this phase and the partnership-specific Actions/Next step rules. The Mac remains
paused. Local edits do not verify Windows adoption or deploy/restart workers.
