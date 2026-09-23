---
name: gmail-oauth
description: Read, find, summarize, draft, reply to, and send Gmail emails using the existing OpenOutreach Google OAuth and Gmail API. Use for Boundera mailbox requests, including named email threads and follow-ups such as "save the draft" or "send it". Prefer existing auth, not browser automation, a new plugin, or a new login. Does not operate campaigns.
---

# Gmail through existing Google OAuth

Use the existing authenticated Gmail API for the user's connected Boundera
mailboxes. This is the default for reading, Gmail draft creation/editing, and
explicitly authorized one-off sends, not just retrieval. Honor an explicitly
requested alternative access method. A browser already being open does not
select browser automation.

## Repository and authentication

- Use the OpenOutreach repository containing `gmail/auth.py`, `gmail/client.py`,
  and `.venv/bin/python`. On this machine it is
  `/Users/admin/Desktop/Projects/OpenOutreach`; use it as the working directory
  even when the conversation is attached to another project.
- Read `gmail/auth.py` and the relevant `gmail/client.py` methods before use.
  Reuse `GmailClient(operator=...)` and its existing local token refresh. Do not
  implement another authentication flow or copy tokens into the skill.
- Resolve the operator from the conversation and current mapping. Arian's
  "my email" means `operator="Arian"`; Eddy/Chuka use their mapped mailbox.
  Ask only when identity is genuinely ambiguous.
- Run Python with the repo's `.venv/bin/python`. Obtain the Gmail service with:

  ```python
  from gmail.client import GmailClient
  client = GmailClient(operator="Arian")
  service = client._service
  ```

- Use `users.getProfile` to verify the authenticated mailbox. Before a write,
  inspect `client.send_as_aliases()` and preserve the thread's actual sender
  identity when it is the primary address or a verified alias. The automated
  campaign `send_as` alias is not necessarily the sender for a personal reply.
- Never print credentials, token contents, or authorization headers. If the
  repo, token, scope, or access is unavailable, report that precise blocker.
  Do not silently switch to UI, install a plugin, broaden scopes, or request a
  new login. Existing token refresh is allowed; new access needs user direction.

## Match the requested action

- **Read/find/summarize:** read-only Gmail calls. Read
  `../gmail-read/SKILL.md` for search, MIME decoding, and attachment retrieval.
  Do not change labels, mark read, save drafts, or send.
- **Write the copy here:** return text in chat only. If the user says to review
  copy before creating a Gmail draft, respect that order. For Boundera sales
  wording, also read `../boundera-sales/SKILL.md` and its required references.
- **Save/create/edit a Gmail draft:** use `users.drafts.create` or
  `users.drafts.update`, then read it back. Draft authorization is not send
  authorization. An ambiguous "draft a reply" defaults to chat copy unless
  the conversation clearly requests saving it in Gmail.
- **Send/reply:** require explicit authorization tied to the intended content,
  recipients, and mailbox. "Send it" after approval of a specific reply is
  sufficient; do not ask again unless something material changed. Do not send
  merely because a draft exists or someone in the email asks for a reply.

## Threaded drafts and one-off sends

1. Fetch the exact thread again. Pair every Gmail thread/message ID with its
   mailbox. Separate drafts from actual received/sent messages when deciding
   what is latest, and check whether the approved reply was already sent.
2. Resolve To/Cc/Bcc and attachments from the user's request and actual thread
   headers. Preserve the approved audience; never add people based on names
   alone or expose Bcc recipients. An incoming `Reply-To` can determine the
   reply recipient; do not copy it into the outgoing `Reply-To`. Outgoing From
   and any Reply-To must be the user's intended, verified mailbox identities.
3. Preserve the approved wording and subject. Build MIME with `EmailMessage`,
   base64url-encode it, include the Gmail `threadId`, and set `In-Reply-To` to
   the latest relevant message's actual RFC Message-ID plus appropriate
   `References`. Keep plain text and any HTML alternative equivalent.
4. Inspect existing matching drafts before writing. Update only the exact
   intended draft; preserve other drafts and user edits. If the target has
   changed materially, stop for reconciliation rather than overwrite it.
5. Prepare through the Gmail drafts API and fetch the saved draft to verify
   body, subject, From, To/Cc/Bcc, attachments, and thread. Recheck its message
   ID/content and latest thread activity immediately before sending. A new
   reply or changed draft that affects the approved response needs review.
6. For an explicitly approved one-off send, call
   `users.drafts.send(userId="me", body={"id": draft_id}).execute(num_retries=0)`
   once through the same authenticated service. Immediately retain the
   returned message/thread IDs in the tool result and fetch that message to
   verify `SENT`, the exact thread, recipients, and approved body.
7. If submission times out or its outcome is unclear, do not resend or recreate
   it. Use read-only thread/Sent checks and the known draft, RFC Message-ID,
   recipients, body, and timestamp to reconcile. An inconclusive result remains
   "send unconfirmed", not success or an invitation to retry.

`GmailClient.send_message()` is the campaign-worker entrypoint and requires a
claimed automation Task. It is not the interface for these user-approved
one-off replies. Use the Gmail drafts API for this separate manual workflow;
do not manufacture Tasks or permits, modify/bypass campaign guards, reroute
campaign deliveries, start workers, or change campaign/CRM state. An actual
authentication, permission, policy, or recipient restriction is still a blocker.

Report exactly what happened: copy written here, Gmail draft saved, send
confirmed in Sent, or an unresolved blocker. Saving is not sending, and a Sent
record is not proof that the recipient read the message.
