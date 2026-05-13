# Human-in-the-Loop Workflows

Two interactive runbooks that sit on top of OpenOutreach's automation tier. Each one is driven by Claude in conversation, with the human reviewing/approving before anything writes to the outside world.

If you want the automation tier (daemon, `sync_sheets`, `backfill_messages`), see `system-flow.txt`. This doc is about what happens *on top* of that data.

---

## Why these are separate from the automation

| | Automation (daemon + crons) | Human workflows (these) |
|---|---|---|
| Who runs it | A scheduler / long-running process | A human in conversation with Claude |
| Output | Database rows + Sheets writes | Drafts in the Sheet, or Sheets/CRM writes you approve |
| Failure mode | Silent — keeps going on next tick | Visible — Claude waits for your nod |
| Frequency | Continuous / cron | Daily to bi-weekly |
| Input | Live LinkedIn API + Gmail | Already-persisted DB + MCP-fetched threads |

The automation gathers raw signal. The human workflows turn that signal into outbound drafts (or Sheets enrichments) that match the sender's voice and judgment. The split exists because tone, prioritization, and "should I send this today" decisions don't automate well.

---

## The two workflows

### 1. `followup-generation-workflow.md` — produce drafts to send

**Purpose:** generate per-prospect follow-up drafts you can copy/paste into LinkedIn or Gmail.

**Output:** Two Google Sheets tabs — `Arian - Followups` and `Chuka - Followups` — each divided into five sections (Met / Replied / Connected, no reply / Active in-flight / Sent). One row per Lead with the draft + ROLE + PRIORITY + MEDIUM + CONVO + a `Sent?` checkbox the operator ticks after dispatching. The `Sent` section preserves rows across runs so you have history.

**Cohorts (set by the ball-on-court classifier in Phase 1):**
- **Ball on us** — they replied, we owe an answer (most time-sensitive)
- **Cold thread** — they replied once and we've been silent ≥ 5 days
- **Active in-flight** — we sent something < 5 days ago, ball on them (visibility-only, no draft)
- **No reply yet** — accepted invite, never replied (different angle than original)
- **Met** — had a Google Meet, follow-up depends on what was discussed

**When to run:** safe to run daily. Ball-on-court classifier prevents drafting on top of fresh outbound.

**Reads from:** `crm.Message` (LinkedIn DMs + Gmail + Calendar — already ingested by data-sync, no MCP calls of its own), existing `Sent? = TRUE` rows in the Followups tabs (to skip already-handled leads), People-tab Outreach status to identify Met / Scheduling cohorts.

**Writes to:** the two Followups tabs in Google Sheets via `linkedin.notifications.sheets.write_followups()`. Optionally a `followups/YYYY-MM-DD/raw.json` archive for history.

### 2. `data-sync-workflow.md` — ingest Google data + enrich Sheets

**Purpose:** single owner of MCP-based ingestion for Google data. Pulls calendar events, Gmail threads, and Drive Gemini meeting notes; persists them to DB (`crm.Message` for Gmail, `crm.Meeting` for Calendar + Gemini); writes the synthesized AI Note + Outreach status updates to the People tab.

**Output:**
- `crm.Message` rows with `source=gmail` (raw thread data)
- `crm.Meeting` rows with calendar event facts + raw Gemini notes (one row per attended meeting)
- People tab updates: AI Notes column, Outreach status (Replied → Wants Meeting → Had Meeting → ...), Stage (Prospecting → Qualification → Meeting → Closing → Won)
- Preview-then-apply gate — Claude shows you the planned diff, you approve before any write fires

**When to run:** weekly/biweekly, or after a batch of meetings (e.g., end of a busy demo week). The followup workflow's Phase 0.5 staleness check flags when this hasn't run recently and recommends running it first.

**Reads from:** the People tab (current state), Gmail (MCP), Calendar (MCP), Drive (MCP), `crm.Message` (CRM).

**Writes to:**
- `crm.Message` (Gmail rows) and `crm.Meeting` (Calendar + Gemini rows) via DB upserts (idempotent on `(source, external_id)`).
- The People tab (with approval) via `sheets.SheetIndex.upsert_row()`.

---

## How they relate

```
        DATA-PRODUCING AUTOMATION TIER
        (covered in system-flow.txt)
        ────────────────────────────────────
              ↓ produces crm.Message rows
              ↓ produces Sheets People-tab state

        HUMAN-IN-THE-LOOP WORKFLOWS TIER  ← this doc
        ────────────────────────────────────
        followup-generation-workflow.md
          • Reads crm.Message + Gmail + Cal + Drive +
            existing Sent? rows in Followups tabs
          • Writes drafts to Arian/Chuka Followups tabs
          • You copy the Draft cell into LinkedIn / Gmail

        data-sync-workflow.md
          • Reads People tab + Gmail + Cal + Drive + crm.Message
          • Writes to People tab (preview-then-apply)
          • Updates Outreach status, Stage,
            per-row AI Notes
```

Neither workflow feeds back into the automation tier. They're consumers, not producers. That's intentional — it means they can't break the daemon or corrupt outreach state.

---

## Shared data dependency: `crm.Message` freshness

Both workflows read `crm.Message` to reason about thread state. The daemon only persists messages **once per lead, at the moment of invite-acceptance** (sweep_connections snapshot). Anything a prospect sends after that is invisible to `crm.Message` unless something else fetches it.

That something else is `manage.py backfill_messages` — see `system-flow.txt`. **If `backfill_messages` isn't on cron, both human workflows produce stale results:**
- Follow-up generation misses recent replies (drafts re-engage when the ball is actually on us with a new inbound)
- Sheets meeting sync misses post-meeting Gmail / DM context (under-attributes the conversation depth)

Verify with `crontab -l` on whichever box runs `backfill_messages` before trusting either workflow's output.

---

## When to run which

| Situation | Workflow |
|---|---|
| It's Monday morning, want to know who to message this week | `followup-generation-workflow.md` |
| Just had a heavy demo week, want the Sheets People tab to reflect reality | `data-sync-workflow.md` |
| New batch of cold-acceptances landed, want to re-engage them | `followup-generation-workflow.md` (connected-no-reply cohort) |
| A meeting happened but the prospect isn't in our DB yet | `data-sync-workflow.md` (it surfaces them; you add via `import_connections` or manual creation) |
| Daily cadence sanity check ("anything blow up overnight?") | `followup-generation-workflow.md` (ball-on-us bucket surfaces same-day inbound) |

You can run both in the same session if you want a full sweep — they don't share state, so order doesn't matter, but doing the meeting sync first means the followup generator reads cleaner sheet "already met" exclusions.

---

## What they explicitly do NOT do

- Send messages on your behalf (followup) — drafts only, you paste
- Mark leads as disqualified — they recommend it, you run a one-liner manually
- Modify `crm.Message` rows — read-only there
- Fire LinkedIn API calls outside what the existing daemon flows already do
- Run on a schedule — both are interactive sessions

If you want any of the above to be automated, that's a different conversation (and probably a different workflow file).
