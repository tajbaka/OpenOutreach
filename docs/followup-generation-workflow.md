# Follow-up Generation Workflow

Reusable runbook for producing tailored LinkedIn DM follow-ups from `crm.Lead` + `Deal` + `Message` data, broken out by cohort and sender. Companion to `docs/data-sync-workflow.md` (which handles the Sheets side after meetings).

## When to run

Safe to run **daily**. The ball-on-court classifier (Phase 1) routes fresh outbounds to a visibility-only ACTIVE-IN-FLIGHT bucket so daily runs don't generate drafts that step on yesterday's nudge. Weekly is fine if outreach volume is low; daily is appropriate once volume picks up and same-day inbound replies need to surface fast.

## Output location

Drafts land in **Google Sheets tabs** — one per active sender:

- `Arian - Followups`
- `Chuka - Followups`
- `Athena - Followups`
- `Leili - Followups`

`write_followups()` drops and recreates whichever tabs appear in its payload, so a new sender's tab is created automatically the first time that sender has cohort rows — no manual sheet setup.

Each tab has 14 columns and is divided into five sections via merged divider rows:

1. **🤝 MET** — rows whose `Status ∈ {Had Meeting, Manual followup, Prospecting to close}`
2. **📅 SCHEDULING** — rows whose `Status ∈ {Wants Meeting, Meeting Booked}`
3. **💬 REPLIED** — rows whose `Cohort ∈ {Ball on us, Cold thread}`
4. **🌊 ACTIVE IN-FLIGHT** — rows whose `Cohort = Active in-flight` (visibility-only, no draft)
5. **✅ SENT** — preserved from prior runs (rows where the operator toggled either `Sent Email` or `Sent LinkedIn` = `Yes`)

> The "Connected, no reply" cohort was retired 2026-05-12: the daemon's `handle_follow_up` task now DMs those leads rigidly from `linkedin/icp_messages.json`, so surfacing them for manual drafting was duplicate work.

Schema (in column order):

| # | Column | Notes |
|---|---|---|
| 1 | `Name` | first + last |
| 2 | `Status` | snapshot of People-tab `Outreach status` |
| 3 | `Cohort` | dropdown — Ball on us / Cold thread / Active in-flight. This is outbound-state only; it no longer stores Met / Scheduling / Sent. |
| 4 | `ROLE` | dropdown — CSP / 3PAO / Advisor / Assessor / Channel |
| 5 | `PRIORITY` | dropdown — HIGH / MEDIUM-HIGH / MEDIUM / LOW / HOLD (conditional-format colored) |
| 6 | `Days since` | int — `(now - msgs[-1].sent_at).days` on the merged timeline regardless of direction. **Met:** `max(latest_message, latest_meeting.start_at)` so a calendar meeting counts as activity even without a message exchange. |
| 7 | `Days since connection` | int — days since `Deal.connected_at` (the moment they accepted our invite). Drives the freshness priority bump. Blank for legacy rows pre-dating the field. |
| 8 | `CONVO` | one or two sentences summarizing the *full* relationship (both mediums) |
| 9 | `Draft Email` | populated when there's real Gmail engagement (typed reply, not just a calendar acceptance) |
| 10 | `Email Link` | `=HYPERLINK("https://mail.google.com/mail/u/0/#search/<email>","<email>")` — opens Gmail search for that person |
| 11 | `Sent Email (manual toggle)` | Yes/No dropdown — operator toggles after sending; default `No` |
| 12 | `Draft LinkedIn` | populated when there's LinkedIn DM engagement (the default channel) |
| 13 | `LinkedIn Message Url` | `=HYPERLINK(...)` deep-link into the DM thread, falls back to profile URL |
| 14 | `Sent LinkedIn (manual toggle)` | Yes/No dropdown — same shape as Sent Email |

Operator workflow per row: copy the relevant `Draft *` cell, click the matching `* Link` / `* Url` to open the conversation, paste + send, then flip the matching `Sent ...` toggle to `Yes`. The next run preserves any row with either Sent toggle = Yes under the SENT section. Hidden columns (View → Hide column) survive across runs — the helper snapshots `hiddenByUser` before recreating the tab and re-applies it.

A `followups/YYYY-MM-DD/raw.json` archive is also written (per-run snapshot of the rows + classifier state) so you can compare what was drafted vs. what got sent. The archive is for history only; the sheet is the working surface.

## Cohorts

Six buckets the workflow generates files / sections for. The drafted ones are split per active sender (Chuka, Arian, etc.). Active-in-flight is a section inside the relevant draft file, not a separate file.

| Cohort | Filter | Sheet section / Cohort value | Why follow up |
|---|---|---|---|
| **Met (post-meeting)** | People-tab `Outreach status ∈ MET_STATUSES` ({Had Meeting, Manual followup, Prospecting to close}) AND not disqualified | 🤝 MET section, routed by `Status` | Real post-meeting follow-up. Drafter leads with the deliverable owed from the call (Loom, repo, doc, intro) or a forward-looking question rooted in what was discussed. |
| **Scheduling (pre-meeting)** | People-tab `Outreach status ∈ PRE_MEETING_STATUSES` ({Wants Meeting, Meeting Booked}) AND not disqualified | 📅 SCHEDULING section, routed by `Status` | Meeting is on the track but hasn't happened. Drafter resurfaces time slots (Wants Meeting) or sends a light pre-meeting confirm (Meeting Booked). Never deliverable-first — we haven't met yet. |
| **Replied, ball on us** | `Deal.state=Connected` AND latest message is **inbound** AND not in pre-meeting / met sets AND not disqualified | 💬 REPLIED, `Cohort = Ball on us` | They replied last, we owe a response. Most time-sensitive cohort — these need same-day or next-day attention. |
| **Replied, cold thread (ball on us to nudge)** | `Deal.state=Connected` AND has ≥1 inbound AND latest message is outbound ≥ `NUDGE_AFTER_DAYS` (default 5) old AND not in pre-meeting / met sets AND not disqualified | 💬 REPLIED, `Cohort = Cold thread` | They engaged once and went quiet. Re-engagement nudge needed. |
| **Active / in-flight** | Latest message is outbound, < `NUDGE_AFTER_DAYS` old | 🌊 ACTIVE IN-FLIGHT, `Cohort = Active in-flight` (no Draft) | They've had a recent reach-out from us; sending again today would step on it. Listed for visibility so they don't disappear from daily runs. |
| **Replied, polite no** | Same filter as Replied cohorts but inbound message contains a `NO_PHRASES` decline phrase | Surfaced in the run's SUMMARY (printed to stdout, not written to sheet) → recommend `Lead.disqualified=True` | Don't follow up. Disqualify the Lead. |

> The "Connected, no reply" cohort was previously surfaced here and drafted into the SENIOR/T1 tiers; it's now handled programmatically by the daemon (`linkedin/tasks/follow_up.py` + `linkedin/icp_messages.json`). Leads who accept an invite and never reply get a rigid template DM from the daemon and are then marked Completed — no operator review step.

**Freshness posture is separate from cohort.** The cohort answers "whose ball is it?" The freshness posture answers "how should this sound given the time gap?" A stale thread can still be `Ball on us`, but the draft must reopen the conversation instead of pretending the last message was recent.

## Step-by-step

### Phase 0 — Re-ground in the FedrampGPT codebase (MANDATORY, every run)

Before any cohort work, the orchestrator (the agent running this workflow) reads the actual FedrampGPT source code at `/Users/admin/Desktop/Projects/FedRampGPT/` so the drafts can only mention features that exist. Operator feedback (2026-05-07): drafts kept promising things that weren't shipped (sometimes things that didn't exist at all), because the prompt assumed a feature inventory that drifted from the code. The fix is to **rebuild that inventory live, every run, by reading code — not READMEs, not markdown docs, not commit messages**.

**Why every run, not a cached `FEATURES.md`:** features ship and break between runs. A static doc would be wrong by the next sprint and we wouldn't notice until a prospect called it out on a demo. Re-reading source on each run is a few minutes; the cost of getting it wrong is a permanent trust break.

**What to read (source code only):**

1. **Backend** — `/Users/admin/Desktop/Projects/FedRampGPT/backend/`. For each top-level Django app (`agents/`, `assessment/`, `boundaries/`, `compliance_checks/`, `controls/`, `evidence/`, `poam/`, `ssp/`, etc.):
   - Read `models.py` to learn what's actually persisted.
   - Read `views.py` / `urls.py` / `serializers.py` to learn what API surface is exposed.
   - For LLM-touching apps (anything in `agents/`, anywhere `openai`/`anthropic`/`litellm` is imported): read the prompt construction code so the drafts don't claim a capability that's a one-line stub.
2. **Frontend** — `/Users/admin/Desktop/Projects/FedRampGPT/frontend/src/`. Read the router config (`App.tsx` / `routes.tsx`) and one page-level component per major route. Confirm a feature has a UI surface before claiming it does.
3. **Integrations** — grep across `backend/` and `frontend/src/` for each integration name a draft might mention (AWS, Azure, GCP, Wiz, CrowdStrike, GitHub, Jira, ServiceNow, Slack, Okta, Splunk, Datadog). A README mention or a TODO comment is **not** a shipped integration. Look for actual SDK calls and configured credentials.
4. **Continuous monitoring / 20x / KSI mapping** — these get name-checked in outreach a lot. Find the code that backs them. Verify what's actually wired vs. talked-about-in-comments.
5. **POAM workflow** — same drill: creation, closure trails, DR (Deviation Request) generation. Read the views, not the README.

**Skip:**
- READMEs (`README.md`, `*.md`), docstrings, marketing copy in `frontend/src/pages/landing/*`. They over-promise. The whole point of this phase is to trust source over docs.
- Standard Django scaffolding (`accounts/` if it's stock auth, `config/`, `core/` if just settings).

**Output of Phase 0:** an in-context understanding the orchestrator carries forward into Phase 5 drafting. Optionally write a one-time scratch summary to `/tmp/fedrampgpt_inventory.md` (path the humanizer can also reference). Not a checked-in artifact — it goes stale.

**Drafting rule that depends on this phase:** every concrete feature claim in a draft must be traceable back to a file the orchestrator read in Phase 0. If you can't name the file, delete the sentence. Hedge language ("scoping", "on the roadmap") is allowed only if there's a stub / planning comment in the code that supports it.

### Phase 0.5 — Prerequisite staleness check (MANDATORY)

Followup is the most downstream workflow in the chain (`import-connections → backfill-messages → data-sync → followup`). It depends on all three upstreams being fresh per operator. If any are stale, the drafts suffer in predictable ways — calendar attendees show as unmatched, LinkedIn DM context is missing from the merged timeline, Met-cohort drafter flies half-blind without fresh Gemini meeting notes.

The shared helper at `linkedin/workflow_prereqs.py` does the check. Same shape as data-sync's Phase 0.5 — TTY-interactive prompt, non-TTY auto-continue with stderr warning. The dependency graph is encoded once in `WORKFLOW_PREREQS` so when it changes, every workflow's Phase 0.5 picks up the new shape automatically.

Followup is **global** (operator=""), but its prereqs are **per-operator**. So the orchestrator runs the check once per active operator and surfaces all warnings in one combined view:

```python
from linkedin.workflow_prereqs import check_prereqs, format_report, prompt_if_stale, StalenessReport

OPERATORS = ["Chuka", "Arian", "Athena", "Leili"]  # add new operators as they join

reports = [check_prereqs("followup", operator=op) for op in OPERATORS]
for r in reports:
    print(format_report(r))

if any(r.has_warnings for r in reports):
    composite = StalenessReport(
        workflow="followup",
        operator="multi-operator",
        rows=[row for r in reports for row in r.rows if row.is_warning],
    )
    if not prompt_if_stale(composite, print_report=False):
        raise SystemExit("Aborted by operator.")
```

**How to decide:** if any per-operator prereq is flagged ⚠, the operator should probably re-run that upstream first. Most common case: if `data-sync` for one operator is stale and that operator has had recent meetings (look at the People tab Outreach status for fresh `Wants Meeting` / `Meeting Booked` entries that didn't exist last followup run), the Met-cohort drafter will be flying half-blind without fresh calendar/Gemini context. Run `data-sync` for that operator first, then the followup.

**Skipping is fine** for ball-on-us / cold-thread cohorts since they depend mostly on `crm.Message` which is updated by `backfill-messages` and the daemon. Met-cohort quality is where this matters most.

**Recent-followup-overwrite guard:** the helper doesn't flag "followup ran <2h ago" today — that was a single-purpose check in the original Phase 0.5 inline block, kept here as a sanity reminder for the operator. If the last followup row in `WorkflowRun` is from earlier in the same session and you haven't sent any of those drafts yet, re-running will overwrite the sheet (operator's `Sent` toggles still preserve verbatim, but the unsent drafts get regenerated).

### Phase 1 — Pull cohort data (ball-on-court classifier)

The classifier is **ball-on-court**, not freshness-based. The right question for a daily run is "whose move is it?" — not "how recent was the last message?" A 24-hour-old outbound shouldn't trigger a re-nudge (the prospect hasn't had time), but a 24-hour-old *inbound* absolutely should surface (we owe a reply). This was a deliberate fix on 2026-04-28 after the previous freshness filter silently hid Mark Milton (calendar-invite-just-sent) and similarly classified active threads as "too fresh."

```bash
.venv/bin/python manage.py shell <<'EOF'
import json
from datetime import datetime, timedelta, timezone
from crm.models import Lead, Deal, Message
from linkedin.notifications.sheets import (
    read_followup_sent_rows,
    read_followup_templates,
    read_icp_goals,
    SheetIndex,
    COL_LINKEDIN_URL, COL_OUTREACH_STATUS,
    MET_STATUSES, PRE_MEETING_STATUSES,
    FU_ROLE_TO_ICP,
)

# Skip leads the operator already ticked Sent? in either tab on a prior
# run. Those rows will be preserved verbatim under ✅ SENT by write_followups()
# below — no need to re-classify or re-draft for them.
#
# Match by Name (case-insensitive, whitespace-normalized): the followups
# tab has Name but no profile URL column (LinkedIn Message Url is a thread
# deep-link, different shape). URL-based matching here silently failed —
# every prior run re-drafted Sent=Yes rows, and write_followups's
# caller-wins dedupe then clobbered the operator's toggle when the tab
# rebuilt. Name reconstructed from Lead.first_name + last_name matches
# the same string the workflow writes into the tab in Phase 6.
def _norm_name(s: str) -> str:
    return " ".join((s or "").split()).lower()

ALREADY_SENT_NAMES: set[str] = set()
for op in ("Arian", "Chuka", "Athena", "Leili"):
    for r in read_followup_sent_rows(op):
        nm = _norm_name(r.get("Name", ""))
        if nm:
            ALREADY_SENT_NAMES.add(nm)

# Leads on the meeting track are pulled from the People tab where Outreach
# status is one of:
#   - MET_STATUSES (Had Meeting, Manual followup, Prospecting to close) →
#     post-meeting; lands in 🤝 MET cohort, drafter leads with deliverable
#   - PRE_MEETING_STATUSES (Wants Meeting, Meeting Booked) →
#     pre-meeting; lands in 📅 SCHEDULING cohort, drafter resurfaces slots
#     or sends pre-meeting confirm (NEVER deliverable-first — we haven't met)
# Failure to load the sheet shouldn't block the rest of the cohort run —
# both cohorts will just be empty for that run.
#
# Past bugs (2026-05-11):
#   (a) MET_STATUSES used to only include Wants/Booked/Had, so leads the
#       operator manually advanced to "Prospecting to close" / "Manual
#       followup" fell back to the ball-on-court classifier and were
#       treated as cold-thread re-engagements. Fixed by adding them.
#   (b) Then MET_STATUSES included Wants Meeting / Meeting Booked which
#       are pre-meeting states with fundamentally different draft strategies
#       (slot pin / pre-meeting confirm vs. post-meeting follow-up). They
#       got split out into PRE_MEETING_STATUSES → 📅 SCHEDULING section.
ALREADY_MET_URLS: set[str] = set()
ALREADY_PRE_MEETING_URLS: set[str] = set()
_met_url_to_status: dict[str, str] = {}
_pre_meeting_url_to_status: dict[str, str] = {}
try:
    _idx = SheetIndex.load()
    _url_col = _idx.actual_index_0[COL_LINKEDIN_URL]
    _status_col = _idx.actual_index_0[COL_OUTREACH_STATUS]
    for _row in _idx.rows[1:]:
        _url = (_row[_url_col] if _url_col < len(_row) else "").strip()
        _status = (_row[_status_col] if _status_col < len(_row) else "").strip()
        if not _url:
            continue
        if _status in MET_STATUSES:
            ALREADY_MET_URLS.add(_url)
            _met_url_to_status[_url] = _status
        elif _status in PRE_MEETING_STATUSES:
            ALREADY_PRE_MEETING_URLS.add(_url)
            _pre_meeting_url_to_status[_url] = _status
except Exception as _e:
    print(f"warning: could not load People tab for Met/Scheduling cohorts: {_e}")

NUDGE_AFTER_DAYS = 5  # how long to wait before nudging an unanswered outbound
ACTIVE_THREAD_DAYS = 7
WARM_THREAD_DAYS = 21
STALE_THREAD_DAYS = 60
COLD_THREAD_DAYS = 90
now = datetime.now(timezone.utc)
nudge_cutoff = now - timedelta(days=NUDGE_AFTER_DAYS)

def _days_since_dt(dt):
    return (now - dt).days if dt else None

def _latest_by_direction(msgs, direction):
    return next((m for m in reversed(msgs) if m.direction == direction), None)

def _freshness_context(klass, latest_any, latest_inbound, latest_outbound, latest_meeting=None):
    """Return (conversation_freshness, draft_posture, freshness_reason).

    Cohort decides whether the row belongs in Ball on us / Cold thread /
    Active in-flight / Met / Scheduling. Freshness decides whether the draft
    can continue the old thread directly or must reopen with light memory.
    """
    anchor_candidates = []
    if latest_any:
        anchor_candidates.append(latest_any.sent_at)
    if klass == "met" and latest_meeting:
        anchor_candidates.append(latest_meeting.start_at)
    anchor = max(anchor_candidates) if anchor_candidates else None
    age = _days_since_dt(anchor)

    if age is None:
        return ("unknown", "new_touch", "no dated conversation anchor")
    if klass == "active_in_flight":
        return ("active", "hold", "latest outbound is still fresh")
    if age <= ACTIVE_THREAD_DAYS:
        return ("active", "reply", "continue the thread directly")
    if age <= WARM_THREAD_DAYS:
        return ("warm", "light_followup", "reference prior context lightly")
    if age <= STALE_THREAD_DAYS:
        return ("stale", "reopen", "reopen the thread; do not write as if it is ongoing")
    if age <= COLD_THREAD_DAYS:
        return ("cold", "memory_reopen", "treat as a new touch with light memory")
    return ("archival", "skip_or_new_reason", "draft only with a fresh external reason")

def classify(lead):
    """
    Returns one of:
      - 'ball_on_us'        : latest msg is inbound, we owe a reply         → DRAFT
      - 'cold_thread'       : latest is outbound, ≥ NUDGE_AFTER_DAYS old    → DRAFT (nudge)
      - 'active_in_flight'  : latest is outbound, < NUDGE_AFTER_DAYS old    → VISIBILITY ONLY
      - 'no_inbound'        : zero inbound messages                         → SKIP (daemon handles)
      - 'no_messages'       : edge case, skip
    """
    msgs = list(Message.objects.filter(lead=lead).order_by("sent_at"))
    if not msgs:
        return ('no_messages', None, [])
    has_inbound = any(m.direction == 'inbound' for m in msgs)
    if not has_inbound:
        # Connected-no-reply lane belongs to the daemon
        # (linkedin/tasks/follow_up.py + icp_messages.json) as of 2026-05-12.
        # Surfacing them for manual draft was duplicate work.
        return ('no_inbound', None, msgs)
    latest = msgs[-1]
    if latest.direction == 'inbound':
        return ('ball_on_us', latest, msgs)
    # latest is outbound
    if latest.sent_at < nudge_cutoff:
        return ('cold_thread', latest, msgs)
    return ('active_in_flight', latest, msgs)

# Cohorts: replied (ball_on_us OR cold_thread → DRAFT), active in-flight
# (latest outbound < NUDGE_AFTER_DAYS old → VISIBILITY only), met / scheduling
# (sourced from the People tab independently above).
cohort_drafts = []          # ball_on_us + cold_thread
cohort_active_in_flight = [] # active_in_flight (visibility only)
cohort_met = []              # post-meeting follow-up (🤝 MET)
cohort_pre_meeting = []      # pre-meeting (📅 SCHEDULING — Wants/Booked)

def _build_row(lead, klass, msgs, latest=None, extra=None):
    """Shared row-dict builder used by both the Met pass and the
    ball-on-court loop, so Met rows have the same shape as everything else."""
    deal = lead.deal_set.order_by('-creation_date').first()
    try: prof = json.loads(lead.description) if lead.description else {}
    except Exception: prof = {}
    latest_any = msgs[-1] if msgs else None
    # Latest past meeting from crm.Meeting — used by Met cohort `Days since`
    # math and by the drafter prompt (Phase 5) for raw Gemini context.
    from linkedin.notifications.calendar_events import latest_meeting_for
    latest_meeting = latest_meeting_for(lead)
    # Cohort-specific anchor for "Days since" — see schema doc above.
    # - met: max(latest_message, latest_meeting.start_at) — a calendar
    #   meeting counts as activity even if no message was exchanged around it,
    #   so anchoring purely on `crm.Message` would under-report time-since-
    #   contact for met leads.
    # - everything else: most recent message on merged timeline regardless
    #   of direction.
    if klass == "met":
        candidates = []
        if latest_any:
            candidates.append(latest_any.sent_at)
        if latest_meeting:
            candidates.append(latest_meeting.start_at)
        anchor = max(candidates) if candidates else None
        days_since = (now - anchor).days if anchor else None
    else:
        days_since = (now - latest_any.sent_at).days if latest_any else None
    latest_inbound = _latest_by_direction(msgs, "inbound")
    latest_outbound = _latest_by_direction(msgs, "outbound")
    freshness, draft_posture, freshness_reason = _freshness_context(
        klass, latest_any, latest_inbound, latest_outbound, latest_meeting,
    )
    row = {
        "lead_id": lead.id, "deal_id": (deal.id if deal else None),
        "first_name": lead.first_name, "last_name": lead.last_name,
        "company_name": lead.company_name,
        "linkedin_url": lead.linkedin_url or "", "email": lead.email or "",
        "headline": prof.get("headline",""),
        "summary": (prof.get("summary","") or "")[:1500],
        "primary_sender": next(iter([m.sender for m in msgs if m.direction=="outbound"]), ""),
        "classification": klass,
        "latest_direction": (latest.direction if latest else None),
        "latest_at": (str(latest.sent_at)[:19] if latest else None),
        "latest_any_direction": (latest_any.direction if latest_any else None),
        "latest_any_at": (str(latest_any.sent_at)[:19] if latest_any else None),
        "last_inbound_at": (str(latest_inbound.sent_at)[:19] if latest_inbound else None),
        "last_outbound_at": (str(latest_outbound.sent_at)[:19] if latest_outbound else None),
        "days_since": days_since,
        "days_since_inbound": _days_since_dt(latest_inbound.sent_at) if latest_inbound else None,
        "days_since_outbound": _days_since_dt(latest_outbound.sent_at) if latest_outbound else None,
        "conversation_freshness": freshness,
        "draft_posture": draft_posture,
        "freshness_reason": freshness_reason,
        "messages": [{"source": m.source, "d": m.direction, "t": str(m.sent_at)[:19], "b": (m.body or "")[:600], "s": m.sender} for m in msgs],
        # Meet context — drafter reads raw Gemini transcript for Met cohort.
        # Truncated to 3500 chars in the JSON dump to keep row size sane;
        # drafter can re-fetch the full row if it needs more.
        "latest_meeting_at": (str(latest_meeting.start_at)[:19] if latest_meeting else None),
        "latest_meeting_title": (latest_meeting.title if latest_meeting else ""),
        "latest_meeting_gemini_notes": ((latest_meeting.gemini_notes_raw or "")[:3500] if latest_meeting else ""),
    }
    if extra:
        row.update(extra)
    return row

# Met pass — strictly post-meeting leads. Query independently so a
# meeting can exist on Calendar with zero recorded DMs / emails and the
# lead still surfaces in 🤝 MET.
met_leads_qs = (Lead.objects
    .filter(disqualified=False, linkedin_url__in=ALREADY_MET_URLS)
    .distinct())

for lead in met_leads_qs:
    if _norm_name(f"{lead.first_name} {lead.last_name}") in ALREADY_SENT_NAMES:
        continue  # operator already sent the post-meeting follow-up
    msgs = list(Message.objects.filter(lead=lead).order_by("sent_at"))
    latest = msgs[-1] if msgs else None
    cohort_met.append(_build_row(lead, "met", msgs, latest, extra={
        "outreach_status": _met_url_to_status.get(lead.linkedin_url or "", ""),
    }))

# Scheduling pass — pre-meeting leads (Wants Meeting / Meeting Booked).
# These have a different draft strategy than Met: resurface time slots
# (Wants Meeting) or send a pre-meeting confirm (Meeting Booked). Never
# deliverable-first — we haven't met them yet.
pre_meeting_qs = (Lead.objects
    .filter(disqualified=False, linkedin_url__in=ALREADY_PRE_MEETING_URLS)
    .distinct())

for lead in pre_meeting_qs:
    if _norm_name(f"{lead.first_name} {lead.last_name}") in ALREADY_SENT_NAMES:
        continue
    msgs = list(Message.objects.filter(lead=lead).order_by("sent_at"))
    latest = msgs[-1] if msgs else None
    cohort_pre_meeting.append(_build_row(lead, "pre_meeting", msgs, latest, extra={
        "outreach_status": _pre_meeting_url_to_status.get(lead.linkedin_url or "", ""),
    }))

# Iterate by Lead (not Deal) so leads whose entire conversation lives in
# Gmail — no LinkedIn invite was ever sent / accepted, so no Deal was
# created — still surface. Examples: Stephen Pratt (Sentar) came in via
# Norris Carden's email intro on 2026-04-22 with zero LinkedIn touch.
# Gate on (a) at least one Message in either direction, and (b) not
# disqualified. The ball-on-court classifier reads the merged timeline
# anyway so it handles email-only threads correctly without further fix.
leads_qs = (Lead.objects
    .filter(disqualified=False, messages__isnull=False)
    .distinct())

for lead in leads_qs:
    if _norm_name(f"{lead.first_name} {lead.last_name}") in ALREADY_SENT_NAMES:
        continue  # preserved under ✅ SENT
    if lead.linkedin_url and lead.linkedin_url in ALREADY_MET_URLS:
        continue  # already added to cohort_met above
    if lead.linkedin_url and lead.linkedin_url in ALREADY_PRE_MEETING_URLS:
        continue  # already added to cohort_pre_meeting above
    klass, latest, msgs = classify(lead)
    if klass in ('no_messages', 'no_inbound'):
        # 'no_inbound' = connected-no-reply lane, owned by the daemon.
        continue
    base = _build_row(lead, klass, msgs, latest)
    if klass == 'active_in_flight':
        cohort_active_in_flight.append(base)
    else:  # ball_on_us or cold_thread
        cohort_drafts.append(base)

with open("/tmp/followup_drafts.json","w") as f: json.dump(cohort_drafts, f, indent=2)
with open("/tmp/followup_active_in_flight.json","w") as f: json.dump(cohort_active_in_flight, f, indent=2)
with open("/tmp/followup_met.json","w") as f: json.dump(cohort_met, f, indent=2)
with open("/tmp/followup_pre_meeting.json","w") as f: json.dump(cohort_pre_meeting, f, indent=2)

# ICP Goals snapshot — per-ICP strategic Goal from the operator's
# `ICP Goals` tab, plus the canonical ROLE→ICP mapping. Phase 5
# reads `goal` only — it's strategic context (what each ICP's draft is
# angling for). Dumping here (vs. relying on Phase 5 to call
# read_icp_goals() itself) ensures `goal` is a hard input dependency.
_icp_goals = {}
try:
    _icp_goals = read_icp_goals()
except Exception as _e:
    print(f"warning: could not load ICP Goals tab: {_e}")
with open("/tmp/icp_goals.json","w") as f:
    json.dump({"role_to_icp": FU_ROLE_TO_ICP, "templates": _icp_goals}, f, indent=2)

print(
    f"drafts: {len(cohort_drafts)}, active-in-flight: {len(cohort_active_in_flight)}, "
    f"met: {len(cohort_met)}, pre-meeting: {len(cohort_pre_meeting)}, "
    f"icp-buckets: {len(_icp_goals)}"
)
EOF
```

The output splits into four cohort files plus the templates snapshot:

- `/tmp/followup_drafts.json` — leads that need a draft. Mix of "ball on us, draft a reply" and "cold thread, draft a nudge." The `latest_direction` field tells you which kind; `conversation_freshness` and `draft_posture` tell the drafter whether to continue the thread directly, reopen it, or skip unless there is a fresh reason.
- `/tmp/followup_pre_meeting.json` — leads with Outreach status `Wants Meeting` or `Meeting Booked`. Drafter resurfaces time slots or sends pre-meeting confirms. Never deliverable-first.
- `/tmp/followup_active_in_flight.json` — visibility only. Listed in the SUMMARY/ACTIVE section of the output file with a one-line state, no draft. These are the leads that under the old freshness filter would have silently disappeared.
- `/tmp/followup_met.json` — post-meeting follow-up cohort. Sourced from the People tab's Outreach status (`Had Meeting` / `Manual followup` / `Prospecting to close`). Each entry carries an `outreach_status` field so Phase 5 can pick the right post-meeting frame. These land in the 🤝 MET section in Phase 6, but the `Cohort` cell should still reflect outbound state rather than `Met`.
- `/tmp/icp_goals.json` — snapshot of the operator's `ICP Goals` tab plus the canonical ROLE→ICP mapping. Shape: `{"role_to_icp": {ROLE: ICP}, "templates": {ICP: {"goal": str}}}`. Phase 5 reads **`goal` only** — it tells the drafter what strategic outcome each ICP's draft is angling for (e.g., advisors → referral-into-CSP-clients, CSPs → design-partner/beta).

Leads with zero inbound messages no longer surface here — the daemon (`linkedin/tasks/follow_up.py`) DMs them rigidly from `linkedin/icp_messages.json` on its own schedule. The classifier still walks them so the per-row build doesn't fail, but the `'no_inbound'` branch in `classify()` drops them before any cohort accumulator.

`NUDGE_AFTER_DAYS = 5` is the threshold for "cold thread" — tunable. Five days catches the bulk of normal B2B reply cadence.

### Phase 2 — Classify replies into sub-buckets

For the **drafts** cohort, scan inbound message text for decline phrases (these are polite-no candidates):

```python
NO_PHRASES = [
    "not interested", "not a fit", "not the best audience",
    "best of luck", "wishing you the best", "i'm good", "i'm not able",
    "no opportunity", "not the right time", "timing is not right",
    "do not play", "unable to participate", "appreciate staying in touch",
    "no longer with",  # job change
    "may be a coi",    # conflict of interest
    "seeking a long-term role",  # job hunting
    "our client is hiring",  # recruiter spam
]

# Any inbound containing a NO_PHRASE = disqualify candidate (write to /tmp/polite_no_candidates.json,
# don't draft a follow-up, recommend Lead.disqualified=True in the SUMMARY).
# Everything else in /tmp/followup_drafts.json proceeds to Phase 3+ for drafting.
```

The old "last outbound within 5 days = ACTIVE, skip" rule is gone — the ball-on-court classifier in Phase 1 already handled freshness correctly by routing fresh outbounds to `followup_active_in_flight.json`.

### Phase 3 — Bucket by sender

`Message.sender` field holds who sent each outbound (e.g., "chukwuka agu", "Arian Taj"). For each candidate, the primary sender = the most-frequent outbound sender on that thread. One follow-up file per sender.

If a sender has zero candidates in a cohort, still create the file with a short "no leads to follow up here yet" note. Easier to scan than missing files.

### Phase 3b — Gmail context (READ-ONLY, from DB)

**Followup no longer pulls Gmail itself.** Gmail ingestion moved to the data-sync workflow (`docs/data-sync-workflow.md`) on 2026-05-11 — separation of concerns means MCP ingestion is owned by data-sync, and followup is a pure consumer.

Phase 1's `_build_row` already reads the merged timeline via `Message.objects.filter(lead=lead).order_by("sent_at")` which naturally spans `source ∈ {linkedin, gmail, calendar}`. So as long as data-sync has run recently, Gmail context is already in `crm.Message` and the ball-on-court classifier sees it transparently — no extra code in followup.

**Staleness contract:** if data-sync hasn't run since the operator's most recent Gmail activity, followup will miss those emails. Phase 0.5 (staleness check) flags this — if `WorkflowRun.objects.filter(name='data-sync', operator=<op>).latest('completed_at')` is more than a day old AND there's known recent meeting/Gmail activity, the operator gets prompted to run data-sync first.

**No re-classification call needed in followup** — the inline classifier in Phase 1 reads the merged DB timeline and produces correct ball-on-court results.

### Phase 5 — Draft

**Freshness posture gate (mandatory before writing copy):**

Before drafting each row, read `row["conversation_freshness"]`, `row["draft_posture"]`, `row["days_since"]`, `row["days_since_inbound"]`, and `row["days_since_outbound"]`. Cohort tells you whether the row belongs in Ball on us / Cold thread / Met / Scheduling. Freshness posture tells you how the message should sound. Never write as if the last thread is active when `draft_posture` is `reopen`, `memory_reopen`, or `skip_or_new_reason`.

| `draft_posture` | Typical age | Drafting rule |
|---|---:|---|
| `reply` | 0-7 days | Continue the thread directly. Answer the latest inbound first, then ask the next low-friction question. |
| `light_followup` | 8-21 days | Reference prior context lightly, then move forward. One memory cue is enough. |
| `reopen` | 22-60 days | Reopen the conversation. Name the old context briefly, then give them an easy current reason to respond. Do not imply the thread is still warm. |
| `memory_reopen` | 61-90 days | Treat it like a new touch with one light memory from the old exchange. The old thread is context, not momentum. |
| `skip_or_new_reason` | 91+ days | Do not draft from the stale thread alone. Draft only if there is a fresh external reason: new product capability verified in Phase 0, a recent company/profile trigger, a new FedRAMP 20x change, or a concrete asset we owe them. Otherwise leave the draft blank, set/keep priority as `HOLD`, and flag it in the SUMMARY. |
| `hold` | active in-flight | No draft. The row is visibility-only. |

Stale examples:

- Bad at 75 days: "Following up on our last thread about evidence automation. Want to grab time next week?"
- Better at 75 days: "We connected a while back around Avaya's 20x work. Quick question, are you still in the early scoping phase, or has evidence ownership become the thing slowing the team down?"
- Bad at 120 days with no new trigger: "Just checking in."
- Better at 120 days: no draft unless there is a current reason to write.

For `pre_meeting` and `met` rows, freshness still applies. If a meeting is actually upcoming, the calendar state wins. If an old `Wants Meeting` / `Meeting Booked` row is 22+ days stale and there is no current calendar event, do not write as if slots or the meeting are live. Reopen with context first, then ask whether the topic is still active. If a post-meeting deliverable is 22+ days late, lead with the deliverable or a fresh artifact, not with an apology.

**Tone rules (learned from user feedback):**
- No em dashes. Use commas, periods, or restructure.
- No "Sharper:", "Easier path:", "Real ask:" connectors. Just say the thing.
- No sales-deck phrasing: "real decision authority and budget", "channel mechanics", "value prop", "purpose-built for".
- No all-caps emphasis ("DESIGN-PARTNER DEAL TERMS ALREADY SHARED").
- No consultant jargon ("your read on", "drop X into your stack").
- Short. 3-5 sentences max for a follow-up DM.
- **No apologetic openers.** `"my fault on the gap"`, `"sorry for the gap"`, `"my apologies"`, `"sorry about the delay"` — cut all of them. Operator feedback 2026-05-07: filler with no payload, undermines the message. If we owe a deliverable, name the deliverable. "Sending the agent-orchestration repo now." not "My fault on the gap, here's the agent-orchestration repo." Same number of words on the apology line, zero of them carry information.

**Structure:**
1. First name greeting + frame (cold-restart, deliverable-first, profile-derived hook — depending on whose ball)
2. One specific concrete reference to their work (extracted from their LinkedIn headline or summary or last DM)
3. The ask, framed as a Loom rather than a call when possible
4. "No pitch attached" or similar low-friction closer

**Frames by ball-on-court (vary across the cohort — none of these patterns should appear in more than ~10% of drafts in a single run):**
- Their ball, gone cold: lead with a profile-derived question or a concrete update on what's shipped since. Rotate openers: "Picking this back up.", "Quick one,", "Saw [X] go live recently — curious how that lands at [their company].", or open with the ask directly.
- Our ball, gone cold: lead with the deliverable. "Sending the [thing] now." Not the apology — drop it entirely.
- Cold lead, never replied: profile-derived hook (their background, prior role, recent post). Skip "no rush, figured I'd send one more note since I never heard back" — worn out.
- **Pre-meeting cohort (`classification: "pre_meeting"`)**: meeting agreed to but not yet held. Branch on `outreach_status`:
  - `Wants Meeting` — they've said yes but haven't picked a slot. Resurface the time slots from the merged timeline (your last outbound likely already proposed them) plus a one-line specific hook from the conversation. No new ask, just a nudge for the slot pick. NEVER deliverable-first — we haven't met.
  - `Meeting Booked` — pre-meeting confirm. Light touch ("looking forward Wed at 2pm PT, here's the doc I'll have queued up"). Don't re-pitch. Don't ask anything that should wait until the call.
- **Met cohort (`classification: "met"`)**: post-meeting follow-up. Branch on `outreach_status`:
  - `Had Meeting` / `Manual followup` / `Prospecting to close` — send the deliverable you owed from the call (Loom, repo link, doc, intro). If no deliverable owed, send a light async question that picks up where the call left off, **rooted in `row["latest_meeting_gemini_notes"]`** so it references something concrete from the actual meeting transcript (the raw Gemini notes from `crm.Meeting.gemini_notes_raw`, populated by the data-sync workflow). Never lead with "thanks for the time" — too generic. If `latest_meeting_gemini_notes` is empty (Gemini doc not yet pulled for this meeting, or data-sync hasn't run), fall back to the People-tab AI Notes prose summary; if both are empty, prompt the operator to run data-sync first.

**Calibrate the ask to engagement temperature.** This is the single biggest unforced error in re-engagement: re-asking for a meeting when the lead barely engaged the first time. Sales hat on. Match the ask to where the relationship actually is, not where you wish it was.

| Engagement signal | Right ask | Wrong ask |
|---|---|---|
| Cold reply ("Thanks", "Hey") or single-word acknowledgment | Low-friction conversational hook. Ask something interesting using their LinkedIn profile (background, prior roles, recent post). The goal is to get them talking again, not to get a calendar event. | "Want to jump on a 20-min call?" / "Want a Loom?" — they barely replied. Asking for time signals you haven't read the room. |
| Substantive reply with a specific question, no answer yet | Answer their question, then a low-stakes follow-on ("happy to send a 2-min Loom that shows it" works here because they asked first). | Skipping the answer and pushing to a meeting. They want information, not your calendar. |
| Said "yes / interested / absolutely" once, then went silent when asked for time | Re-engage with content or a question, NOT another time-ask. Their silence after the time-ask is the signal that the ask was wrong-shaped. Send something they can react to async. | A second "what's your availability?" — same trigger, same outcome. |
| Active back-and-forth in last few exchanges | Loom or call ask is appropriate here. They're warm. | Yet another low-touch question — this is the moment to advance, not stall. |
| Met once, ball on us | Send the deliverable you owed (Loom, doc, repo link, intro) and use that as the conversation. Don't ask for time until you've delivered. | Asking for "another quick chat" without the deliverable. |
| Met once, ball on them | Light check-in, low-friction question, or a relevant new artifact. No time-ask. | Calendar follow-up before they've responded to whatever's open. |

**Profile-derived hooks that work for low-engagement re-pings:**

- Cross-functional background — "you've sat in three seats most people only sit in one of: [X, Y, Z]. What's the gap between [X-side expectation] and [Y-side reality]?"
- Recent public post or talk — "saw your post on [X], curious how you'd apply that to [adjacent thing we both work on]"
- Specific prior role — "you spent N years at [Company] doing [X] before [current role]. What's the thing about [topic] you wish more people on the [other side] understood?"
- Niche they own — "your headline mentions [specific framework / cert / tool]. We're building toward [adjacent thing]. What's one thing about [niche] that's broken right now that nobody's talking about?"

The principle: ask something they'd want to answer at a conference panel for free. Their answer becomes signal that they're back in the conversation, and that's when you escalate the ask.

**Loom > call still applies for warm leads** (someone who replied substantively, asked a real question, or already booked a meeting). It's a lower-friction ask than 30 minutes on calendar. But for cold or barely-engaged leads, even Loom is too much — get a one-line reply first.

**Feature claims must trace to Phase 0:** every concrete capability mentioned in a draft (a shipped feature, a working integration, a UI surface) must be traceable to a source file the orchestrator read in Phase 0. If you can't name the file, delete the sentence. The earlier "Anthropic-pattern repo" placeholder Percy got is the cautionary lesson — don't reference an artifact unless it exists where we'd send the prospect.

**Per-lead dual drafting (the cell-population rule):**

Each row in the followups tab has TWO draft cells (`Draft Email`, `Draft LinkedIn`). Populate each independently based on which medium has real engagement on that lead's merged timeline:

- **`Draft LinkedIn`** — populate when the lead has ANY LinkedIn DM history (the default). Concise, casual, no signature. 3-5 sentences.
- **`Draft Email`** — populate when the lead has at least one *real* (typed, not a calendar-acceptance auto-reply) inbound Gmail message. Slightly longer acceptable, can include light signature, can reference an attachment / link. 4-7 sentences.
- **Both populated** — for leads with substantive convo on each channel, both columns get a draft. The unified context is the same merged-timeline view (the email draft can reference what was said on LinkedIn and vice versa); the medium-specific phrasing is what differs. Operator decides which channel(s) to fire and toggles the corresponding `Sent ...` cell on send.
- **Neither populated** — only when the cohort row is `Active in-flight` (visibility-only, no draft regardless of medium).

**Per-ICP Goal (from the `ICP Goals` tab) — MANDATORY input:**

Phase 1 already dumped `/tmp/icp_goals.json` for you. Open it before drafting:

```python
import json
icp = json.load(open("/tmp/icp_goals.json"))
role_to_icp = icp["role_to_icp"]    # {ROLE: ICP-bucket}
templates = icp["templates"]        # {ICP: {"goal": str}}
```

**What to use:**

1. For every lead, resolve `ICP = role_to_icp[ROLE]` → grab `entry = templates[ICP]`.
2. Read `entry["goal"]` — this is the strategic outcome the draft is angling for (e.g., advisors → "introduce us to ONE of their CSP clients", CSPs → "design-partner / beta track"). The draft must serve this goal, not just be a generic pitch.

**Use `goal` like this:**

- `goal` informs *what the draft is trying to accomplish* — keep that target in mind, but write the body around it.
- For ball_on_us / cold_thread: a CSP-goal draft should angle toward a design-partner / beta conversation; an Advisor-goal draft should angle toward a referral-into-clients ask (without making the entire DM about the referral program — drop it in once, then move on).
- For Met / Scheduling: cohort framing dominates (deliverable-first / slot pin / pre-meeting confirm). `goal` still informs the *kind* of next step proposed.
- `FU_ROLE_TO_ICP` mapping (for orientation): CSP → CSPs, 3PAO/Assessor → 3PAOs/Assessors, Advisor/Channel → Advisors.

**Fallback when a `goal` is missing or the tab is unreachable:**

- If `entry["goal"]` is `""` for a lead's ICP: fall back to the workflow doc's default ROLE framing table (later in this Phase 5 section). Print a one-line warning to the run summary.
- If the whole `templates` dict is empty: loud warning, fall back wholesale to the ROLE framing table for every lead.

Past run history (2026-05-10 → 2026-05-13):
- 2026-05-10 — Advisor drafts came out generic because Phase 5 was reading `goal` only without strong cohort-specific framing rules. Verbatim templates were introduced as a fix.
- 2026-05-13 — Verbatim templates retired: they over-corrected. The same 4-bullet pitch + referral-program block appearing across every Advisor or CSP cold reply made the output feel templated when scanned top-to-bottom, and the chassis fit no other cohort. Cohort framing rules (this Phase 5 section, plus the ROLE framing table below) now do the load-bearing work; `goal` is the strategic compass.

**Voice consistency:** before drafting, pull the N (default 30) most recent **outbound** rows from `crm.Message` where `sender` matches the operator (e.g. "Chuka Eddy Jack", "Arian Taj"). Use those as voice / format reference samples. The drafter mirrors phrasing patterns the operator actually uses, instead of generating fresh "AI tone" each run.

**Priority labels** are internal-only metadata that live in the row dict columns; never duplicate them in draft body text:

- `ROLE: CSP | 3PAO | Advisor | Assessor | Channel` — describes whose seat the lead is in. Drives draft framing; if the framing in the body copy contradicts the ROLE, that's a bug.
- `PRIORITY: HIGH/MEDIUM-HIGH/MEDIUM/LOW/HOLD (reasoning in plain language)` — `HOLD` is for leads that should have a draft but the freshness window hasn't opened yet (e.g., we already nudged in the last few days on another channel).
- `CONVO: <one-or-two-sentence summary of the thread to date>` — required, so the drafts make sense in isolation without re-reading messages. Same value across both medium drafts (it summarizes the relationship, not one medium's slice).

**ROLE values explained:**

| ROLE | Who it covers | Their pain | Right framing for the draft |
|---|---|---|---|
| **CSP** | Cloud service provider running their own system through FedRAMP authorization (HPE, AWS, Salesforce, Cisco, DigiCert, Motorola, Maximus subsidiaries that own systems, Abnormal AI, NGA911, Rackspace, Project Hosts, Cellebrite, etc.) — also covers federal agencies authoring their own SSPs (AmeriCorps, USDA) | Authoring SSPs from scratch, ConMon drift across their boundary, evidence collection during assessments, surviving the next audit | "Your team writes/maintains the SSP" / "Your ConMon cycles" / "Your boundary" |
| **3PAO** | Person at a third-party assessment org (Coalfire, Schellman, Prescient, A-LIGN, BARR Advisory, etc.) | Reviewing CSP packages efficiently, pushing back on weak narratives, SRTM coverage, evidence quality | "What CSPs hand to your team during assessments" / "Holds up to assessor scrutiny" / "From the assessor lens" |
| **Advisor** | Consultant, vCISO, advisory firm, federal services contractor, internal compliance lead at a non-CSP — anyone helping CSPs prepare or anyone with a multi-client portfolio (Compliance Counsel, ResilientTech, ComplySec360, Booz Allen, Maximus services side, ASM Research, DecisionPoint, KyberStorm, AvaCompliance, etc.) | Multi-client portfolio efficiency, repeating work across clients, helping clients pass | "Drop into one of your clients" / "Your delivery cycles" / "Multi-tenant authoring" |
| **Assessor** | Independent assessor not at a 3PAO firm, OR agency-side authorizing official / authorizing-body staff (USDA AO, VITA running StateRAMP, FDIC) | "Trust but verify" — reading SSPs, deciding risk acceptance, deciding whether evidence is sufficient | "Pre-assessment doc review" / "Where define / identify language is or isn't satisfied" / "Risk acceptance scoping" |
| **Channel** | Reseller, distributor, partner-sales seat at a fed-tech firm (Carahsoft, CDW•G, Canonical federal, Red River for the GTM side) | Co-sell motion, what to bundle, what to recommend | "Bundle into deals" / "Co-sell story" / "Belongs in your catalog" |

If a lead spans two ROLEs (e.g., a 3PAO firm employee who also runs an advisory side hustle), pick the one that matches the role they had in your outreach thread. Don't try to encode both — the message should land with one persona.

**ROLE-vs-framing sanity check:** before sending, read the draft and ask "does this message assume the right thing about whose seat they're in?" If the body copy says "your team's SSP" but ROLE is 3PAO, the framing is wrong — 3PAOs don't write SSPs, they review them. Fix the body, not the ROLE.

### Phase 5b — Humanize the drafts

After every draft is written, run the `humanizer` skill (installed at `~/.claude/skills/humanizer`, source: https://github.com/blader/humanizer) over the body copy. The skill targets ~29 AI-writing tells from Wikipedia's "Signs of AI writing" guide and is the canonical anti-slop pass for this workflow.

**How to invoke:**

The humanizer pass works on the in-memory list of row dicts (the input to Phase 6's `write_followups()` call). For each row, pass BOTH `row["Draft Email"]` and `row["Draft LinkedIn"]` through the humanizer (independently — the email draft can stay slightly longer and more formal than the DM draft) and replace the fields with the humanized outputs. ROLE / PRIORITY / CONVO / Cohort / Email Link / LinkedIn Message Url / Sent toggles are structural metadata — never rewrite those. Active-in-flight rows have empty drafts on both columns and should be skipped entirely.

**All drafts are free-form as of 2026-05-13** — no verbatim chassis to preserve, so the humanizer can rewrite the whole body. (Historical note: prior to 2026-05-13 the replied cohort was assembled from a verbatim ICP template chassis with one `{Add personal message …}` hole. That contract is retired; `goal` is the only load-bearing ICP input now.)

**Top tells to watch for in this workflow's drafts specifically** (observed in past runs):

- **Rule-of-three feature lists** — "AWS evidence scanning, AI-drafted SSP narratives, POA&M closure trails" repeated across drafts makes the cohort feel templated when scanned top-to-bottom. Trim to one or two specific items, or drop entirely when not adding info.
- **"exactly the/where/journey/kind"** — single most overused phrasing across the cohort. Vary or cut.
- **"The play of X is Y" / "the piece that lands hardest"** — persuasive-authority tropes (humanizer Pattern #27). Replace with plain assertions.
- **Copula avoidance** — "DigiCert sits in...", "Okta sits in an interesting spot" — humanizer Pattern #8. Use "is" / "are".
- **Negative parallelism** — "Less prompt-engineering, more deterministic" (Pattern #9). Rewrite as a real sentence.
- **"Curious whether/if..." closer** — fine once or twice across the cohort, slop when it's the closer for half the drafts. Vary.

**Voice exemplar to preserve** (don't let the humanizer strip these):

- Openers: `"<Name>, no rush, figured I'd send one more note."` (allowed but rare). **Do not use** `"<Name>, my fault on the gap."` or any other apology opener — strip them out if the drafter slipped them in.
- CTA: `"Want a 2-min Loom against a sample env?"` (the canonical ask — Loom > call when possible)
- Closer: `"No pitch attached."` / `"No pitch."` (occasional, not on every draft)

**Apology strip (humanizer hard rule, 2026-05-07):** scan the drafts for "my fault", "sorry for", "apologies", "sorry about", "my apology", "apologize" and rewrite each occurrence to lead with the deliverable or the substance instead. No exceptions, no per-batch budget — the entire pattern is filler.

When invoking the skill, point it at the user's `followups.txt` exemplar at the FedRampGPT repo root as the voice-calibration sample so it doesn't over-formalize the tone:

```
Humanize these draft messages. Use the writing style at
/Users/admin/Desktop/Projects/OpenOutreach/followups.txt as a reference sample.
```

Outputs:
- Draft v1 of each rewritten message
- "What still makes this obviously AI-generated?" audit pass per the humanizer skill spec
- Final v2 stored back into `row["draft"]` for Phase 6 to write to the sheet

Skip Phase 5b only if you're explicitly running with `--no-humanize` for speed; the default for any run intended to be sent is to humanize.

### Phase 6 — Sheet output

The drafts land in one Google Sheets tab per active sender (`Arian - Followups`, `Chuka - Followups`, `Athena - Followups`, `Leili - Followups`) via a single helper call. Build a row dict per Lead from **all four** cohort files — `/tmp/followup_drafts.json`, `/tmp/followup_active_in_flight.json`, `/tmp/followup_met.json`, and `/tmp/followup_pre_meeting.json` — group by operator, and call `write_followups()`. The `Cohort` field is outbound-state only (`Ball on us`, `Cold thread`, `Active in-flight`). Section routing is handled by `write_followups()`: rows with post-meeting statuses land in 🤝 MET, pre-meeting statuses land in 📅 SCHEDULING, replied rows land in 💬 REPLIED, active rows land in 🌊 ACTIVE IN-FLIGHT, and preserved sent-history rows land in ✅ SENT.

```python
from linkedin.notifications.sheets import (
    write_followups, FU_HEADERS,
    email_search_hyperlink, linkedin_message_hyperlink,
)

# One row dict per Lead. Keys must match FU_HEADERS exactly (case-sensitive).
# Both Draft cells default to "" (empty); populate the ones the lead has
# real engagement on. Email Link / LinkedIn Message Url are HYPERLINK
# formula strings — the helpers below build them.
arian_rows = [
    {
        "Name":     "Jane Doe",
        "Status":   "Replied",        # from People tab Outreach status
        "Cohort":   "Ball on us",     # outbound-state label, not the section header
        "ROLE":     "CSP",            # see FU_ROLES
        "PRIORITY": "HIGH",           # see FU_PRIORITIES
        "Days since": row["days_since"], # int — pre-computed in Phase 1 (msgs[-1].sent_at). Don't re-derive from "last inbound" — see schema note.
        "Days since connection": 47,  # int — Deal.connected_at to now; blank for legacy rows
        "CONVO":    "She replied via email after our LinkedIn intro asking about ConMon scope.",
        "Draft Email":    "Hi Jane, ... <full email body>",
        "Email Link":     email_search_hyperlink("jane@acme.com"),
        "Sent Email (manual toggle)":    "No",   # default — operator toggles to "Yes"
        "Draft LinkedIn": "Jane, picking this back up, ...",
        "LinkedIn Message Url": linkedin_message_hyperlink(
            thread_external_id="urn:li:conv:2-XYZABC=",
            profile_url="https://www.linkedin.com/in/janedoe/",
        ),
        "Sent LinkedIn (manual toggle)": "No",
    },
    # ...
]
chuka_rows = [...]
# ...one list per active sender (Athena, Leili, ...) keyed by canonical handle.

# One key per sender that has rows this run; empty senders can be omitted.
write_followups({
    "Arian": arian_rows,
    "Chuka": chuka_rows,
    "Athena": athena_rows,
    "Leili": leili_rows,
})
```

`write_followups()` does this on each call:
1. Reads the existing followups tab for each operator. Any row where EITHER `Sent Email` OR `Sent LinkedIn` toggle = `Yes` is captured and **preserved verbatim** under the `✅ SENT` history section at the bottom (deduped by Name against the fresh payload — caller's data wins for redraft scenarios).
2. Snapshots `hiddenByUser` column metadata so the operator's hide/show state survives the rewrite.
3. Drops and recreates the tab with fresh layout (frozen header, section dividers, dropdowns, conditional formatting, `=HYPERLINK` formulas evaluated via `value_input_option=USER_ENTERED`).
4. Writes fresh rows under the section implied by `Status` plus outbound-state `Cohort`.
5. Sorts within each section by PRIORITY desc, then Days since desc.
6. Re-applies the snapshotted hidden-column state, coalesced into contiguous ranges.
7. Returns `{operator: row_count}` for logging.

**Archive (optional):** also dump the full per-row data + classifier state to `followups/YYYY-MM-DD/raw.json` as a history artifact. Don't write txt files — those are deprecated.

**Sent semantics:** the operator copies a draft into LinkedIn / Gmail, sends it, then flips the relevant `Sent ... (manual toggle)` cell from `No` → `Yes`. The row stays in the sheet under the SENT section on the next run. Toggling either cell back to `No` causes the next run to regenerate the draft for that medium.

**Polite-no candidates:** print to stdout / SUMMARY of the run output, do not include in the sheet payload. The operator runs `Lead.objects.filter(...).update(disqualified=True)` separately.

### Phase 7 — Surface decisions to user + record the run

Print a SUMMARY block to stdout (and optionally include in `raw.json`) at the end of the run. Items to flag:

- **Dedupes** between cohorts (someone classified into both replied + met, or someone in user's manual `followups.txt` exemplar)
- **Same-firm salvos** (multiple contacts at one company → coordinate messaging)
- **Polite-no candidates** for `Lead.disqualified=True` batch
- **Already-met-but-not-in-People-tab** contacts (Section C from `docs/data-sync-workflow.md`)
- **Action items** owed by us across multiple threads (e.g., "we owe Percy the Anthropic-pattern repo link")
- **Stale/cold posture exceptions**: rows where `draft_posture in {"reopen", "memory_reopen", "skip_or_new_reason"}`. For archival rows, state whether they were held blank or drafted because of a fresh trigger.

**Finally — record the WorkflowRun so the next session's Phase 0.5 staleness check knows when followup last ran:**

```python
from linkedin.models import WorkflowRun
all_followup_rows = cohort_drafts + cohort_met + cohort_pre_meeting
stale_reopens = [
    r for r in all_followup_rows
    if r.get("draft_posture") in {"reopen", "memory_reopen"}
]
archival_holds = [
    r for r in all_followup_rows
    if r.get("draft_posture") == "skip_or_new_reason"
]
WorkflowRun.objects.create(
    name="followup",
    operator="",  # global — drafts for all operators in one pass
    summary=(
        f"drafts={len(cohort_drafts)} met={len(cohort_met)} "
        f"pre_meeting={len(cohort_pre_meeting)} "
        f"active={len(cohort_active_in_flight)} polite_no={len(polite_no)} "
        f"stale_reopens={len(stale_reopens)} archival_holds={len(archival_holds)}"
    ),
    counts={
        "drafts":               len(cohort_drafts),
        "met":                  len(cohort_met),
        "pre_meeting":          len(cohort_pre_meeting),
        "active_in_flight":     len(cohort_active_in_flight),
        "polite_no_candidates": len(polite_no),
        "stale_reopens":        len(stale_reopens),
        "archival_holds":       len(archival_holds),
    },
)
```

## Tone exemplar

The canonical tone reference is the user's hand-written `followups.txt` (kept at repo root, not in the dated subdirs). Re-read it before drafting to recalibrate. Key patterns to mimic:

- "no rush, figured I'd send one more note"
- "Want a 2-min Loom showing it on a sample env?"
- "No pitch, just curious if it'd dent your workload."

If a draft starts sounding like an LLM business memo, restart it.

## Templates (for batch sends in Tier 2 / Tier 3)

Tier 2 leads don't get individual drafts; they share a name-personalized template. Three archetypes:

**Template A (CISO / GRC director at small-mid CSP or consultancy):**
> [Name], no rush, figured I'd send one more note. With your background in [headline-derived focus], FedrampGPT might dent meaningful cycles on your FedRAMP work. Continuous evidence ingest from cloud + repos, AI-drafted SSP narratives, POA&M closure trails. Want a 2-min Loom against a sample env? No pitch attached.

**Template B (Founder / Co-founder at adjacent compliance tooling — competitive/peer lens):**
> [Name], no rush, figured I'd send one more note. Saw your work at [company]. We're building FedrampGPT in adjacent territory and I'd value your read, whether as a design partner or just to compare notes. Want a 2-min Loom against a sample env? No pitch attached.

**Template C (Sales/PM/channel role at fed-tech firm — partnership angle):**
> [Name], no rush, figured I'd send one more note. Saw your [channel/sales/PM] role at [company]. FedrampGPT might fit as something you bundle or recommend to customers going through FedRAMP authorization. Want a 2-min Loom and a quick chat on partnership angles? No pitch attached.

## Reference data

### Sender field
- `Message.sender` (CharField on `crm.Message`) holds the outbound sender identifier — a LinkedIn display name for `source=linkedin` rows and a Gmail address for `source=gmail` rows.
- LinkedIn display names: `"chukwuka agu"` → Chuka, `"Arian Taj"` → Arian, `"Leili Amirshahi"` → Leili, `"Athena Aghdami"` → Athena.
- Gmail addresses: `eddy@tryfedrampgpt.com` → Chuka (host operator), `ariantajbaka@gmail.com` / `ariant2013@gmail.com` → Arian, `leili.ash2011@yahoo.com` → Leili, `athenaaghdami@gmail.com` → Athena.
- The canonical mapping lives in `linkedin/operators.py` (`resolve_operator`) — use it rather than re-deriving handles here; the examples above are just the common cases.
- Operator-routing rule for the drafter: prefer the LinkedIn display name when present; fall back to the Gmail address mapping above for email-only leads (Stephen Pratt, John@mindanvil, etc.). A lead can have outbounds in both — pick the operator who owns the most recent outbound thread on the merged timeline.
- Use this to bucket per-sender, since each sender's threads should stay continuous to the same prospect.

### Already-met-but-not-in-sheet carryover
Some calendar attendees had meetings but aren't yet reflected in the People tab's Outreach status (e.g., Lauren@ResilientTech, Oreale Kouo). They show up in the replied cohort as "looks like never had a meeting" but actually did. Cross-reference against `/tmp/cal_meetings.json` from the data-sync workflow before drafting.

### File path conventions
- Generation scratch: `/tmp/followup_drafts.json`, `/tmp/followup_active_in_flight.json`, `/tmp/followup_met.json`, `/tmp/followup_pre_meeting.json`, `/tmp/icp_goals.json`, `/tmp/polite_no_candidates.json`
- Final output: one `<Sender> - Followups` tab per active sender (`Arian - Followups`, `Chuka - Followups`, `Athena - Followups`, `Leili - Followups`) in the Google Sheet (via `linkedin.notifications.sheets.write_followups()`)
- Optional archive: `followups/YYYY-MM-DD/raw.json` (per-run snapshot of rows + classifier state, for history only)
- Tone exemplar: `followups.txt` at repo root (manual reference, do not overwrite)

## Out of scope of this workflow

- Actually sending the DMs (operator copies the `Draft` cell, sends manually via LinkedIn / Gmail, ticks `Sent?` in the sheet — or future automation via existing follow-up agent)
- Disqualifying polite-no Leads in the DB (separate one-liner: `Lead.objects.filter(...).update(disqualified=True)`)
- Updating `Outreach status` to `Had Meeting` after a meeting (covered in `docs/data-sync-workflow.md`)
