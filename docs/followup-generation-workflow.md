# Follow-up Generation Workflow

Reusable runbook for producing tailored LinkedIn DM follow-ups from `crm.Lead` + `Deal` + `Message` data, broken out by cohort and sender. Companion to `docs/attio-meeting-sync-workflow.md` (which handles the Attio side after meetings).

## When to run

Safe to run **daily**. The ball-on-court classifier (Phase 1) routes fresh outbounds to a visibility-only ACTIVE-IN-FLIGHT bucket so daily runs don't generate drafts that step on yesterday's nudge. Weekly is fine if outreach volume is low; daily is appropriate once volume picks up and same-day inbound replies need to surface fast.

## Output location

```
followups/YYYY-MM-DD/
  replied_chuka.txt              # ball_on_us + cold_thread for Chuka
  replied_arian.txt              # same, for Arian
  connected_no_reply_chuka.txt   # accepted invite, never replied (Chuka)
  connected_no_reply_arian.txt   # same, for Arian
  met_chuka.txt                  # post-meeting follow-ups for Chuka
  met_arian.txt                  # same, for Arian
```

One directory per generation run, named with today's date. Old runs stay around for reference and to compare drafts vs. what actually got sent.

## Cohorts

Five buckets the workflow generates files / sections for. The drafted ones are split per active sender (Chuka, Arian, etc.). Active-in-flight is a section inside the relevant draft file, not a separate file.

| Cohort | Filter | Output | Why follow up |
|---|---|---|---|
| **Replied, ball on us** | `Deal.state=Connected` AND latest message is **inbound** AND not in already-met set AND not disqualified | Draft in `replied_<sender>.txt` | They replied last, we owe a response. Most time-sensitive cohort — these need same-day or next-day attention. |
| **Replied, cold thread (ball on us to nudge)** | `Deal.state=Connected` AND has ≥1 inbound AND latest message is outbound ≥ `NUDGE_AFTER_DAYS` (default 5) old AND not in already-met set AND not disqualified | Draft in `replied_<sender>.txt` | They engaged once and went quiet. Re-engagement nudge needed. |
| **Active / in-flight** | Latest message is outbound, < `NUDGE_AFTER_DAYS` old | Visibility line in `replied_<sender>.txt` SUMMARY area, no draft | They've had a recent reach-out from us; sending again today would step on it. Listed for visibility so they don't disappear from daily runs. |
| **Connected, no reply** | `Deal.state=Connected` AND zero inbound messages AND not disqualified AND ICP-relevant title | Draft in `connected_no_reply_<sender>.txt` | Cold lead, but they accepted the invite. Try a different angle than the original. |
| **Met (post-meeting)** | `Deal.state=Connected` AND lead has had a Google Meet (per `cal_meetings.json` / Drive Gemini notes) | Draft in `met_<sender>.txt` | Post-meeting follow-up; ball-on-court derived from the merged LinkedIn + Gmail + meeting timeline. |
| **Replied, polite no** | Same filter as Replied cohorts but inbound message contains a `NO_PHRASES` decline phrase | Listed in SUMMARY of `replied_<sender>.txt` as polite-no candidates → recommend `Lead.disqualified=True` | Don't follow up. Disqualify the Lead. |

## Step-by-step

### Phase 1 — Pull cohort data (ball-on-court classifier)

The classifier is **ball-on-court**, not freshness-based. The right question for a daily run is "whose move is it?" — not "how recent was the last message?" A 24-hour-old outbound shouldn't trigger a re-nudge (the prospect hasn't had time), but a 24-hour-old *inbound* absolutely should surface (we owe a reply). This was a deliberate fix on 2026-04-28 after the previous freshness filter silently hid Mark Milton (calendar-invite-just-sent) and similarly classified active threads as "too fresh."

```bash
.venv/bin/python manage.py shell <<'EOF'
import json
from datetime import datetime, timedelta, timezone
from crm.models import Lead, Deal, Message

ALREADY_MET_ATTIO_IDS = {  # update each run
    "55fe2a6d-...","6fa371ad-...",  # the 14 (or however many) we already met
}

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

deals = (Deal.objects
    .filter(state="Connected", lead__disqualified=False)
    .select_related("lead"))

for d in deals:
    lead = d.lead
    if lead.attio_person_id in ALREADY_MET_ATTIO_IDS:
        continue
    klass, latest, msgs = classify(lead)
    if klass == 'no_messages':
        continue
    try: prof = json.loads(lead.description) if lead.description else {}
    except Exception: prof = {}
    base = {
        "lead_id": lead.id, "deal_id": d.id, "attio_person_id": lead.attio_person_id,
        "first_name": lead.first_name, "last_name": lead.last_name,
        "company_name": lead.company_name, "linkedin_url": lead.linkedin_url, "email": lead.email or "",
        "headline": prof.get("headline",""),
        "summary": (prof.get("summary","") or "")[:1500],
        "primary_sender": next(iter([m.sender for m in msgs if m.direction=="outbound"]), ""),
        "classification": klass,
        "latest_direction": (latest.direction if latest else None),
        "latest_at": (str(latest.sent_at)[:19] if latest else None),
        "messages": [{"d": m.direction, "t": str(m.sent_at)[:19], "b": (m.body or "")[:600], "s": m.sender} for m in msgs],
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

### Phase 3b — Merge Gmail threads with LinkedIn DMs

For each candidate, pull Gmail threads via MCP and merge into a single timeline sorted by `sent_at`. This determines the **reply venue** (LinkedIn DM vs email) — whichever source the latest message is from is where the follow-up should land.

```
mcp__claude_ai_Gmail__search_threads
  query: "from:<lead.email> OR to:<lead.email>"
  pageSize: 5
```

Build a merged timeline:
- LinkedIn DMs from `crm.Message` (source=linkedin)
- Gmail messages from MCP (treat as source=gmail in the merge)
- Sort by timestamp; latest source determines reply venue

Cost-bounded: only the cohort size of follow-up candidates triggers Gmail calls (~25-30 per run, not the whole DB). No persistence layer; always-fresh data including manual replies sent minutes ago.

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

**Structure:**
1. First name greeting + frame (no rush, my fault on the gap, etc., depending on whose ball)
2. One specific concrete reference to their work (extracted from their LinkedIn headline or summary or last DM)
3. The ask, framed as a Loom rather than a call when possible
4. "No pitch attached" or similar low-friction closer

**Frames by ball-on-court:**
- Their ball, gone cold: "Name, no rush, figured I'd send one more note."
- Our ball, gone cold: "Name, my fault on the gap. [acknowledge what we owed them]."
- Cold lead, never replied: "Name, no rush, figured I'd send one more note since I never heard back."

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

**Reply venue inference (from Phase 3b merged timeline):**
- Latest message source = `linkedin` → draft a LinkedIn DM (concise, casual, no signature)
- Latest message source = `gmail` → draft an email (slightly longer is acceptable, can include signature, subject line if it's a fresh thread vs. reply-in-thread)
- No prior messages on either → default to LinkedIn DM (matches the original cold outreach venue)

**Priority labels** in the file are internal-only metadata (never appear in the actual message). Format (in this order under each `--- Name, title, company ---` header):

- `ROLE: CSP | 3PAO | Advisor | Assessor | Channel` — describes whose seat the lead is in. Drives draft framing; if the framing in the body copy contradicts the ROLE, that's a bug.
- `PRIORITY: HIGH/MEDIUM-HIGH/MEDIUM/LOW/HOLD (reasoning in plain language)` — `HOLD` is for leads that should have a draft but the freshness window hasn't opened yet (e.g., we already nudged in the last few days on another channel).
- `MEDIUM: linkedin | gmail` (the channel to send the reply on, per Phase 3b)
- `CONVO: <one-or-two-sentence summary of the thread to date>` — required, so the draft makes sense in isolation without re-reading messages.

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

```
Use the humanizer skill on followups/YYYY-MM-DD/<file>.txt — only rewrite the
draft message bodies, leave the structural metadata (=== headers, --- entries,
PRIORITY / MEDIUM / CONVO lines, SUMMARY, ACTIVE-IN-FLIGHT) untouched.
```

**Top tells to watch for in this workflow's drafts specifically** (observed in past runs):

- **Rule-of-three feature lists** — "AWS evidence scanning, AI-drafted SSP narratives, POA&M closure trails" repeated across drafts makes the file feel templated when read top-to-bottom. Trim to one or two specific items, or drop entirely when not adding info.
- **"exactly the/where/journey/kind"** — single most overused phrasing across the cohort. Vary or cut.
- **"The play of X is Y" / "the piece that lands hardest"** — persuasive-authority tropes (humanizer Pattern #27). Replace with plain assertions.
- **Copula avoidance** — "DigiCert sits in...", "Okta sits in an interesting spot" — humanizer Pattern #8. Use "is" / "are".
- **Negative parallelism** — "Less prompt-engineering, more deterministic" (Pattern #9). Rewrite as a real sentence.
- **"Curious whether/if..." closer** — fine once or twice across the file, slop when it's the closer for half the drafts. Vary.

**Voice exemplar to preserve** (don't let the humanizer strip these):

- Openers: `"<Name>, no rush, figured I'd send one more note."`, `"<Name>, my fault on the gap."`
- CTA: `"Want a 2-min Loom against a sample env?"` (the canonical ask — Loom > call when possible)
- Closer: `"No pitch attached."` / `"No pitch."` (occasional, not on every draft)

When invoking the skill, point it at the user's `followups.txt` exemplar at the FedRampGPT repo root as the voice-calibration sample so it doesn't over-formalize the tone:

```
Humanize this text. Use the writing style at
/Users/admin/Desktop/Projects/FedRampGPT/followups.txt as a reference sample.
```

Outputs:
- Draft v1 of each rewritten message
- "What still makes this obviously AI-generated?" audit pass per the humanizer skill spec
- Final v2 written back to the file via Edit

Skip Phase 5b only if you're explicitly running with `--no-humanize` for speed; the default for any run intended to be sent is to humanize.

### Phase 6 — File output

One file per cohort × sender. Format mirrors the existing examples:

```
=================================================
FEDRAMPGPT FOLLOW-UPS, [COHORT NAME] ([SENDER])
=================================================
[Brief description of cohort + filters applied]


=================================================
HIGH PRIORITY (N)
=================================================


--- [Name], [Title], [Company] ---
ROLE: CSP | 3PAO | Advisor | Assessor | Channel
PRIORITY: HIGH ([reasoning])
MEDIUM: linkedin | gmail
CONVO: [one or two sentences summarizing the thread to date]

[Draft message in user's voice — formatted for the chosen medium and matching the ROLE framing]


--- [next person] ---
...


=================================================
ACTIVE / IN-FLIGHT (no draft this run, ball is on them)
=================================================
[Visibility-only list of leads from /tmp/followup_active_in_flight.json. One bullet per lead, with current state and when to revisit. No drafts.]


=================================================
SUMMARY
=================================================
[counts, recommended order, dedupe alerts, action items, polite-no candidates]
```

The **ACTIVE / IN-FLIGHT** section is required even when the list is empty (write "No active in-flight threads this run" in that case). This is the section that makes daily runs safe — it shows the user that mid-scheduling threads, calendar invites just sent, etc., are accounted for and don't need action today. Without this section, leads silently disappear and the user can't tell the difference between "filtered out" and "never existed."

### Phase 7 — Surface decisions to user

In the SUMMARY section, flag:

- **Dedupes** between files (someone in replied + connected-no-reply lists, or someone in user's manual `followups.txt`)
- **Same-firm salvos** (multiple contacts at one company → coordinate messaging)
- **Polite-no candidates** for `Lead.disqualified=True` batch
- **Already-met-but-not-in-Attio** contacts (Section C from `docs/attio-meeting-sync-workflow.md`)
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
- `Message.sender` (CharField on `crm.Message`) holds the LinkedIn sender's display name
- "chukwuka agu" = Chuka, "Arian Taj" = Arian
- Use this to bucket per-sender, since each sender's threads should stay continuous to the same prospect

### Already-met-but-not-in-Attio carryover
Some calendar attendees had meetings but aren't in the Attio Sales list yet (e.g., Lauren@ResilientTech, Oreale Kouo). They show up in the replied cohort as "looks like never had a meeting" but actually did. Cross-reference against `/tmp/cal_meetings.json` from the meeting-sync workflow before drafting.

### File path conventions
- Generation scratch: `/tmp/replied_no_meeting.json`, `/tmp/connected_no_reply_icp.json`, `/tmp/followup_real.json`, `/tmp/connected_chuka_tiered.json`
- Final output: `followups/YYYY-MM-DD/[cohort]_[sender].txt`
- Tone exemplar: `followups.txt` at repo root (manual reference, do not overwrite)

## Out of scope of this workflow

- Actually sending the DMs (manual paste into LinkedIn, or future automation via existing follow-up agent)
- Disqualifying polite-no Leads in the DB (separate one-liner: `Lead.objects.filter(...).update(disqualified=True)`)
- Adding the already-met-but-not-in-Attio folks into the Sales list (covered in the meeting-sync workflow)
