# Attio Meeting Context Sync — MCP Workflow

Reusable runbook for enriching Attio Sales-list contacts with calendar / Gmail / Drive / LinkedIn DM context, then updating outreach status, entry stage, and AI Notes.

Run periodically (weekly/biweekly) or after a batch of meetings.

## Prerequisites

- MCPs connected to the connected Google account: **Attio**, **Gmail**, **Google Calendar**, **Google Drive**
- `.env` has `ATTIO_API_KEY`, `ATTIO_SALES_LIST_ID`, `DATABASE_URL`
- Project venv: `.venv/bin/python`

## Inputs (clarify before starting)

1. **Time window** — default: last 90d + next 30d
2. **Write mode** — preview-first (recommended) | apply directly | dry-run
3. **Calendar scope** — primary only (default) | all calendars

---

## Workflow

### Phase 1 — Discover meetings

**1.1 Calendar scan (primary)**
```
mcp__claude_ai_Google_Calendar__list_events
  startTime: <window start, ISO Z>
  endTime:   <window end, ISO Z>
  pageSize:  250
  orderBy:   startTime
  eventTypeFilter: ["default"]
```
Response usually exceeds context — write to `/tmp/cal_*.json` via persisted-output, then jq.

**1.2 Identify host email** — `jq '.events[] | .organizer | select(.self == true) | .email' | unique` (typically `eddy@tryfedrampgpt.com`)

**1.3 Slim to external meetings**
```bash
jq '[.events[]
  | select(.attendees != null)
  | {id, summary, start: (.start.dateTime // .start.date), description: (.description // ""),
     external_attendees: [.attendees[]
       | select(.email != "<host>" and .email != null and .resource != true)
       | {email, name: (.displayName // ""), responseStatus}]}
  | select(.external_attendees | length > 0)]' "$CAL" > /tmp/cal_meetings.json
```

**1.4 Filter team noise** — exclude:
- Internal team emails (current: `ariant2013@gmail.com`, `ariantajbaka@gmail.com`)
- Recurring "Standup" events
- Vendor onboarding (e.g. `*@apollomail.io` Apollo tool)

Result: unique external attendee email set.

### Phase 2 — Cross-check Attio People

For each unique attendee email, run in parallel:
```
mcp__attio__list-records
  object: people
  filter: {"attribute": "email_addresses", "op": "eq", "value": "<email>"}
  limit:  3
```

Record: `email → {person_id, company_id, current outreach_status, name, job_title, description}`.

Two buckets emerge:
- **Matched** → continue to Phase 3
- **Unmatched** → Phase 5 ("not in Attio yet")

### Phase 3a — Multi-affiliation tiebreaker (which company is "their company"?)

People often have multiple current affiliations: an employer + a personal LLC + advisor seats + a non-profit they founded. Pick `Person.company` using this priority order, capture the rest in `ai_notes`:

1. **Corporate email domain** (highest signal) — `@usda.gov` → USDA, `@prescientsecurity.com` → Prescient, `@wiz.io` → Wiz. They literally show up using that company's identity.
2. **Gmail signature block** in their replies — when they sign emails as "X, Director, Company Y", that's the company they're representing in this conversation.
3. **LinkedIn headline name-drop** — "FedRAMP Compliance Specialist **at AWS**", "Head of Compliance **at Armada**".
4. **LinkedIn `positions` array** — top entry / "Present" job from the Voyager scrape.
5. **Calendar attendee email** — only useful if it's a corporate domain; useless for personal gmail.

**Practical tiebreaker rule:** `Person.company` = the org they'd answer with if asked *"where do you work?"* — typically the employer for employees, the consulting LLC for consultants, the company writing the paycheck for everyone else.

**Edge cases worth flagging in ai_notes:**
- Consultants juggling multiple client engagements → primary = consulting LLC, secondary clients in ai_notes
- Multi-hat advisors → primary = current employer, advisor seats in ai_notes ("active paid advisor at X")
- Founder + employee combo (e.g. founder of non-profit while employed at AWS) → primary = the employer, founder role in ai_notes
- Stale enrichment (e.g. Attio enrichment shows old company, DB Voyager scrape shows current) → trust DB, mention in ai_notes that Attio data is stale and worth re-enriching

**Cascading concerns when changing `Person.company`:**
- The Sales-list entry is parented to a *Company* not a Person. Moving `Person.company` from A→B leaves the Sales-list entry orphaned under A. Decide:
  - Delete the orphaned entry (use `mcp__attio__delete-list-entry` after retrieving entry_id) — clean state if A was incorrect
  - Re-create entry under B (use `mcp__attio__add-record-to-list`) if there isn't one yet
  - If B already has a Sales-list entry (likely for shared employers like AWS, Cisco), the Person now joins that entry's "team"; consider promoting B's entry stage to match the highest progress across all linked People
- Update `crm.Lead.company_name` and `crm.Lead.attio_company_id` in the DB so future `sync_attio` runs don't recreate the wrong association
- The corresponding Company record in A may now be empty — leave it (no harm) or delete it

### Phase 3 — Cross-check OpenOutreach DB

For each matched Person email, query the Postgres DB to get richer context than Attio holds. **Two distinct profile descriptions matter**:

- `crm.Lead.description` — LinkedIn bio scraped via Voyager during outreach (authoritative, our source of truth for who we actually qualified)
- Attio `Person.description` — third-party enrichment (e.g. FullContact). Sometimes scrapes the wrong person — example: Blake Loring's Attio description came back as a Spanish-language maintenance-engineer profile that wasn't him. Always compare against `Lead.description` if present.

```bash
.venv/bin/python manage.py shell <<'EOF'
from crm.models import Lead, Deal, Message
lead = Lead.objects.filter(email__iexact='<email>').first() \
       or Lead.objects.filter(linkedin_url__iendswith='<linkedin handle>').first()
if lead:
    deal = lead.deal_set.order_by('-created_at').first()
    msgs = Message.objects.filter(lead=lead).order_by('sent_at')
    print('--- Lead profile ---')
    print('Name:', lead.first_name, lead.last_name)
    print('Company:', lead.company_name)
    print('LinkedIn:', lead.linkedin_url)
    print('Disqualified:', lead.disqualified)
    print('Description:', lead.description[:1000])  # Voyager-scraped LinkedIn bio
    print('--- Deal state ---')
    if deal:
        print(deal.state, 'last_reply:', deal.last_reply_at,
              'wants_meeting_detected:', deal.wants_meeting_detected_at,
              'last_synthesized:', deal.last_synthesized_at)
    print('--- LinkedIn DM thread ---')
    for m in msgs[:20]:
        print(m.source, m.direction, m.sent_at, (m.body or '')[:200])
EOF
```

Captures:
- **`Lead.description`** — Voyager-scraped LinkedIn bio (authoritative profile data)
- LinkedIn name / company / URL (`Lead.first_name`, `last_name`, `company_name`, `linkedin_url`)
- LinkedIn DM history (`crm.Message` rows with `source=linkedin`)
- `Deal.state`, `last_reply_at`, `wants_meeting_detected_at`, `last_synthesized_at`
- `Lead.disqualified` (permanent exclusion flag)
- `Lead.embedding` (qualification ML signal)
- Attio linkage IDs (`attio_person_id`, `attio_company_id`, `attio_entry_id`)

**Reconciliations to surface:**
- `Person.outreach_status` should be ≥ `deal_to_outreach_status(deal)` per the don't-downgrade rule. Mismatches mean a previous human or synthesis-pass change.
- If Attio `Person.description` ≠ `Lead.description` and the Attio one looks like a wrong-person scrape (different language, different industry, etc.), flag it in the AI Note and don't trust enrichment-derived inferences.
- If `Lead.disqualified=True` but the Person has had recent meetings, that's a contradiction worth surfacing.

### Phase 4 — Pull external thread context

**4.1 Gmail** — per matched email:
```
mcp__claude_ai_Gmail__search_threads
  query:    "from:<email> OR to:<email>"
  pageSize: 3-5
```
Watch for: pricing discussions, declined offers, no-shows, follow-up commitments, slide sends.

**4.2 Drive Gemini meeting notes** — bulk search once:
```
mcp__claude_ai_Google_Drive__search_files
  query: "title contains 'FedrampGPT' and modifiedTime > '<window start>'"
```
Doc title pattern: `<event title> - YYYY/MM/DD HH:MM EST - Notes by Gemini` — match by event title + date.

**4.3 Read high-signal docs only** — Gemini docs run 30-80KB each.
- Read 2-4 most important docs in full (active commercial deals, repeat meetings, technical follow-ups).
- For others: rely on calendar metadata + Gmail signals + Attio profile description.
- Drive `read_file_content` often exceeds context — use jq on persisted-output files.

### Phase 5 — Get current Attio state (avoid clobbering)

```
mcp__attio__get-records-by-ids
  object: people
  record_ids: [<all matched person_ids>]
```
Check `ai_notes` — if non-empty, decide whether to overwrite, append, or skip per contact.

```
mcp__attio__list-records-in-list
  list: sales
```
Sample current entry stages (paginate as needed).

### Phase 6 — Compose AI Notes (per Person)

4-6 line structure:
1. Role + company context
2. Meeting history with dates
3. Key takeaways / commitments (3-5 bullets max)
4. Outstanding loops (we owe them X / they owe us Y)
5. Recommended next action

Keep plain text, escape `\n` properly. Don't include markdown headings — Attio renders the field as plain text.

### Phase 7 — Decide updates (preview first)

Per matched contact, propose:

| Field | Options | Constraint |
|---|---|---|
| `Person.outreach_status` | Invite Sent → Connected → Replied → Wants Meeting → Meeting Booked → Had Meeting → Prospecting to close → Won | Forward-only (don't-downgrade); Lost is terminal-negative but overridable |
| Sales-list entry `stage` | Prospecting → Qualification → Meeting → Closing → Won | Forward-only; Lost terminal-negative |

**Common patterns:**
- Meeting happened, no follow-up → status `Had Meeting`, stage `Meeting`
- Active commercial discussion (pricing shared, etc.) → status `Prospecting to close`, stage `Closing`
- No-show → leave status, AI Note flags reschedule action
- Declined offer / not a fit → status `Had Meeting` (factually true), stage `Lost` *with explicit confirmation*
- Mismatched profile data → flag in AI Note, don't auto-promote

**Edge cases to surface for user decision:**
- Declined-offer / advisor-only contacts (Lost vs keep)
- No-shows past their reschedule window
- LinkedIn description scraped wrong person
- Partner-track contacts (Wiz-style integration partners ≠ customers)

### Phase 8 — Output preview to user

Structured markdown with:
- Section A: Stage + status update table per matched contact
- Section B: Verbatim AI Note text per contact
- Section C: Calendar attendees not in Attio (with recommended action: add @ Prospecting / Qualification / Meeting / skip)
- Section D: Action items captured from notes (commitments not stored anywhere yet)

Ask: `go` / `go all` / `go + add missing` / `notes only` / per-item edits.

Save preview to `/tmp/attio_update_plan.md`.

### Phase 9 — Apply on approval

Parallel writes:

**Person updates (one call per person):**
```
mcp__attio__update-record
  object: people
  record_id: <id>
  values: {"ai_notes": "...", "outreach_status": "Had Meeting"}
```

**Entry stage updates:**
```
mcp__attio__update-list-entry-by-record-id
  list: sales
  parent_object: companies
  parent_record_id: <company_id>
  entry_values: {"stage": "Meeting"}
```

**For unmatched-but-add contacts:**
```
mcp__attio__create-record (companies)  → company_id
mcp__attio__create-record (people, with company link)  → person_id
mcp__attio__add-record-to-list (sales, parent=company_id)  → entry_id
```

---

## Reference data

### Sales list
- Name: `Sales` · slug: `sales` · parent object: `companies`
- Stage attribute slug: `stage`
- Entry attribute "Notes": `notes` (separate from Person.ai_notes)

### People object
- AI Notes attribute slug: `ai_notes` (text)
- Outreach status slug: `outreach_status` (select)
- Email slug: `email_addresses` (multiselect)

### Status / stage hierarchies
See `linkedin/notifications/attio.py`:
- `OUTREACH_RANK` — Person status order
- `PROGRESSION_RANK` — Sales-list entry stage order
- `should_patch_outreach_status` / `should_patch_stage` — the don't-downgrade gates

### Filter constants
- Host email: `eddy@tryfedrampgpt.com`
- Internal team emails: `ariant2013@gmail.com`, `ariantajbaka@gmail.com`
- Skip-event titles: `Standup` (recurring)

### File path conventions
- Calendar slim dump: `/tmp/cal_meetings.json`
- Match table: `/tmp/matches.json`
- Plan preview: `/tmp/attio_update_plan.md`

---

## Known token-budget gotchas

- `list_events` over 90+ days will exceed context — always allow persisted-output → jq path.
- Gemini doc `read_file_content` typically 30-80KB — only read the highest-signal 2-4 docs in full.
- Bulk `get-records-by-ids` for People can also exceed — chunk to 10-15 at a time if descriptions are long.
- Do not ask a subagent for Attio MCP work — MCP tools generally don't pass through to subagents.

## Out of scope of this workflow

- Backfilling LinkedIn DMs into `crm.Message` — see `manage.py backfill_messages`
- Importing CSVs from a separate LinkedIn account — see `manage.py import_connections`
- Synthesis-pass auto-detection of meeting intent — runs in `sync_attio` per cron tick (LLM-driven, separate from this workflow)
