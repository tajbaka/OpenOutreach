# Follow-up Generation Workflow

Reusable runbook for producing tailored LinkedIn DM follow-ups from `crm.Lead` + `Deal` + `Message` data, broken out by cohort and sender. Companion to `docs/sheets-meeting-sync-workflow.md` (which handles the Sheets side after meetings).

## When to run

Safe to run **daily**. The ball-on-court classifier (Phase 1) routes fresh outbounds to a visibility-only ACTIVE-IN-FLIGHT bucket so daily runs don't generate drafts that step on yesterday's nudge. Weekly is fine if outreach volume is low; daily is appropriate once volume picks up and same-day inbound replies need to surface fast.

## Output location

Drafts land in two **Google Sheets tabs** — one per active sender:

- `Arian - Followups`
- `Chuka - Followups`

Each tab has 14 columns and is divided into five sections via merged divider rows:

1. **🤝 MET** — `Cohort = Met`
2. **💬 REPLIED** — `Cohort ∈ {Ball on us, Cold thread}`
3. **⏳ CONNECTED, NO REPLY** — `Cohort = No reply yet`
4. **🌊 ACTIVE IN-FLIGHT** — `Cohort = Active in-flight` (visibility-only, no draft)
5. **✅ SENT** — preserved from prior runs (rows where the operator toggled either `Sent Email` or `Sent LinkedIn` = `Yes`)

Schema (in column order):

| # | Column | Notes |
|---|---|---|
| 1 | `Name` | first + last |
| 2 | `Status` | snapshot of People-tab `Outreach status` |
| 3 | `Cohort` | dropdown — Met / Ball on us / Cold thread / No reply yet / Active in-flight / Sent |
| 4 | `ROLE` | dropdown — CSP / 3PAO / Advisor / Assessor / Channel |
| 5 | `PRIORITY` | dropdown — HIGH / MEDIUM-HIGH / MEDIUM / LOW / HOLD (conditional-format colored) |
| 6 | `Days since` | int — days since the last message on either medium (merged latest) |
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

Five buckets the workflow generates files / sections for. The drafted ones are split per active sender (Chuka, Arian, etc.). Active-in-flight is a section inside the relevant draft file, not a separate file.

| Cohort | Filter | Sheet section / Cohort value | Why follow up |
|---|---|---|---|
| **Replied, ball on us** | `Deal.state=Connected` AND latest message is **inbound** AND not in already-met set AND not disqualified | 💬 REPLIED, `Cohort = Ball on us` | They replied last, we owe a response. Most time-sensitive cohort — these need same-day or next-day attention. |
| **Replied, cold thread (ball on us to nudge)** | `Deal.state=Connected` AND has ≥1 inbound AND latest message is outbound ≥ `NUDGE_AFTER_DAYS` (default 5) old AND not in already-met set AND not disqualified | 💬 REPLIED, `Cohort = Cold thread` | They engaged once and went quiet. Re-engagement nudge needed. |
| **Active / in-flight** | Latest message is outbound, < `NUDGE_AFTER_DAYS` old | 🌊 ACTIVE IN-FLIGHT, `Cohort = Active in-flight` (no Draft) | They've had a recent reach-out from us; sending again today would step on it. Listed for visibility so they don't disappear from daily runs. |
| **Connected, no reply** | `Deal.state=Connected` AND zero inbound messages AND not disqualified AND ICP-relevant title | ⏳ CONNECTED, NO REPLY, `Cohort = No reply yet` | Cold lead, but they accepted the invite. Try a different angle than the original. |
| **Met (post-meeting)** | `Deal.state=Connected` AND lead has had a Google Meet (per `cal_meetings.json` / Drive Gemini notes) | 🤝 MET, `Cohort = Met` | Post-meeting follow-up; ball-on-court derived from the merged LinkedIn + Gmail + meeting timeline. |
| **Replied, polite no** | Same filter as Replied cohorts but inbound message contains a `NO_PHRASES` decline phrase | Surfaced in the run's SUMMARY (printed to stdout, not written to sheet) → recommend `Lead.disqualified=True` | Don't follow up. Disqualify the Lead. |

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

### Phase 1 — Pull cohort data (ball-on-court classifier)

The classifier is **ball-on-court**, not freshness-based. The right question for a daily run is "whose move is it?" — not "how recent was the last message?" A 24-hour-old outbound shouldn't trigger a re-nudge (the prospect hasn't had time), but a 24-hour-old *inbound* absolutely should surface (we owe a reply). This was a deliberate fix on 2026-04-28 after the previous freshness filter silently hid Mark Milton (calendar-invite-just-sent) and similarly classified active threads as "too fresh."

```bash
.venv/bin/python manage.py shell <<'EOF'
import json
from datetime import datetime, timedelta, timezone
from crm.models import Lead, Deal, Message
from linkedin.notifications.sheets import read_followup_sent_rows

# Skip leads the operator already ticked Sent? in either tab on a prior
# run. Those rows will be preserved verbatim under ✅ SENT by write_followups()
# below — no need to re-classify or re-draft for them.
ALREADY_SENT_URLS = set()
for op in ("Arian", "Chuka"):
    for r in read_followup_sent_rows(op):
        url = (r.get("LinkedIn URL") or "").strip()
        if url:
            ALREADY_SENT_URLS.add(url)

# Leads who already had a meeting (post-meeting follow-up cohort) are
# pulled from the People tab where Outreach status = Had Meeting / Meeting
# Booked / Wants Meeting. The classifier below puts them in the Met cohort.
# Update this set if you have meetings tracked outside the sheet.
ALREADY_MET_URLS: set[str] = set()  # populate from sheet if needed

NUDGE_AFTER_DAYS = 5  # how long to wait before nudging an unanswered outbound
now = datetime.now(timezone.utc)
nudge_cutoff = now - timedelta(days=NUDGE_AFTER_DAYS)

def classify(lead):
    """
    Returns one of:
      - 'ball_on_us'        : latest msg is inbound, we owe a reply         → DRAFT
      - 'cold_thread'       : latest is outbound, ≥ NUDGE_AFTER_DAYS old    → DRAFT (nudge)
      - 'active_in_flight'  : latest is outbound, < NUDGE_AFTER_DAYS old    → VISIBILITY ONLY
      - 'no_reply_yet'      : zero inbound messages                         → connected-no-reply cohort
      - 'no_messages'       : edge case, skip
    """
    msgs = list(Message.objects.filter(lead=lead).order_by("sent_at"))
    if not msgs:
        return ('no_messages', None, [])
    has_inbound = any(m.direction == 'inbound' for m in msgs)
    if not has_inbound:
        return ('no_reply_yet', None, msgs)
    latest = msgs[-1]
    if latest.direction == 'inbound':
        return ('ball_on_us', latest, msgs)
    # latest is outbound
    if latest.sent_at < nudge_cutoff:
        return ('cold_thread', latest, msgs)
    return ('active_in_flight', latest, msgs)

# Cohort A: replied, no meeting (ball_on_us OR cold_thread → DRAFT)
# Cohort A-active: latest outbound < NUDGE_AFTER_DAYS old → VISIBILITY only
# Cohort B: connected, no reply

cohort_drafts = []          # ball_on_us + cold_thread
cohort_active_in_flight = [] # active_in_flight (visibility only)
cohort_no_reply = []         # no_reply_yet

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
    if lead.linkedin_url and lead.linkedin_url in ALREADY_SENT_URLS:
        continue  # preserved under ✅ SENT
    if lead.linkedin_url and lead.linkedin_url in ALREADY_MET_URLS:
        continue  # met cohort handled below from People tab status
    klass, latest, msgs = classify(lead)
    if klass == 'no_messages':
        continue
    deal = lead.deal_set.order_by('-creation_date').first()
    try: prof = json.loads(lead.description) if lead.description else {}
    except Exception: prof = {}
    base = {
        "lead_id": lead.id, "deal_id": (deal.id if deal else None),
        "first_name": lead.first_name, "last_name": lead.last_name,
        "company_name": lead.company_name, "linkedin_url": lead.linkedin_url or "", "email": lead.email or "",
        "headline": prof.get("headline",""),
        "summary": (prof.get("summary","") or "")[:1500],
        "primary_sender": next(iter([m.sender for m in msgs if m.direction=="outbound"]), ""),
        "classification": klass,
        "latest_direction": (latest.direction if latest else None),
        "latest_at": (str(latest.sent_at)[:19] if latest else None),
        "messages": [{"source": m.source, "d": m.direction, "t": str(m.sent_at)[:19], "b": (m.body or "")[:600], "s": m.sender} for m in msgs],
    }
    if klass == 'no_reply_yet':
        cohort_no_reply.append(base)
    elif klass == 'active_in_flight':
        cohort_active_in_flight.append(base)
    else:  # ball_on_us or cold_thread
        cohort_drafts.append(base)

with open("/tmp/followup_drafts.json","w") as f: json.dump(cohort_drafts, f, indent=2)
with open("/tmp/followup_active_in_flight.json","w") as f: json.dump(cohort_active_in_flight, f, indent=2)
with open("/tmp/followup_no_reply.json","w") as f: json.dump(cohort_no_reply, f, indent=2)
print(f"drafts: {len(cohort_drafts)}, active-in-flight: {len(cohort_active_in_flight)}, no-reply: {len(cohort_no_reply)}")
EOF
```

The output splits into three files instead of two:

- `/tmp/followup_drafts.json` — leads that need a draft. Mix of "ball on us, draft a reply" and "cold thread, draft a nudge." The `latest_direction` field tells you which kind.
- `/tmp/followup_active_in_flight.json` — visibility only. Listed in the SUMMARY/ACTIVE section of the output file with a one-line state, no draft. These are the leads that under the old freshness filter would have silently disappeared.
- `/tmp/followup_no_reply.json` — the connected-no-reply cohort, unchanged from before.

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

For the connected-no-reply cohort, apply ICP filter on `Lead.description` (Voyager scrape headline + summary):

```python
ICP_KEYWORDS = ["fedramp","cmmc","nist","rmf","fisma","govcloud","govern","compliance","grc",
                "ciso","cco","cso","3pao","isso","ato","auth","audit","security","cyber",
                "risk","privacy","trust","assessor"]
EXCLUDE = ["recruit","talent","marketing","sales rep","customer success"]
```

### Phase 3 — Bucket by sender

`Message.sender` field holds who sent each outbound (e.g., "chukwuka agu", "Arian Taj"). For each candidate, the primary sender = the most-frequent outbound sender on that thread. One follow-up file per sender.

If a sender has zero candidates in a cohort, still create the file with a short "no leads to follow up here yet" note. Easier to scan than missing files.

### Phase 3b — Merge Gmail threads with LinkedIn DMs (MANDATORY)

The ball-on-court classifier in Phase 1 reads `crm.Message`, so any lead who replied via email after going silent on LinkedIn would still classify as `cold_thread` until their Gmail reply lands in the same table. **This phase is what makes the classifier honest.** Skipping it means the followup pipeline is operating on stale LinkedIn-only context for any lead with a meeting / pre-existing email thread.

**For each candidate that has `Lead.email` populated:**

1. Pull Gmail threads via MCP:
   ```
   mcp__claude_ai_Gmail__search_threads
     query: "from:<lead.email> OR to:<lead.email>"
     pageSize: 5
   ```
2. Persist via `linkedin.notifications.gmail_threads.persist_gmail_threads(lead=..., threads=<MCP response.threads>, host_email=HOST_EMAIL, team_emails=TEAM_EMAILS)`. The helper is idempotent on `(source, external_id)` — re-running is a free no-op upsert. Direction is inferred by comparing the From header to `HOST_EMAIL` / `TEAM_EMAILS` in `linkedin/conf.py` (env-loaded; both empty disables the merge).
3. **Re-classify** by calling `classify_ball_on_court(lead, nudge_after_days=5)` from the same module instead of the inline classifier. It operates on the merged timeline from `crm.Message` (LinkedIn + Gmail union) so an email reply correctly flips the lead to `ball_on_us`.

Once Gmail messages are persisted, downstream features (synthesis pass, sheet status derivation, Phase 6 `Status` column) read merged context for free — no second MCP roundtrip on subsequent runs.

**Reply venue:** after persisting, the merged timeline's latest message's `source` is the canonical answer — `linkedin` → DM, `gmail` → email. Phase 5 reads this off `Message.source`, no extra logic.

**Cost:** one MCP search call per lead with an email (typically 30-100 per run depending on cohort size); persisted on first contact, cached in DB thereafter. The host filter on `Lead.email` keeps cost bounded — no email, no Gmail call.

### Phase 4 — Tier classification (connected-no-reply only)

The connected-no-reply cohort can be huge (dozens to hundreds). Tier into:

- **T1_SENIOR**: Tier-1 company AND senior title (CISO/Director/VP/Founder/etc.) → full personalized draft
- **T1**: Tier-1 company, mid-level title → full personalized draft if scope allows
- **SENIOR**: Senior title at non-Tier-1 → batch template with 1-line personalization
- **OTHER**: ICP-keyword match but title isn't a buyer → list-only, manual review

Tier-1 companies are CSPs the FedRAMP universe revolves around (AWS, Salesforce, Oracle, Microsoft, Cisco, etc.) plus top 3PAOs (Coalfire, Schellman, Prescient) plus federal services giants (Booz Allen, GDIT, Maximus, Leidos, etc.).

### Phase 5 — Draft

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

**ICP-level Goal (from the `ICP Templates` tab):** before drafting, call `linkedin.notifications.sheets.read_icp_templates()` to load `{ICP: goal_text}`. Map each lead's `ROLE` to its ICP via `FU_ROLE_TO_ICP` (CSP → CSPs, 3PAO/Assessor → 3PAOs/Assessors, Advisor/Channel → Advisors). Use the matching Goal text as strategic direction — what each draft should aim for at the ICP level, not just per-lead. The Goal text may include explicit instructions like "mention the demo gif" — incorporate those into the draft naturally.

**Voice consistency:** before drafting, pull the N (default 30) most recent **outbound** rows from `crm.Message` where `sender` matches the operator (e.g. "Chuka Eddy Jack", "Arian Taj"). Use those as voice / format reference samples. The drafter mirrors phrasing patterns the operator actually uses, instead of generating fresh "AI tone" each run.

**Priority labels** are internal-only metadata that live in the row dict columns; never duplicate them in draft body text:

- `ROLE: CSP | 3PAO | Advisor | Assessor | Channel` — describes whose seat the lead is in. Drives draft framing; if the framing in the body copy contradicts the ROLE, that's a bug.
- `PRIORITY: HIGH/MEDIUM-HIGH/MEDIUM/LOW/HOLD (reasoning in plain language)` — `HOLD` is for leads that should have a draft but the freshness window hasn't opened yet (e.g., we already nudged in the last few days on another channel).
- `CONVO: <one-or-two-sentence summary of the thread to date>` — required, so the drafts make sense in isolation without re-reading messages. Same value across both medium drafts (it summarizes the relationship, not one medium's slice).

**Freshness priority bump (`no_reply_yet` cohort only):** apply `linkedin.notifications.sheets.fresh_connection_priority(days_since_connection, cohort, fallback_priority=tier_priority)` after the tier classifier proposes a base PRIORITY. The helper bumps UP for recently-connected leads but never down. Effective rule:

| days since accept | bumps `fallback_priority` to at least |
|---|---|
| <3 days | HIGH |
| <7 days | MEDIUM-HIGH |
| <14 days | MEDIUM |
| ≥14 days | unchanged — tier classifier's answer wins (a T1_SENIOR connected 60 days ago is still HIGH if seniority rule put them there) |

The bump is additive urgency for the warm-handshake window only; after that, ICP / role / seniority drive PRIORITY as before. Legacy rows where `Deal.connected_at` is null get fallback_priority unchanged.

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

The drafts land in two Google Sheets tabs (`Arian - Followups`, `Chuka - Followups`) via a single helper call. Build a row dict per Lead with the schema below, group by operator, and call `write_followups()`:

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
        "Cohort":   "Ball on us",     # see FU_SECTIONS
        "ROLE":     "CSP",            # see FU_ROLES
        "PRIORITY": "HIGH",           # see FU_PRIORITIES
        "Days since": 5,              # int — merged latest across both mediums
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

write_followups({"Arian": arian_rows, "Chuka": chuka_rows})
```

`write_followups()` does this on each call:
1. Reads the existing followups tab for each operator. Any row where EITHER `Sent Email` OR `Sent LinkedIn` toggle = `Yes` is captured and **preserved verbatim** under the `✅ SENT` section at the bottom (deduped by Name against the fresh payload — caller's data wins for redraft scenarios).
2. Snapshots `hiddenByUser` column metadata so the operator's hide/show state survives the rewrite.
3. Drops and recreates the tab with fresh layout (frozen header, section dividers, dropdowns, conditional formatting, `=HYPERLINK` formulas evaluated via `value_input_option=USER_ENTERED`).
4. Writes fresh rows under the section corresponding to their `Cohort` value.
5. Sorts within each section by PRIORITY desc, then Days since desc.
6. Re-applies the snapshotted hidden-column state, coalesced into contiguous ranges.
7. Returns `{operator: row_count}` for logging.

**Archive (optional):** also dump the full per-row data + classifier state to `followups/YYYY-MM-DD/raw.json` as a history artifact. Don't write txt files — those are deprecated.

**Sent semantics:** the operator copies a draft into LinkedIn / Gmail, sends it, then flips the relevant `Sent ... (manual toggle)` cell from `No` → `Yes`. The row stays in the sheet under the SENT section on the next run. Toggling either cell back to `No` causes the next run to regenerate the draft for that medium.

**Polite-no candidates:** print to stdout / SUMMARY of the run output, do not include in the sheet payload. The operator runs `Lead.objects.filter(...).update(disqualified=True)` separately.

### Phase 7 — Surface decisions to user

Print a SUMMARY block to stdout (and optionally include in `raw.json`) at the end of the run. Items to flag:

- **Dedupes** between cohorts (someone classified into both replied + connected-no-reply, or someone in user's manual `followups.txt` exemplar)
- **Same-firm salvos** (multiple contacts at one company → coordinate messaging)
- **Polite-no candidates** for `Lead.disqualified=True` batch
- **Already-met-but-not-in-People-tab** contacts (Section C from `docs/sheets-meeting-sync-workflow.md`)
- **Action items** owed by us across multiple threads (e.g., "we owe Percy the Anthropic-pattern repo link")

## Tone exemplar

The canonical tone reference is the user's hand-written `followups.txt` (kept at repo root, not in the dated subdirs). Re-read it before drafting to recalibrate. Key patterns to mimic:

- "no rush, figured I'd send one more note"
- "my fault on the gap"
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
- LinkedIn display names: `"chukwuka agu"` → Chuka, `"Arian Taj"` → Arian.
- Gmail addresses: `eddy@tryfedrampgpt.com` → Chuka (host operator), `ariantajbaka@gmail.com` / `ariant2013@gmail.com` → Arian.
- Operator-routing rule for the drafter: prefer the LinkedIn display name when present; fall back to the Gmail address mapping above for email-only leads (Stephen Pratt, John@mindanvil, etc.). A lead can have outbounds in both — pick the operator who owns the most recent outbound thread on the merged timeline.
- Use this to bucket per-sender, since each sender's threads should stay continuous to the same prospect.

### Already-met-but-not-in-sheet carryover
Some calendar attendees had meetings but aren't yet reflected in the People tab's Outreach status (e.g., Lauren@ResilientTech, Oreale Kouo). They show up in the replied cohort as "looks like never had a meeting" but actually did. Cross-reference against `/tmp/cal_meetings.json` from the meeting-sync workflow before drafting.

### File path conventions
- Generation scratch: `/tmp/followup_drafts.json`, `/tmp/followup_active_in_flight.json`, `/tmp/followup_no_reply.json`, `/tmp/polite_no_candidates.json`
- Final output: `Arian - Followups` and `Chuka - Followups` tabs in the Google Sheet (via `linkedin.notifications.sheets.write_followups()`)
- Optional archive: `followups/YYYY-MM-DD/raw.json` (per-run snapshot of rows + classifier state, for history only)
- Tone exemplar: `followups.txt` at repo root (manual reference, do not overwrite)

## Out of scope of this workflow

- Actually sending the DMs (operator copies the `Draft` cell, sends manually via LinkedIn / Gmail, ticks `Sent?` in the sheet — or future automation via existing follow-up agent)
- Disqualifying polite-no Leads in the DB (separate one-liner: `Lead.objects.filter(...).update(disqualified=True)`)
- Updating `Outreach status` to `Had Meeting` after a meeting (covered in `docs/sheets-meeting-sync-workflow.md`)
