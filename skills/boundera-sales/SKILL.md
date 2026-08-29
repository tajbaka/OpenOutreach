---
name: boundera-sales
description: Draft, rewrite, shorten, or critique concise one-off Boundera sales copy for email, LinkedIn connection notes, DMs and InMail, SMS or Slack, meeting recaps, re-engagement, scheduling, and objection replies. Use for ready-to-send prospect or partner wording outside the structured ICP Messages Google Sheet. Do not use for ICP Messages Sheet campaign copy, call preparation, opportunity strategy, CRM stages, or the 15-step sales-motion tracker.
---

# Boundera Sales Copy

## Scope

This is a one-off copy-generation skill. It produces the words a Boundera
sender will send in an individual conversation; it does not author structured
ICP Messages Sheet campaigns, build target lists, decide the opportunity's
sales stage, or recreate the video sales framework.

For call preparation, discovery design, stakeholder strategy, or deal
inspection, use the repo's sales-motion strategy references instead. For Sales
Motion Sheet work, use `boundera-sales-motion`. Do not load those materials for
an ordinary copy request.

For campaign copy in a sender's `ICP Messages` Google Sheet, use
`boundera-icp-messages`. Do not edit the Sheet or its runtime JSON stores from
this skill.

## Required References

Before drafting:

1. Read `references/copy-patterns.md` for the applicable channel, evidence, and
   measurable budget.
2. Read `references/fedramp-20x.md` for any FedRAMP 20x claim. Read
   `references/fedramp-rev5.md` as well when the message mentions Rev5, SSP,
   SAP, SAR, POA&M, legacy ConMon, Ready, In Process, migration, or a 20x-versus-
   Rev5 comparison.
3. Browse current official FedRAMP sources before using a date, deadline,
   program phase, class/path availability, mandatory/optional status, exact
   quotation, or other time-sensitive requirement. Never rely on a search
   snippet or consultancy summary for those claims.
4. For a cold first touch or a request to match established campaign voice,
   inspect the exact sender/ICP block in `linkedin/icp_messages.json`. If the
   user asks for Chuka's voice, use the `Chuka` block. Never treat Chuka as a
   runtime fallback for another sender, and never copy a template without
   rechecking its context, length, and claims. An actual conversation thread
   outranks a campaign template.

## Workflow

1. Recover the actual conversation context when available.
   - Treat pasted text as the newest source.
   - If a person or company is named, inspect the latest relevant CRM, Gmail,
     LinkedIn, meeting-note, or local-note context available in OpenOutreach.
   - Identify the sender, recipient, channel, relationship temperature, latest
     message, requested outcome, and any explicit communication boundary.
   - Never invent a relationship, trigger, customer example, result, deadline,
     or product capability.

2. Pick one message profile and its budget.

   | Profile | Target | Boundera ceiling |
   | --- | ---: | ---: |
   | Cold email | 50–90 words; 3–4 sentences | 100 words |
   | Warm scheduling email | 30–80 words | 120 words |
   | Email reply or objection | 20–75 words | 100 words |
   | Post-meeting recap | 80–150 words | 180 words |
   | Cold-email bump | 5–25 words; 1 sentence | 35 words |
   | LinkedIn connection note | 120–180 characters | 200 characters |
   | LinkedIn first DM or InMail | 200–400 characters | 500 characters |
   | LinkedIn reply or nudge | 15–50 words | 80 words |
   | SMS or Slack note | 10–45 words | 70 words |

   These are Boundera operating budgets, not universal platform limits. A
   requested technical answer, proposal, or substantive recap may exceed them,
   but only when the extra content is necessary.

3. Choose one relevant idea.
   - Default FedRAMP product copy to the current 20x model unless the actual
     conversation is explicitly Rev5 or mixed-path.
   - Anchor product relevance to one appropriate concept: KSI measures and
     evidence, verification and validation, persistent validation, Security
     Decision Records, current human- and machine-readable certification data,
     assessor review, trust-center sharing, or vulnerability response.
   - Do not cram several concepts into a short message. Plain language beats a
     glossary.

4. Draft in this order.
   - Verified reason for writing or direct acknowledgement.
   - Recipient priority, problem, or requested context in their language.
   - One concrete Boundera relevance or proof point, when needed.
   - One low-friction primary call to action.

5. Edit once for compression and accuracy.
   - Remove any sentence that does not advance the single desired outcome.
   - Prefer short words, contractions, and natural founder language.
   - Keep paragraphs to one or two sentences.
   - Silently count the final body, including the greeting and CTA but excluding
     the subject and standard signature. For close calls, run this from the
     OpenOutreach repo root and pass the final body through `--text` or standard
     input:

     ```bash
     .venv/bin/python skills/boundera-sales/scripts/check_copy.py email-cold --text "Final body"
     ```

   - If the user asks for a length that conflicts with these defaults, follow
     the user's instruction and disclose the count only when useful.

## Copy Rules

- One message has one primary outcome and one primary CTA. This is a Boundera
  house rule that prevents competing asks.
- Cold first touches should usually ask about relevance or offer a useful
  comparison, example, or perspective. Do not lead with a calendar link.
- Once the recipient is engaged, propose one or two concrete times. Add the
  correct booking link only as a convenient fallback.
- Email subjects should be 1–4 words, usually 12–40 characters, and tied to the
  recipient's priority or shared context.
- Personalization must explain why this recipient or company is relevant. A
  first-name token or generic compliment is not personalization.
- Start with the recipient's situation, not Boundera's company history.
- Use one product proof point, not a feature inventory.
- Do not default to `AI`, `platform`, `all-in-one`, `10x`, ROI promises, or
  generic efficiency language.
- Avoid `hope you're well`, `just checking in`, `circling back`, and
  `following up`. State the reason for writing.
- Do not add fake urgency, false scarcity, excessive enthusiasm, or a
  manufactured compliment.
- Do not argue with a no. Respect opt-outs and channel preferences immediately.

## Product Grounding

For a high-level description that does not require a named capability, use a
deliberately conservative baseline such as:

> Boundera helps cloud providers keep FedRAMP security evidence, validation,
> and remediation work current and easier to review.

This baseline is intentionally incomplete; do not treat it as a product
inventory.

Before naming or describing a Boundera capability, locate the current
FedRampGPT product repo and inspect the relevant product documentation,
implementation, routes or services, and tests. This applies to capabilities
such as VDR, trust-center sharing, Security Decision Records, JSON or schema
exports, connectors, KSI evaluation, assessor workflows, automation cadence,
and class or path coverage.

- Search only for the capability needed by the message; do not inventory the
  whole product for every draft.
- Confirm the user-visible behavior and its scope. A matching string, dormant
  code path, campaign template, old note, or existing marketing statement is
  not sufficient product evidence by itself.
- Existing `icp_messages.json` and `gmail/icp_emails.json` copy proves what is
  configured to send, not that its product claims remain accurate.
- Use `helps`, `supports`, or `makes it easier to` unless the verified product
  behavior completes the stated workflow end to end.
- If current product evidence is unavailable or ambiguous, omit the named
  capability or describe only the narrower verified behavior.

## Booking Links

`linkedin/calendar_links.py` in the OpenOutreach repo is the canonical source.
Locate the repo root when the current working directory differs, read the file
at drafting time, and select from `ARIAN_CALENDAR_LINKS`:

- `intro`: first conversation.
- `next_steps`: the next meeting in an established opportunity.
- `deep_dive`: a detailed product, technical, or assessor session.
- `general`: a normal meeting that does not fit another type.
- `quick_chat`: a short conversation.

Do not reproduce remembered Cal.com URLs in this skill. Do not substitute
Arian's link for another sender. If the sender's canonical link is unavailable,
use the campaign's configured link or omit it; never guess.

For a warm scheduling note, prefer this sequence:

1. Offer one or two specific times in the recipient's known timezone.
2. Say what the meeting is for.
3. Add the selected calendar link as a fallback in natural prose.

## FedRAMP and Product Accuracy

- Distinguish a provider's FedRAMP Certification from an agency's ATO.
- Distinguish CSP, assessor, FedRAMP, and agency responsibilities.
- Never say Boundera guarantees, grants, or replaces Certification, an assessor,
  FedRAMP review, agency review, or an ATO.
- Never call the entire process fully automated. Some evidence and decisions are
  human or non-machine based.
- Never say 20x simply eliminated SSPs. The provider Security Decision Record
  replaces the traditional provider SSP under applicable current rules; an
  agency still documents its own use and authorization.
- Treat exact timelines, class mappings, transition dates, and rule
  applicability as time-sensitive.
- Never imply support for every cloud, tool, connector, class, path, artifact,
  or workflow without current product evidence.
- Never invent customer results, logos, savings, or authorization timelines.
- Do not call Boundera an `all-in-one compliance platform`, `one-click
  FedRAMP`, or `FedRAMP approved` product without precise current evidence that
  makes the wording accurate.

## Output Contract

- Return the ready-to-send version first.
- For email, include a subject and body unless the user asks for body only.
- For LinkedIn or SMS, return only the message unless context is required.
- Do not add an explanation, strategy memo, or a list of alternatives by
  default. Provide at most three variants when the user asks for options.
- Preserve the sender's real voice and the thread's existing level of
  familiarity.
- Create a Gmail draft or mutate a temp file only when explicitly asked. Never
  send a message without explicit authorization.

## References

- `references/copy-patterns.md`: research, measurable budgets, structures, and
  caveats.
- `references/fedramp-20x.md`: current 20x concepts, language, and source links.
- `references/fedramp-rev5.md`: Rev5 and Consolidated Rules transition language.
