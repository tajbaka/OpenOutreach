# Conversation Storage + Attio Hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist LinkedIn conversation threads to a new `crm.Message` table, harden Attio writes against duplicates, ship a CSV-driven backfill command for an alternate LinkedIn account, and add an email-extraction + "Wants Meeting" intent pass to the existing hourly `sync_attio` cron.

**Architecture:** Four independently-shippable phases. **A** fixes a real existing bug in the Attio outreach-status rank table and switches Person/Company creation to Attio's "assert" (matching-attribute) pattern so manual Attio inserts aren't duplicated. **B** introduces `crm.Message` (FK to Lead, source enum, idempotent on `external_id`) and adds an upsert hook in `actions/conversations.py:get_conversation()` so every existing caller persists threads as a side effect. **C** is a `manage.py import_connections` command that takes a CSV (LinkedIn URL + first name), logs in as a separate account, scrapes the Connections page back 90 days, and writes Lead+Deal rows at `state=CONNECTED` (the daemon ignores them; `sync_attio` mirrors them to Attio on its next run). **D** extends the per-Deal loop in `sync_attio` to (D1) regex-extract emails from inbound `crm.Message` rows and append to Attio Person `email_addresses`, and (D2) run a cheap LLM call over the thread to detect "wants meeting" intent and patch Outreach status accordingly — both gated by Phase A's completed rank table to prevent reverts from `Meeting Booked`/`Had Meeting`/`Prospecting to close`/`Won`.

**Tech Stack:** Python 3.13, Django 5.x, Postgres (via `dj-database-url`), `langchain-openai` (existing LLM pattern), Jinja2 prompts in `linkedin/templates/prompts/`, pytest, requests (for Attio REST), Playwright (existing browser automation, used unchanged by Phase C).

**Decisions locked in by the brainstorm:**
- `Lead.email` local mirror: **yes** (Phase D adds it).
- Attio "why we flagged" note for `Wants Meeting`: **yes** (Phase D2 writes it).
- Sales-list company-Stage stays human-driven; D2 only patches per-Person `outreach_status`.
- Deal records in our DB own a Lead's Attio Person; if a CSV row's URL already has a Deal in any campaign, the import skips it.

**Project conventions to follow:**
- Always use `.venv/bin/python` (never system `python3`).
- Single-line commit messages, no `Co-Authored-By` line, no body.
- Dependencies live in `requirements/*.txt`. No new top-level deps required by this plan.
- Custom exceptions in `linkedin/exceptions.py`. Crash on unexpected errors; `try/except` only for expected, recoverable failures.
- No backward-compat shims; CRM models are owned by this project.

---

## File Structure

**Phase A** modifies `linkedin/notifications/attio.py` (new constants, expanded rank table, switch from POST to PUT-with-matching_attribute on `create_person`/`create_company`). Adds new test file `tests/test_attio.py`.

**Phase B** adds `crm/models/message.py` and migration `crm/migrations/0005_add_message.py`. Updates `crm/models/__init__.py` to export `Message`. Modifies `linkedin/actions/conversations.py:parse_messages` to capture per-message `entityUrn` and `linkedin/actions/conversations.py:get_conversation` to call a new `linkedin/db/messages.py:persist_thread` helper. Adds `tests/test_messages.py`.

**Phase C** adds `linkedin/management/commands/import_connections.py`. Adds `tests/test_import_connections.py`. No model changes; reuses everything from Phases A and B.

**Phase D** adds fields to existing models: `Lead.email` (`crm/models/lead.py`), `Deal.last_synthesized_at`, `Deal.wants_meeting_detected_at` (`crm/models/deal.py`). Migration `crm/migrations/0006_add_email_and_synthesis_fields.py`. Adds `linkedin/notifications/synthesis.py` (D1 + D2 helpers) and `linkedin/templates/prompts/wants_meeting.j2`. Modifies `linkedin/management/commands/sync_attio.py` to call the synthesis pass per Deal. Adds `tests/test_synthesis.py`.

---

# PHASE A — Attio assert-pattern + rank-table completeness

**Why first:** The rank-table bug is live in production today — manually setting `Wants Meeting` / `Had Meeting` / `Prospecting to close` in Attio gets silently demoted on the next sync. Independently shippable; no other phase blocks on it.

## Task A.1: Add missing outreach-status constants and complete the rank table

**Files:**
- Modify: `linkedin/notifications/attio.py:114-149`
- Test: `tests/test_attio.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_attio.py`:

```python
from linkedin.notifications import attio


def test_outreach_rank_includes_all_active_statuses():
    expected = {
        attio.STATUS_INVITE_SENT: 1,
        attio.STATUS_CONNECTED: 2,
        attio.STATUS_REPLIED: 3,
        attio.STATUS_WANTS_MEETING: 4,
        attio.STATUS_MEETING_BOOKED: 5,
        attio.STATUS_HAD_MEETING: 6,
        attio.STATUS_PROSPECTING_TO_CLOSE: 7,
        attio.STATUS_WON: 8,
    }
    assert attio.OUTREACH_RANK == expected


def test_should_patch_outreach_status_blocks_demotion_from_had_meeting():
    assert attio.should_patch_outreach_status(
        attio.STATUS_HAD_MEETING, attio.STATUS_REPLIED,
    ) is False


def test_should_patch_outreach_status_blocks_demotion_from_meeting_booked_to_wants_meeting():
    assert attio.should_patch_outreach_status(
        attio.STATUS_MEETING_BOOKED, attio.STATUS_WANTS_MEETING,
    ) is False


def test_should_patch_outreach_status_blocks_demotion_from_prospecting_to_close():
    assert attio.should_patch_outreach_status(
        attio.STATUS_PROSPECTING_TO_CLOSE, attio.STATUS_HAD_MEETING,
    ) is False


def test_should_patch_outreach_status_allows_promotion_to_wants_meeting():
    assert attio.should_patch_outreach_status(
        attio.STATUS_REPLIED, attio.STATUS_WANTS_MEETING,
    ) is True
    assert attio.should_patch_outreach_status(
        attio.STATUS_CONNECTED, attio.STATUS_WANTS_MEETING,
    ) is True


def test_should_patch_outreach_status_won_overrides_anything():
    assert attio.should_patch_outreach_status(
        attio.STATUS_HAD_MEETING, attio.STATUS_WON,
    ) is True


def test_should_patch_outreach_status_lost_is_overridable():
    assert attio.should_patch_outreach_status(
        attio.STATUS_LOST, attio.STATUS_REPLIED,
    ) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_attio.py -v`
Expected: FAIL — `AttributeError: module 'linkedin.notifications.attio' has no attribute 'STATUS_WANTS_MEETING'`.

- [ ] **Step 3: Add the constants and update the rank table**

Edit `linkedin/notifications/attio.py` — replace lines 114-149 with:

```python
STATUS_INVITE_SENT = "Invite Sent"
STATUS_CONNECTED = "Connected"
STATUS_REPLIED = "Replied"
STATUS_WANTS_MEETING = "Wants Meeting"
STATUS_MEETING_BOOKED = "Meeting Booked"
STATUS_HAD_MEETING = "Had Meeting"
STATUS_PROSPECTING_TO_CLOSE = "Prospecting to close"
STATUS_WON = "Won"
STATUS_LOST = "Lost"


def deal_to_outreach_status(deal) -> str:
    """Compute Person.outreach_status from a Deal.

    Only the auto-managed states map here — Wants Meeting / Meeting Booked /
    Had Meeting / Prospecting to close are human- or LLM-driven and the
    don't-downgrade rule preserves them via `should_patch_outreach_status`.
    """
    from linkedin.enums import ProfileState

    state = deal.state
    if state == ProfileState.COMPLETED:
        return STATUS_WON
    if state == ProfileState.FAILED:
        return STATUS_LOST
    if state == ProfileState.CONNECTED:
        return STATUS_REPLIED if deal.last_reply_at is not None else STATUS_CONNECTED
    return STATUS_INVITE_SENT  # PENDING


# Outreach-status progression: Won wins; otherwise furthest-along beats
# earlier. Lost is terminal-negative, never "more progress" than active.
OUTREACH_RANK = {
    STATUS_INVITE_SENT:         1,
    STATUS_CONNECTED:           2,
    STATUS_REPLIED:             3,
    STATUS_WANTS_MEETING:       4,
    STATUS_MEETING_BOOKED:      5,
    STATUS_HAD_MEETING:         6,
    STATUS_PROSPECTING_TO_CLOSE: 7,
    STATUS_WON:                 8,
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_attio.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_attio.py linkedin/notifications/attio.py
git commit -m "fix(attio): complete outreach-status rank table to prevent demotion of human-set statuses"
```

## Task A.2: Switch `create_person` to PUT with `matching_attribute=linkedin`

**Files:**
- Modify: `linkedin/notifications/attio.py:174-252`
- Test: `tests/test_attio.py`

Background: `create_person` currently does `POST /objects/people/records` which always creates. Attio supports `PUT /objects/people/records?matching_attribute=<slug>` which finds-or-creates by that attribute. Switching keys off `linkedin` (text field on Person, set to `linkedin_url` when present) so a manually-inserted Person with the same LinkedIn URL gets reused.

Both endpoints return the same response shape under `data.id.record_id`, so `_extract_id` doesn't change.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_attio.py`:

```python
from unittest.mock import patch


def _fake_attio_response(record_id: str = "rec_abc123") -> dict:
    return {"data": {"id": {"record_id": record_id}}}


@patch("linkedin.notifications.attio._request")
def test_create_person_uses_put_with_linkedin_matching_attribute(mock_request):
    mock_request.return_value = _fake_attio_response("rec_xyz")

    pid = attio.create_person(
        first_name="Waylon",
        last_name="Krush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
    )

    assert pid == "rec_xyz"
    call = mock_request.call_args
    method, path = call.args[0], call.args[1]
    body = call.args[2]
    assert method == "PUT"
    assert path == "/objects/people/records?matching_attribute=linkedin"
    assert body["data"]["values"]["linkedin"] == "https://www.linkedin.com/in/waylonkrush/"


@patch("linkedin.notifications.attio._request")
def test_create_person_omits_matching_attribute_when_no_linkedin_url(mock_request):
    mock_request.return_value = _fake_attio_response("rec_no_li")

    pid = attio.create_person(first_name="Jane", last_name="Doe")

    assert pid == "rec_no_li"
    call = mock_request.call_args
    method, path = call.args[0], call.args[1]
    assert method == "POST"
    assert path == "/objects/people/records"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_attio.py::test_create_person_uses_put_with_linkedin_matching_attribute -v`
Expected: FAIL — current implementation calls `_request("POST", "/objects/people/records", ...)`.

- [ ] **Step 3: Implement assert-pattern in `create_person`**

Replace `create_person` in `linkedin/notifications/attio.py` (currently lines ~224-252):

```python
def create_person(
    *,
    first_name: str,
    last_name: str,
    linkedin_url: str = "",
    job_title: str = "",
    company_id: str = "",
) -> str:
    """Create-or-find a Person record by LinkedIn URL. Returns record_id.

    Uses Attio's assert (PUT ?matching_attribute=) endpoint when a LinkedIn
    URL is provided so manual Attio inserts with the same URL are reused
    rather than duplicated. Falls back to POST when no URL is available.
    """
    full = f"{first_name} {last_name}".strip()
    values: dict = {
        "name": [{
            "first_name": first_name or "",
            "last_name": last_name or "",
            "full_name": full,
        }],
    }
    if linkedin_url:
        values["linkedin"] = linkedin_url
    if job_title:
        values["job_title"] = job_title
    if company_id:
        values["company"] = [{
            "target_object": "companies",
            "target_record_id": company_id,
        }]
    body = {"data": {"values": values}}

    if linkedin_url:
        path = "/objects/people/records?matching_attribute=linkedin"
        resp = _request("PUT", path, body)
    else:
        resp = _request("POST", "/objects/people/records", body)
    return _extract_id(resp, "record_id")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_attio.py -v`
Expected: 9 passed (7 from A.1 + 2 from A.2).

- [ ] **Step 5: Commit**

```bash
git add tests/test_attio.py linkedin/notifications/attio.py
git commit -m "feat(attio): create_person uses PUT matching_attribute=linkedin to dedupe against manual inserts"
```

## Task A.3: Switch `create_company` to PUT with `matching_attribute=name`

**Files:**
- Modify: `linkedin/notifications/attio.py:217-221`
- Test: `tests/test_attio.py`

Companies dedupe by `name` since we don't carry domain. Two "Acme Inc" vs "Acme, Inc." can still split — acceptable for now; future enhancement when `Lead.company_domain` lands.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_attio.py`:

```python
@patch("linkedin.notifications.attio._request")
def test_create_company_uses_put_with_name_matching_attribute(mock_request):
    mock_request.return_value = _fake_attio_response("rec_co_1")

    cid = attio.create_company("Acme Inc")

    assert cid == "rec_co_1"
    call = mock_request.call_args
    method, path = call.args[0], call.args[1]
    body = call.args[2]
    assert method == "PUT"
    assert path == "/objects/companies/records?matching_attribute=name"
    assert body["data"]["values"]["name"] == "Acme Inc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_attio.py::test_create_company_uses_put_with_name_matching_attribute -v`
Expected: FAIL — current implementation uses `POST`.

- [ ] **Step 3: Switch `create_company` to PUT**

Replace `create_company` (currently at line 217):

```python
def create_company(name: str) -> str:
    """Create-or-find a Company record by name. Returns record_id."""
    body = {"data": {"values": {"name": name}}}
    resp = _request(
        "PUT", "/objects/companies/records?matching_attribute=name", body,
    )
    return _extract_id(resp, "record_id")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_attio.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_attio.py linkedin/notifications/attio.py
git commit -m "feat(attio): create_company uses PUT matching_attribute=name to dedupe against manual inserts"
```

## Task A.4: Update CLAUDE.md and ARCHITECTURE.md to reflect the rank-table fix

**Files:**
- Modify: `CLAUDE.md` (Attio sync section)
- Modify: `ARCHITECTURE.md` (notifications module)

- [ ] **Step 1: Update CLAUDE.md**

Find the Attio sync paragraph in `CLAUDE.md` and append: "Outreach status rank includes the full Attio enum (Invite Sent → Connected → Replied → Wants Meeting → Meeting Booked → Had Meeting → Prospecting to close → Won); `should_patch_outreach_status` blocks demotion in either direction. Person/Company creation uses Attio's assert pattern (`PUT ?matching_attribute=linkedin` for People, `=name` for Companies) so manual Attio inserts with the same identifier are reused."

- [ ] **Step 2: Update ARCHITECTURE.md**

Add a paragraph under the Attio notifications module describing the assert-pattern dedupe behavior and the complete rank table, noting that statuses 5-7 (`Meeting Booked`/`Had Meeting`/`Prospecting to close`) are emitted only by humans or by Phase D's LLM, never by `deal_to_outreach_status`.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md ARCHITECTURE.md
git commit -m "docs: document Attio assert-pattern and complete outreach-status rank table"
```

---

# PHASE B — `crm.Message` model + write hook

**Why second:** Foundational for both C and D. Pure addition — no behavior change for existing flows.

## Task B.1: Add `crm.Message` model

**Files:**
- Create: `crm/models/message.py`
- Modify: `crm/models/__init__.py`
- Test: `tests/test_messages.py` (new)

Schema:
- `lead = FK(Lead, on_delete=CASCADE, related_name="messages")` — the relational anchor.
- `source = CharField(choices=...)` — `linkedin`, `gmail`, `calendar`. Calendar reserved for future, not used yet.
- `external_id = CharField(max_length=200)` — per-source unique ID (LinkedIn message URN, Gmail message ID).
- `direction = CharField(choices=...)` — `inbound` (from lead) or `outbound` (from us).
- `sender = CharField(max_length=200, blank=True, default="")` — display name as captured.
- `body = TextField(blank=True, default="")`.
- `sent_at = DateTimeField(db_index=True)`.
- `thread_external_id = CharField(max_length=200, blank=True, default="")` — LinkedIn conversation URN / Gmail thread ID.
- `raw = JSONField(blank=True, default=dict)` — the original parsed payload, in case we need to re-parse later.
- `creation_date = DateTimeField(default=timezone.now)`.

Constraints:
- `unique_together = ("source", "external_id")` — idempotent upserts.
- `Index(["lead", "sent_at"])` — fast thread reconstruction per lead.

- [ ] **Step 1: Write failing tests for the model**

Create `tests/test_messages.py`:

```python
import pytest
from datetime import datetime, timezone

from crm.models import Lead, Message


def test_message_can_be_created_for_a_lead():
    lead = Lead.objects.create(
        first_name="Waylon", linkedin_url="https://www.linkedin.com/in/waylonkrush/",
    )
    msg = Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id="urn:li:message:abc123",
        direction=Message.Direction.OUTBOUND,
        sender="Arian Tajbakhsh",
        body="Hey Waylon, ...",
        sent_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
    )
    assert msg.pk is not None
    assert lead.messages.count() == 1


def test_message_unique_together_source_and_external_id():
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-1/",
    )
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id="urn:li:message:1",
        direction=Message.Direction.INBOUND,
        body="hi",
        sent_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    from django.db import IntegrityError
    with pytest.raises(IntegrityError):
        Message.objects.create(
            lead=lead,
            source=Message.Source.LINKEDIN,
            external_id="urn:li:message:1",
            direction=Message.Direction.INBOUND,
            body="hi (dup)",
            sent_at=datetime(2026, 4, 2, tzinfo=timezone.utc),
        )


def test_message_same_external_id_allowed_across_sources():
    """Gmail and LinkedIn might coincidentally share an ID format."""
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-2/",
    )
    Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id="x123",
        direction=Message.Direction.INBOUND,
        sent_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    # No exception expected.
    Message.objects.create(
        lead=lead,
        source=Message.Source.GMAIL,
        external_id="x123",
        direction=Message.Direction.INBOUND,
        sent_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_messages.py -v`
Expected: FAIL — `ImportError: cannot import name 'Message' from 'crm.models'`.

- [ ] **Step 3: Create the model**

Create `crm/models/message.py`:

```python
from django.db import models
from django.utils import timezone


class Message(models.Model):
    """A single message between us and a Lead, persisted across sources.

    Sources today: LinkedIn DMs (populated by linkedin.actions.conversations
    via the get_conversation hook, plus by manage.py import_connections from
    CSVs). Gmail/Calendar reserved for future phases.
    """

    class Source(models.TextChoices):
        LINKEDIN = "linkedin", "LinkedIn"
        GMAIL = "gmail", "Gmail"
        CALENDAR = "calendar", "Calendar"

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound (from lead)"
        OUTBOUND = "outbound", "Outbound (from us)"

    lead = models.ForeignKey(
        "crm.Lead", on_delete=models.CASCADE, related_name="messages",
    )
    source = models.CharField(max_length=16, choices=Source.choices)
    external_id = models.CharField(max_length=200)
    direction = models.CharField(max_length=16, choices=Direction.choices)
    sender = models.CharField(max_length=200, blank=True, default="")
    body = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(db_index=True)
    thread_external_id = models.CharField(max_length=200, blank=True, default="")
    raw = models.JSONField(blank=True, default=dict)
    creation_date = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("source", "external_id")
        indexes = [models.Index(fields=["lead", "sent_at"])]
        ordering = ["sent_at"]

    def __str__(self):
        return f"[{self.source}/{self.direction}] {self.lead_id}: {self.body[:60]}"
```

Update `crm/models/__init__.py` to add `from crm.models.message import Message` and add `"Message"` to `__all__` (or whatever existing export pattern is in that file).

- [ ] **Step 4: Generate the migration**

Run: `.venv/bin/python manage.py makemigrations crm --name add_message`
Expected output: `Migrations for 'crm': crm/migrations/0005_add_message.py - Create model Message`.

Commit the generated migration as-is — do not edit it.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_messages.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add crm/models/message.py crm/models/__init__.py crm/migrations/0005_add_message.py tests/test_messages.py
git commit -m "feat(crm): add Message model for persisting LinkedIn/Gmail conversation threads"
```

## Task B.2: Capture `entityUrn` per message in `parse_messages`

**Files:**
- Modify: `linkedin/actions/conversations.py:57-79`
- Test: `tests/api/test_conversations_parse.py` (new — `tests/api/` already exists)

`parse_messages` currently returns `{sender, text, timestamp}` — but Voyager's `messengerMessages` payload includes a per-message `entityUrn` that we'll use as `Message.external_id` for idempotency. Add it to the parsed dict.

- [ ] **Step 1: Write failing test**

Create `tests/api/test_conversations_parse.py`:

```python
from linkedin.actions.conversations import parse_messages


def test_parse_messages_includes_entity_urn():
    raw = {
        "data": {
            "messengerMessagesBySyncToken": {
                "elements": [
                    {
                        "entityUrn": "urn:li:msg:m1",
                        "body": {"text": "hello there"},
                        "deliveredAt": 1714560000000,  # 2024-05-01 ish
                        "sender": {
                            "participantType": {
                                "member": {
                                    "firstName": {"text": "Waylon"},
                                    "lastName": {"text": "Krush"},
                                },
                            },
                        },
                    },
                ],
            },
        },
    }
    parsed = parse_messages(raw)
    assert len(parsed) == 1
    assert parsed[0]["entity_urn"] == "urn:li:msg:m1"
    assert parsed[0]["text"] == "hello there"
    assert parsed[0]["sender"] == "Waylon Krush"


def test_parse_messages_skips_message_without_entity_urn():
    """Defensive — if Voyager ever omits entityUrn we still return the others."""
    raw = {
        "data": {
            "messengerMessagesBySyncToken": {
                "elements": [
                    {
                        "body": {"text": "no urn here"},
                        "deliveredAt": 1714560000000,
                        "sender": {"participantType": {"member": {
                            "firstName": {"text": "X"}, "lastName": {"text": "Y"},
                        }}},
                    },
                    {
                        "entityUrn": "urn:li:msg:m2",
                        "body": {"text": "with urn"},
                        "deliveredAt": 1714560000000,
                        "sender": {"participantType": {"member": {
                            "firstName": {"text": "X"}, "lastName": {"text": "Y"},
                        }}},
                    },
                ],
            },
        },
    }
    parsed = parse_messages(raw)
    assert len(parsed) == 1
    assert parsed[0]["entity_urn"] == "urn:li:msg:m2"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/api/test_conversations_parse.py -v`
Expected: FAIL — `parsed[0]` has no `"entity_urn"` key.

- [ ] **Step 3: Update `parse_messages`**

Modify `linkedin/actions/conversations.py:57-79` — replace `parse_messages` with:

```python
def parse_messages(raw: dict) -> list[dict]:
    """Parse raw messages response into a list of {entity_urn, sender, text, timestamp} dicts.

    Messages without an entityUrn are skipped — without it we have no
    stable per-message identity for idempotent persistence into crm.Message.
    """
    elements = raw.get("data", {}).get("messengerMessagesBySyncToken", {}).get("elements", [])

    messages = []
    for msg in elements:
        entity_urn = msg.get("entityUrn") or ""
        if not entity_urn:
            continue

        body = msg.get("body", {})
        text = body.get("text", "") if isinstance(body, dict) else str(body)
        if not text:
            continue

        participant = msg.get("sender", {}).get("participantType", {}).get("member", {})
        first = (participant.get("firstName") or {}).get("text", "")
        last = (participant.get("lastName") or {}).get("text", "")
        sender_name = f"{first} {last}".strip()

        delivered_at = msg.get("deliveredAt")
        ts = datetime.fromtimestamp(delivered_at / 1000).strftime("%Y-%m-%d %H:%M") if delivered_at else ""

        messages.append({
            "entity_urn": entity_urn,
            "sender": sender_name or "unknown",
            "text": text,
            "timestamp": ts,
        })

    messages.sort(key=lambda m: m["timestamp"])
    return messages
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/api/test_conversations_parse.py -v`
Expected: 2 passed.

Run the existing test suite to verify nothing else broke:
Run: `.venv/bin/pytest -v`
Expected: existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add linkedin/actions/conversations.py tests/api/test_conversations_parse.py
git commit -m "feat(api): parse_messages captures entityUrn for idempotent persistence"
```

## Task B.3: Add `persist_thread` helper

**Files:**
- Create: `linkedin/db/messages.py`
- Test: `tests/test_messages.py` (extend)

`persist_thread` takes a Lead, a list of parsed message dicts, our own LinkedIn account display name (used to derive `direction`), and a thread URN. Upserts each message into `crm.Message` keyed on `(source=linkedin, external_id=entity_urn)`.

Direction rule: if `sender` matches our account name, `outbound`; else `inbound`. Names are imperfect in general, but for LinkedIn DMs Voyager always returns the participant's `firstName`/`lastName` exactly as they appear on their profile, so an exact-match against the daemon's profile display name is reliable.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_messages.py`:

```python
from linkedin.db.messages import persist_thread


def test_persist_thread_creates_messages():
    lead = Lead.objects.create(
        first_name="Waylon", linkedin_url="https://www.linkedin.com/in/waylonkrush/",
    )
    parsed = [
        {
            "entity_urn": "urn:li:msg:m1",
            "sender": "Arian Tajbakhsh",
            "text": "Hey Waylon, ...",
            "timestamp": "2026-04-01 10:00",
        },
        {
            "entity_urn": "urn:li:msg:m2",
            "sender": "Waylon Krush",
            "text": "Sounds interesting",
            "timestamp": "2026-04-02 14:30",
        },
    ]
    persist_thread(
        lead=lead,
        parsed=parsed,
        our_display_name="Arian Tajbakhsh",
        thread_external_id="urn:li:conv:c1",
    )

    msgs = list(lead.messages.order_by("sent_at"))
    assert len(msgs) == 2
    assert msgs[0].direction == Message.Direction.OUTBOUND
    assert msgs[0].external_id == "urn:li:msg:m1"
    assert msgs[1].direction == Message.Direction.INBOUND
    assert msgs[1].thread_external_id == "urn:li:conv:c1"


def test_persist_thread_is_idempotent():
    lead = Lead.objects.create(
        first_name="Waylon", linkedin_url="https://www.linkedin.com/in/waylonkrush/",
    )
    parsed = [{
        "entity_urn": "urn:li:msg:dup",
        "sender": "Waylon Krush",
        "text": "hi",
        "timestamp": "2026-04-01 10:00",
    }]
    persist_thread(lead=lead, parsed=parsed, our_display_name="Arian Tajbakhsh")
    persist_thread(lead=lead, parsed=parsed, our_display_name="Arian Tajbakhsh")
    assert lead.messages.count() == 1


def test_persist_thread_handles_unparseable_timestamp():
    """If timestamp is empty or malformed, fall back to now() — never raise."""
    lead = Lead.objects.create(
        first_name="X", linkedin_url="https://www.linkedin.com/in/x-1/",
    )
    parsed = [{
        "entity_urn": "urn:li:msg:no_ts",
        "sender": "Waylon Krush",
        "text": "hi",
        "timestamp": "",
    }]
    persist_thread(lead=lead, parsed=parsed, our_display_name="Arian Tajbakhsh")
    msg = lead.messages.get()
    assert msg.sent_at is not None  # fell back to now
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_messages.py -v`
Expected: FAIL — `ImportError: cannot import name 'persist_thread' from 'linkedin.db.messages'`.

- [ ] **Step 3: Implement `persist_thread`**

Create `linkedin/db/messages.py`:

```python
"""Idempotent persistence of conversation threads into crm.Message."""
from __future__ import annotations

import logging
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from crm.models import Message

logger = logging.getLogger(__name__)


def persist_thread(
    *,
    lead,
    parsed: list[dict],
    our_display_name: str,
    thread_external_id: str = "",
    source: str = Message.Source.LINKEDIN,
) -> int:
    """Upsert each parsed message into crm.Message. Returns count newly created.

    Idempotent on (source, entity_urn): re-running with the same payload is
    a no-op. Direction is derived from sender match against our display name.
    """
    created = 0
    with transaction.atomic():
        for m in parsed:
            entity_urn = (m.get("entity_urn") or "").strip()
            if not entity_urn:
                continue

            sender = (m.get("sender") or "").strip()
            direction = (
                Message.Direction.OUTBOUND
                if sender and sender.lower() == our_display_name.strip().lower()
                else Message.Direction.INBOUND
            )

            sent_at = _parse_timestamp(m.get("timestamp") or "")

            _, was_created = Message.objects.get_or_create(
                source=source,
                external_id=entity_urn,
                defaults={
                    "lead": lead,
                    "direction": direction,
                    "sender": sender,
                    "body": m.get("text") or "",
                    "sent_at": sent_at,
                    "thread_external_id": thread_external_id,
                    "raw": m,
                },
            )
            if was_created:
                created += 1
    return created


def _parse_timestamp(ts: str):
    """Parse 'YYYY-MM-DD HH:MM' into aware datetime; fall back to now() if empty/bad."""
    if not ts:
        return timezone.now()
    try:
        naive = datetime.strptime(ts, "%Y-%m-%d %H:%M")
        return timezone.make_aware(naive, timezone.get_current_timezone())
    except ValueError:
        logger.debug("persist_thread: malformed timestamp %r — falling back to now()", ts)
        return timezone.now()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_messages.py -v`
Expected: 6 passed (3 from B.1 + 3 from B.3).

- [ ] **Step 5: Commit**

```bash
git add linkedin/db/messages.py tests/test_messages.py
git commit -m "feat(db): persist_thread helper for idempotent message storage"
```

## Task B.4: Hook `persist_thread` into `get_conversation`

**Files:**
- Modify: `linkedin/actions/conversations.py:82-106`
- Test: `tests/test_messages.py` (extend with hook test)

The hook resolves the Lead by `public_identifier` / `linkedin_url`, derives our display name from the active LinkedInProfile's stored name (or falls back to the LinkedIn handle), and calls `persist_thread`. If the lead doesn't exist in our DB (e.g., during the import-connections flow before the Lead is created), the hook silently no-ops — `persist_thread` is *not* the only way data gets in, so callers always get the parsed messages back regardless.

- [ ] **Step 1: Write failing test for the hook**

Append to `tests/test_messages.py`:

```python
from unittest.mock import patch, MagicMock


@patch("linkedin.actions.conversations.fetch_messages")
@patch("linkedin.actions.conversations.find_conversation_urn")
@patch("linkedin.actions.conversations.find_conversation_urn_via_navigation")
@patch("linkedin.db.leads.resolve_urn")
def test_get_conversation_persists_messages_when_lead_exists(
    mock_resolve, mock_nav, mock_find, mock_fetch, fake_session,
):
    from linkedin.actions.conversations import get_conversation

    Lead.objects.create(
        first_name="Waylon",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
        public_identifier="waylonkrush",
    )

    mock_resolve.return_value = "urn:li:fsd_profile:abc"
    mock_find.return_value = "urn:li:conv:c1"
    mock_fetch.return_value = {
        "data": {"messengerMessagesBySyncToken": {"elements": [
            {
                "entityUrn": "urn:li:msg:hook1",
                "body": {"text": "hi"},
                "deliveredAt": 1714560000000,
                "sender": {"participantType": {"member": {
                    "firstName": {"text": "Waylon"},
                    "lastName": {"text": "Krush"},
                }}},
            },
        ]}},
    }

    result = get_conversation(fake_session, "waylonkrush")
    assert result and len(result) == 1
    assert Message.objects.filter(external_id="urn:li:msg:hook1").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_messages.py::test_get_conversation_persists_messages_when_lead_exists -v`
Expected: FAIL — Message not persisted (no hook yet).

- [ ] **Step 3: Add the hook to `get_conversation`**

Modify `linkedin/actions/conversations.py:82-106` — replace `get_conversation` with:

```python
def get_conversation(session, public_identifier: str) -> list[dict] | None:
    """Retrieve past messages with a profile.

    Returns a list of {entity_urn, sender, text, timestamp} dicts, or None if
    no conversation exists. Side effect: any messages found are upserted into
    crm.Message via persist_thread when a matching Lead row exists in our DB.
    """
    from linkedin.db.leads import resolve_urn
    from linkedin.db.messages import persist_thread
    from linkedin.db.urls import public_id_to_url
    from crm.models import Lead

    session.ensure_browser()
    api = PlaywrightLinkedinAPI(session=session)

    target_urn = resolve_urn(public_identifier, session=session)
    if not target_urn:
        logger.warning("Could not resolve URN for %s", public_identifier)
        return None

    conversation_urn = find_conversation_urn(api, target_urn)
    if not conversation_urn:
        logger.debug("Not in recent conversations, trying navigation fallback")
        conversation_urn = find_conversation_urn_via_navigation(session, target_urn)
    if not conversation_urn:
        logger.info("No conversation found for %s", public_identifier)
        return None

    raw = fetch_messages(api, conversation_urn)
    parsed = parse_messages(raw)

    # Side-effect persistence — only if we have a matching Lead row.
    lead = Lead.objects.filter(
        linkedin_url=public_id_to_url(public_identifier),
    ).first()
    if lead and parsed:
        our_name = _our_display_name(session)
        try:
            persist_thread(
                lead=lead,
                parsed=parsed,
                our_display_name=our_name,
                thread_external_id=conversation_urn,
            )
        except Exception as e:
            # Persistence is best-effort; never break the caller's flow.
            logger.warning("persist_thread failed for %s: %s", public_identifier, e)

    return parsed


def _our_display_name(session) -> str:
    """Best-effort display name for the daemon's account, used to label outbound messages."""
    profile = getattr(session, "linkedin_profile", None)
    if profile is None:
        return ""
    full = f"{getattr(profile, 'first_name', '') or ''} {getattr(profile, 'last_name', '') or ''}".strip()
    return full or getattr(profile, "linkedin_username", "") or ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_messages.py -v`
Expected: 7 passed.

Run full suite: `.venv/bin/pytest -v`
Expected: existing tests still pass (no regressions in sweep_connections / follow_up tests).

- [ ] **Step 5: Update CLAUDE.md and ARCHITECTURE.md**

Add to CLAUDE.md (under Architecture quick reference): "**Message store**: `crm.Message` (FK to Lead, source enum, idempotent on `(source, external_id)`). Populated as a side effect of `linkedin.actions.conversations.get_conversation()` — every caller (sweep_connections, follow_up, agent, future synthesis) auto-persists threads with no extra plumbing."

Add a corresponding paragraph to ARCHITECTURE.md.

- [ ] **Step 6: Commit**

```bash
git add linkedin/actions/conversations.py tests/test_messages.py CLAUDE.md ARCHITECTURE.md
git commit -m "feat(actions): get_conversation persists threads into crm.Message as a side effect"
```

---

# PHASE C — `manage.py import_connections` CSV command

**Why third:** Uses Phases A and B but is independently shippable. Doesn't touch the daemon's task queue.

## Task C.1: Scaffold the command and CSV parser

**Files:**
- Create: `linkedin/management/commands/import_connections.py`
- Test: `tests/test_import_connections.py` (new)

CSV format (from `leads/linkedin-batch4-messages.csv`):
```
LinkedIn URL,First Name,Message
https://www.linkedin.com/in/waylonkrush/,Waylon,"Hey Waylon, ..."
```

The parser:
- Required columns: `LinkedIn URL`, `First Name`. (`Message` is optional — only persisted to `crm.Message` as outbound when present.)
- Refuses to run if `LinkedIn URL` column is missing.
- Emits `(public_id, linkedin_url, first_name, outbound_message)` tuples.
- Skips rows with empty `LinkedIn URL`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_import_connections.py`:

```python
import io
import textwrap

import pytest

from linkedin.management.commands.import_connections import (
    parse_csv, CsvFormatError,
)


def _csv(text: str) -> io.StringIO:
    return io.StringIO(textwrap.dedent(text).lstrip())


def test_parse_csv_basic():
    rows = list(parse_csv(_csv("""\
        LinkedIn URL,First Name,Message
        https://www.linkedin.com/in/waylonkrush/,Waylon,"Hey Waylon"
        https://www.linkedin.com/in/jane-d/,Jane,"Hi Jane"
    """)))
    assert len(rows) == 2
    assert rows[0].public_id == "waylonkrush"
    assert rows[0].linkedin_url == "https://www.linkedin.com/in/waylonkrush/"
    assert rows[0].first_name == "Waylon"
    assert rows[0].outbound_message == "Hey Waylon"


def test_parse_csv_message_column_optional():
    rows = list(parse_csv(_csv("""\
        LinkedIn URL,First Name
        https://www.linkedin.com/in/waylonkrush/,Waylon
    """)))
    assert rows[0].outbound_message == ""


def test_parse_csv_skips_blank_url():
    rows = list(parse_csv(_csv("""\
        LinkedIn URL,First Name,Message
        ,Nobody,"orphan row"
        https://www.linkedin.com/in/waylonkrush/,Waylon,"hi"
    """)))
    assert len(rows) == 1
    assert rows[0].public_id == "waylonkrush"


def test_parse_csv_raises_when_url_column_missing():
    with pytest.raises(CsvFormatError, match="LinkedIn URL"):
        list(parse_csv(_csv("""\
            First Name,Message
            Waylon,"hi"
        """)))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_import_connections.py -v`
Expected: FAIL — `ImportError: cannot import name ...`.

- [ ] **Step 3: Implement parser + scaffold**

Create `linkedin/management/commands/import_connections.py`:

```python
"""Backfill Attio with already-accepted LinkedIn connections from a CSV.

Logs into LinkedIn as a separate account, scrapes the Connections page back
N days, and for each CSV row whose URL matches a connection card, creates a
Lead + Deal at state=CONNECTED in our DB. The hourly sync_attio cron then
mirrors these to Attio. Does NOT enqueue follow-ups or touch the daemon
task queue.
"""
from __future__ import annotations

import csv as csv_module
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, IO

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from linkedin.db.urls import url_to_public_id

logger = logging.getLogger(__name__)


class CsvFormatError(Exception):
    pass


@dataclass(frozen=True)
class CsvRow:
    public_id: str
    linkedin_url: str
    first_name: str
    outbound_message: str


def parse_csv(fp: IO) -> Iterable[CsvRow]:
    reader = csv_module.DictReader(fp)
    if reader.fieldnames is None or "LinkedIn URL" not in reader.fieldnames:
        raise CsvFormatError(
            "CSV must include a 'LinkedIn URL' column. "
            f"Got: {reader.fieldnames}"
        )
    if "First Name" not in reader.fieldnames:
        raise CsvFormatError("CSV must include a 'First Name' column.")

    for row in reader:
        url = (row.get("LinkedIn URL") or "").strip()
        if not url:
            continue
        public_id = url_to_public_id(url) or ""
        if not public_id:
            logger.warning("Could not extract public_id from %r — skipping", url)
            continue
        yield CsvRow(
            public_id=public_id,
            linkedin_url=url,
            first_name=(row.get("First Name") or "").strip(),
            outbound_message=(row.get("Message") or "").strip(),
        )


class Command(BaseCommand):
    help = "Backfill Attio with already-accepted LinkedIn connections from a CSV."

    def add_arguments(self, parser):
        parser.add_argument("--csv", required=True, help="Path to the CSV file.")
        parser.add_argument(
            "--handle", required=True,
            help="LinkedInProfile.linkedin_username for the account whose connections to scrape.",
        )
        parser.add_argument(
            "--since-days", type=int, default=90,
            help="How far back on the Connections page to paginate (default: 90 days).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print the plan without writing to the DB.",
        )

    def handle(self, *args, **opts):
        # Implementation lands in subsequent tasks (C.2-C.4).
        raise NotImplementedError("Wired up in subsequent tasks")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_import_connections.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add linkedin/management/commands/import_connections.py tests/test_import_connections.py
git commit -m "feat(import): scaffold import_connections command with CSV parser"
```

## Task C.2: Three-way DB dedupe rule

**Files:**
- Modify: `linkedin/management/commands/import_connections.py`
- Test: `tests/test_import_connections.py`

Rule:
- URL **not in DB** → create Lead + Backfill Deal at `state=CONNECTED`.
- URL **in DB, only as a Deal in the backfill campaign** → upsert (refresh `last_reply_at` if a reply showed up).
- URL **in DB with a Deal in any other campaign** → skip, log it.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_import_connections.py`:

```python
from datetime import datetime, timezone
from unittest.mock import MagicMock

from crm.models import Lead, Deal
from linkedin.enums import ProfileState
from linkedin.management.commands.import_connections import (
    DedupeDecision, decide_dedupe, get_or_create_backfill_campaign,
)


@pytest.fixture
def backfill_campaign(db):
    return get_or_create_backfill_campaign("backfill@example.com")


def test_decide_dedupe_url_not_in_db_creates(backfill_campaign):
    decision = decide_dedupe(
        linkedin_url="https://www.linkedin.com/in/new-person/",
        backfill_campaign=backfill_campaign,
    )
    assert decision == DedupeDecision.CREATE


def test_decide_dedupe_existing_backfill_only_upserts(backfill_campaign):
    lead = Lead.objects.create(
        first_name="W", linkedin_url="https://www.linkedin.com/in/dup-1/",
    )
    Deal.objects.create(
        lead=lead, campaign=backfill_campaign, state=ProfileState.CONNECTED,
    )
    decision = decide_dedupe(
        linkedin_url=lead.linkedin_url, backfill_campaign=backfill_campaign,
    )
    assert decision == DedupeDecision.UPSERT


def test_decide_dedupe_existing_in_other_campaign_skips(backfill_campaign, fake_session):
    lead = Lead.objects.create(
        first_name="W", linkedin_url="https://www.linkedin.com/in/dup-2/",
    )
    Deal.objects.create(
        lead=lead, campaign=fake_session.campaign, state=ProfileState.CONNECTED,
    )
    decision = decide_dedupe(
        linkedin_url=lead.linkedin_url, backfill_campaign=backfill_campaign,
    )
    assert decision == DedupeDecision.SKIP
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_import_connections.py -v -k decide_dedupe`
Expected: FAIL — symbols don't exist yet.

- [ ] **Step 3: Implement dedupe + campaign helper**

Append to `linkedin/management/commands/import_connections.py`:

```python
import enum

from crm.models import Lead, Deal
from linkedin.models import Campaign


class DedupeDecision(enum.Enum):
    CREATE = "create"
    UPSERT = "upsert"
    SKIP = "skip"


def get_or_create_backfill_campaign(handle: str) -> Campaign:
    """One backfill campaign per source account.

    Naming groups all imports from the same separate-account login under
    the same campaign, keeps active outreach campaigns clean, and lets
    `sync_attio --campaign N` target backfill independently.
    """
    name = f"Backfill: {handle}"
    campaign, _ = Campaign.objects.get_or_create(name=name)
    return campaign


def decide_dedupe(*, linkedin_url: str, backfill_campaign: Campaign) -> DedupeDecision:
    lead = Lead.objects.filter(linkedin_url=linkedin_url).first()
    if lead is None:
        return DedupeDecision.CREATE

    deals = list(Deal.objects.filter(lead=lead).select_related("campaign"))
    if not deals:
        return DedupeDecision.CREATE

    other_campaign_deal = next(
        (d for d in deals if d.campaign_id != backfill_campaign.pk),
        None,
    )
    if other_campaign_deal is not None:
        return DedupeDecision.SKIP
    return DedupeDecision.UPSERT
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_import_connections.py -v`
Expected: 7 passed (4 from C.1 + 3 from C.2).

- [ ] **Step 5: Commit**

```bash
git add linkedin/management/commands/import_connections.py tests/test_import_connections.py
git commit -m "feat(import): three-way dedupe rule and per-account backfill campaign helper"
```

## Task C.3: Per-match write logic (Lead + Deal at CONNECTED, persist outbound from CSV)

**Files:**
- Modify: `linkedin/management/commands/import_connections.py`
- Test: `tests/test_import_connections.py`

For each matched row:
- Create or update Lead (`linkedin_url`, `first_name`, `public_identifier`).
- Create or update Deal under the backfill campaign at `state=CONNECTED`.
- If the CSV had an outbound `Message`, persist it as `Message(source=linkedin, direction=outbound)` with a synthetic external_id (`csv:<csv_path>:<linkedin_url>`).
- Caller still fetches `get_conversation()` separately; that handles inbound messages and `last_reply_at`.

- [ ] **Step 1: Write failing test**

Append to `tests/test_import_connections.py`:

```python
from linkedin.management.commands.import_connections import (
    apply_match, CsvRow,
)
from crm.models import Lead, Deal, Message


def test_apply_match_creates_lead_deal_and_outbound_message(backfill_campaign):
    row = CsvRow(
        public_id="waylonkrush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
        first_name="Waylon",
        outbound_message="Hey Waylon, we built FedrampGPT",
    )
    apply_match(
        row=row, backfill_campaign=backfill_campaign,
        csv_source_id="leads/batch4.csv",
    )
    lead = Lead.objects.get(linkedin_url=row.linkedin_url)
    assert lead.first_name == "Waylon"
    assert lead.public_identifier == "waylonkrush"
    deal = Deal.objects.get(lead=lead, campaign=backfill_campaign)
    assert deal.state == ProfileState.CONNECTED
    msg = Message.objects.get(lead=lead, source=Message.Source.LINKEDIN)
    assert msg.direction == Message.Direction.OUTBOUND
    assert "FedrampGPT" in msg.body
    assert msg.external_id.startswith("csv:")


def test_apply_match_is_idempotent(backfill_campaign):
    row = CsvRow(
        public_id="waylonkrush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
        first_name="Waylon",
        outbound_message="Hey Waylon",
    )
    apply_match(row=row, backfill_campaign=backfill_campaign, csv_source_id="x.csv")
    apply_match(row=row, backfill_campaign=backfill_campaign, csv_source_id="x.csv")
    assert Lead.objects.filter(linkedin_url=row.linkedin_url).count() == 1
    assert Deal.objects.filter(lead__linkedin_url=row.linkedin_url).count() == 1
    assert Message.objects.filter(lead__linkedin_url=row.linkedin_url).count() == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_import_connections.py -v -k apply_match`
Expected: FAIL — `apply_match` doesn't exist.

- [ ] **Step 3: Implement `apply_match`**

Append to `linkedin/management/commands/import_connections.py`:

```python
from django.db import transaction

from crm.models import Message
from linkedin.enums import ProfileState


def apply_match(*, row: CsvRow, backfill_campaign: Campaign, csv_source_id: str) -> None:
    """Idempotent: create Lead + Deal at CONNECTED, persist outbound CSV message."""
    with transaction.atomic():
        lead, _ = Lead.objects.get_or_create(
            linkedin_url=row.linkedin_url,
            defaults={
                "first_name": row.first_name,
                "public_identifier": row.public_id,
            },
        )
        # Backfill identifying fields if Lead pre-existed without them.
        updates = {}
        if not lead.first_name and row.first_name:
            updates["first_name"] = row.first_name
        if not lead.public_identifier:
            updates["public_identifier"] = row.public_id
        if updates:
            for k, v in updates.items():
                setattr(lead, k, v)
            lead.save(update_fields=list(updates.keys()))

        Deal.objects.get_or_create(
            lead=lead, campaign=backfill_campaign,
            defaults={"state": ProfileState.CONNECTED},
        )

        if row.outbound_message:
            Message.objects.get_or_create(
                source=Message.Source.LINKEDIN,
                external_id=f"csv:{csv_source_id}:{row.linkedin_url}",
                defaults={
                    "lead": lead,
                    "direction": Message.Direction.OUTBOUND,
                    "sender": "",  # CSV doesn't carry our display name
                    "body": row.outbound_message,
                    "sent_at": timezone.now(),
                    "raw": {"csv_source": csv_source_id},
                },
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_import_connections.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add linkedin/management/commands/import_connections.py tests/test_import_connections.py
git commit -m "feat(import): per-match write — Lead/Deal at CONNECTED + outbound CSV message"
```

## Task C.4: Wire it all together — handle() with separate-account session, scrape, match, reply detection

**Files:**
- Modify: `linkedin/management/commands/import_connections.py:handle`
- Test: `tests/test_import_connections.py`

`handle()` flow:
1. Parse CSV.
2. Resolve `LinkedInProfile` by `--handle`.
3. Spin up a session via existing `linkedin.browser.registry.get_or_create_session(handle=...)` (this is already what `linkedin/actions/conversations.py:128` does for the standalone CLI).
4. Determine the matching campaign with `get_or_create_backfill_campaign(handle)`.
5. Pre-filter rows via `decide_dedupe` — skips logged, upserts proceed (no scrape needed for upsert? Actually we still need to scrape to confirm they're connected, but the writes are idempotent — fine).
6. Compute `stop_before = today - since_days`.
7. Call `scrape_connections(session, stop_before=stop_before)`.
8. Build `accepted_by_pid` from scrape results.
9. For each non-skipped CSV row whose public_id is in the scrape: call `apply_match`, then `get_conversation` for inbound persistence + reply detection. If a reply exists, set `Deal.last_reply_at`.
10. Print summary. Honor `--dry-run` (no writes; just print).

This step is a wiring / integration task — no new logic — so the test mocks the LinkedIn session boundary and validates the end-to-end flow.

- [ ] **Step 1: Write failing integration test**

Append to `tests/test_import_connections.py`:

```python
from io import StringIO
from unittest.mock import patch, MagicMock
from django.core.management import call_command


@patch("linkedin.management.commands.import_connections.get_conversation")
@patch("linkedin.management.commands.import_connections.scrape_connections")
@patch("linkedin.management.commands.import_connections.get_or_create_session")
def test_handle_end_to_end_creates_connected_deal_for_matches(
    mock_get_session, mock_scrape, mock_get_conv, db, tmp_path,
):
    from linkedin.actions.connections import ConnectionEntry
    from datetime import date

    csv_path = tmp_path / "batch.csv"
    csv_path.write_text(
        "LinkedIn URL,First Name,Message\n"
        "https://www.linkedin.com/in/waylonkrush/,Waylon,\"Hey Waylon\"\n"
        "https://www.linkedin.com/in/not-yet-connected/,Foo,\"Hey Foo\"\n"
    )

    # LinkedInProfile must exist for --handle to resolve.
    from linkedin.models import LinkedInProfile
    from tests.factories import UserFactory
    user = UserFactory(username="backfill@example.com")
    LinkedInProfile.objects.create(
        user=user,
        linkedin_username="backfill@example.com",
        linkedin_password="x",
    )

    mock_get_session.return_value = MagicMock()
    # Only Waylon shows up in the scraped Connections page.
    mock_scrape.return_value = [
        ConnectionEntry(
            public_id="waylonkrush",
            full_name="Waylon Krush",
            connected_on=date(2026, 4, 1),
        ),
    ]
    mock_get_conv.return_value = []  # no reply

    out = StringIO()
    call_command(
        "import_connections",
        "--csv", str(csv_path),
        "--handle", "backfill@example.com",
        stdout=out,
    )

    # Waylon: matched + created at CONNECTED.
    deal = Deal.objects.get(lead__linkedin_url="https://www.linkedin.com/in/waylonkrush/")
    assert deal.state == ProfileState.CONNECTED
    # Foo: not in scrape, so no Deal created.
    assert not Lead.objects.filter(
        linkedin_url="https://www.linkedin.com/in/not-yet-connected/",
    ).exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_import_connections.py::test_handle_end_to_end_creates_connected_deal_for_matches -v`
Expected: FAIL — `handle()` raises `NotImplementedError`.

- [ ] **Step 3: Implement `handle()`**

Replace `Command.handle` in `linkedin/management/commands/import_connections.py`:

```python
def handle(self, *args, **opts):
    from linkedin.actions.connections import scrape_connections
    from linkedin.actions.conversations import get_conversation
    from linkedin.browser.registry import get_or_create_session
    from linkedin.notifications.slack import latest_reply_from_lead
    from linkedin.models import LinkedInProfile

    csv_path = opts["csv"]
    handle_name = opts["handle"]
    since_days = opts["since_days"]
    dry_run = opts["dry_run"]

    if not LinkedInProfile.objects.filter(linkedin_username=handle_name).exists():
        raise CommandError(f"No LinkedInProfile with linkedin_username={handle_name!r}")

    with open(csv_path, newline="") as fp:
        try:
            rows = list(parse_csv(fp))
        except CsvFormatError as e:
            raise CommandError(str(e))

    backfill_campaign = get_or_create_backfill_campaign(handle_name)
    self.stdout.write(f"Loaded {len(rows)} rows; backfill campaign: {backfill_campaign.name}")

    # Phase 1: dedupe pre-pass
    actionable: list[CsvRow] = []
    skipped = 0
    for row in rows:
        decision = decide_dedupe(
            linkedin_url=row.linkedin_url, backfill_campaign=backfill_campaign,
        )
        if decision == DedupeDecision.SKIP:
            skipped += 1
            self.stdout.write(f"  skip (other-campaign Deal exists): {row.linkedin_url}")
            continue
        actionable.append(row)
    self.stdout.write(f"Actionable: {len(actionable)}, skipped: {skipped}")

    if dry_run:
        self.stdout.write("[dry-run] would log in, scrape, and write the above.")
        return

    session = get_or_create_session(handle=handle_name)
    session.campaign = backfill_campaign

    stop_before = (timezone.now() - timedelta(days=since_days)).date()
    self.stdout.write(f"Scraping connections back to {stop_before} ...")
    entries = scrape_connections(session, stop_before=stop_before)
    accepted_by_pid = {e.public_id: e for e in entries}
    self.stdout.write(f"Scraped {len(entries)} connection cards.")

    matched, with_reply = 0, 0
    csv_source_id = csv_path
    for row in actionable:
        if row.public_id not in accepted_by_pid:
            continue

        apply_match(
            row=row, backfill_campaign=backfill_campaign,
            csv_source_id=csv_source_id,
        )
        matched += 1

        # Inbound side: fetch conversation (this also runs persist_thread via the hook).
        try:
            messages = get_conversation(session, row.public_id)
        except Exception as e:
            logger.warning("get_conversation failed for %s: %s", row.public_id, e)
            messages = None

        if messages:
            full_name = f"{row.first_name}".strip() or row.public_id
            reply = latest_reply_from_lead(messages, full_name)
            if reply:
                _stamp_reply(
                    linkedin_url=row.linkedin_url,
                    backfill_campaign=backfill_campaign,
                    reply=reply,
                )
                with_reply += 1

    self.stdout.write(
        f"import_connections: matched={matched} (with_reply={with_reply}) "
        f"of {len(actionable)} actionable; sync_attio will mirror these on its next run."
    )


def _stamp_reply(*, linkedin_url: str, backfill_campaign, reply: dict):
    """Set Deal.last_reply_at from the latest inbound message timestamp."""
    from datetime import datetime as _dt
    deal = Deal.objects.filter(
        lead__linkedin_url=linkedin_url, campaign=backfill_campaign,
    ).first()
    if deal is None:
        return
    ts_str = (reply.get("timestamp") or "").strip()
    if not ts_str:
        return
    try:
        naive = _dt.strptime(ts_str, "%Y-%m-%d %H:%M")
        deal.last_reply_at = timezone.make_aware(
            naive, timezone.get_current_timezone(),
        )
        deal.save(update_fields=["last_reply_at"])
    except ValueError:
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_import_connections.py -v`
Expected: 10 passed.

Run full suite: `.venv/bin/pytest -v`
Expected: no regressions.

- [ ] **Step 5: Update CLAUDE.md**

In CLAUDE.md, add to the Commands section:

```bash
# Backfill from CSV (uses a separate LinkedIn account)
.venv/bin/python manage.py import_connections \
  --csv leads/linkedin-batch4-messages.csv \
  --handle backfill@example.com \
  --since-days 90 \
  [--dry-run]
```

And add a one-paragraph description: "Reads a CSV (`LinkedIn URL,First Name,Message`), logs into LinkedIn as the named LinkedInProfile, scrapes the Connections page back N days, and creates Lead+Deal rows at `state=CONNECTED` for each CSV row that's also in the scraped connections. Skips URLs that already have a Deal in any non-backfill campaign so the daemon's outreach state is never disturbed. Outbound message from the CSV is persisted to `crm.Message`; inbound messages flow through `get_conversation`'s persist hook. `sync_attio` mirrors these to Attio on its next hourly run — this command never touches Attio directly."

- [ ] **Step 6: Commit**

```bash
git add linkedin/management/commands/import_connections.py tests/test_import_connections.py CLAUDE.md
git commit -m "feat(import): wire import_connections handle() with scrape + match + reply detection"
```

---

# PHASE D — email extraction + "Wants Meeting" LLM in `sync_attio`

**Why last:** Depends on B's `crm.Message` table for both passes. Highest external-API surface area (LLM, Attio email writes), so it benefits from A and B being stable first.

## Task D.1: Add `Lead.email`, `Deal.last_synthesized_at`, `Deal.wants_meeting_detected_at`

**Files:**
- Modify: `crm/models/lead.py`
- Modify: `crm/models/deal.py`
- Migration: `crm/migrations/0006_add_email_and_synthesis_fields.py`
- Test: `tests/test_synthesis.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/test_synthesis.py`:

```python
from datetime import datetime, timezone

from crm.models import Lead, Deal


def test_lead_has_email_field():
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-1/",
        email="a@example.com",
    )
    assert lead.email == "a@example.com"


def test_lead_email_defaults_to_blank():
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-2/",
    )
    assert lead.email == ""


def test_deal_has_synthesis_tracking_fields(fake_session):
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-3/",
    )
    deal = Deal.objects.create(lead=lead, campaign=fake_session.campaign)
    assert deal.last_synthesized_at is None
    assert deal.wants_meeting_detected_at is None
    deal.last_synthesized_at = datetime(2026, 4, 27, tzinfo=timezone.utc)
    deal.wants_meeting_detected_at = datetime(2026, 4, 27, tzinfo=timezone.utc)
    deal.save()
    deal.refresh_from_db()
    assert deal.last_synthesized_at is not None
    assert deal.wants_meeting_detected_at is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_synthesis.py -v`
Expected: FAIL — fields don't exist.

- [ ] **Step 3: Add the fields**

Modify `crm/models/lead.py` — add after `linkedin_url`:

```python
email = models.EmailField(max_length=200, blank=True, default="", db_index=True)
```

Modify `crm/models/deal.py` — add at the end of the field block (alongside `last_reply_at`):

```python
last_synthesized_at = models.DateTimeField(null=True, blank=True)
wants_meeting_detected_at = models.DateTimeField(null=True, blank=True)
```

- [ ] **Step 4: Generate migration**

Run: `.venv/bin/python manage.py makemigrations crm --name add_email_and_synthesis_fields`
Expected: `crm/migrations/0006_add_email_and_synthesis_fields.py` created.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_synthesis.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add crm/models/lead.py crm/models/deal.py crm/migrations/0006_add_email_and_synthesis_fields.py tests/test_synthesis.py
git commit -m "feat(crm): add Lead.email, Deal.last_synthesized_at, Deal.wants_meeting_detected_at"
```

## Task D.2: Email-extraction helper (D1)

**Files:**
- Create: `linkedin/notifications/synthesis.py`
- Test: `tests/test_synthesis.py`

`extract_email_from_messages(messages)` — scan inbound messages chronologically; return the first email address found via standard regex, or `""`. The caller decides what to do with it.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_synthesis.py`:

```python
from datetime import datetime, timezone as _tz

from crm.models import Message
from linkedin.notifications.synthesis import extract_email_from_messages


def _msg(lead, *, body, direction, sent_at):
    return Message.objects.create(
        lead=lead,
        source=Message.Source.LINKEDIN,
        external_id=f"urn:li:msg:{body[:10]}_{direction}_{sent_at.isoformat()}",
        direction=direction,
        body=body,
        sent_at=sent_at,
    )


def test_extract_email_returns_first_inbound_email():
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-em-1/",
    )
    _msg(lead, body="Hey, you can reach me at jane@example.com",
         direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 1, tzinfo=_tz.utc))
    _msg(lead, body="Or also janedoe@gmail.com",
         direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 2, tzinfo=_tz.utc))
    assert extract_email_from_messages(lead.messages.all()) == "jane@example.com"


def test_extract_email_ignores_outbound():
    """We sent 'reach me at us@ours.com' — shouldn't be extracted as the lead's email."""
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-em-2/",
    )
    _msg(lead, body="Reach me at us@ours.com",
         direction=Message.Direction.OUTBOUND,
         sent_at=datetime(2026, 4, 1, tzinfo=_tz.utc))
    _msg(lead, body="OK noted",
         direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 2, tzinfo=_tz.utc))
    assert extract_email_from_messages(lead.messages.all()) == ""


def test_extract_email_returns_empty_when_none_found():
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-em-3/",
    )
    _msg(lead, body="just text no email",
         direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 1, tzinfo=_tz.utc))
    assert extract_email_from_messages(lead.messages.all()) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_synthesis.py -v -k extract_email`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `extract_email_from_messages`**

Create `linkedin/notifications/synthesis.py`:

```python
"""Hourly synthesis pass: email extraction (D1) and Wants Meeting LLM (D2).

Designed to run inside the per-Deal loop of manage.py sync_attio. All
operations are best-effort — failures must never block the existing
Stage/Status sync from completing.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from crm.models import Message

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def extract_email_from_messages(messages: Iterable[Message]) -> str:
    """Return the first email mentioned in an inbound message, or ''."""
    for msg in sorted(
        (m for m in messages if m.direction == Message.Direction.INBOUND),
        key=lambda m: m.sent_at,
    ):
        match = EMAIL_RE.search(msg.body or "")
        if match:
            return match.group(0)
    return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_synthesis.py -v`
Expected: 6 passed (3 from D.1 + 3 from D.2).

- [ ] **Step 5: Commit**

```bash
git add linkedin/notifications/synthesis.py tests/test_synthesis.py
git commit -m "feat(synthesis): extract_email_from_messages helper for D1"
```

## Task D.3: Append email to Attio Person via PATCH

**Files:**
- Modify: `linkedin/notifications/attio.py` (add `add_person_email`)
- Test: `tests/test_attio.py`

Attio `email_addresses` is a multiselect with `is_unique=true`. To add an email, PATCH the Person record with the new value(s). Attio handles dedupe — re-patching the same email is a no-op.

PATCH body shape for adding an email:
```json
{"data": {"values": {"email_addresses": [{"email_address": "x@y.com"}]}}}
```

This *replaces* the field; to append we'd need to GET the existing list first. **Decision:** since Attio's uniqueness constraint prevents dupes per-Person, sending the same value as a list doesn't create dupes; but if there are *other* emails on the Person already, replace would drop them. So: **always GET first, append, PATCH back.** Slightly chattier but correct.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_attio.py`:

```python
@patch("linkedin.notifications.attio._request")
def test_add_person_email_appends_when_existing_emails_present(mock_request):
    # First call: GET — return one existing email.
    # Second call: PATCH — should send both.
    mock_request.side_effect = [
        {"data": {"values": {"email_addresses": [
            {"email_address": "existing@old.com"},
        ]}}},
        {"data": {"id": {"record_id": "rec_p1"}}},
    ]
    attio.add_person_email("rec_p1", "new@example.com")
    patch_call = mock_request.call_args_list[1]
    method, path = patch_call.args[0], patch_call.args[1]
    body = patch_call.args[2]
    emails = [e["email_address"] for e in body["data"]["values"]["email_addresses"]]
    assert method == "PATCH"
    assert path == "/objects/people/records/rec_p1"
    assert "existing@old.com" in emails
    assert "new@example.com" in emails


@patch("linkedin.notifications.attio._request")
def test_add_person_email_no_op_when_already_present(mock_request):
    mock_request.return_value = {"data": {"values": {"email_addresses": [
        {"email_address": "x@y.com"},
    ]}}}
    attio.add_person_email("rec_p1", "x@y.com")
    # Only the GET should have happened — no PATCH.
    assert mock_request.call_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_attio.py -v -k add_person_email`
Expected: FAIL — `add_person_email` doesn't exist.

- [ ] **Step 3: Implement `add_person_email`**

Append to `linkedin/notifications/attio.py`:

```python
def add_person_email(person_id: str, email: str) -> None:
    """Append `email` to the Person's email_addresses list (no-op if already present).

    Attio's email_addresses is multiselect+unique; Attio rejects duplicate
    emails across People with a 4xx, but adding to a single Person is
    idempotent if we filter client-side first.
    """
    if not email:
        return
    resp = _request("GET", f"/objects/people/records/{person_id}", None)
    values = ((resp.get("data") or {}).get("values") or {})
    existing = [
        (e or {}).get("email_address") or ""
        for e in (values.get("email_addresses") or [])
    ]
    if email in existing:
        return

    new_list = [{"email_address": e} for e in existing if e]
    new_list.append({"email_address": email})
    body = {"data": {"values": {"email_addresses": new_list}}}
    _request("PATCH", f"/objects/people/records/{person_id}", body)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_attio.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add linkedin/notifications/attio.py tests/test_attio.py
git commit -m "feat(attio): add_person_email helper appends to multiselect email_addresses"
```

## Task D.4: LLM "wants meeting" intent detection

**Files:**
- Create: `linkedin/templates/prompts/wants_meeting.j2`
- Modify: `linkedin/notifications/synthesis.py`
- Test: `tests/test_synthesis.py`

Reuses the existing `langchain_openai.ChatOpenAI` pattern (see `linkedin/ml/qualifier.py:47-73`). Structured output via Pydantic; cheap model (whatever `AI_MODEL` is set to — Haiku-class in production).

Prompt: "Read this LinkedIn conversation thread between [us] and [lead]. Did the lead express they want a meeting / call / demo? Return `wants_meeting: bool` and `reason: str` quoting the line that triggered yes (or 'no clear signal' if false)."

- [ ] **Step 1: Create the prompt template**

Create `linkedin/templates/prompts/wants_meeting.j2`:

```jinja
You are reading a LinkedIn conversation thread between us and a sales prospect.

Determine whether the **prospect** has expressed they want to have a meeting, call, demo, or live conversation. Implicit signals count (e.g., "send me a calendar link", "happy to chat", "let's set something up", "what does your calendar look like") — they don't have to use the literal word "meeting".

Do NOT flag yes based on our outbound messages — only on what the prospect said.

Conversation (sorted oldest → newest):

{% for m in messages -%}
[{{ m.timestamp }}] {{ "PROSPECT" if m.direction == "inbound" else "US" }}: {{ m.body }}
{% endfor %}

Return:
- wants_meeting: true if the prospect expressed meeting intent, else false
- reason: if true, quote the line. If false, "no clear signal".
```

- [ ] **Step 2: Write failing tests**

Append to `tests/test_synthesis.py`:

```python
from unittest.mock import patch, MagicMock

from linkedin.notifications.synthesis import detect_wants_meeting


@patch("linkedin.notifications.synthesis._build_llm")
def test_detect_wants_meeting_true_with_quote(mock_build):
    fake_llm = MagicMock()
    fake_llm.invoke.return_value = MagicMock(
        wants_meeting=True, reason='"send me a calendar link"',
    )
    mock_build.return_value = fake_llm

    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-w-1/",
    )
    msgs = [
        _msg(lead, body="Hey", direction=Message.Direction.OUTBOUND,
             sent_at=datetime(2026, 4, 1, tzinfo=_tz.utc)),
        _msg(lead, body="send me a calendar link", direction=Message.Direction.INBOUND,
             sent_at=datetime(2026, 4, 2, tzinfo=_tz.utc)),
    ]
    result = detect_wants_meeting(msgs)
    assert result.wants_meeting is True
    assert "calendar link" in result.reason


@patch("linkedin.notifications.synthesis._build_llm")
def test_detect_wants_meeting_false_when_no_signal(mock_build):
    fake_llm = MagicMock()
    fake_llm.invoke.return_value = MagicMock(
        wants_meeting=False, reason="no clear signal",
    )
    mock_build.return_value = fake_llm

    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-w-2/",
    )
    msgs = [
        _msg(lead, body="not interested", direction=Message.Direction.INBOUND,
             sent_at=datetime(2026, 4, 1, tzinfo=_tz.utc)),
    ]
    result = detect_wants_meeting(msgs)
    assert result.wants_meeting is False
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_synthesis.py -v -k detect_wants_meeting`
Expected: FAIL — `detect_wants_meeting` doesn't exist.

- [ ] **Step 4: Implement `detect_wants_meeting`**

Append to `linkedin/notifications/synthesis.py`:

```python
from dataclasses import dataclass

import jinja2
from pydantic import BaseModel, Field


class WantsMeetingDecision(BaseModel):
    wants_meeting: bool = Field(description="True if the prospect expressed meeting intent.")
    reason: str = Field(description="Quoted line if true; 'no clear signal' if false.")


@dataclass
class DetectionResult:
    wants_meeting: bool
    reason: str


def detect_wants_meeting(messages: Iterable[Message]) -> DetectionResult:
    """Run LLM over the thread; return structured decision."""
    msgs = sorted(messages, key=lambda m: m.sent_at)
    llm = _build_llm()
    prompt = _render_prompt(msgs)
    decision = llm.invoke(prompt)
    return DetectionResult(
        wants_meeting=bool(decision.wants_meeting),
        reason=str(decision.reason or ""),
    )


def _build_llm():
    from langchain_openai import ChatOpenAI
    from linkedin.conf import AI_MODEL, LLM_API_KEY, LLM_API_BASE

    if not LLM_API_KEY:
        raise ValueError("LLM_API_KEY is not set")
    base = ChatOpenAI(
        model=AI_MODEL, temperature=0, api_key=LLM_API_KEY,
        base_url=LLM_API_BASE, timeout=30,
    )
    return base.with_structured_output(WantsMeetingDecision)


def _render_prompt(messages: list[Message]) -> str:
    from linkedin.conf import PROMPTS_DIR
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))
    template = env.get_template("wants_meeting.j2")
    payload = [
        {
            "timestamp": m.sent_at.isoformat(),
            "direction": m.direction,
            "body": m.body,
        }
        for m in messages
    ]
    return template.render(messages=payload)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_synthesis.py -v`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add linkedin/templates/prompts/wants_meeting.j2 linkedin/notifications/synthesis.py tests/test_synthesis.py
git commit -m "feat(synthesis): detect_wants_meeting LLM call (D2)"
```

## Task D.5: `synthesize_for_deal` orchestrator (D1+D2 with all gates)

**Files:**
- Modify: `linkedin/notifications/synthesis.py`
- Test: `tests/test_synthesis.py`

Per-Deal flow:
1. Skip if `deal.wants_meeting_detected_at IS NOT NULL` AND `lead.email != ""` — both signals locked in, nothing to do.
2. Load `crm.Message` for the Lead. Skip if no messages or `Deal.last_synthesized_at >= max(message.sent_at)`.
3. **D1**: if `lead.email == ""`, run `extract_email_from_messages`. If found, set `lead.email` AND call `add_person_email(lead.attio_person_id, ...)`.
4. **D2**: if `deal.wants_meeting_detected_at IS NULL` AND current Outreach status rank < `Wants Meeting` rank: run `detect_wants_meeting`. If true, patch status (via `should_patch_outreach_status`) and POST a "why we flagged" Attio note. Set `deal.wants_meeting_detected_at`.
5. Set `deal.last_synthesized_at = now()`. Save.
6. All API/LLM calls wrapped — never raise out.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_synthesis.py`:

```python
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone as _tz

from linkedin.notifications.synthesis import synthesize_for_deal


@patch("linkedin.notifications.synthesis.detect_wants_meeting")
@patch("linkedin.notifications.synthesis.add_person_email")
@patch("linkedin.notifications.synthesis.create_person_note")
@patch("linkedin.notifications.synthesis.set_person_outreach_status")
@patch("linkedin.notifications.synthesis.get_person_outreach_status")
def test_synthesize_extracts_email_and_flags_wants_meeting(
    mock_get_status, mock_set_status, mock_create_note,
    mock_add_email, mock_detect, fake_session,
):
    from linkedin.notifications import attio
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-syn-1/",
        attio_person_id="rec_attio_1",
    )
    deal = Deal.objects.create(
        lead=lead, campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
        last_reply_at=datetime(2026, 4, 2, tzinfo=_tz.utc),
    )
    _msg(lead, body="reach me at jane@example.com — happy to chat",
         direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 2, 10, 0, tzinfo=_tz.utc))

    mock_get_status.return_value = attio.STATUS_REPLIED
    mock_detect.return_value = MagicMock(
        wants_meeting=True, reason='"happy to chat"',
    )

    synthesize_for_deal(deal)

    lead.refresh_from_db()
    deal.refresh_from_db()
    assert lead.email == "jane@example.com"
    mock_add_email.assert_called_once_with("rec_attio_1", "jane@example.com")
    mock_set_status.assert_called_once_with("rec_attio_1", attio.STATUS_WANTS_MEETING)
    mock_create_note.assert_called_once()
    assert deal.wants_meeting_detected_at is not None
    assert deal.last_synthesized_at is not None


@patch("linkedin.notifications.synthesis.detect_wants_meeting")
def test_synthesize_skips_llm_when_already_detected(mock_detect, fake_session):
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-syn-2/",
    )
    deal = Deal.objects.create(
        lead=lead, campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
        wants_meeting_detected_at=datetime(2026, 4, 1, tzinfo=_tz.utc),
    )
    _msg(lead, body="I want to meet", direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 5, tzinfo=_tz.utc))
    lead.email = "x@y.com"
    lead.save()

    synthesize_for_deal(deal)

    mock_detect.assert_not_called()


@patch("linkedin.notifications.synthesis.detect_wants_meeting")
def test_synthesize_skips_llm_when_no_new_messages_since_last_run(mock_detect, fake_session):
    lead = Lead.objects.create(
        first_name="A", linkedin_url="https://www.linkedin.com/in/a-syn-3/",
    )
    deal = Deal.objects.create(
        lead=lead, campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
        last_synthesized_at=datetime(2026, 4, 10, tzinfo=_tz.utc),
    )
    # Message older than last_synthesized_at.
    _msg(lead, body="stale", direction=Message.Direction.INBOUND,
         sent_at=datetime(2026, 4, 1, tzinfo=_tz.utc))

    synthesize_for_deal(deal)
    mock_detect.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_synthesis.py -v -k synthesize`
Expected: FAIL — `synthesize_for_deal` doesn't exist.

- [ ] **Step 3: Add `create_person_note` helper to attio.py first** (synthesize_for_deal will import it)

Append to `linkedin/notifications/attio.py`:

```python
def create_person_note(*, person_id: str, title: str, content: str) -> str:
    """POST a note to a Person record. Returns note_id."""
    body = {
        "data": {
            "parent_object": "people",
            "parent_record_id": person_id,
            "title": title,
            "format": "plaintext",
            "content": content,
        },
    }
    resp = _request("POST", "/notes", body)
    return _extract_id(resp, "note_id")
```

- [ ] **Step 4: Implement `synthesize_for_deal`**

Append to `linkedin/notifications/synthesis.py`:

```python
from django.utils import timezone

from linkedin.notifications.attio import (
    OUTREACH_RANK,
    STATUS_WANTS_MEETING,
    add_person_email,
    create_person_note,
    get_person_outreach_status,
    set_person_outreach_status,
    should_patch_outreach_status,
)


def synthesize_for_deal(deal) -> None:
    """Run D1 (email) and D2 (wants meeting) for a single Deal. Best-effort.

    Gates (any one true → skip the corresponding pass):
      D1: lead.email is already populated.
      D2: deal.wants_meeting_detected_at is set, OR current Outreach status
          rank >= Wants Meeting rank.
    Outer gate: no new messages since last_synthesized_at.
    """
    lead = deal.lead
    msgs = list(lead.messages.all())
    if not msgs:
        return

    last_msg_at = max(m.sent_at for m in msgs)
    if deal.last_synthesized_at and deal.last_synthesized_at >= last_msg_at:
        return

    # D1: Email extraction.
    if not lead.email:
        try:
            extracted = extract_email_from_messages(msgs)
            if extracted:
                lead.email = extracted
                lead.save(update_fields=["email"])
                if lead.attio_person_id:
                    add_person_email(lead.attio_person_id, extracted)
        except Exception as e:
            logger.warning("D1 email extraction failed for lead %s: %s", lead.pk, e)

    # D2: Wants Meeting LLM.
    should_run_d2 = (
        deal.wants_meeting_detected_at is None
        and lead.attio_person_id
    )
    if should_run_d2:
        try:
            current_status = get_person_outreach_status(lead.attio_person_id)
            current_rank = OUTREACH_RANK.get(current_status, 0)
            if current_rank < OUTREACH_RANK[STATUS_WANTS_MEETING]:
                decision = detect_wants_meeting(msgs)
                if decision.wants_meeting:
                    if should_patch_outreach_status(current_status, STATUS_WANTS_MEETING):
                        set_person_outreach_status(lead.attio_person_id, STATUS_WANTS_MEETING)
                        try:
                            create_person_note(
                                person_id=lead.attio_person_id,
                                title="Wants Meeting (auto-detected)",
                                content=(
                                    f"Flagged based on message thread: {decision.reason}\n\n"
                                    f"— Auto-flagged by sync_attio synthesis pass on "
                                    f"{timezone.now().date().isoformat()}."
                                ),
                            )
                        except Exception as e:
                            logger.warning("Could not write Attio note: %s", e)
                    deal.wants_meeting_detected_at = timezone.now()
        except Exception as e:
            logger.warning("D2 wants-meeting detection failed for deal %s: %s", deal.pk, e)

    deal.last_synthesized_at = timezone.now()
    deal.save(update_fields=["last_synthesized_at", "wants_meeting_detected_at"])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_synthesis.py -v`
Expected: 11 passed.

- [ ] **Step 6: Commit**

```bash
git add linkedin/notifications/synthesis.py linkedin/notifications/attio.py tests/test_synthesis.py
git commit -m "feat(synthesis): synthesize_for_deal orchestrator with all gates and Attio note"
```

## Task D.6: Wire synthesis into `sync_attio`'s per-Deal loop

**Files:**
- Modify: `linkedin/management/commands/sync_attio.py` (add call after the existing per-Deal Stage/Status sync)
- Test: `tests/test_synthesis.py`

The synthesis pass runs *after* the existing Stage/Status logic for each Deal so the Person record exists in Attio before D1 tries to PATCH its email and D2 tries to set its status. Wrap the call so any failure is logged and doesn't break the surrounding loop.

- [ ] **Step 1: Write failing integration test**

Append to `tests/test_synthesis.py`:

```python
@patch("linkedin.notifications.synthesis.synthesize_for_deal")
def test_sync_attio_calls_synthesize_for_deal_per_deal(mock_synth, fake_session, monkeypatch):
    """Smoke test: sync_attio invokes synthesis once per processed Deal."""
    # Stub all the Attio writes the existing sync_attio would make.
    from linkedin.notifications import attio as attio_mod
    monkeypatch.setattr(attio_mod, "create_company", lambda *a, **kw: "rec_co")
    monkeypatch.setattr(attio_mod, "create_person", lambda *a, **kw: "rec_p")
    monkeypatch.setattr(attio_mod, "set_person_outreach_status", lambda *a, **kw: None)
    monkeypatch.setattr(attio_mod, "get_person_outreach_status", lambda *a, **kw: "")
    monkeypatch.setattr(attio_mod, "create_sales_entry", lambda *a, **kw: "rec_e")
    monkeypatch.setattr(attio_mod, "get_sales_entry_state", lambda *a, **kw: {"stage": "", "mpoc_id": ""})
    monkeypatch.setattr(attio_mod, "patch_sales_entry_stage", lambda *a, **kw: None)
    monkeypatch.setattr(attio_mod, "patch_sales_entry_mpoc", lambda *a, **kw: None)

    lead = Lead.objects.create(
        first_name="A", company_name="Acme",
        linkedin_url="https://www.linkedin.com/in/a-syn-int-1/",
    )
    Deal.objects.create(
        lead=lead, campaign=fake_session.campaign,
        state=ProfileState.CONNECTED,
    )

    from io import StringIO
    from django.core.management import call_command
    call_command("sync_attio", "--campaign", str(fake_session.campaign.pk), stdout=StringIO())

    # The synthesis function should have been called for this Deal.
    assert mock_synth.call_count >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_synthesis.py::test_sync_attio_calls_synthesize_for_deal_per_deal -v`
Expected: FAIL — `synthesize_for_deal` is never called from `sync_attio`.

- [ ] **Step 3: Add the call site in `sync_attio.py`**

Locate the per-Deal inner loop in `linkedin/management/commands/sync_attio.py` (the for-loop iterating `group` around line 148-186). After `lead.save(update_fields=["attio_person_id", "attio_company_id"])` and before the Sales-list-entry block, add:

```python
                    # ---- Phase D synthesis: D1 email + D2 Wants Meeting.
                    # Best-effort; any failure stays inside synthesize_for_deal.
                    if not dry_run:
                        try:
                            from linkedin.notifications.synthesis import synthesize_for_deal
                            synthesize_for_deal(deal_in_group)
                        except Exception as e:
                            self.stdout.write(self.style.WARNING(
                                f"    ! synthesis failed for {full}: {e}"
                            ))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_synthesis.py -v`
Expected: 12 passed.

Run full suite: `.venv/bin/pytest -v`
Expected: no regressions in any existing tests.

- [ ] **Step 5: Update CLAUDE.md and ARCHITECTURE.md**

Add to CLAUDE.md (Architecture quick reference / Attio sync section): "**Synthesis pass (Phase D)**: `sync_attio`'s per-Deal loop also runs `linkedin.notifications.synthesis.synthesize_for_deal` after the Stage/Status sync. **D1**: regex-extracts an email from inbound `crm.Message` rows and appends it to `Lead.email` + the Attio Person's `email_addresses` (multiselect, idempotent). **D2**: runs a cheap LLM (`AI_MODEL`) over the thread; if the prospect expressed meeting intent, patches Outreach status to `Wants Meeting` (don't-downgrade rule preserves higher human-set statuses) and POSTs a "Wants Meeting (auto-detected)" note to the Person. Gated by `Deal.wants_meeting_detected_at` (lock-in) and `Deal.last_synthesized_at` vs latest `Message.sent_at` (skip when no new signal). All synthesis failures are logged and never block the Stage/Status sync."

Mirror in ARCHITECTURE.md.

- [ ] **Step 6: Commit**

```bash
git add linkedin/management/commands/sync_attio.py tests/test_synthesis.py CLAUDE.md ARCHITECTURE.md
git commit -m "feat(sync_attio): wire synthesize_for_deal into per-Deal loop after Stage/Status sync"
```

---

## Final cross-phase verification

- [ ] **Run the full test suite end-to-end**

Run: `.venv/bin/pytest -v`
Expected: all tests pass — A's 12 + B's 7 + C's 10 + D's 12 + everything that was already passing.

- [ ] **Smoke test the migrations apply cleanly**

Run: `.venv/bin/python manage.py migrate`
Expected: `0005_add_message` and `0006_add_email_and_synthesis_fields` apply with no errors.

- [ ] **Smoke test `sync_attio` against a small live dataset**

In a non-production environment with `ATTIO_API_KEY` set:

Run: `.venv/bin/python manage.py sync_attio --dry-run`
Expected: prints the plan without writing — confirms the synthesis hook is short-circuited under dry-run.

Then a real run against one campaign:

Run: `.venv/bin/python manage.py sync_attio --campaign 1`
Expected: existing Stage/Status sync output plus, where applicable, synthesis-related output ("`! synthesis failed`" only if something legitimately failed; otherwise silent — synthesis logs are at `WARNING` level only).

- [ ] **Smoke test `import_connections` against a small CSV**

Run: `.venv/bin/python manage.py import_connections --csv leads/linkedin-batch4-messages.csv --handle <separate-account> --dry-run`
Expected: prints `Loaded N rows; backfill campaign: Backfill: <handle>` and a list of skip-decisions, then `[dry-run] would log in, scrape, and write the above.` — no DB writes.
