# Reusable email attachments

OpenOutreach can attach an approved file to a campaign email step or to an
individual email. A file is a reusable **email asset**: registered once from a
storage provider, approved by its exact SHA-256, and then either assigned to
any number of Leads or named directly by a step. Google Drive is the first
provider. Files stay private in Drive; OpenOutreach reads them through the
existing Google service account at send time.

Code lives in the `email_assets` Django app (migration `email_assets.0001`).

## Concepts

| Concept | What it is |
| --- | --- |
| `EmailAsset` | One provider file (`provider`, `provider_file_id`), the attachment filename and type, the SHA-256 last read from the provider, and the `approved_sha256` an operator approved. Status: awaiting approval, approved, retired. |
| `LeadEmailAsset` | One Lead's slot, for example `marketplace_pdf`, mapped to one asset. Several Leads can share one asset (all contacts at one company receive the same PDF). |
| Step requirement | A Gmail step in a published message program declares `"attachments": [{"slot": "marketplace_pdf", "required": true}]` (resolved per recipient) or `[{"asset": "<asset key>", "required": true}]` (same file for everyone). It is part of the immutable `MessageProgramVersion`, like LinkedIn `media`. |

`required` defaults to true. An optional slot with no assignment for a
recipient is skipped. Every other problem holds the email.

## One-time setup: Drive access

The Drive provider uses the service account in `GOOGLE_SHEETS_CREDENTIALS_PATH`
with the read-only Drive scope. No Gmail OAuth scope or new login is needed.

1. Share each file, or the folder that holds them, with the service account's
   email as a **Viewer**. `register_email_asset` and `show_email_asset --check`
   print that address when access is missing.
2. The service account's Google Cloud project must have the Drive API enabled
   (verified for the current account on 2026-11-10).
3. Viewers must be allowed to download the file. Drive can let a viewer see a
   file (and its checksum) while blocking downloads; OpenOutreach reads Drive's
   `capabilities.canDownload` and holds such a file as `inaccessible`.
4. Every machine whose Gmail worker sends attachment steps needs the service
   account file at `GOOGLE_SHEETS_CREDENTIALS_PATH`. Without it those emails
   are held as `inaccessible`. Emails without attachments are unaffected.

Upload a real file (PDF, PNG, JPEG, GIF, DOCX, XLSX or PPTX). Native Google
Docs/Sheets/Slides are refused: Drive regenerates their exports on every
download, so an approved hash could never be verified again. Export to PDF
first. Limits: at most 5 attachments and 18 MiB of attachment bytes per email.

## Operator workflow

Every command previews by default and writes only with `--apply`.

```bash
# 1. Register (fetches and hashes the private file; does not approve it)
.venv/bin/python manage.py register_email_asset \
  'https://drive.google.com/file/d/<file-id>/view?usp=sharing' \
  --key acme-marketplace-pdf --name 'Acme Marketplace PDF' \
  --registered-by Arian --apply

# 2. Review the exact bytes, then approve that hash
.venv/bin/python manage.py show_email_asset acme-marketplace-pdf \
  --download artifacts/email-assets/acme-marketplace.pdf
.venv/bin/python manage.py approve_email_asset acme-marketplace-pdf \
  --sha256 <hash printed by register/show> --approved-by Arian --apply

# 3. Assign to Leads: by ID, or every contact at one company in one campaign
.venv/bin/python manage.py assign_email_asset acme-marketplace-pdf \
  --slot marketplace_pdf --campaign <id> --company 'Acme Cloud' \
  --assigned-by Arian --apply

# 4. Before activating or while running: who gets which file on which step
.venv/bin/python manage.py review_email_attachments --campaign <id> --check-files \
  --output artifacts/email-assets/campaign-<id>-review.csv
```

Approval binds to the hash the reviewer saw: `approve_email_asset` refetches the
file and refuses if it changed. The Django Admin offers the same flow
(**Email Attachments → Email assets → Add**: paste the Drive link; then the
"Approve the current registered bytes" and "Re-read the file" actions). Lead
assignments can also be edited in Admin. Admin edits of an asset change only
its name and notes; status and hashes change only through approve, refresh
and retire. An assignment edit changes only its file, uses the same checks as
`assign_email_asset`, and refuses retired assets.

`review_email_attachments` lists, for each Deal and each attachment step, the
asset, filename, approved hash, delivery status and one of: `ready`
(`ready_unchecked` without `--check-files`), `skipped_optional`, `sent` (with
the hash actually attached), `missing_assignment`, `pending_approval`,
`retired`, `unknown_asset`, `inaccessible`, `changed_since_approval`,
`invalid_file`, `invalid_combination`, `delivery_stopped|failed|unclear`,
`variant_undecided` or `unresolved_audience`. `--problems` prints only held
rows. It is read-only.

`--check-files` does what a send does for each distinct approved file, once:
it confirms Drive allows the download, downloads the bytes and requires the
approved hash. A matching Drive checksum alone is not trusted. Every review
also applies the sending rule for each email's combination of files (no two
attachments with one filename, case-insensitive; at most 5 files and 18 MiB in
total, using registered sizes), so `ready` means the send would accept it.

### When a Drive file changes

Editing an approved file in Drive makes its next email hold with
`changed_since_approval`. Run `refresh_email_asset <key> --apply` to record the
new bytes (the asset returns to awaiting approval), review them, then approve
the new hash. Held emails send automatically on their next check.
Refresh changes only provider facts (bytes hash, size, checksums, type) on the
current database row, so a retirement or approval made while it downloads is
kept. Approval refuses if the asset's status, hash, type or filename changed
while it was downloading; review it again and rerun.
`refresh_email_asset <key> --retire --apply` stops all use of an asset (emails
needing it are held as `retired`); approving it again by hash reinstates it.
Assets cannot be deleted, because sent messages reference them as evidence.

## Declaring an attachment on a campaign step

Campaign-specific programs live in `shared_programs` of
`gmail/icp_emails.json` (the same place as other dedicated programs). Add the
requirement to the Gmail step row. Example: the personalized Marketplace PDF
on Email 2 of a six-email sequence:

```json
{
  "audience_key": "csp-any-size-business-leaders-rev5-authorized-both",
  "channel": "gmail",
  "step_key": "email-2",
  "step_index": 1,
  "delay_hours": 192,
  "variant_key": "primary",
  "subject": "Your Marketplace one-pager",
  "body": "Hi {first_name}, I attached the {company_name} Marketplace PDF ...",
  "media": [],
  "sender_override": "",
  "notes": "",
  "attachments": [{"slot": "marketplace_pdf", "required": true}]
}
```

Rules:

- Only Gmail steps may declare attachments; LinkedIn steps are refused.
- The published payload stores the canonical `{kind: key, "required": bool}`
  form and omits the field when empty, so programs without attachments keep
  their existing content hashes and versions.
- The eight-column General ICP Messages Sheet cannot carry attachments;
  rendering a program that has them back to that layout is refused, like
  `media`.
- Requirements are frozen in the version a Deal is enrolled in. Which file
  satisfies a slot is read from the recipient's assignment at send time, so
  assignments may be made or corrected after enrollment.

For the Marketplace example: generate each company's PDF with
`skills/boundera-personalized-collateral/`, upload the PDFs to a Drive folder
shared with the service account, register one asset per company, assign each
to that company's contacts with `--campaign/--company`, and run the review
before activating the campaign.

## What happens at send time

The account-scoped Gmail worker, for a version-bound `gmail_follow_up` Task
whose step declares attachments, after every existing check (campaign active,
reply/stop policy, drip ownership, sent-step dedupe, usable address, due time
and thread context):

1. Resolves each requirement for the recipient and checks the combination of
   files (DB only, before any download).
2. Confirms the provider allows the download, downloads each file and proves
   its bytes equal `approved_sha256`.
3. Issues the single-use delivery permit over the exact message **and** the
   attachment identities (filename, type, SHA-256, size). The client refuses
   any other bytes.
4. At the submission boundary, inside the transaction that stamps it, locks
   the recipient's slot assignments and the assets and re-plans from current
   state. If an asset was retired or unapproved, re-approved with other bytes,
   unassigned or reassigned while the file was downloading, nothing is
   submitted and the email is held (`assignment_changed` or the matching
   reason). Changes committed after that boundary apply to the next email.
   Assignment changes lock the same rows in the same order (Lead, then slot
   assignment, then asset), so a reassignment during a send waits for it
   instead of deadlocking with it.
5. Builds the plain-text body exactly as before, adds the files as MIME
   attachments and submits through Gmail's upload endpoint (35 MB limit),
   keeping `threadId`, `In-Reply-To` and `References`.
6. Records evidence in `crm.Message.raw["attachments"]` (asset ID/key,
   requirement, provider file ID, filename, type, size, SHA-256).

If any requirement cannot be met, nothing is submitted. The same Task returns
to pending with `error = "Attachment hold: <reasons>"`, its delivery stays
queued, and it rechecks every 60 minutes (`ATTACHMENT_HOLD_RETRY`) until the
problem is fixed. An ops Slack alert is posted once per new reason. Later steps
are scheduled from the actual send, so a held Email 2 also delays Email 3.
Stopping the campaign or a reply still ends the sequence as before.

Unchanged: emails whose step declares no attachments use the identical inline
request, permit fingerprint and stored Message fields; threading, reply stops,
campaign activity checks, sender windows, the submission boundary, unclear-send
handling and crash recovery are the existing code paths.

## Individual emails

One-off emails use the Gmail drafts API (`skills/gmail-oauth`). The helpers in
`email_assets/one_off.py` resolve and verify approved files and read the saved
draft or sent message back:

```python
from email_assets.one_off import (
    attach_to_message, recheck_one_off_attachments,
    resolve_one_off_attachments, verify_gmail_attachments,
)

attachments = resolve_one_off_attachments(asset_keys=["acme-marketplace-pdf"])
# or: resolve_one_off_attachments(lead_id=lead.pk, slots=["marketplace_pdf"])
attach_to_message(mime_message, attachments)   # after set_content(body)
# users.drafts.create(...) as the skill describes, then:
verify_gmail_attachments(service, draft_id=draft_id, expected=attachments)
# immediately before users.drafts.send (after explicit approval):
recheck_one_off_attachments(attachments)   # lead_id=... for slot files
```

A draft is still not authorization to send, and these helpers never send.

## Scope

- Supported: version-bound campaign Gmail steps and individual draft emails.
- Not supported: legacy unbound sender-JSON Gmail copy, drip campaigns and
  LinkedIn messages (LinkedIn keeps its own reviewed `media`).
- No live Drive file, campaign or send was used to build this feature; the
  first real use should register one Marketplace PDF, review it, and run
  `review_email_attachments --check-files` on a disabled campaign before
  activation.

## Deployment

1. Apply migration `email_assets.0001` (two new tables; nothing existing
   changes).
2. Fully restart each sender's supervisor so its Gmail worker loads the new
   code.
3. Confirm the sending machine has the service account file and the files are
   shared with it (`show_email_asset <key> --check` there).
