# CRM v2: active-account contract

## Outcome

The managed CRM is a concise account workspace backed by Postgres, not the
generated People contact view. An account
appears in the managed CRM only when there is current, meaningful sales
evidence or an explicit human pin.

The managed workbook surfaces are intentionally small:

1. `Active Accounts`: the operator's manual company/stage ledger; bound rows
   may reference a stable account/opportunity and unbound rows remain valid.
2. `Actions`: one owner-filterable work queue scoped to exact bound Opportunity
   IDs in Active Accounts, plus retained handled history inside that scope.
   There is no separate Pipeline or Recovery data source; stage, waiting
   state, and attention state are fields on the account/action rows.
3. `People`: a derived contact view whose account boundary comes from every
   material Active Accounts row. Contacts additionally require human inbound
   Gmail, calendar/Granola participation, an exact Main point of contact name,
   or a deliberately curated OpportunityContact. It is not an admission,
   suppression, or send-permission input.

## Admission order

Admission is account-first and uses the strongest qualifying evidence below.
One-sided outbound activity never qualifies by itself.

| Tier | Qualifying evidence | Default active window |
| --- | --- | --- |
| 1 | Human `manual_pin`, a named Sales Motion tab, or a non-closed human-managed opportunity | Until explicitly unpinned or closed |
| 2 | Upcoming external meeting, or a completed external meeting with stored Gemini/Granola context | Upcoming, or 180 days after the meeting |
| 3 | Human Gmail inbound/bidirectional thread | 120 days after the latest human message |
| 4 | Substantive bidirectional LinkedIn conversation | 90 days after the latest substantive turn |

An existing open/waiting human action retains the account until the action is
resolved.  Closed Won/Lost remains human-authoritative.  Expired evidence moves
an unpinned account out of Active Accounts; it does not create a Recovery row.

## Channel rules

### Meetings

- Upcoming external meetings always qualify and create meeting-prep work.
- Completed external meetings qualify when there is a matched event or note;
  a calendar invitation alone is not proof that a meeting happened.
- A complete, matched Granola/Gemini note linked to the exact Opportunity may
  create one post-meeting action without a Calendar Meeting row only when a
  next-step bullet explicitly names the resolved Opportunity owner. Recording
  time determines recency; Granola wins only same-time ties.
- The exact note UUID keys the commitment. Unrelated outbound messages do not
  clear it; explicitly handling/completing its Action does.

### Gmail

- Candidate discovery is independent of LinkedIn Deal state and outreach
  suppression.
- Human inbound and outbound messages are joined by exact participant email and
  thread identity.  Automated replies, newsletters, bulk mail, and system
  notifications are not meaningful sales evidence.
- If the latest meaningful message is inbound, the account is `Needs response`.
- If the latest meaningful message is outbound, the account is `Waiting`; the
  next reminder is derived from the last outbound date.

### LinkedIn

- Outbound-only messages, invitations, connection acceptance, one-word
  acknowledgements, automated text, and polite declines remain contact-only in
  Postgres.
- LinkedIn qualifies only when the thread is genuinely bidirectional and the
  inbound content indicates a continuing conversation, scheduling/meeting
  intent, a concrete question, or multiple substantive turns.
- LinkedIn is always lower evidence than meetings or Gmail for the same account.

## Sales relevance versus send permission

`Lead.disqualified` and company suppression prevent automated outreach. Legacy
People `Don't send` cells are ignored. These controls do not erase meeting/email history, remove an active
account, or cancel a human-owned opportunity.  Every generated action still
checks the target contact's send permission before it can enter a sender queue.

## Account identity

- Contacts roll up to a stable Account; views never create one opportunity per
  LinkedIn Lead.
- Exact normalized domain is strongest.  Explicit aliases are next.  A unique
  conservative normalized company name is the final fallback.
- Legal suffixes and known variants may be aliases, but ambiguous names never
  auto-merge.
- A row carries a stable Account/Opportunity ID and exposes `Why active` so the
  operator can see which evidence admitted it.

## Active Accounts fields

The visible working set is limited to:

`Account`, `Owner`, `Stage`, `Attention`, `Why active`, `Last meaningful touch`,
`Who owes`, `Next action`, `Due`, and `Key contacts`.

Stable IDs, source details, merge baselines, and sync timestamps remain hidden
system columns.  Value, probability, sales-motion step, and closure fields stay
human-owned but are shown only when they are populated or explicitly needed.

## Action rules

At most one current action exists per opportunity:

- `Meeting prep`
- `Needs response`
- `Post-meeting follow-up`
- `Waiting`
- explicit human `Next step`

The generated Actions surface is narrower than the database: an Opportunity
must have its exact hidden ID in Active Accounts. Generated current work outside
that scope is cancelled. Human-authored database tasks outside it are preserved
but not published. A company-name-only row is valid for People, but cannot
authorize Actions until it is bound to an Opportunity ID.

The current action must have an authoritative target contact before appearing
in the owner-filterable `Actions` queue, except for an exact Opportunity-linked
meeting-note commitment, which is deliberately account-level. An unresolved
target stays on Active Accounts with `Attention = Needs contact`, never in a
sender queue. No workflow sends a message; it only creates a reminder or draft
for review.

## Operational invariants

Every preview, first cutover, and routine refresh must preserve these checks:

- Ramp is admitted from Sales Motion/Gmail/meeting evidence.
- StackArmor can remain sales-relevant while database suppression blocks outreach.
- one-sided LinkedIn history is absent from Active Accounts.
- every active row has a deterministic admission reason.
- every action has one owner, one opportunity, and one target contact.
- a second identical dry-run produces no semantic changes.

## Durable reconciliation

`linkedin/crm_v2_reconcile.py` persists the evidence decision separately from
human sales state. Each Opportunity carries an Active Account flag, primary
and supporting admission reasons, evidence tier, evaluation time, and
reversible inactive timestamp/reason. Only admitted evidence may create a new
Account and primary Opportunity. Existing human owner, stage, motion step,
value, probability, names, and nonblank domains are never overwritten.

An exact Lead ID remains linkable when database outreach controls suppress it:
suppression is not account identity. A unique business email domain may fill
a blank Account domain, while duplicate names, conflicting domains, missing
stable IDs, and cross-account contact links fail closed. Expired automated
bootstrap/system Opportunities are marked inactive without deletion or stage
closure. Manual/Sheet Opportunities and human pins remain authoritative. The
reconciler never creates Actions or sends messages.
