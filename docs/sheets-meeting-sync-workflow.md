# Sheets Meeting Context Sync — MCP Workflow

Reusable runbook for enriching the Google Sheets People tab with calendar / Gmail / Drive / LinkedIn DM context, then updating Outreach status, Stage, and AI Notes per contact.

Run periodically (weekly/biweekly) or after a batch of meetings.

## Prerequisites

- MCPs connected to the connected Google account: **Gmail**, **Google Calendar**, **Google Drive**
- `.env` has `GOOGLE_SHEETS_ID`, `GOOGLE_SHEETS_CREDENTIALS_PATH`, `DATABASE_URL`
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

### Phase 2 — Cross-check Sheets People tab

The People tab is keyed by `LinkedIn URL`, but meetings only give us emails. Bridge by reading the sheet + DB:

```bash
.venv/bin/python manage.py shell <<'EOF'
import json
from crm.models import Lead
from linkedin.notifications.sheets import SheetIndex

# Load the People tab once.
idx = SheetIndex.load()
url_to_row = {url: idx.get_row(url) for url in idx.url_to_row_idx}

# Build email → linkedin_url lookup (DB has authoritative Lead.email).
emails = ["<email1>", "<email2>", "..."]  # from /tmp/cal_meetings.json
matches, unmatched = [], []
for email in emails:
    lead = Lead.objects.filter(email__iexact=email).first()
    if lead and lead.linkedin_url and lead.linkedin_url in url_to_row:
        matches.append({"email": email, "lead_id": lead.id, "linkedin_url": lead.linkedin_url,
                        "row": url_to_row[lead.linkedin_url]})
    else:
        unmatched.append(email)
with open("/tmp/matches.json", "w") as f: json.dump(matches, f, indent=2)
with open("/tmp/unmatched.json", "w") as f: json.dump(unmatched, f, indent=2)
print(f"matched: {len(matches)}, unmatched: {len(unmatched)}")
EOF
```

Two buckets emerge:
- **Matched** → continue to Phase 3
- **Unmatched** → Phase 8 ("not in our CRM yet") — recommend adding via `import_connections` or manual Lead creation

### Phase 3a — Multi-affiliation tiebreaker (which company is "their company"?)

People often have multiple current affiliations: an employer + a personal LLC + advisor seats + a non-profit they founded. Pick `Lead.company_name` (and the People tab `Company` cell) using this priority order, capture the rest in AI Notes:

1. **Corporate email domain** (highest signal) — `@usda.gov` → USDA, `@prescientsecurity.com` → Prescient, `@wiz.io` → Wiz. They literally show up using that company's identity.
2. **Gmail signature block** in their replies — when they sign emails as "X, Director, Company Y", that's the company they're representing in this conversation.
3. **LinkedIn headline name-drop** — "FedRAMP Compliance Specialist **at AWS**", "Head of Compliance **at Armada**".
4. **LinkedIn `positions` array** — top entry / "Present" job from the Voyager scrape (parsed from `Lead.description` JSON).
5. **Calendar attendee email** — only useful if it's a corporate domain; useless for personal gmail.

**Practical tiebreaker rule:** `Lead.company_name` = the org they'd answer with if asked *"where do you work?"* — typically the employer for employees, the consulting LLC for consultants, the company writing the paycheck for everyone else.

**Edge cases worth flagging in AI Notes:**
- Consultants juggling multiple client engagements → primary = consulting LLC, secondary clients in AI Notes
- Multi-hat advisors → primary = current employer, advisor seats in AI Notes ("active paid advisor at X")
- Founder + employee combo (e.g. founder of non-profit while employed at AWS) → primary = the employer, founder role in AI Notes
- Stale enrichment → trust DB (`Lead.description` from Voyager scrape), mention in AI Notes

**Cascading concerns when changing `Lead.company_name`:**

The Sheets People tab has Company as a denormalized text column on each row. Changing `Lead.company_name` is straightforward:

1. Update `Lead.company_name` in the DB
2. Update the `Company` cell on the corresponding People-tab row (or wait for `sync_sheets` to do it on its next run — `sync_sheets` always overwrites Company from `Lead.company_name`, the don't-downgrade rule only protects Stage and Outreach status)
3. Recompute the company-aggregate Stage if the company change moves the Lead to a new aggregate group with different leads at different stages

No orphaned link records to clean up — Sheets has no first-class linked records, so there's nothing to break.

### Phase 3 — Cross-check OpenOutreach DB

For each matched email, query the Postgres DB to get richer context than the sheet holds. **Two distinct profile descriptions matter**:

- `crm.Lead.description` — LinkedIn bio scraped via Voyager during outreach (authoritative, our source of truth for who we actually qualified)
- People tab `Title` cell — usually mirrors `job_title` plus the LinkedIn headline. Sometimes wrong (e.g., manually entered, or scraped at a stale point in the prospect's career).

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

**Reconciliations to surface:**
- Sheet Outreach status should be ≥ `deal_to_outreach_status(deal)` per the don't-downgrade rule. Mismatches mean a previous human or synthesis-pass change.
- If People tab `Title` ≠ `Lead.description` headline, decide which is more authoritative (usually `Lead.description` if recent; sheet Title if it was edited intentionally).
- If `Lead.disqualified=True` but the contact has had recent meetings, that's a contradiction worth surfacing.

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
- For others: rely on calendar metadata + Gmail signals + sheet Title / Notes columns.
- Drive `read_file_content` often exceeds context — use jq on persisted-output files.

### Phase 5 — Read current sheet state (avoid clobbering)

```python
from linkedin.notifications.sheets import SheetIndex, COL_AI_NOTES, COL_NOTES, COL_OUTREACH_STATUS, COL_STAGE
idx = SheetIndex.load()
for url in matched_urls:
    row = idx.get_row(url)
    print(url, "→", {
        "ai_notes": row[COL_AI_NOTES],
        "notes":    row[COL_NOTES],
        "status":   row[COL_OUTREACH_STATUS],
        "stage":    row[COL_STAGE],
    })
```

Check `AI Notes` — if non-empty, decide whether to overwrite, append, or skip per contact. Notes is **human-only** (per the workflow — synthesis pass and meeting-sync should not touch it); confirm before writing there.

### Phase 6 — Compose AI Notes (per contact)

4-6 line structure:
1. Role + company context
2. Meeting history with dates
3. Key takeaways / commitments (3-5 bullets max)
4. Outstanding loops (we owe them X / they owe us Y)
5. Recommended next action

Keep plain text, escape `\n` properly. The AI Notes column accepts multi-line — use real newlines, not literal `\n` escape sequences.

### Phase 7 — Decide updates (preview first)

Per matched contact, propose:

| Field | Options | Constraint |
|---|---|---|
| `Outreach status` (sheet) | Invite Sent → Connected → Waiting → Replied → Wants Meeting → Meeting Booked → Had Meeting → Manual followup → Prospecting to close → Won | Forward-only (don't-downgrade); Lost is terminal-negative but overridable; Don't send is sticky human-set "stop" |
| `Stage` (sheet) | Prospecting → Qualification → Meeting → Closing → Won | Forward-only; Lost terminal-negative |
| `AI Notes` (sheet) | freeform multi-line | Append vs overwrite is a per-contact decision |
| `Title` (sheet) | freeform | Only update if you have higher-signal data than what's there |
| `Email addresses` (sheet) | newline-joined list | Append-only (use the union with `Lead.email`); never remove |

**Common patterns:**
- Meeting happened, no follow-up → status `Had Meeting`, stage `Meeting`
- Active commercial discussion (pricing shared, etc.) → status `Prospecting to close`, stage `Closing`
- No-show → leave status, AI Notes flags reschedule action
- Declined offer / not a fit → status `Had Meeting` (factually true), stage `Lost` *with explicit confirmation*
- Mismatched profile data → flag in AI Notes, don't auto-promote

**Edge cases to surface for user decision:**
- Declined-offer / advisor-only contacts (Lost vs keep)
- No-shows past their reschedule window
- LinkedIn description scraped wrong person
- Partner-track contacts (Wiz-style integration partners ≠ customers)

### Phase 8 — Output preview to user

Structured markdown with:
- Section A: Stage + status update table per matched contact
- Section B: Verbatim AI Notes text per contact
- Section C: Calendar attendees not in the sheet (with recommended action: add via `import_connections` / manual Lead creation / skip)
- Section D: Action items captured from notes (commitments not stored anywhere yet)

Ask: `go` / `go all` / `go + add missing` / `notes only` / per-item edits.

Save preview to `/tmp/sheets_update_plan.md`.

### Phase 9 — Apply on approval

Use `SheetIndex.upsert_row()` for the changes; it respects the don't-downgrade rules on Stage + Outreach status automatically.

```python
from datetime import datetime, timezone
from linkedin.notifications.sheets import SheetIndex, build_row_payload, COL_AI_NOTES
from crm.models import Lead

idx = SheetIndex.load()
last_synced = datetime.now(timezone.utc).date().isoformat()

for change in approved_changes:  # list of dicts from the preview
    lead = Lead.objects.get(linkedin_url=change["linkedin_url"])
    existing = idx.get_row(lead.linkedin_url) or {}

    payload = build_row_payload(
        lead=lead,
        title=change.get("title") or existing.get("Title", ""),
        emails=change.get("emails") or [],
        outreach_status=change.get("outreach_status") or existing.get("Outreach status", ""),
        stage=change.get("stage") or existing.get("Stage", ""),
        priority=existing.get("Priority", ""),
        primary_location=existing.get("Primary location", ""),
        notes=existing.get("Notes", ""),  # human-only — never modify here
        ai_notes=change["ai_notes"],
        last_synced=last_synced,
    )
    idx.upsert_row(payload)

idx.flush()
```

**For unmatched-but-add contacts:** create the Lead in the DB first (via `import_connections` if you have a LinkedIn URL, or manual Lead creation if not), then re-run Phase 5 onwards. The next `sync_sheets` cron run will surface them in the People tab automatically.

---

## Reference data

### People tab
- AI Notes column header: `AI Notes` (multi-line text)
- Notes column header: `Notes` (multi-line text — **human-only**, do not write to from any automation or this workflow)
- Outreach status column header: `Outreach status` (single-select dropdown)
- Stage column header: `Stage` (single-select dropdown)
- Email column header: `Email addresses` (newline-joined list)
- Natural key: `LinkedIn URL`

### Status / stage hierarchies
See `linkedin/notifications/sheets.py`:
- `OUTREACH_RANK` — Outreach status order (Invite Sent → ... → Won → Don't send)
- `PROGRESSION_RANK` — Stage order (Prospecting → ... → Won)
- `should_patch_outreach_status` / `should_patch_stage` — the don't-downgrade gates

### Filter constants
- Host email: `eddy@tryfedrampgpt.com`
- Internal team emails: `ariant2013@gmail.com`, `ariantajbaka@gmail.com`
- Skip-event titles: `Standup` (recurring)

### File path conventions
- Calendar slim dump: `/tmp/cal_meetings.json`
- Match table: `/tmp/matches.json`
- Plan preview: `/tmp/sheets_update_plan.md`

---

## Known token-budget gotchas

- `list_events` over 90+ days will exceed context — always allow persisted-output → jq path.
- Gemini doc `read_file_content` typically 30-80KB — only read the highest-signal 2-4 docs in full.
- `SheetIndex.load()` reads the entire People tab (~600 rows × 15 cols) in one call — fine, but don't call it repeatedly inside a loop.

## Out of scope of this workflow

- Backfilling LinkedIn DMs into `crm.Message` — see `manage.py backfill_messages`
- Importing CSVs from a separate LinkedIn account — see `manage.py import_connections`
- Synthesis-pass auto-detection of meeting intent — runs in `sync_sheets` per cron tick (LLM-driven, separate from this workflow)
