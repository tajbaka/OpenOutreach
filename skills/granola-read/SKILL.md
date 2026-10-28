---
name: granola-read
description: Read, find, summarize, or review Granola meeting notes and transcripts through OpenOutreach's existing read-only Granola API integration. Use for meeting searches, call reviews and meeting evidence in contextual CRM reviews. Do not use the Granola desktop app or browser UI unless the user explicitly requests it.
---

# Granola Read

Use the existing Granola public API client in OpenOutreach. This skill reads meeting evidence; Sheet writes follow the separately authorized CRM review runbooks.

## Canonical location and route

- This repository folder is the canonical skill. Read it directly from the checkout. If personal skill discovery needs an entry, link to this folder rather than maintaining another copy.
- Work from the authorized OpenOutreach checkout root. Use `.venv/bin/python` on macOS/Linux or `.venv/Scripts/python.exe` on Windows, never system Python.
- Use `manage.py granola_notes`, which calls `linkedin.granola.GranolaClient` with the configured `GRANOLA_API_KEY` and existing API settings.
- Do not open the Granola desktop app, browser app or computer-use surface as a fallback. If the key, scope, transport or note access fails, report the exact blocker and incomplete coverage unless the user explicitly requests another route. Do not treat unavailable meeting evidence as an absence of commitments.
- Never expose API keys or OAuth material in output, artifacts or Git.
- Do not run `sync_crm_v2_context`, `refresh_crm_v2`, `refresh_crm`, `generate_followups` or an `--apply` workflow to read or review meetings.

## Find the meeting

Search by the strongest supplied identity: attendee email, attendee name, company or meeting title.

macOS/Linux:

```bash
.venv/bin/python manage.py granola_notes --search "SEARCH TERM" --deep-search --no-gemini-fallback
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe manage.py granola_notes --search "SEARCH TERM" --deep-search --no-gemini-fallback
```

Keep `--no-gemini-fallback` for Granola evidence so stored Gemini notes are not misrepresented as Granola. The CRM review may consider stored Gemini notes separately as dated supporting evidence. If several meetings match, use title, attendees, date and account context to select the exact note; ask only when a material identity ambiguity remains.

For a request such as "latest Granola conversation" without a usable identity, omit `--search` and `--deep-search` to list recent notes, then select from visible metadata.

Search defaults are 365 days, 20 returned notes and 300 inspected notes. `--since-days`, `--limit` (up to 100), `--scan-limit` (up to 3000) and `--folder-id` can narrow or deliberately extend coverage. A capped search is not exhaustive. Use focused searches and report coverage gaps rather than assuming no older commitments exist. The Actions and Next step runbooks determine the required review history.

## Read or review the meeting

Use the exact returned `not_...` ID to retrieve the full transcript.

macOS/Linux:

```bash
.venv/bin/python manage.py granola_notes --note-id not_EXACTID --include-transcript
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe manage.py granola_notes --note-id not_EXACTID --include-transcript
```

For a substantive call review, read the full transcript rather than relying only on `summary_text` or `summary_markdown`. The client retrieves transcript pages; generated summaries remain supporting material. Distinguish prospect statements, Boundera statements, commitments, requested next steps, interpretations and external claims needing verification.

If the response is too large for a tool result, use `--output` with a private temporary path appropriate to the operating system, then inspect focused transcript ranges locally. Never commit transcripts or print an entire private transcript when a focused extract suffices.

## Report and authorized CRM use

Answer at the requested level of detail. For sales-call reviews, identify evidence-backed stakeholder roles, buying signals, unresolved risks, commitments and the next action. Link the returned Granola `web_url` when useful. Treat statements about current regulations, product status or pricing as claims until independently verified.

For the authorized contextual reviews, follow `docs/actions-workflow.md` and `docs/active-account-next-steps-workflow.md`: reconcile meeting commitments with later verified LinkedIn and Gmail evidence, preserve uncertain rows and verify permitted Sheet writes through the Sheets API. Those existing user instructions authorize the specified Sheet updates; this reading skill does not require asking again. A standalone meeting-read request does not authorize Sheet, CRM, note, draft or message changes.
