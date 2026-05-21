# Email Follow-up Sender (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the daemon's automated LinkedIn no-reply follow-up DM gets no response after 3 days, send a single follow-up email from the operator's Gmail using a per-ICP template — without modifying the existing LinkedIn outbound state machine.

**Architecture:** Pure sidecar. The runner queries `crm.Deal` × `crm.Message` to identify eligible leads (LinkedIn `daemon-send:` outbound ≥ 3 days old, zero inbound on any channel, zero outbound Gmail), then sends through Gmail API using per-operator OAuth tokens stored in `data/google-tokens-<operator>.json`. The send result is persisted as a new `crm.Message(source=GMAIL, direction=OUTBOUND)` row — which is itself the dedupe record. Zero changes to `linkedin/tasks/follow_up.py`, zero changes to `Deal.state`, zero new DB tables/columns. Runs as a separate process: `python manage.py send_email_followups --loop --interval-seconds 300`, alongside the daemon.

**Tech Stack:** Python 3.13, Django 5.2, `google-api-python-client`, `google-auth-oauthlib`, `pytest`. Templates extend the existing `linkedin/icp_messages.json` with a new `email_connect_followup` channel (and `email_connect_followup_subject` for subject lines) per operator × ICP.

**Out of scope (later phases):**
- Inbound Gmail reply polling/listener (Phase 2)
- Slack notification on inbound Gmail reply + enrichment menu (Phase 3)
- The broader `linkedin/notifications/google_apis.py` read-API surface in `docs/plan-direct-google-apis.md` (Calendar/Drive/Gmail-read) — Phase 1 ships only the `gmail_send_message` method on that class so the broader plan can extend it.

**Prerequisite (one-time, performed by the operator, NOT a task):**
1. Create a Google Cloud project at https://console.cloud.google.com/.
2. Enable the Gmail API.
3. Create an OAuth 2.0 Client ID, **type: Desktop app**.
4. Download the client credentials JSON and save it at `data/google-oauth-client.json` in the repo. This file is gitignored (the `data/` directory is already in `.gitignore`).
5. Add the operator's Gmail address (e.g. `ariantajbakh@gmail.com`) as a Test User on the OAuth consent screen (so the app stays "Testing" without verification).

---

## File Structure

**Files this plan creates:**

| Path | Responsibility |
|---|---|
| `linkedin/notifications/google_apis.py` | `GoogleApis(operator)` class — loads OAuth token, refreshes if needed, exposes `gmail_send_message(to, subject, body)`. Aligned with `docs/plan-direct-google-apis.md` so future read-API methods land on the same class. |
| `linkedin/email_followup/__init__.py` | Empty package marker. (Named `email_followup` to avoid shadowing Python's stdlib `email` package used by `linkedin/notifications/gmail_threads.py`.) |
| `linkedin/email_followup/eligibility.py` | `eligible_deals(operator: str, *, delay_days: int)` — returns a QuerySet of `Deal`s ready for an email follow-up, scoped to leads whose LinkedIn outbound originated from that operator. |
| `linkedin/email_followup/sender.py` | `send_email_followups(operator: str, *, limit: int, dry_run: bool)` — orchestrator: eligibility → template fill → send → persist `Message` row → Slack notify. |
| `linkedin/management/commands/google_oauth.py` | One-time interactive OAuth flow: `python manage.py google_oauth --operator Arian`. Opens browser, writes `data/google-tokens-Arian.json`. |
| `linkedin/management/commands/send_email_followups.py` | Runner. One-shot by default; `--loop` plus `--interval-seconds` for the long-running sidecar process. Respects active hours. |
| `tests/test_google_apis.py` | Unit tests for the `GoogleApis` class (mocked Gmail API). |
| `tests/test_email_eligibility.py` | Unit tests for the eligibility query. |
| `tests/test_email_sender.py` | Unit tests for the orchestrator (mocked Gmail send). |

**Files this plan modifies:**

| Path | Change |
|---|---|
| `requirements/base.txt` | Add `google-api-python-client`, `google-auth-oauthlib`. (`google-auth` is already a transitive dep via `gspread` but list it explicitly.) |
| `linkedin/exceptions.py` | Add `EmailFollowupError` exception class. |
| `linkedin/env_spec.py` | Register 4 new env vars (see Task 1). |
| `linkedin/conf.py` | Add 4 conf constants reading those env vars. |
| `linkedin/notifications/slack.py` | Add `notify_email_followup_sent` function. |
| `linkedin/icp_messages.json` | Add `email_connect_followup_subject` + `email_connect_followup` channels to every existing ICP bucket under `Arian` and `Chuka` (4 ICPs × 2 channels × 2 operators = 16 new entries). Other operators' blocks are not touched in Phase 1. |
| `.env.example` | Add the 4 new env vars in registry order. |
| `.gitignore` | `data/` is already ignored, but add an explicit `data/google-tokens-*.json` line as documentation. |
| `CLAUDE.md` | Add a section under "Commands" documenting the runner + OAuth setup, and add a bullet under "Architecture" referencing `linkedin/email_followup/`. |
| `ARCHITECTURE.md` | Add a new section "Email follow-up sidecar" after the existing "Phone enrichment" section. |

---

## Task 1: Scaffolding — dependencies, exception, env vars, conf constants

**Files:**
- Modify: `requirements/base.txt`
- Modify: `linkedin/exceptions.py:1-31`
- Modify: `linkedin/env_spec.py:120` (end of `ENV_VARS` tuple)
- Modify: `linkedin/conf.py:225` (after the `PROSPEO_API_KEY` line, before the node-monitoring section)
- Modify: `.env.example:38` (after the `# enrichment` block)
- Modify: `.gitignore:6` (after `data/`)

- [ ] **Step 1.1: Add new dependencies**

Modify `requirements/base.txt` — append after the existing `google-auth` line:

```
google-api-python-client
google-auth-oauthlib
```

Install them:

```bash
.venv/bin/pip install -r requirements/base.txt
```

Expected: both packages install cleanly. No version pin — Google's libraries follow semver well and we want the latest patches.

- [ ] **Step 1.2: Add the EmailFollowupError exception**

Append to `linkedin/exceptions.py`:

```python
class EmailFollowupError(Exception):
    """Raised when the email follow-up runner fails for an expected reason —
    missing OAuth token, template lookup miss, Gmail API rejection of a
    well-formed send. Unexpected errors (network blips, JSON parse fails)
    propagate so the daemon's crash-on-unexpected rule applies."""
    pass
```

- [ ] **Step 1.3: Register new env vars**

Add to `linkedin/env_spec.py`, appended inside the `ENV_VARS = (` tuple after the existing `TASK_FAILURE_STREAK_THRESHOLD` entry (before the closing `)`):

```python
    EnvVar("ENABLE_EMAIL_FOLLOWUP", False, False, "false", "feature_flags",
           "Enable the email-followup sidecar runner."),
    EnvVar("EMAIL_FOLLOWUP_DELAY_DAYS", False, False, "3", "limits",
           "Days to wait after a LinkedIn no-reply follow-up before sending email."),
    EnvVar("GOOGLE_OAUTH_CLIENT_PATH", False, False, "data/google-oauth-client.json",
           "google_apis",
           "Path to the OAuth 2.0 Desktop client JSON downloaded from Google Cloud Console."),
    EnvVar("GOOGLE_TOKEN_DIR", False, False, "data", "google_apis",
           "Directory holding per-operator OAuth refresh tokens (google-tokens-<operator>.json)."),
```

- [ ] **Step 1.4: Add conf constants**

Add to `linkedin/conf.py` after `PROSPEO_API_KEY = ...` (around line 224), before the `# Node monitoring` section header:

```python
# ----------------------------------------------------------------------
# Email follow-up sidecar (see linkedin/email_followup/)
# ----------------------------------------------------------------------
# Master kill-switch for the email-followup runner. When false, the
# `send_email_followups` command exits immediately with a clear log line
# and the sidecar process becomes a sleep loop. Mirrors the existing
# ENABLE_* gates so flipping it does not require restarting the LinkedIn
# daemon. Default OFF — operators opt in by setting it to true and
# completing OAuth setup via `manage.py google_oauth --operator <name>`.
ENABLE_EMAIL_FOLLOWUP = os.getenv("ENABLE_EMAIL_FOLLOWUP", "false").strip().lower() in {
    "1", "true", "yes", "on",
}

# How many days to wait after a successful LinkedIn no-reply follow-up DM
# before sending the email nudge. The clock starts from the most recent
# `daemon-send:` outbound LinkedIn Message on the lead. Smaller than this
# feels like a same-day multi-channel pile-on; larger and warm intent
# fades. 3 days is the recommended default.
EMAIL_FOLLOWUP_DELAY_DAYS = int(os.getenv("EMAIL_FOLLOWUP_DELAY_DAYS") or 3)

# Path to the Google Cloud OAuth 2.0 Desktop client JSON. Used only by
# the one-time `google_oauth` bootstrap command — refresh tokens stored
# under GOOGLE_TOKEN_DIR are what the runner reads on each invocation.
GOOGLE_OAUTH_CLIENT_PATH = os.getenv(
    "GOOGLE_OAUTH_CLIENT_PATH", "data/google-oauth-client.json"
).strip()

# Directory under which per-operator OAuth refresh tokens are stored
# (one file per operator: `google-tokens-<operator>.json`). Defaults to
# `data/` so tokens are gitignored alongside cookies and other secrets.
GOOGLE_TOKEN_DIR = os.getenv("GOOGLE_TOKEN_DIR", "data").strip()

```

- [ ] **Step 1.5: Mirror env vars into `.env.example`**

Add to `.env.example`, in registry order — after the `# enrichment` block (line 38) add an `# email_followup` block; the `# feature_flags` block (line 39) gets the new `ENABLE_EMAIL_FOLLOWUP` line; and the `# limits` block gets `EMAIL_FOLLOWUP_DELAY_DAYS`.

After line 37 (`PROSPEO_API_KEY=`), insert:

```
# google_apis
GOOGLE_OAUTH_CLIENT_PATH=data/google-oauth-client.json
GOOGLE_TOKEN_DIR=data
```

In the `# feature_flags` block, add a line for `ENABLE_EMAIL_FOLLOWUP=false` after `ENABLE_AUTO_PHONE_ENRICHMENT=false`.

In the `# limits` block, add a line for `EMAIL_FOLLOWUP_DELAY_DAYS=3` after `CONNECTION_SWEEP_INTERVAL_HOURS=2`.

- [ ] **Step 1.6: Document token files in .gitignore**

Insert into `.gitignore` after `data/` (line 6):

```
# OAuth refresh tokens — written by `manage.py google_oauth` per operator.
data/google-tokens-*.json
```

(Token files are already covered by `data/`; this line is documentation so a future operator running `git status` doesn't wonder why they don't see the files.)

- [ ] **Step 1.7: Verify the package still imports cleanly**

Run:

```bash
.venv/bin/python -c "import linkedin.conf; import linkedin.exceptions; print('ok')"
```

Expected: `ok`. If `EmailFollowupError` is misspelled or `linkedin.conf` blows up reading an env var, fix before committing.

- [ ] **Step 1.8: Commit**

```bash
git add requirements/base.txt linkedin/exceptions.py linkedin/env_spec.py linkedin/conf.py .env.example .gitignore
git commit -m "scaffold email-followup sidecar config"
```

---

## Task 2: Email templates — extend `icp_messages.json`

**Files:**
- Modify: `linkedin/icp_messages.json`

The existing JSON shape is `{sender: {icp: {channel: [variants]}}}`. For email we add two new channels per ICP bucket: `email_connect_followup_subject` (subject line variants) and `email_connect_followup` (body variants). Substitution tokens supported by `fill_message` (`{first_name}`, `{last_name}`, `{company_name}`, `{my_name}`, `{our_company_name}`, `{our_website_url}`) work unchanged. The runner picks the same variant index for subject and body so they stay paired.

- [ ] **Step 2.1: Add email channels under Arian's "CSPs" bucket**

Open `linkedin/icp_messages.json`. Inside `Arian.CSPs` (currently has `linkedin_connect_note` + `linkedin_connect_followup`), add two new sibling keys:

```json
      "email_connect_followup_subject": [
        "Following up — {our_company_name}"
      ],
      "email_connect_followup": [
        "Hi {first_name},\n\nFollowing up from LinkedIn — wanted to make sure this didn't get lost.\n\n{our_company_name} is an easy way to transition {company_name} to FedRAMP 20x, or map NIST controls faster:\n\n- Integrates directly into AWS / Azure / GCP\n- Identifies and helps fix what is missing\n- Manages POA&Ms\n- Generates your machine-readable SSP\n\nThis removes a lot of billable hours to advisors and makes the whole process much easier.\n\nMore at {our_website_url}.\n\nWould love to grab 15 min if it's relevant — happy to send a few times.\n\n{my_name}"
      ]
```

- [ ] **Step 2.2: Add email channels under Arian's "3PAOs/Assessors" bucket**

Inside `Arian["3PAOs/Assessors"]`, add:

```json
      "email_connect_followup_subject": [
        "Following up — {our_company_name} (3PAO referral program)"
      ],
      "email_connect_followup": [
        "Hi {first_name},\n\nFollowing up from LinkedIn — wanted to make sure this didn't get lost.\n\n{our_company_name} is an easier path to FedRAMP 20x for the CSPs you assess:\n\n- Integrates directly into AWS / Azure / GCP\n- Helps CSPs manage POA&Ms\n- Generates machine-readable SSPs\n- Includes an assessor portal for live evidence review\n\nWe run a referral program that gives 3PAOs a percentage of pilot + first-year commission for any CSPs you recommend that sign up with us.\n\nMore at {our_website_url}.\n\nWould love to grab 15 min if it's relevant.\n\n{my_name}"
      ]
```

- [ ] **Step 2.3: Add email channels under Arian's "Advisors" bucket**

Inside `Arian.Advisors`, add:

```json
      "email_connect_followup_subject": [
        "Following up — {our_company_name} (advisor referral program)"
      ],
      "email_connect_followup": [
        "Hi {first_name},\n\nFollowing up from LinkedIn — wanted to make sure this didn't get lost.\n\n{our_company_name} is an easier path to FedRAMP 20x for the CSPs you advise:\n\n- Integrates directly into AWS / Azure / GCP\n- Helps CSPs manage POA&Ms\n- Generates machine-readable SSPs\n\nWe run a referral program that gives advisors a percentage of pilot + first-year commission for any CSPs you recommend that sign up with us.\n\nMore at {our_website_url}.\n\nWould love to grab 15 min if it's relevant.\n\n{my_name}"
      ]
```

- [ ] **Step 2.4: Add email channels under Arian's "Channel" bucket**

Inside `Arian.Channel`, add:

```json
      "email_connect_followup_subject": [
        "Following up — {our_company_name} channel partnership"
      ],
      "email_connect_followup": [
        "Hi {first_name},\n\nFollowing up from LinkedIn — wanted to make sure this didn't get lost.\n\n{our_company_name} compresses FedRAMP authorization + ConMon work for CSPs. We're looking for channel partners who already touch customers in the FedRAMP path:\n\n- AI drafts SSP narratives + POA&M closure trails\n- Continuous evidence ingest from AWS / Azure / GCP\n- Channel partner program with commission on CSP customers that sign up through you\n\nLive demo at {our_website_url}.\n\nWorth a 2-min chat on partnership angles?\n\n{my_name}"
      ]
```

- [ ] **Step 2.5: Repeat 2.1-2.4 for Chuka's blocks**

Add the same four entries (identical body text, same subject) under `Chuka.CSPs`, `Chuka["3PAOs/Assessors"]`, `Chuka.Advisors`, `Chuka.Channel`. Copy verbatim from Arian — `{my_name}` substitution fills the operator handle so the same body works for both.

- [ ] **Step 2.6: Verify the JSON parses and templates fill**

Run:

```bash
.venv/bin/python -c "
from linkedin.icp_outbound import fill_message
out = fill_message(
    sender='Arian', icp='CSPs', channel='email_connect_followup',
    first_name='Pat', company_name='Acme', my_name='Arian', lead_id=1,
)
print('BODY:', repr(out.body[:80]))
subj = fill_message(
    sender='Arian', icp='CSPs', channel='email_connect_followup_subject',
    first_name='Pat', company_name='Acme', my_name='Arian', lead_id=1,
)
print('SUBJECT:', repr(subj.body))
"
```

Expected: BODY starts with `'Hi Pat,'` and SUBJECT renders the substituted company name. If `SheetsError` is raised, the JSON edit was malformed — fix and rerun.

- [ ] **Step 2.7: Commit**

```bash
git add linkedin/icp_messages.json
git commit -m "add email_connect_followup templates for Arian and Chuka"
```

---

## Task 3: `GoogleApis` class — OAuth-aware Gmail client

**Files:**
- Create: `linkedin/notifications/google_apis.py`
- Create: `tests/test_google_apis.py`

This is the single low-level wrapper around Google's Python clients. Phase 1 ships exactly one method: `gmail_send_message`. The broader `docs/plan-direct-google-apis.md` plan adds read-API methods to the same class later — we set up the OAuth scaffolding so that extension is a 5-line addition.

OAuth scopes for Phase 1: just `https://www.googleapis.com/auth/gmail.send`. (When the read-API broader plan ships, the operator re-runs `google_oauth` to add `gmail.readonly`, `calendar.readonly`, `drive.readonly`.)

- [ ] **Step 3.1: Write the failing test scaffold**

Create `tests/test_google_apis.py`:

```python
"""Unit tests for linkedin.notifications.google_apis.

Gmail API calls are mocked — we never hit the real Google API during
tests. The OAuth token file is a temp file written per test.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from linkedin.exceptions import EmailFollowupError
from linkedin.notifications.google_apis import GoogleApis, token_path_for


def _write_fake_token(token_dir: Path, operator: str) -> Path:
    """Drop a minimally-valid token JSON the google-auth library accepts."""
    token = {
        "token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "fake-client-id.apps.googleusercontent.com",
        "client_secret": "fake-secret",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
    }
    path = token_path_for(operator, token_dir=token_dir)
    path.write_text(json.dumps(token))
    return path


class TestTokenPathFor:
    def test_lowercases_operator(self, tmp_path):
        path = token_path_for("Arian", token_dir=tmp_path)
        assert path == tmp_path / "google-tokens-arian.json"

    def test_rejects_path_traversal(self, tmp_path):
        with pytest.raises(EmailFollowupError):
            token_path_for("../etc/passwd", token_dir=tmp_path)


class TestGoogleApisLoad:
    def test_missing_token_raises(self, tmp_path):
        with pytest.raises(EmailFollowupError, match="No OAuth token for operator"):
            GoogleApis(operator="Arian", token_dir=tmp_path)

    def test_loads_existing_token(self, tmp_path):
        _write_fake_token(tmp_path, "Arian")
        with patch(
            "linkedin.notifications.google_apis.Credentials.from_authorized_user_file"
        ) as creds:
            creds.return_value = MagicMock(valid=True, expired=False)
            api = GoogleApis(operator="Arian", token_dir=tmp_path)
            assert api.operator == "Arian"


class TestGmailSendMessage:
    def test_returns_message_id_on_success(self, tmp_path):
        _write_fake_token(tmp_path, "Arian")
        fake_message_id = "abc123"
        with patch(
            "linkedin.notifications.google_apis.Credentials.from_authorized_user_file"
        ) as creds, patch(
            "linkedin.notifications.google_apis.build"
        ) as build:
            creds.return_value = MagicMock(valid=True, expired=False)
            service = MagicMock()
            service.users.return_value.messages.return_value.send.return_value.execute.return_value = {
                "id": fake_message_id,
            }
            build.return_value = service

            api = GoogleApis(operator="Arian", token_dir=tmp_path)
            msg_id = api.gmail_send_message(
                from_address="arian@example.com",
                to="lead@example.com",
                subject="Hi",
                body="Hi Pat,\n\nFollowing up.",
            )
            assert msg_id == fake_message_id

            # Verify we passed a base64url-encoded RFC 2822 message
            send_kwargs = service.users.return_value.messages.return_value.send.call_args.kwargs
            assert send_kwargs["userId"] == "me"
            assert "raw" in send_kwargs["body"]

    def test_gmail_error_raises_email_followup_error(self, tmp_path):
        from googleapiclient.errors import HttpError

        _write_fake_token(tmp_path, "Arian")
        with patch(
            "linkedin.notifications.google_apis.Credentials.from_authorized_user_file"
        ) as creds, patch(
            "linkedin.notifications.google_apis.build"
        ) as build:
            creds.return_value = MagicMock(valid=True, expired=False)
            service = MagicMock()
            resp = MagicMock(status=400, reason="Bad Request")
            service.users.return_value.messages.return_value.send.return_value.execute.side_effect = HttpError(
                resp=resp, content=b'{"error":"bad"}',
            )
            build.return_value = service

            api = GoogleApis(operator="Arian", token_dir=tmp_path)
            with pytest.raises(EmailFollowupError, match="Gmail API rejected"):
                api.gmail_send_message(
                    from_address="arian@example.com",
                    to="lead@example.com",
                    subject="Hi",
                    body="Hi",
                )
```

- [ ] **Step 3.2: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/test_google_apis.py -v
```

Expected: ImportError or ModuleNotFoundError on `linkedin.notifications.google_apis`.

- [ ] **Step 3.3: Write the `google_apis.py` module**

Create `linkedin/notifications/google_apis.py`:

```python
"""OAuth-aware Google API client (Phase 1: Gmail send only).

One class per operator: `GoogleApis(operator)`. Reads the operator's
OAuth refresh token from `data/google-tokens-<operator>.json` (path
configurable via `GOOGLE_TOKEN_DIR`), refreshes if expired, and exposes
Google-API methods.

Phase 1 surface: `gmail_send_message(from_address, to, subject, body)`.
The broader `docs/plan-direct-google-apis.md` plan adds Gmail-read /
Calendar / Drive methods to this same class later. Keeping the class
single-instance-per-operator means each method can reuse the cached
`build(...)` service object.

OAuth scopes used in Phase 1: `gmail.send` only. The bootstrap command
(`manage.py google_oauth`) requests this scope; expanding scopes means
re-running the bootstrap for each operator.

Token files are gitignored (`data/google-tokens-*.json`). Generate them
with `python manage.py google_oauth --operator <name>` after the
operator has completed Google Cloud setup (see Task 4).
"""
from __future__ import annotations

import base64
import logging
import re
from email.message import EmailMessage
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from linkedin.conf import GOOGLE_TOKEN_DIR, ROOT_DIR
from linkedin.exceptions import EmailFollowupError

logger = logging.getLogger(__name__)

# Phase 1: send-only. Broader plan adds gmail.readonly, calendar.readonly,
# drive.readonly later — operator re-runs `google_oauth` to re-consent
# with the expanded scope list.
SCOPES = ("https://www.googleapis.com/auth/gmail.send",)

_SAFE_OPERATOR_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def token_path_for(operator: str, *, token_dir: Path | None = None) -> Path:
    """Resolve the per-operator token file path.

    Filename is `google-tokens-<lowercased-operator>.json`. The operator
    string is validated against `_SAFE_OPERATOR_RE` so an injection
    attempt like `../etc/passwd` raises rather than escaping the data
    directory.
    """
    op = (operator or "").strip()
    if not op or not _SAFE_OPERATOR_RE.match(op):
        raise EmailFollowupError(
            f"google_apis: operator {operator!r} is not a safe filename component "
            f"(allowed: alphanumerics, underscore, hyphen)"
        )
    base = Path(token_dir) if token_dir is not None else ROOT_DIR / GOOGLE_TOKEN_DIR
    return base / f"google-tokens-{op.lower()}.json"


class GoogleApis:
    """OAuth-aware Google API client for one operator.

    Construction reads the operator's token from disk and refreshes it
    if expired. Methods reuse the cached service objects. Token refresh
    writes the new access token back to disk so the next process gets
    the fresh one.
    """

    def __init__(self, operator: str, *, token_dir: Path | None = None) -> None:
        self.operator = operator
        self._token_path = token_path_for(operator, token_dir=token_dir)
        if not self._token_path.exists():
            raise EmailFollowupError(
                f"No OAuth token for operator {operator!r} at {self._token_path}. "
                f"Run `python manage.py google_oauth --operator {operator}` first."
            )
        self._creds = Credentials.from_authorized_user_file(
            str(self._token_path), list(SCOPES),
        )
        if self._creds.expired and self._creds.refresh_token:
            try:
                self._creds.refresh(Request())
            except Exception as e:
                raise EmailFollowupError(
                    f"OAuth refresh failed for operator {operator!r}: {e}. "
                    f"Re-run `python manage.py google_oauth --operator {operator}` "
                    f"to re-consent."
                ) from e
            self._token_path.write_text(self._creds.to_json())
        self._gmail_service = None

    def _gmail(self):
        """Lazily build (and cache) the Gmail service object."""
        if self._gmail_service is None:
            self._gmail_service = build(
                "gmail", "v1", credentials=self._creds, cache_discovery=False,
            )
        return self._gmail_service

    def gmail_send_message(
        self,
        *,
        from_address: str,
        to: str,
        subject: str,
        body: str,
    ) -> str:
        """Send a plain-text email via Gmail API. Returns the Gmail message ID.

        Builds an RFC 2822 message, base64url-encodes the bytes, POSTs to
        `users.messages.send`. Raises `EmailFollowupError` on a Gmail
        rejection — caller treats this as an expected failure (skip this
        lead, log, continue). Network/JSON errors propagate.
        """
        msg = EmailMessage()
        msg["From"] = from_address
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        try:
            response = self._gmail().users().messages().send(
                userId="me", body={"raw": raw},
            ).execute()
        except HttpError as e:
            raise EmailFollowupError(
                f"Gmail API rejected send for {to!r}: status={e.resp.status} "
                f"reason={e.resp.reason}"
            ) from e

        message_id = response.get("id")
        if not message_id:
            raise EmailFollowupError(
                f"Gmail API returned no message ID for send to {to!r}: {response!r}"
            )
        return message_id
```

- [ ] **Step 3.4: Run the tests**

```bash
.venv/bin/pytest tests/test_google_apis.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 3.5: Commit**

```bash
git add linkedin/notifications/google_apis.py tests/test_google_apis.py
git commit -m "add GoogleApis OAuth client with gmail_send_message"
```

---

## Task 4: One-time OAuth bootstrap command

**Files:**
- Create: `linkedin/management/commands/google_oauth.py`

This is the only interactive command in the feature. The operator runs it once per Gmail account; subsequent runs of the email runner use the cached token.

- [ ] **Step 4.1: Write the command**

Create `linkedin/management/commands/google_oauth.py`:

```python
"""One-time OAuth bootstrap for Google APIs (Phase 1: Gmail send).

Per operator:
    .venv/bin/python manage.py google_oauth --operator Arian

Opens a local-server OAuth consent flow in the operator's default
browser, captures the refresh token, writes it to
`data/google-tokens-<operator>.json`. The runner reads that file on
each invocation and refreshes the access token as needed.

Prerequisites (operator does this once in Google Cloud Console):
  1. Create a Google Cloud project.
  2. Enable the Gmail API.
  3. Create an OAuth 2.0 Client ID — type: Desktop app.
  4. Download the client JSON, save at `data/google-oauth-client.json`
     (path overridable via `GOOGLE_OAUTH_CLIENT_PATH`).
  5. Add the operator's Gmail address as a Test User on the OAuth
     consent screen (so the app stays in "Testing" without verification).

Re-run this command any time SCOPES change (e.g. when the broader
`plan-direct-google-apis.md` plan adds read scopes).
"""
from __future__ import annotations

import logging
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from google_auth_oauthlib.flow import InstalledAppFlow

from linkedin.conf import GOOGLE_OAUTH_CLIENT_PATH, ROOT_DIR
from linkedin.notifications.google_apis import SCOPES, token_path_for

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the OAuth consent flow for a Gmail operator and cache the refresh token."

    def add_arguments(self, parser):
        parser.add_argument(
            "--operator", type=str, required=True,
            help="Canonical operator handle (e.g. Arian, Chuka). Matches the "
                 "filename suffix used by GoogleApis to look up the token.",
        )
        parser.add_argument(
            "--port", type=int, default=0,
            help="Local port for the OAuth redirect server (0 = pick free).",
        )

    def handle(self, *args, **opts):
        operator = opts["operator"]
        port = opts["port"]

        client_path = Path(GOOGLE_OAUTH_CLIENT_PATH)
        if not client_path.is_absolute():
            client_path = ROOT_DIR / client_path
        if not client_path.exists():
            raise CommandError(
                f"Google OAuth client JSON not found at {client_path}. "
                f"Download it from Google Cloud Console (OAuth 2.0 Client ID, "
                f"type=Desktop) and save it there, or set GOOGLE_OAUTH_CLIENT_PATH."
            )

        out_path = token_path_for(operator)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_path), list(SCOPES),
        )
        self.stdout.write(
            f"Opening browser for {operator}'s Google consent flow "
            f"(scopes: {', '.join(SCOPES)})..."
        )
        creds = flow.run_local_server(port=port, open_browser=True)
        out_path.write_text(creds.to_json())
        self.stdout.write(self.style.SUCCESS(
            f"Wrote refresh token to {out_path}. "
            f"This file is gitignored — keep it private."
        ))
```

- [ ] **Step 4.2: Smoke-test the command's error path**

We can't run the full interactive OAuth in a test, but verify the missing-client-file error path:

```bash
GOOGLE_OAUTH_CLIENT_PATH=/tmp/does-not-exist.json .venv/bin/python manage.py google_oauth --operator Arian 2>&1 | head -5
```

Expected: `CommandError: Google OAuth client JSON not found at /tmp/does-not-exist.json...`. (The Django CLI surfaces this as a non-zero exit.)

- [ ] **Step 4.3: Commit**

```bash
git add linkedin/management/commands/google_oauth.py
git commit -m "add google_oauth bootstrap command"
```

---

## Task 5: Eligibility query

**Files:**
- Create: `linkedin/email_followup/__init__.py`
- Create: `linkedin/email_followup/eligibility.py`
- Create: `tests/test_email_eligibility.py`

The query identifies `Deal`s where:
1. The operator's LinkedIn `daemon-send:` outbound on this lead is older than `delay_days`.
2. There are zero inbound Messages on the lead (any source).
3. There are zero outbound Gmail Messages on the lead (dedupe — we have not already emailed them).
4. `Lead.email` is non-empty.
5. `Lead.disqualified` is false.
6. The lead's LinkedIn outbound originator (per `lead_outbound_operators`) is this operator or empty (consistent with `enqueue_no_reply_followups` and `handle_follow_up`).

- [ ] **Step 5.1: Write the failing test**

Create `linkedin/email_followup/__init__.py` (empty file):

```bash
touch linkedin/email_followup/__init__.py
```

Create `tests/test_email_eligibility.py`:

```python
"""Tests for the email-followup eligibility query."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from crm.models import Deal, Lead, Message
from linkedin.email_followup.eligibility import eligible_deals
from linkedin.enums import ProfileState
from linkedin.models import Campaign


def _make_lead(*, email="lead@example.com", first_name="Pat", linkedin_url=None,
               disqualified=False) -> Lead:
    return Lead.objects.create(
        first_name=first_name,
        last_name="Doe",
        company_name="Acme",
        linkedin_url=linkedin_url or f"https://www.linkedin.com/in/{first_name.lower()}/",
        email=email,
        disqualified=disqualified,
    )


def _make_deal(lead, campaign, state=ProfileState.CONNECTED.value) -> Deal:
    return Deal.objects.create(lead=lead, campaign=campaign, state=state)


def _make_linkedin_outbound(lead, sender, days_ago):
    return Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id=f"daemon-send:{lead.id}:{days_ago}",
        direction=Message.Direction.OUTBOUND,
        sender=sender,
        body="rigid follow-up DM",
        sent_at=timezone.now() - timedelta(days=days_ago),
    )


@pytest.fixture
def campaign(fake_session):
    return fake_session.campaign


class TestEligibleDeals:
    def test_includes_lead_with_old_linkedin_followup_and_no_reply(self, db, campaign):
        lead = _make_lead()
        _make_deal(lead, campaign)
        _make_linkedin_outbound(lead, sender="Arian", days_ago=5)
        eligible = list(eligible_deals(operator="Arian", delay_days=3))
        assert lead.id in [d.lead_id for d in eligible]

    def test_excludes_lead_inside_delay_window(self, db, campaign):
        lead = _make_lead()
        _make_deal(lead, campaign)
        _make_linkedin_outbound(lead, sender="Arian", days_ago=1)
        assert list(eligible_deals(operator="Arian", delay_days=3)) == []

    def test_excludes_lead_with_inbound_reply(self, db, campaign):
        lead = _make_lead()
        _make_deal(lead, campaign)
        _make_linkedin_outbound(lead, sender="Arian", days_ago=5)
        Message.objects.create(
            lead=lead, source=Message.Source.LINKEDIN,
            external_id=f"reply:{lead.id}",
            direction=Message.Direction.INBOUND,
            sender="Pat Doe", body="thanks!",
            sent_at=timezone.now() - timedelta(days=2),
        )
        assert list(eligible_deals(operator="Arian", delay_days=3)) == []

    def test_excludes_lead_with_gmail_inbound(self, db, campaign):
        lead = _make_lead()
        _make_deal(lead, campaign)
        _make_linkedin_outbound(lead, sender="Arian", days_ago=5)
        Message.objects.create(
            lead=lead, source=Message.Source.GMAIL,
            external_id=f"gmail:reply:{lead.id}",
            direction=Message.Direction.INBOUND,
            sender="pat@example.com", body="thanks!",
            sent_at=timezone.now() - timedelta(days=2),
        )
        assert list(eligible_deals(operator="Arian", delay_days=3)) == []

    def test_excludes_lead_already_emailed(self, db, campaign):
        lead = _make_lead()
        _make_deal(lead, campaign)
        _make_linkedin_outbound(lead, sender="Arian", days_ago=5)
        Message.objects.create(
            lead=lead, source=Message.Source.GMAIL,
            external_id=f"gmail:sent:{lead.id}",
            direction=Message.Direction.OUTBOUND,
            sender="arian@example.com", body="hey",
            sent_at=timezone.now() - timedelta(days=1),
        )
        assert list(eligible_deals(operator="Arian", delay_days=3)) == []

    def test_excludes_lead_without_email(self, db, campaign):
        lead = _make_lead(email="")
        _make_deal(lead, campaign)
        _make_linkedin_outbound(lead, sender="Arian", days_ago=5)
        assert list(eligible_deals(operator="Arian", delay_days=3)) == []

    def test_excludes_disqualified_lead(self, db, campaign):
        lead = _make_lead(disqualified=True)
        _make_deal(lead, campaign)
        _make_linkedin_outbound(lead, sender="Arian", days_ago=5)
        assert list(eligible_deals(operator="Arian", delay_days=3)) == []

    def test_excludes_lead_owned_by_other_operator(self, db, campaign):
        lead = _make_lead()
        _make_deal(lead, campaign)
        _make_linkedin_outbound(lead, sender="Chuka", days_ago=5)
        assert list(eligible_deals(operator="Arian", delay_days=3)) == []

    def test_includes_lead_with_no_outbound_when_called_by_any_operator(self, db, campaign):
        """Freshly-CONNECTED lead with no LinkedIn outbound yet → not eligible
        because we key on the LinkedIn followup having been sent."""
        lead = _make_lead()
        _make_deal(lead, campaign)
        # No outbound LinkedIn Message at all.
        assert list(eligible_deals(operator="Arian", delay_days=3)) == []
```

- [ ] **Step 5.2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_email_eligibility.py -v
```

Expected: ImportError on `linkedin.email_followup.eligibility`.

- [ ] **Step 5.3: Implement the query**

Create `linkedin/email_followup/eligibility.py`:

```python
"""Eligibility query for the email follow-up sender.

A `Deal` is eligible iff:
  1. Most recent LinkedIn outbound message tagged `daemon-send:` on this
     lead was sent more than `delay_days` ago. (This is the proxy for
     "the daemon's rigid no-reply follow-up DM was sent and we have
     waited the cooling-off window".)
  2. Zero inbound Messages on the lead from any source.
  3. Zero outbound Gmail Messages on the lead. (We have not already
     emailed them; the email send itself becomes the dedupe record.)
  4. Lead has a non-empty `email`.
  5. Lead is not disqualified.
  6. The operator the runner is acting as either owns the lead's
     LinkedIn DM thread (per `lead_outbound_operators`) or the lead has
     no outbound at all (the latter never matches today since rule 1
     also requires a daemon-send outbound, but the check matches the
     pattern used by `handle_follow_up` / `enqueue_no_reply_followups`
     so the contract is consistent across surfaces).

This is intentionally a sidecar query: it does NOT inspect `Deal.state`
or `Deal.closing_reason`. The state machine has already moved leads
through CONNECTED → COMPLETED; we key on Message rows because they are
the durable evidence of "the daemon sent the followup DM", independent
of state transitions.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Iterable

from django.db.models import Exists, OuterRef, QuerySet, Subquery
from django.utils import timezone

from crm.models import Deal, Message
from linkedin.db.messages import lead_outbound_operators


def eligible_deals(*, operator: str, delay_days: int) -> list[Deal]:
    """Return Deals whose lead is eligible for the email follow-up nudge.

    Scope: leads whose LinkedIn outbound originated from `operator`
    (consistent with `handle_follow_up`'s owner-scoping guard) or who
    have no outbound owner yet (no-op in practice for this query — see
    module docstring).

    `delay_days` is the minimum age of the most recent `daemon-send:`
    outbound LinkedIn Message before the lead becomes eligible.

    Returns a list rather than a QuerySet because the operator-ownership
    filter is applied in Python (it would otherwise require a
    Postgres-only array subquery just to express "this operator's name
    is among the distinct senders").
    """
    cutoff = timezone.now() - timedelta(days=delay_days)

    latest_daemon_send = (
        Message.objects
        .filter(
            lead_id=OuterRef("lead_id"),
            source=Message.Source.LINKEDIN,
            direction=Message.Direction.OUTBOUND,
            external_id__startswith="daemon-send:",
        )
        .order_by("-sent_at")
        .values("sent_at")[:1]
    )
    has_inbound = Exists(Message.objects.filter(
        lead_id=OuterRef("lead_id"),
        direction=Message.Direction.INBOUND,
    ))
    has_outbound_gmail = Exists(Message.objects.filter(
        lead_id=OuterRef("lead_id"),
        source=Message.Source.GMAIL,
        direction=Message.Direction.OUTBOUND,
    ))

    qs = (
        Deal.objects
        .filter(lead__disqualified=False)
        .exclude(lead__email="")
        .annotate(
            _last_daemon_send=Subquery(latest_daemon_send),
            _has_inbound=has_inbound,
            _has_outbound_gmail=has_outbound_gmail,
        )
        .filter(
            _last_daemon_send__isnull=False,
            _last_daemon_send__lte=cutoff,
            _has_inbound=False,
            _has_outbound_gmail=False,
        )
        .select_related("lead", "campaign")
    )

    # Owner scoping: pull into Python so we can use the existing helper
    # (which resolves sender display names through `resolve_operator`,
    # absorbing alias variation that an SQL exact-match would miss).
    out: list[Deal] = []
    for deal in qs:
        owners = lead_outbound_operators(deal.lead)
        if not owners or operator in owners:
            out.append(deal)
    return out
```

- [ ] **Step 5.4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_email_eligibility.py -v
```

Expected: all 9 tests pass.

- [ ] **Step 5.5: Commit**

```bash
git add linkedin/email_followup/__init__.py linkedin/email_followup/eligibility.py tests/test_email_eligibility.py
git commit -m "add email-followup eligibility query"
```

---

## Task 6: Sender orchestrator

**Files:**
- Create: `linkedin/email_followup/sender.py`
- Create: `tests/test_email_sender.py`

The orchestrator binds eligibility → template → send → persistence + notification together. Receives the operator and a `GoogleApis` instance; returns the count of sent messages. Each per-lead failure is logged + swallowed (other leads continue) — the only fatal errors are programmer bugs (template missing, etc.) that propagate.

- [ ] **Step 6.1: Write the failing tests**

Create `tests/test_email_sender.py`:

```python
"""Tests for the email-followup sender orchestrator."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from crm.models import Deal, Lead, Message
from linkedin.email_followup.sender import send_email_followups
from linkedin.enums import ProfileState


def _make_lead_with_followup(campaign, sender="Arian", days_ago=5,
                             email="pat@example.com"):
    lead = Lead.objects.create(
        first_name="Pat", last_name="Doe", company_name="Acme",
        linkedin_url=f"https://www.linkedin.com/in/pat-{days_ago}/",
        email=email,
    )
    Deal.objects.create(lead=lead, campaign=campaign,
                        state=ProfileState.CONNECTED.value)
    Message.objects.create(
        lead=lead, source=Message.Source.LINKEDIN,
        external_id=f"daemon-send:{lead.id}:test",
        direction=Message.Direction.OUTBOUND,
        sender=sender, body="rigid DM",
        sent_at=timezone.now() - timedelta(days=days_ago),
    )
    return lead


@pytest.fixture
def fake_gmail():
    """A GoogleApis-shaped mock that records each send."""
    api = MagicMock()
    api.operator = "Arian"
    api.gmail_send_message.return_value = "gmail-msg-id-123"
    return api


@pytest.fixture
def campaign(fake_session):
    return fake_session.campaign


class TestSendEmailFollowups:
    def test_dry_run_sends_nothing_and_persists_nothing(self, db, campaign, fake_gmail):
        lead = _make_lead_with_followup(campaign)
        sent = send_email_followups(
            operator="Arian", from_address="arian@example.com",
            google_apis=fake_gmail, delay_days=3, limit=10, dry_run=True,
        )
        assert sent == 1  # reports what WOULD be sent
        fake_gmail.gmail_send_message.assert_not_called()
        assert not Message.objects.filter(
            lead=lead, source=Message.Source.GMAIL,
        ).exists()

    def test_send_persists_outbound_gmail_message(self, db, campaign, fake_gmail):
        lead = _make_lead_with_followup(campaign)
        sent = send_email_followups(
            operator="Arian", from_address="arian@example.com",
            google_apis=fake_gmail, delay_days=3, limit=10, dry_run=False,
        )
        assert sent == 1
        msg = Message.objects.get(
            lead=lead, source=Message.Source.GMAIL,
            direction=Message.Direction.OUTBOUND,
        )
        assert msg.external_id == "gmail:gmail-msg-id-123"
        assert msg.sender == "arian@example.com"
        assert "Following up" in msg.body or "Hi Pat" in msg.body

    def test_second_run_is_idempotent(self, db, campaign, fake_gmail):
        _make_lead_with_followup(campaign)
        send_email_followups(
            operator="Arian", from_address="arian@example.com",
            google_apis=fake_gmail, delay_days=3, limit=10, dry_run=False,
        )
        sent = send_email_followups(
            operator="Arian", from_address="arian@example.com",
            google_apis=fake_gmail, delay_days=3, limit=10, dry_run=False,
        )
        assert sent == 0
        fake_gmail.gmail_send_message.assert_called_once()

    def test_limit_caps_send_count(self, db, campaign, fake_gmail):
        for i in range(3):
            lead = Lead.objects.create(
                first_name=f"Pat{i}", linkedin_url=f"https://www.linkedin.com/in/pat-{i}/",
                email=f"pat{i}@example.com",
            )
            Deal.objects.create(lead=lead, campaign=campaign,
                                state=ProfileState.CONNECTED.value)
            Message.objects.create(
                lead=lead, source=Message.Source.LINKEDIN,
                external_id=f"daemon-send:{lead.id}:t",
                direction=Message.Direction.OUTBOUND,
                sender="Arian", body="x",
                sent_at=timezone.now() - timedelta(days=5),
            )
        sent = send_email_followups(
            operator="Arian", from_address="arian@example.com",
            google_apis=fake_gmail, delay_days=3, limit=2, dry_run=False,
        )
        assert sent == 2
        assert fake_gmail.gmail_send_message.call_count == 2

    def test_per_lead_send_failure_does_not_block_others(self, db, campaign, fake_gmail):
        from linkedin.exceptions import EmailFollowupError

        for i in range(3):
            lead = Lead.objects.create(
                first_name=f"Pat{i}", linkedin_url=f"https://www.linkedin.com/in/pat-{i}/",
                email=f"pat{i}@example.com",
            )
            Deal.objects.create(lead=lead, campaign=campaign,
                                state=ProfileState.CONNECTED.value)
            Message.objects.create(
                lead=lead, source=Message.Source.LINKEDIN,
                external_id=f"daemon-send:{lead.id}:t",
                direction=Message.Direction.OUTBOUND,
                sender="Arian", body="x",
                sent_at=timezone.now() - timedelta(days=5),
            )

        fake_gmail.gmail_send_message.side_effect = [
            "msg-1",
            EmailFollowupError("Gmail bounced"),
            "msg-3",
        ]
        sent = send_email_followups(
            operator="Arian", from_address="arian@example.com",
            google_apis=fake_gmail, delay_days=3, limit=10, dry_run=False,
        )
        assert sent == 2
        assert Message.objects.filter(
            source=Message.Source.GMAIL, direction=Message.Direction.OUTBOUND,
        ).count() == 2
```

- [ ] **Step 6.2: Run the tests to verify they fail**

```bash
.venv/bin/pytest tests/test_email_sender.py -v
```

Expected: ImportError on `linkedin.email_followup.sender`.

- [ ] **Step 6.3: Implement the orchestrator**

Create `linkedin/email_followup/sender.py`:

```python
"""Orchestrator for the email follow-up sidecar.

Pulls eligible Deals (`eligibility.eligible_deals`), renders the
`email_connect_followup` template per Lead, sends via the supplied
`GoogleApis`, persists each send as a `crm.Message(source=GMAIL,
direction=OUTBOUND)` row, and posts a lean Slack notification per send.

The persisted Message row is itself the dedupe record — the next run's
eligibility query will skip this lead because `has_outbound_gmail` is
now True.

Per-lead failures are logged and swallowed so one Gmail rejection does
not block the rest of the batch. The function returns the count of
messages successfully sent (in dry-run mode: the count that WOULD have
been sent).
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from crm.models import Message
from linkedin.exceptions import EmailFollowupError
from linkedin.icp_outbound import classify_role, fill_for_lead, resolve_icp
from linkedin.notifications.slack import notify_email_followup_sent

from linkedin.email_followup.eligibility import eligible_deals

logger = logging.getLogger(__name__)


def send_email_followups(
    *,
    operator: str,
    from_address: str,
    google_apis,
    delay_days: int,
    limit: int,
    dry_run: bool,
) -> int:
    """Send one email follow-up per eligible Lead. Returns count sent.

    `operator` — canonical handle (e.g. "Arian") for owner scoping +
    template selection.
    `from_address` — the operator's Gmail address (used as the `From`
    header; Gmail signs the message as the authenticated identity but
    the From header must match).
    `google_apis` — an instance of `linkedin.notifications.google_apis.
    GoogleApis` (or a duck-typed test double).
    `delay_days` — passed to the eligibility query.
    `limit` — cap on number of sends (0 = no cap).
    `dry_run` — render templates and report what would be sent, but
    skip the actual send + persistence + Slack notification.
    """
    deals = eligible_deals(operator=operator, delay_days=delay_days)
    if limit and len(deals) > limit:
        deals = deals[:limit]

    sent_count = 0
    for deal in deals:
        lead = deal.lead
        try:
            role = classify_role(lead)
            # `resolve_icp` is the same routing both the connect-note picker
            # and the LinkedIn follow-up use — keeps the email send aligned
            # with whatever ICP the lead was bucketed under elsewhere.
            resolve_icp(lead)

            subject_msg = fill_for_lead(
                sender=operator, role=role,
                channel="email_connect_followup_subject",
                lead=lead, my_name=operator,
            )
            body_msg = fill_for_lead(
                sender=operator, role=role,
                channel="email_connect_followup",
                lead=lead, my_name=operator,
            )
        except Exception:
            logger.exception(
                "email_followup: template fill failed for lead=%s — skipping",
                lead.id,
            )
            continue

        if dry_run:
            logger.info(
                "email_followup [DRY-RUN] would send to lead=%s <%s> subject=%r",
                lead.id, lead.email, subject_msg.body,
            )
            sent_count += 1
            continue

        try:
            gmail_message_id = google_apis.gmail_send_message(
                from_address=from_address,
                to=lead.email,
                subject=subject_msg.body,
                body=body_msg.body,
            )
        except EmailFollowupError:
            logger.exception(
                "email_followup: Gmail send failed for lead=%s — skipping",
                lead.id,
            )
            continue

        with transaction.atomic():
            Message.objects.create(
                lead=lead,
                source=Message.Source.GMAIL,
                external_id=f"gmail:{gmail_message_id}",
                direction=Message.Direction.OUTBOUND,
                sender=from_address,
                body=body_msg.body,
                sent_at=timezone.now(),
                thread_external_id="",
            )

        try:
            notify_email_followup_sent(lead=lead, operator=operator,
                                       subject=subject_msg.body)
        except Exception:
            logger.exception(
                "email_followup: Slack notify failed for lead=%s — send already "
                "persisted, continuing",
                lead.id,
            )

        sent_count += 1
        logger.info(
            "email_followup sent to lead=%s <%s> role=%s gmail_id=%s",
            lead.id, lead.email, role, gmail_message_id,
        )

    return sent_count
```

- [ ] **Step 6.4: Run the tests — they will FAIL because `notify_email_followup_sent` does not exist yet**

```bash
.venv/bin/pytest tests/test_email_sender.py -v
```

Expected: ImportError on `notify_email_followup_sent`. Proceed to Task 7 to add it.

---

## Task 7: Slack notification

**Files:**
- Modify: `linkedin/notifications/slack.py` (append new function near `notify_phone_enriched`)
- Modify: `tests/test_slack_notify.py` (add a class for the new function)

- [ ] **Step 7.1: Write the failing test**

Append to `tests/test_slack_notify.py`:

```python


class TestNotifyEmailFollowupSent:
    def test_no_op_when_webhook_unset(self, monkeypatch):
        from linkedin.notifications import slack
        monkeypatch.setattr(slack, "SLACK_WEBHOOK_URL", "")
        called = []
        monkeypatch.setattr(slack, "_post_to_slack",
                            lambda *a, **k: called.append((a, k)))

        class FakeLead:
            id = 1
            first_name = "Pat"
            last_name = "Doe"
            company_name = "Acme"
            public_identifier = "pat-doe"
            linkedin_url = "https://www.linkedin.com/in/pat-doe/"

        slack.notify_email_followup_sent(
            lead=FakeLead(), operator="Arian", subject="hi",
        )
        assert called == []

    def test_posts_when_webhook_set(self, monkeypatch):
        from linkedin.notifications import slack
        monkeypatch.setattr(slack, "SLACK_WEBHOOK_URL", "https://hooks.example/x")
        seen = {}

        def _capture(webhook_url, payload, label):
            seen["webhook"] = webhook_url
            seen["payload"] = payload
            seen["label"] = label

        monkeypatch.setattr(slack, "_post_to_slack", _capture)

        class FakeLead:
            id = 1
            first_name = "Pat"
            last_name = "Doe"
            company_name = "Acme"
            public_identifier = "pat-doe"
            linkedin_url = "https://www.linkedin.com/in/pat-doe/"

        slack.notify_email_followup_sent(
            lead=FakeLead(), operator="Arian",
            subject="Following up — FedRampGPT",
        )
        assert seen["webhook"] == "https://hooks.example/x"
        assert "Pat Doe" in seen["payload"]["text"]
        assert "Arian" in seen["payload"]["text"]
```

- [ ] **Step 7.2: Run the test to verify failure**

```bash
.venv/bin/pytest tests/test_slack_notify.py::TestNotifyEmailFollowupSent -v
```

Expected: AttributeError on `notify_email_followup_sent`.

- [ ] **Step 7.3: Implement the notification**

Append to `linkedin/notifications/slack.py` (after `notify_phone_enriched`, before `notify_degraded`):

```python
def notify_email_followup_sent(*, lead, operator: str, subject: str) -> None:
    """Post a 'sent email follow-up' notification. No-op if ops webhook unset.

    Fires from the email-followup sidecar runner (`linkedin/email_followup/
    sender.py`) after a successful Gmail send + Message persist. Posts to
    SLACK_WEBHOOK_URL (the ops channel) since this is outbound activity,
    not an inbound reply.
    """
    if not SLACK_WEBHOOK_URL:
        return

    full_name = (
        f"{lead.first_name or ''} {lead.last_name or ''}".strip()
        or getattr(lead, "public_identifier", "")
        or "Unknown lead"
    )
    profile_url = getattr(lead, "linkedin_url", "") or ""
    name_md = f"<{profile_url}|{full_name}>" if profile_url else full_name
    op_clean = (operator or "").strip()
    op_suffix = f" — {op_clean}'s lead" if op_clean else ""

    action_line = f":envelope_with_arrow: Sent email follow-up to *{name_md}*{op_suffix}"
    fallback = f":envelope_with_arrow: {full_name} sent email follow-up{op_suffix}"

    elements: list[dict] = []
    if op_clean:
        elements.append({"type": "mrkdwn", "text": f"*Lead for:* {op_clean}"})
    if getattr(lead, "company_name", ""):
        elements.append({"type": "mrkdwn", "text": f"*Company:* {lead.company_name}"})
    elements.append({"type": "mrkdwn", "text": f"*Subject:* {subject}"})

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": action_line}},
        {"type": "context", "elements": elements},
    ]
    payload = {"text": fallback, "blocks": blocks}
    _post_to_slack(SLACK_WEBHOOK_URL, payload, f"email-followup-sent ({full_name})")
```

- [ ] **Step 7.4: Run all relevant tests**

```bash
.venv/bin/pytest tests/test_slack_notify.py::TestNotifyEmailFollowupSent tests/test_email_sender.py -v
```

Expected: all 7 tests pass (2 from Slack, 5 from sender).

- [ ] **Step 7.5: Commit**

```bash
git add linkedin/email_followup/sender.py tests/test_email_sender.py linkedin/notifications/slack.py tests/test_slack_notify.py
git commit -m "add email-followup sender orchestrator and Slack notification"
```

---

## Task 8: `send_email_followups` management command

**Files:**
- Create: `linkedin/management/commands/send_email_followups.py`

The command is the operator-facing surface. One-shot by default; `--loop` makes it a long-running sidecar. Respects active hours by sleeping when outside them (reuses `linkedin.daemon.seconds_until_active`).

- [ ] **Step 8.1: Write the command**

Create `linkedin/management/commands/send_email_followups.py`:

```python
"""Sidecar runner for the email follow-up sender.

One-shot:
    .venv/bin/python manage.py send_email_followups --operator Arian \\
        --from-address arian@example.com

Long-running sidecar (run in a separate process alongside the daemon):
    .venv/bin/python manage.py send_email_followups --operator Arian \\
        --from-address arian@example.com --loop --interval-seconds 300

Dry-run (no Gmail send, no DB writes, no Slack — just log what would happen):
    .venv/bin/python manage.py send_email_followups --operator Arian \\
        --from-address arian@example.com --dry-run

Active-hours: the command respects ACTIVE_START_HOUR / ACTIVE_END_HOUR /
REST_DAYS the same way the daemon does. In `--loop` mode it sleeps
through off-hours; in one-shot mode it just exits with a log line.

Kill-switch: `ENABLE_EMAIL_FOLLOWUP` (default false). When false, the
command logs and exits (or sleeps in loop mode) without sending anything.
"""
from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand, CommandError

from linkedin.conf import (
    EMAIL_FOLLOWUP_DELAY_DAYS,
    ENABLE_EMAIL_FOLLOWUP,
)
from linkedin.daemon import seconds_until_active
from linkedin.email_followup.sender import send_email_followups
from linkedin.notifications.google_apis import GoogleApis
from linkedin.notifications.slack import notify_on_error

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Send email follow-ups to leads who got the LinkedIn no-reply nudge but did not respond."

    def add_arguments(self, parser):
        parser.add_argument(
            "--operator", type=str, required=True,
            help="Canonical operator handle (e.g. Arian, Chuka). Matches the OAuth token filename.",
        )
        parser.add_argument(
            "--from-address", type=str, required=True,
            help="The operator's Gmail address used as the From header. Must match "
                 "the Gmail account that consented to the OAuth flow.",
        )
        parser.add_argument(
            "--delay-days", type=int, default=EMAIL_FOLLOWUP_DELAY_DAYS,
            help="Days to wait after the LinkedIn followup DM before sending email.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Cap sends per pass (0 = no cap).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Render templates and log eligible sends, but don't send.",
        )
        parser.add_argument(
            "--loop", action="store_true",
            help="Run forever, sleeping `--interval-seconds` between passes. "
                 "Use this when running as a long-lived sidecar process.",
        )
        parser.add_argument(
            "--interval-seconds", type=int, default=300,
            help="In --loop mode, seconds to sleep between passes. Default 5 min.",
        )

    def handle(self, *args, **opts):
        operator = opts["operator"]
        from_address = opts["from_address"]
        delay_days = opts["delay_days"]
        limit = opts["limit"]
        dry_run = opts["dry_run"]
        loop = opts["loop"]
        interval = opts["interval_seconds"]

        if not ENABLE_EMAIL_FOLLOWUP:
            logger.warning(
                "ENABLE_EMAIL_FOLLOWUP=false — email follow-up is disabled. "
                "Set it to true in .env to enable."
            )
            if not loop:
                return

        try:
            google_apis = GoogleApis(operator=operator)
        except Exception as e:
            raise CommandError(
                f"Could not initialize GoogleApis for operator {operator!r}: {e}"
            ) from e

        if loop:
            self._run_loop(operator, from_address, delay_days, limit, dry_run,
                           google_apis, interval)
        else:
            self._run_once(operator, from_address, delay_days, limit, dry_run,
                           google_apis)

    def _run_once(self, operator, from_address, delay_days, limit, dry_run,
                  google_apis):
        wait = seconds_until_active()
        if wait > 0:
            logger.info(
                "send_email_followups: outside active hours (next active in "
                "%ds). One-shot mode — exiting without sending.", int(wait),
            )
            return
        with notify_on_error("send_email_followups",
                             context={"operator": operator}):
            count = send_email_followups(
                operator=operator, from_address=from_address,
                google_apis=google_apis, delay_days=delay_days,
                limit=limit, dry_run=dry_run,
            )
        verb = "would send" if dry_run else "sent"
        self.stdout.write(self.style.SUCCESS(f"{verb} {count} email follow-up(s)."))

    def _run_loop(self, operator, from_address, delay_days, limit, dry_run,
                  google_apis, interval):
        self.stdout.write(
            f"send_email_followups: --loop mode, interval={interval}s, "
            f"operator={operator}, dry_run={dry_run}"
        )
        while True:
            if not ENABLE_EMAIL_FOLLOWUP:
                logger.info("ENABLE_EMAIL_FOLLOWUP=false — sleeping %ds", interval)
                time.sleep(interval)
                continue

            wait = seconds_until_active()
            if wait > 0:
                logger.info(
                    "send_email_followups: outside active hours, sleeping %ds "
                    "until next active window.", int(wait),
                )
                time.sleep(wait)
                continue

            try:
                with notify_on_error("send_email_followups",
                                     context={"operator": operator}):
                    count = send_email_followups(
                        operator=operator, from_address=from_address,
                        google_apis=google_apis, delay_days=delay_days,
                        limit=limit, dry_run=dry_run,
                    )
                verb = "would send" if dry_run else "sent"
                logger.info("send_email_followups pass: %s %d", verb, count)
            except Exception:
                # notify_on_error already re-raised — but we want the loop to
                # survive transient failures rather than crash the sidecar.
                # The re-raise above wrote to Slack; swallow here so the
                # next pass still runs after `interval` seconds.
                logger.exception("send_email_followups pass crashed — continuing")
            time.sleep(interval)
```

- [ ] **Step 8.2: Smoke-test the command's argument parsing**

```bash
.venv/bin/python manage.py send_email_followups --help
```

Expected: Django prints the usage with all 7 arguments listed.

- [ ] **Step 8.3: Smoke-test the disabled-flag path**

```bash
ENABLE_EMAIL_FOLLOWUP=false .venv/bin/python manage.py send_email_followups \
    --operator Arian --from-address arian@example.com 2>&1 | grep -i "ENABLE_EMAIL_FOLLOWUP=false"
```

Expected: a warning line about the flag being false. No CommandError.

- [ ] **Step 8.4: Run the full test suite to confirm nothing else broke**

```bash
.venv/bin/pytest -x 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 8.5: Commit**

```bash
git add linkedin/management/commands/send_email_followups.py
git commit -m "add send_email_followups management command"
```

---

## Task 9: Documentation — CLAUDE.md + ARCHITECTURE.md

**Files:**
- Modify: `CLAUDE.md`
- Modify: `ARCHITECTURE.md`

- [ ] **Step 9.1: Update CLAUDE.md — add Commands section**

In `CLAUDE.md`, under the existing `## Commands` section (after the `backfill_messages` / `import_connections` block), append:

```markdown

# Email follow-up sidecar — sends a follow-up email 3 days after the
# daemon's LinkedIn no-reply DM, if the lead didn't respond on any
# channel. Runs as a separate process alongside the daemon (same repo,
# same DB, separate process). Configure in .env:
#   ENABLE_EMAIL_FOLLOWUP=true
#   EMAIL_FOLLOWUP_DELAY_DAYS=3
#   GOOGLE_OAUTH_CLIENT_PATH=data/google-oauth-client.json
#   GOOGLE_TOKEN_DIR=data
# One-time per operator: complete OAuth (opens browser):
.venv/bin/python manage.py google_oauth --operator Arian
# Run the sidecar (one-shot):
.venv/bin/python manage.py send_email_followups --operator Arian \
    --from-address arian@example.com
# Run the sidecar as a long-lived process (recommended):
.venv/bin/python manage.py send_email_followups --operator Arian \
    --from-address arian@example.com --loop --interval-seconds 300
# Dry-run:
.venv/bin/python manage.py send_email_followups --operator Arian \
    --from-address arian@example.com --dry-run
```

- [ ] **Step 9.2: Update CLAUDE.md — add Architecture bullet**

In `CLAUDE.md`, in the `## Architecture (quick reference)` section, after the `**Phone enrichment**` bullet, insert:

```markdown
- **Email follow-up sidecar**: `linkedin/email_followup/` + `linkedin/notifications/google_apis.py` — sends a single follow-up email when the daemon's no-reply LinkedIn DM got no response after `EMAIL_FOLLOWUP_DELAY_DAYS` (default 3). Runs as a separate process: `manage.py send_email_followups --operator <name> --from-address <addr> --loop`. Pure sidecar — does not touch `Deal.state` or the daemon. Eligibility query (`eligibility.eligible_deals`) reads `crm.Message` rows to identify leads where the most recent `daemon-send:` LinkedIn outbound is older than the delay window, there is zero inbound on any channel, and there is no outbound Gmail yet. Templates extend `linkedin/icp_messages.json` with `email_connect_followup_subject` + `email_connect_followup` channels per ICP. Gmail send uses `linkedin.notifications.google_apis.GoogleApis` (`gmail_send_message`) with per-operator OAuth tokens cached at `data/google-tokens-<operator>.json` (one-time bootstrap: `manage.py google_oauth --operator <name>`). Each successful send is persisted as `crm.Message(source=GMAIL, direction=OUTBOUND, external_id="gmail:<id>")` — the row is the dedupe record. Slack notify on send via `notify_email_followup_sent` (ops channel). Kill-switch: `ENABLE_EMAIL_FOLLOWUP` (default false).
```

- [ ] **Step 9.3: Update ARCHITECTURE.md — add a new section**

In `ARCHITECTURE.md`, after the existing phone-enrichment section, insert a new section:

```markdown
## Email Follow-up Sidecar

Lives in `linkedin/email_followup/` (eligibility + orchestrator) and
`linkedin/notifications/google_apis.py` (OAuth-aware Gmail client). Runs
as a separate process (`manage.py send_email_followups --loop`)
alongside the daemon — same repo, same DB, separate process. The design
goal is to add an email follow-up channel without touching the LinkedIn
outbound state machine.

### Trigger

A lead becomes eligible for the email nudge when ALL of:

- The most recent `daemon-send:` outbound LinkedIn `crm.Message` is
  older than `EMAIL_FOLLOWUP_DELAY_DAYS` (default 3).
- The lead has zero inbound `crm.Message` from any source.
- The lead has zero outbound Gmail `crm.Message`.
- `Lead.email` is non-empty.
- `Lead.disqualified` is False.
- The runner's operator owns the lead's LinkedIn outbound (per
  `lead_outbound_operators`), or the lead has no outbound owner yet.

The query never reads `Deal.state` or `Deal.closing_reason` — it keys
on Message rows so the state machine and its progression to COMPLETED
is irrelevant to email eligibility.

### Templates

Per-operator × per-ICP, stored in `linkedin/icp_messages.json` alongside
the LinkedIn templates. Two new channels:

- `email_connect_followup_subject` — subject line variants (single
  entry per ICP in Phase 1).
- `email_connect_followup` — body variants.

Variant selection is `lead_id mod len(variants)` so the same lead always
gets the same subject + body pairing across re-runs. Substitution
tokens (`{first_name}`, `{company_name}`, `{my_name}`, `{our_company_name}`,
`{our_website_url}`) are filled by `fill_for_lead` exactly as for
LinkedIn templates.

### OAuth

Per-operator tokens live at `data/google-tokens-<operator>.json` (gitignored).
The bootstrap command `manage.py google_oauth --operator <name>` opens
a local-server OAuth consent flow in the browser and writes the refresh
token. Phase 1 scope: `gmail.send` only. The broader plan in
`docs/plan-direct-google-apis.md` adds Calendar + Drive + Gmail-read
scopes on the same `GoogleApis` class — operators re-run `google_oauth`
to re-consent with the expanded scope list.

### Dedupe + idempotency

Each successful Gmail send is persisted as `crm.Message(source=GMAIL,
direction=OUTBOUND, external_id="gmail:<gmail_message_id>")`. The next
eligibility pass excludes this lead because `has_outbound_gmail` is now
True. The `Message.unique_together = ("source", "external_id")`
constraint means even a same-second retry cannot create a duplicate row.

### Active hours

The sidecar respects the same active-hours config as the daemon
(`ENABLE_ACTIVE_HOURS`, `ACTIVE_START_HOUR`, `ACTIVE_END_HOUR`,
`ACTIVE_TIMEZONE`, `REST_DAYS`). In `--loop` mode it sleeps through
off-hours; in one-shot mode it exits with a log line.

### Failure handling

Per-lead failures (template miss, Gmail API rejection) are logged and
swallowed so one bad lead doesn't block the rest of the batch.
`EmailFollowupError` is the marker for expected/recoverable failures.
Unexpected exceptions propagate up through `notify_on_error`
("send_email_followups" workflow tag) and post to Slack ops.
```

- [ ] **Step 9.4: Verify the full test suite passes one more time**

```bash
.venv/bin/pytest -x 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 9.5: Commit**

```bash
git add CLAUDE.md ARCHITECTURE.md
git commit -m "document email-followup sidecar in CLAUDE.md and ARCHITECTURE.md"
```

---

## Self-Review

After completing all tasks, run through this checklist:

- [ ] **Spec coverage:**
  - Outbound email follow-up for connected-but-no-reply leads → Tasks 5+6+8.
  - Uses `icp_messages.json` for templates with new `email_connect_followup` channel → Task 2.
  - Per-operator Gmail OAuth, tokens in `data/` → Tasks 3+4.
  - 3-day delay default → Task 1 (`EMAIL_FOLLOWUP_DELAY_DAYS=3`).
  - One email max per stage (dedupe) → Task 5 (`has_outbound_gmail=False`) + Task 6 (persists Message after send).
  - Skip if any inbound reply (LinkedIn or Gmail) → Task 5.
  - Separate process, runs alongside daemon → Task 8 `--loop`.
  - Zero changes to existing LinkedIn outbound state machine → confirmed (no edits to `linkedin/tasks/follow_up.py`, `Deal.state`, daemon loop).
  - Slack notify on send → Task 7.
  - Active-hours respect → Task 8.
  - Kill-switch `ENABLE_EMAIL_FOLLOWUP` → Tasks 1+8.

- [ ] **Verify no placeholders remain.** Search this plan file for `TBD`, `TODO`, `placeholder` — there should be zero hits.

- [ ] **Verify type consistency.**
  - `eligible_deals(operator, *, delay_days)` is called the same way in `sender.py` and tested with the same signature.
  - `gmail_send_message(*, from_address, to, subject, body)` — same keyword args in test, sender, and implementation.
  - `notify_email_followup_sent(*, lead, operator, subject)` — same signature in sender + test + impl.
  - `GoogleApis(operator, *, token_dir=None)` — same constructor signature in tests + impl.
  - `EmailFollowupError` — defined in `linkedin/exceptions.py`, imported by `google_apis.py`, `sender.py`, and tests.

- [ ] **The runner does not modify `Deal.state`.** Search the diff for `set_profile_state` and `Deal.state =` — there should be zero hits in any file this plan creates or modifies (except untouched references in existing files).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-20-email-followup-sender.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
