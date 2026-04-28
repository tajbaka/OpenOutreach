# Follow-up Generation Workflow

Reusable runbook for producing tailored LinkedIn DM follow-ups from `crm.Lead` + `Deal` + `Message` data, broken out by cohort and sender. Companion to `docs/attio-meeting-sync-workflow.md` (which handles the Attio side after meetings).

## When to run

- Weekly or bi-weekly, depending on outreach volume
- After a batch of new connections or replies has accumulated
- Whenever you want to clean up open threads and figure out what to send next

## Output location

```
followups/YYYY-MM-DD/
  replied_chuka.txt
  replied_arian.txt
  connected_no_reply_chuka.txt
  connected_no_reply_arian.txt
```

One directory per generation run, named with today's date. Old runs stay around for reference and to compare drafts vs. what actually got sent.

## Cohorts

Three buckets the workflow generates files for. Each bucket gets one file per active sender (Chuka, Arian, etc.).

| Cohort | Filter | Why follow up |
|---|---|---|
| **Replied, no meeting** | `Deal.state=Connected` AND `Deal.last_reply_at IS NOT NULL` AND has at least one inbound `Message` AND not in already-met set AND not disqualified | They engaged. Most actionable cohort. |
| **Connected, no reply** | `Deal.state=Connected` AND `Deal.last_reply_at IS NULL` AND not disqualified AND ICP-relevant title | Cold lead, but they accepted the invite. Try a different angle than the original. |
| **Replied, polite no** | Same as "replied" but inbound message contains decline phrases | Don't follow up. Disqualify the Lead. |

## Step-by-step

### Phase 1 — Pull cohort data

```bash
.venv/bin/python manage.py shell <<'EOF'
import json
from crm.models import Lead, Deal, Message

ALREADY_MET_ATTIO_IDS = {  # update each run
    "55fe2a6d-...","6fa371ad-...",  # the 14 (or however many) we already met
}

# Cohort A: replied, no meeting
deals_replied = (Deal.objects
    .filter(state="Connected", last_reply_at__isnull=False, lead__disqualified=False)
    .select_related("lead").order_by("-last_reply_at"))

# Cohort B: connected, no reply
deals_no_reply = (Deal.objects
    .filter(state="Connected", last_reply_at__isnull=True, lead__disqualified=False)
    .select_related("lead"))

# For each candidate gather: lead profile (from Lead.description JSON), full message thread, primary outbound sender
candidates = []
for d in deals_replied:
    lead = d.lead
    if lead.attio_person_id in ALREADY_MET_ATTIO_IDS: continue
    msgs = list(Message.objects.filter(lead=lead).order_by("sent_at").values("source","direction","sent_at","body","sender"))
    if not any(m["direction"]=="inbound" for m in msgs): continue
    try: prof = json.loads(lead.description) if lead.description else {}
    except Exception: prof = {}
    candidates.append({
        "lead_id": lead.id, "first_name": lead.first_name, "last_name": lead.last_name,
        "company_name": lead.company_name, "headline": prof.get("headline",""),
        "summary": (prof.get("summary","") or "")[:1500],
        "primary_sender": next(iter([m["sender"] for m in msgs if m["direction"]=="outbound"]), ""),
        "messages": [{"d": m["direction"], "t": str(m["sent_at"])[:19], "b": (m["body"] or "")[:600], "s": m["sender"]} for m in msgs],
    })

with open("/tmp/replied_candidates.json","w") as f: json.dump(candidates, f, indent=2)
EOF
```

### Phase 2 — Classify replies into buckets

For the replied cohort, scan inbound message text for decline phrases and active-scheduling indicators:

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

# A lead replied + last outbound was within 5 days = ACTIVE (skip, too fresh)
# A lead's inbound contains a NO_PHRASE = disqualify
# Otherwise = follow-up candidate
```

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

**Priority labels** in the file are internal-only metadata (never appear in the actual message). Format: `PRIORITY: HIGH/MEDIUM-HIGH/MEDIUM/LOW (reasoning in plain language)`.

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
PRIORITY: HIGH ([reasoning])

[Draft message in user's voice]


--- [next person] ---
...


=================================================
SUMMARY
=================================================
[counts, recommended order, dedupe alerts, action items]
```

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
