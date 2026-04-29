# Human-in-the-Loop Workflows

Two interactive runbooks that sit on top of OpenOutreach's automation tier. Each one is driven by Claude in conversation, with the human reviewing/approving before anything writes to the outside world.

If you want the automation tier (daemon, `sync_attio`, `backfill_messages`), see `system-flow.txt`. This doc is about what happens *on top* of that data.

---

## Why these are separate from the automation

| | Automation (daemon + crons) | Human workflows (these) |
|---|---|---|
| Who runs it | A scheduler / long-running process | A human in conversation with Claude |
| Output | Database rows + Attio writes | Drafts you paste, or Attio writes you approve |
| Failure mode | Silent — keeps going on next tick | Visible — Claude waits for your nod |
| Frequency | Continuous / cron | Daily to bi-weekly |
| Input | Live LinkedIn API + Gmail | Already-persisted DB + MCP-fetched threads |

The automation gathers raw signal. The human workflows turn that signal into outbound drafts (or Attio enrichments) that match the sender's voice and judgment. The split exists because tone, prioritization, and "should I send this today" decisions don't automate well.

---

## The two workflows

### 1. `followup-generation-workflow.md` — produce drafts to send

**Purpose:** generate per-prospect follow-up drafts you can copy/paste into LinkedIn or Gmail.

**Output:** `followups/YYYY-MM-DD/*.txt` — six files split by cohort × sender:
- `replied_chuka.txt` / `replied_arian.txt` — drafts for leads who replied and need a response
- `connected_no_reply_chuka.txt` / `connected_no_reply_arian.txt` — re-engagement drafts for cold-but-accepted leads, tier-classified
- `met_chuka.txt` / `met_arian.txt` — post-meeting follow-ups built from LinkedIn + Gmail + Calendar + Drive Gemini notes

**Cohorts (set by the ball-on-court classifier in Phase 1):**
- **Ball on us** — they replied, we owe an answer (most time-sensitive)
- **Cold thread** — they replied once and we've been silent ≥ 5 days
- **Active in-flight** — we sent something < 5 days ago, ball on them (visibility-only, no draft)
- **Connected, no reply** — accepted invite, never replied (different angle than original)
- **Met** — had a Google Meet, follow-up depends on what was discussed

**When to run:** safe to run daily. Ball-on-court classifier prevents drafting on top of fresh outbound.

**Reads from:** `crm.Message` (LinkedIn DMs), Gmail (MCP), Calendar (`/tmp/cal_meetings.json`), Drive (Gemini meeting notes via MCP).

**Writes to:** `followups/YYYY-MM-DD/*.txt`. Nothing else. The drafts are for you to paste.

### 2. `attio-meeting-sync-workflow.md` — enrich Attio after meetings

**Purpose:** keep Attio Sales-list contacts current with what's actually happening — calendar meetings, Gmail threads, Drive meeting notes, LinkedIn DM context — and roll that up into a per-Person AI Note plus the right Outreach status / Entry stage.

**Output:** writes directly to Attio:
- Per-Person AI Notes (composed from cross-source thread context)
- Outreach status updates (Replied → Wants Meeting → Meeting Booked → Had Meeting → ...)
- Entry stage updates (Prospecting → Qualification → Meeting → Closing → Won)
- Preview-then-apply gate — Claude shows you the planned diff, you approve before any write fires

**When to run:** weekly/biweekly, or after a batch of meetings (e.g., end of a busy demo week).

**Reads from:** Attio Sales list (MCP), Gmail (MCP), Calendar (MCP), Drive (MCP), `crm.Message` (CRM).

**Writes to:** Attio (with approval). Does NOT write to `crm.Message` — read-only there.

---

## How they relate

```
        DATA-PRODUCING AUTOMATION TIER
        (covered in system-flow.txt)
        ────────────────────────────────────
              ↓ produces crm.Message rows
              ↓ produces Attio Sales list state

        HUMAN-IN-THE-LOOP WORKFLOWS TIER  ← this doc
        ────────────────────────────────────
        followup-generation-workflow.md
          • Reads crm.Message + Gmail + Cal + Drive
          • Writes to followups/*.txt
          • You paste into LinkedIn / Gmail

        attio-meeting-sync-workflow.md
          • Reads Attio + Gmail + Cal + Drive + crm.Message
          • Writes to Attio (preview-then-apply)
          • Updates Outreach status, Entry stage,
            per-Person AI Notes
```

Neither workflow feeds back into the automation tier. They're consumers, not producers. That's intentional — it means they can't break the daemon or corrupt outreach state.

---

## Shared data dependency: `crm.Message` freshness

Both workflows read `crm.Message` to reason about thread state. The daemon only persists messages **once per lead, at the moment of invite-acceptance** (sweep_connections snapshot). Anything a prospect sends after that is invisible to `crm.Message` unless something else fetches it.

That something else is `manage.py backfill_messages` — see `system-flow.txt`. **If `backfill_messages` isn't on cron, both human workflows produce stale results:**
- Follow-up generation misses recent replies (drafts re-engage when the ball is actually on us with a new inbound)
- Attio meeting sync misses post-meeting Gmail / DM context (under-attributes the conversation depth)

Verify with `crontab -l` on whichever box runs `backfill_messages` before trusting either workflow's output.

---

## When to run which

| Situation | Workflow |
|---|---|
| It's Monday morning, want to know who to message this week | `followup-generation-workflow.md` |
| Just had a heavy demo week, want Attio to reflect reality | `attio-meeting-sync-workflow.md` |
| New batch of cold-acceptances landed, want to re-engage them | `followup-generation-workflow.md` (connected-no-reply cohort) |
| A meeting happened but the prospect isn't in the Attio Sales list yet | `attio-meeting-sync-workflow.md` (it adds them) |
| Daily cadence sanity check ("anything blow up overnight?") | `followup-generation-workflow.md` (ball-on-us bucket surfaces same-day inbound) |

You can run both in the same session if you want a full sweep — they don't share state, so order doesn't matter, but doing the meeting sync first means the followup generator reads cleaner Attio "already met" exclusions.

---

## What they explicitly do NOT do

- Send messages on your behalf (followup) — drafts only, you paste
- Mark leads as disqualified — they recommend it, you run a one-liner manually
- Modify `crm.Message` rows — read-only there
- Fire LinkedIn API calls outside what the existing daemon flows already do
- Run on a schedule — both are interactive sessions

If you want any of the above to be automated, that's a different conversation (and probably a different workflow file).
