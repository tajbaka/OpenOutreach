---
name: boundera-icp-messages
description: Create, revise, or validate sender-specific Boundera campaign copy in the OpenOutreach ICP Messages Google Sheet, including connection notes, LinkedIn follow-ups, and Gmail subjects and bodies. Use only for structured ICP Messages Sheet authoring or review; default authoring work to Chuka when the user does not name another sender. Do not use for one-off sales replies, scheduling, opportunity strategy, or sending messages.
---

# Boundera ICP Messages

## Scope

Use this skill only for copy in a sender's `ICP Messages` Google Sheet tab. It
authors or reviews the rigid templates that OpenOutreach later pulls into the
LinkedIn and Gmail JSON stores. It never sends outreach.

Use `boundera-sales` for an individual reply, scheduling note, recap, objection,
or other ready-to-send message outside this structured Sheet workflow. Use
`boundera-sales-motion` for opportunity tracking.

## Sources

Locate the OpenOutreach repo before working. For a Sheet-copy request:

1. Read `references/icp-personas.md` for the human-maintained persona priority
   and attention angle. This file contains strategy, not deployed copy.
2. Read the target sender's current blocks in `linkedin/icp_messages.json` and
   `gmail/icp_emails.json`. These are the runtime copies and the concrete voice
   and sequence reference.
3. Read the live target worksheet before drafting. The Sheet may contain human
   edits that have not yet been pulled into JSON; compare rather than assuming
   either side is newer.
4. Read the shared references under `skills/boundera-sales/references/`:
   - `copy-patterns.md` for measurable channel budgets;
   - `fedramp-20x.md` for 20x claims; and
   - `fedramp-rev5.md` when the copy mentions Rev5, SSP, SAP, SAR, POA&M,
     legacy ConMon, Ready, In Process, migration, or a 20x-versus-Rev5
     comparison.
5. Browse current official FedRAMP sources before using a date, deadline,
   current program status, class/path availability, mandatory/optional claim,
   or exact quotation.
6. For any named Boundera capability, locate the current FedRampGPT product
   repo and verify the exact behavior in relevant product documentation,
   implementation, routes or services, and tests before using it in Sheet copy.

## Sender and ICP Selection

- Default authoring and review requests to the `Chuka ICP Messages` tab when
  the user does not name another sender. State that choice in the work summary.
- `Chuka` is an authoring default, not a runtime fallback. OpenOutreach requires
  an exact sender block and must never borrow Chuka's templates for another
  operator.
- Preserve the exact ICP label already used by the row and JSON configuration.
  Do not rename, merge, or add an ICP bucket unless the user requests that
  structural change.
- A persona is more specific than an ICP row. Use the persona reference to
  choose the angle, but keep the row keyed by its supported OpenOutreach ICP
  label.
- Never infer FedRAMP stage, budget pressure, buying authority, urgency, or
  technical ownership from a title alone.

## Sheet Workflow

1. Inspect the target tab and the matching sender/ICP blocks in both JSON files.
   Identify the exact row and dynamic message columns before proposing edits.
2. Identify the intended persona, verified campaign context, channel, and one
   desired response for each message. If the persona is not represented in
   `icp-personas.md`, use confirmed user context and propose a persona-reference
   addition separately; do not invent buyer facts.
3. Draft each cell as a standalone message. LinkedIn and Gmail lanes can run
   independently, so an email must not rely on the recipient having seen a
   particular LinkedIn follow-up, or vice versa.
4. Apply the shared copy budgets and one-primary-CTA rule. Connection notes
   should establish relevance without a calendar link or product monologue.
5. Use only placeholders supported by OpenOutreach:
   `{first_name}`, `{last_name}`, `{company_name}`, `{my_name}`,
   `{our_company_name}`, and `{our_website_url}`. Do not introduce a new
   placeholder or media token without verifying the runtime parser and assets.
6. Map copy to the existing Sheet schema:
   - `ICP`
   - `Connect Message`
   - `Followup Message N`
   - `Email Subject N`
   - `Email Body N`

   Step columns are interleaved and the width is dynamic. Email subject/body
   cells for a step are a required pair.
7. When the user authorizes a Sheet edit, update only the intended cells or
   row, then read them back to verify the stored values. Preserve other rows,
   formulas, formatting, and unknown columns.

## Product Grounding

For a high-level description that does not require a named capability, use a
deliberately conservative baseline such as:

> Boundera helps cloud providers keep FedRAMP security evidence, validation,
> and remediation work current and easier to review.

This is not a capability inventory. Before mentioning VDR, trust-center
sharing, Security Decision Records, JSON or schema exports, connectors, KSI
evaluation, assessor workflows, automation cadence, or class/path coverage,
inspect the current FedRampGPT implementation and tests for that specific
claim.

- A matching string, dormant code path, Sheet cell, campaign JSON message, old
  note, or marketing statement is not sufficient evidence by itself.
- Runtime JSON proves what is configured to send, not that a product claim is
  still accurate.
- Use `helps`, `supports`, or `makes it easier to` unless verified behavior
  completes the stated workflow end to end.
- Never say Boundera grants Certification or an ATO, replaces FedRAMP, an
  agency, or an independent assessor, or makes the entire process fully
  automated.
- Never imply support for every cloud, tool, connector, class, path, artifact,
  or workflow; invent customer outcomes; or use `all-in-one`, `one-click
  FedRAMP`, or `FedRAMP approved` without precise current evidence.
- If current product evidence is unavailable or ambiguous, omit the named
  capability or describe only the narrower verified behavior.

## Safety and Publication

- Do not use `sync_icp_messages --push` for a targeted copy edit. The push path
  clears and rewrites the entire sender worksheet from JSON.
- A Sheet edit does not change the runtime JSON. Run the following only when
  the user explicitly asks to publish, apply, pull, or sync the reviewed Sheet
  copy:

  ```bash
  .venv/bin/python manage.py sync_icp_messages --sender Chuka --pull
  ```

- `--pull` imports the complete populated sender tab into both
  `linkedin/icp_messages.json` and `gmail/icp_emails.json`. Review the full tab
  first and inspect both git diffs afterward.
- Pull preserves existing JSON cadence for corresponding sequence steps and
  JSON-only fields such as media. New steps receive parser defaults because
  the Sheet stores copy, not cadence.
- Blank Gmail cells in a valid ICP row disable that sender/ICP's Gmail lane on
  pull. Never clear them casually.
- Do not add, delete, reorder, or flatten sequence columns without explicit
  authorization and full-tab validation.
- Never send Gmail or LinkedIn messages from this skill.

## Validation

Before completing a Sheet write or JSON pull, verify:

- the sender and worksheet title are exact;
- the ICP row is supported and appears only once;
- `Connect Message` and the first `Followup Message` are nonblank;
- every populated email step has both subject and body;
- placeholders are supported and render without sentinel text;
- each message fits its channel budget or has a documented reason not to;
- each message has one purpose and one primary CTA;
- product claims pass current FedRampGPT verification and FedRAMP claims pass
  the applicable official-source references; and
- a read-after-write matches the intended cells.

After a JSON pull, run the repo's template validation and relevant ICP outbound
tests before reporting completion.

## Output Contract

- For review-only work, return the proposed Sheet cell values grouped by exact
  header.
- For an applied Sheet edit, report the sender tab, ICP row, cells changed, and
  validation counts.
- State whether runtime JSON was left unchanged or explicitly pulled.
- Do not include an unsolicited campaign-strategy memo or alter another sender.

## Reference

- `references/icp-personas.md`: concise human-maintained persona priorities and
  attention angles; never deployed message copy.
