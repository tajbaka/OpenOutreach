---
name: gmail-read
description: Find, read, and summarize emails, conversations, or attachments from the connected Boundera mailboxes using OpenOutreach's existing Google OAuth tokens and Gmail API. Use when the user says to check email, look at a thread, find someone's message, or get something from email, even without mentioning Gmail or Google auth. Does not send mail or operate campaigns.
---

# Gmail retrieval through existing Google auth

For drafting, saving Gmail drafts, or explicitly approved one-off sends, read
`../gmail-oauth/SKILL.md`. That skill is the overall OAuth-first email router;
this file remains the read-only retrieval procedure.

In this repository, a request to get something from email means direct Gmail
API access with the existing local Google OAuth credentials. Use this path
first. Do not open or control the user's browser, suggest installing a Gmail
plugin, or request a new login before checking the existing authenticated path.
An explicit user request for a different access method takes precedence.

## Authentication and mailbox selection

- Run from the repository root with `.venv/bin/python`.
- Read `gmail/auth.py` for the current `GMAIL_OPERATOR_MAPPING` and
  `token_path()`; use `gmail/client.py`'s `GmailClient(operator=...)` to load
  credentials and perform its existing token refresh. Do not duplicate auth.
- Arian's mailbox is `arian_boundera`; Eddy/Chuka's is `eddy_boundera`.
  For Arian's requests about “my email,” start with `operator="Arian"`.
  Use the sender/mailbox established by the conversation; ask only if the
  intended mailbox is genuinely ambiguous. Do not search every mailbox by default.
- Tokens live under ignored `data/gmail/`, resolved by `token_path()`.
  Never print, copy into artifacts, or commit credentials or token JSON.
- Read calls do not need send-as alias validation. `send_as` is an outbound
  alias, not proof of the mailbox's primary address; use `users.getProfile`
  if the actual authenticated address matters.

## Retrieve the requested material

Use the existing client's authenticated service for read-only Gmail calls.
For example, adapt the operator and search terms to the current request:

```python
from gmail.client import GmailClient

client = GmailClient(operator="Arian")
service = client._service
page = service.users().threads().list(
    userId="me", q="Pragya", maxResults=50,
).execute()
```

1. Search narrowly by name, exact sender/recipient, company domain, subject,
   or requested date range. Follow `nextPageToken` when needed for the requested
   scope; disclose limits rather than claiming a partial search is exhaustive.
2. Inspect candidate headers with `threads.get(format="metadata")`, then read
   matching conversations with `threads.get(format="full")`. Search snippets
   are discovery aids, not substitutes for the actual messages.
3. Walk nested MIME parts and base64url-decode bodies. Prefer `text/plain`;
   extract readable text from HTML when no plain-text alternative exists.
   Separate each message's new text from quoted history; do not count a quoted
   reply as a separate new message. Identify the latest actual sender and date.
4. Fetch requested attachments with `users.messages.attachments.get` using
   the exact message ID and attachment ID. Keep any necessary local downloads
   in ignored artifacts, not the skill or tracked files.
5. Summarize what the messages establish, including outstanding questions and
   the latest reply. Scope absence claims to the mailbox and search performed.
   Preserve the mailbox/thread ID pair; Gmail IDs are mailbox-local. A thread
   link can use `https://mail.google.com/mail/?authuser=ADDRESS#all/THREAD_ID`
   with the verified primary address, URL-encoded.

If authentication fails, report the specific missing-token, expired/revoked
authorization, or permission blocker without exposing secrets. Do not silently
switch to browser control, widen scopes, or launch a new OAuth consent flow.

## Scope

Retrieval authorizes reading, not sending, creating drafts, changing labels,
marking messages read, syncing CRM records, or scheduling outreach. Do not run
campaign workers or CRM import commands to retrieve a conversation. The existing
client may refresh its local token as part of authentication.

Use `skills/boundera-sales/SKILL.md` if the user also requests reply wording.
For opportunity strategy or call preparation, follow the sales-conversation
references in AGENTS.md. An email-only summary does not require those workflows.
