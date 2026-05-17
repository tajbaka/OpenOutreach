# Phone Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the realtime listener detects a newly-replied lead, enrich that lead's mobile phone number through a multi-provider failover waterfall and post the result to Slack.

**Architecture:** A single `EnrichmentWorker` background thread inside the daemon process claims `enrich_phone` tasks from the existing `Task` table (the outbound loop never touches them), runs an ordered provider chain (BetterContact → LeadMagic → Prospeo) until one returns a result or all fail, writes `Lead.phone`/`phone_enriched_at`, and posts a separate Slack message. The listener enqueues one task per fresh inbound reply.

**Tech Stack:** Django 6 ORM, Python `threading`, `urllib` (stdlib HTTP — no new dependency), pytest. Source spec: `docs/superpowers/specs/2026-05-17-phone-enrichment-design.md`.

**Conventions (from CLAUDE.md):** Run Python as `.venv/bin/python`. Commits are single-line, no `Co-Authored-By`. Crash on unexpected errors — `try/except` only for expected, recoverable cases. Update `CLAUDE.md` + `ARCHITECTURE.md` when code changes.

**Test command:** `.venv/bin/python -m pytest <path> -v`

---

### Task 1: Schema — add `phone` / `phone_enriched_at` to `crm.Lead`

**Files:**
- Modify: `crm/models/lead.py:16` (after the `email` field)
- Create: `crm/migrations/0012_lead_phone.py`
- Test: `tests/test_phone_enrichment_schema.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_phone_enrichment_schema.py`:

```python
"""Schema tests for the phone-enrichment Lead fields."""
import pytest

from crm.models import Lead


@pytest.mark.django_db
def test_lead_phone_defaults_blank():
    lead = Lead.objects.create(
        first_name="Ada", linkedin_url="https://www.linkedin.com/in/ada/",
    )
    assert lead.phone == ""
    assert lead.phone_enriched_at is None


@pytest.mark.django_db
def test_lead_phone_fields_persist():
    from django.utils import timezone

    now = timezone.now()
    lead = Lead.objects.create(
        first_name="Grace", linkedin_url="https://www.linkedin.com/in/grace/",
        phone="+14155550199", phone_enriched_at=now,
    )
    lead.refresh_from_db()
    assert lead.phone == "+14155550199"
    assert lead.phone_enriched_at == now
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_phone_enrichment_schema.py -v`
Expected: FAIL — `TypeError` / `FieldError` on the unknown `phone` field.

- [ ] **Step 3: Add the model fields**

In `crm/models/lead.py`, immediately after the `email` field (line 16), add:

```python
    phone = models.CharField(max_length=32, blank=True, default="")
    phone_enriched_at = models.DateTimeField(null=True, blank=True)
```

- [ ] **Step 4: Create the migration**

Create `crm/migrations/0012_lead_phone.py`:

```python
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0011_lead_icp"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="phone",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="lead",
            name="phone_enriched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
```

- [ ] **Step 5: Verify the migration matches the model**

Run: `.venv/bin/python manage.py makemigrations --check --dry-run`
Expected: `No changes detected` (the hand-written migration matches the model).

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_phone_enrichment_schema.py -v`
Expected: PASS (both tests).

- [ ] **Step 7: Commit**

```bash
git add crm/models/lead.py crm/migrations/0012_lead_phone.py tests/test_phone_enrichment_schema.py
git commit -m "Add phone and phone_enriched_at fields to crm.Lead"
```

---

### Task 2: Add the `ENRICH_PHONE` task type

**Files:**
- Modify: `linkedin/models.py:256-260` (the `Task.TaskType` enum)
- Create: `linkedin/migrations/0006_alter_task_task_type.py`
- Test: `tests/tasks/test_tasks.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/tasks/test_tasks.py`:

```python
def test_enrich_phone_task_type_exists():
    from linkedin.models import Task

    assert Task.TaskType.ENRICH_PHONE == "enrich_phone"
    assert "enrich_phone" in {choice[0] for choice in Task.TaskType.choices}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/tasks/test_tasks.py::test_enrich_phone_task_type_exists -v`
Expected: FAIL — `AttributeError: ENRICH_PHONE`.

- [ ] **Step 3: Add the enum member**

In `linkedin/models.py`, add to `Task.TaskType` (after `SWEEP_CONNECTIONS`, line 260):

```python
        ENRICH_PHONE = "enrich_phone"
```

- [ ] **Step 4: Create the migration**

Create `linkedin/migrations/0006_alter_task_task_type.py`:

```python
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("linkedin", "0005_campaign_user_fk"),
    ]

    operations = [
        migrations.AlterField(
            model_name="task",
            name="task_type",
            field=models.CharField(
                choices=[
                    ("connect", "Connect"),
                    ("check_pending", "Check Pending"),
                    ("follow_up", "Follow Up"),
                    ("sweep_connections", "Sweep Connections"),
                    ("enrich_phone", "Enrich Phone"),
                ],
                max_length=20,
            ),
        ),
    ]
```

- [ ] **Step 5: Verify the migration matches the model**

Run: `.venv/bin/python manage.py makemigrations --check --dry-run`
Expected: `No changes detected`.

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/tasks/test_tasks.py::test_enrich_phone_task_type_exists -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add linkedin/models.py linkedin/migrations/0006_alter_task_task_type.py tests/tasks/test_tasks.py
git commit -m "Add ENRICH_PHONE task type"
```

---

### Task 3: Config keys in `conf.py`

**Files:**
- Modify: `linkedin/conf.py` (after the `ENABLE_REALTIME_LISTENER` block, ~line 167)
- Test: `tests/test_conf.py` (append a test class)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_conf.py`:

```python
class TestPhoneEnrichmentConfig:
    def test_flag_defaults_off(self, monkeypatch):
        import importlib
        import linkedin.conf as conf
        monkeypatch.delenv("ENABLE_PHONE_ENRICHMENT", raising=False)
        importlib.reload(conf)
        assert conf.ENABLE_PHONE_ENRICHMENT is False
        importlib.reload(conf)

    def test_flag_truthy_strings_enable(self, monkeypatch):
        import importlib
        import linkedin.conf as conf
        for raw in ("1", "true", "YES", "on"):
            monkeypatch.setenv("ENABLE_PHONE_ENRICHMENT", raw)
            importlib.reload(conf)
            assert conf.ENABLE_PHONE_ENRICHMENT is True
        importlib.reload(conf)

    def test_tuning_constants_have_defaults(self, monkeypatch):
        import importlib
        import linkedin.conf as conf
        for var in (
            "ENRICHMENT_MAX_DURATION_SECONDS",
            "ENRICHMENT_HTTP_TIMEOUT_SECONDS",
            "BETTERCONTACT_POLL_INTERVAL_SECONDS",
        ):
            monkeypatch.delenv(var, raising=False)
        importlib.reload(conf)
        assert conf.ENRICHMENT_MAX_DURATION_SECONDS == 600
        assert conf.ENRICHMENT_HTTP_TIMEOUT_SECONDS == 5
        assert conf.BETTERCONTACT_POLL_INTERVAL_SECONDS == 15
        importlib.reload(conf)

    def test_api_keys_default_empty(self, monkeypatch):
        import importlib
        import linkedin.conf as conf
        for var in ("BETTERCONTACT_API_KEY", "LEADMAGIC_API_KEY", "PROSPEO_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        importlib.reload(conf)
        assert conf.BETTERCONTACT_API_KEY == ""
        assert conf.LEADMAGIC_API_KEY == ""
        assert conf.PROSPEO_API_KEY == ""
        importlib.reload(conf)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_conf.py::TestPhoneEnrichmentConfig -v`
Expected: FAIL — `AttributeError: ENABLE_PHONE_ENRICHMENT`.

- [ ] **Step 3: Add the config block**

In `linkedin/conf.py`, after the `LISTENER_CDP_PORT` line (~line 183), add:

```python
# ----------------------------------------------------------------------
# Phone enrichment (multi-provider waterfall — see linkedin/enrichment/)
# ----------------------------------------------------------------------
# Kill-switch for the phone-enrichment worker. When true, the daemon spawns
# a background thread that enriches a lead's mobile number (via BetterContact
# → LeadMagic → Prospeo) after the realtime listener detects an inbound reply.
# Default OFF — enrichment is an enhancement; with it disabled the daemon
# behaves exactly as before. Mirrors the existing ENABLE_* gates.
ENABLE_PHONE_ENRICHMENT = os.getenv("ENABLE_PHONE_ENRICHMENT", "false").strip().lower() in {
    "1", "true", "yes", "on",
}

# Hard cap on a single BetterContact submit→poll cycle. Past this the provider
# returns API_FAILURE and the waterfall fails over to the next provider.
ENRICHMENT_MAX_DURATION_SECONDS = int(os.getenv("ENRICHMENT_MAX_DURATION_SECONDS") or 600)

# Per-request timeout for every enrichment HTTP call.
ENRICHMENT_HTTP_TIMEOUT_SECONDS = int(os.getenv("ENRICHMENT_HTTP_TIMEOUT_SECONDS") or 5)

# Delay between BetterContact async-result polls.
BETTERCONTACT_POLL_INTERVAL_SECONDS = int(os.getenv("BETTERCONTACT_POLL_INTERVAL_SECONDS") or 15)

# Provider API keys. Empty disables that provider (it returns API_FAILURE so
# the waterfall fails over). Read here as constants — mirrors LLM_API_KEY —
# so provider modules never call os.getenv directly.
BETTERCONTACT_API_KEY = os.getenv("BETTERCONTACT_API_KEY", "").strip()
LEADMAGIC_API_KEY = os.getenv("LEADMAGIC_API_KEY", "").strip()
PROSPEO_API_KEY = os.getenv("PROSPEO_API_KEY", "").strip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_conf.py::TestPhoneEnrichmentConfig -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add linkedin/conf.py tests/test_conf.py
git commit -m "Add phone-enrichment config keys"
```

---

### Task 4: Enrichment base types + `EnrichmentError`

**Files:**
- Create: `linkedin/enrichment/__init__.py` (empty)
- Create: `linkedin/enrichment/base.py`
- Modify: `linkedin/exceptions.py` (append)
- Create: `tests/enrichment/__init__.py` (empty)
- Test: `tests/enrichment/test_base.py`

- [ ] **Step 1: Write the failing test**

Create `tests/enrichment/__init__.py` (empty file), then create `tests/enrichment/test_base.py`:

```python
"""Tests for the enrichment base types."""
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus


def test_enrichment_status_values():
    assert EnrichmentStatus.FOUND == "found"
    assert EnrichmentStatus.NOT_FOUND == "not_found"
    assert EnrichmentStatus.API_FAILURE == "api_failure"


def test_enrichment_result_defaults():
    result = EnrichmentResult(status=EnrichmentStatus.NOT_FOUND, provider="leadmagic")
    assert result.phone is None
    assert result.raw == {}


def test_enrichment_result_found():
    result = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="prospeo",
        phone="+14155550199", raw={"ok": True},
    )
    assert result.phone == "+14155550199"
    assert result.raw == {"ok": True}


def test_enrichment_error_is_exception():
    from linkedin.exceptions import EnrichmentError

    assert issubclass(EnrichmentError, Exception)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/enrichment/test_base.py -v`
Expected: FAIL — `ModuleNotFoundError: linkedin.enrichment.base`.

- [ ] **Step 3: Create the base module**

Create `linkedin/enrichment/__init__.py` as an empty file. Create `linkedin/enrichment/base.py`:

```python
"""Phone-enrichment provider protocol and result types.

A provider is any object with a `name` and an `enrich(lead, task)` method
returning an EnrichmentResult. The waterfall (waterfall.py) iterates an
ordered list of them. See docs/superpowers/specs/2026-05-17-phone-enrichment-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class EnrichmentStatus(str, Enum):
    """Outcome of one provider call.

    FOUND / NOT_FOUND are both *terminal* for the waterfall — a NOT_FOUND
    from BetterContact (a 20+ provider waterfall itself) is authoritative.
    API_FAILURE drives failover to the next provider.
    """

    FOUND = "found"
    NOT_FOUND = "not_found"
    API_FAILURE = "api_failure"


@dataclass
class EnrichmentResult:
    status: EnrichmentStatus
    provider: str
    phone: str | None = None
    raw: dict = field(default_factory=dict)


@runtime_checkable
class PhoneProvider(Protocol):
    """Structural type every provider satisfies. `task` is the enrich_phone
    Task — BetterContact reads/writes `payload.bettercontact_request_id` on
    it; other providers ignore it."""

    name: str

    def enrich(self, lead, task) -> EnrichmentResult:
        ...
```

- [ ] **Step 4: Add `EnrichmentError` to `exceptions.py`**

Append to `linkedin/exceptions.py`:

```python


class EnrichmentError(Exception):
    """A phone-enrichment provider returned a valid-JSON but unexpected
    response (missing required keys). Transport failures use HttpError and
    convert to API_FAILURE instead — this one is a real bug and propagates."""
    pass
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/enrichment/test_base.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add linkedin/enrichment/__init__.py linkedin/enrichment/base.py linkedin/exceptions.py tests/enrichment/__init__.py tests/enrichment/test_base.py
git commit -m "Add enrichment base types and EnrichmentError"
```

---

### Task 5: HTTP helper

**Files:**
- Create: `linkedin/enrichment/http.py`
- Test: `tests/enrichment/test_http.py`

- [ ] **Step 1: Write the failing test**

Create `tests/enrichment/test_http.py`:

```python
"""Tests for the enrichment urllib JSON helper."""
import io
import json
from unittest.mock import MagicMock, patch

import pytest

from linkedin.enrichment.http import HttpError, get_json, post_json


def _fake_response(payload: dict):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_post_json_returns_parsed_body():
    with patch("linkedin.enrichment.http.request.urlopen",
               return_value=_fake_response({"id": "abc"})):
        result = post_json("https://x.test/submit", payload={"q": 1}, timeout=5)
    assert result == {"id": "abc"}


def test_get_json_returns_parsed_body():
    with patch("linkedin.enrichment.http.request.urlopen",
               return_value=_fake_response({"status": "terminated"})):
        result = get_json("https://x.test/poll", timeout=5)
    assert result == {"status": "terminated"}


def test_http_error_status_raises_httperror():
    from urllib.error import HTTPError

    err = HTTPError("https://x.test", 500, "Server Error", {}, io.BytesIO(b""))
    with patch("linkedin.enrichment.http.request.urlopen", side_effect=err):
        with pytest.raises(HttpError):
            post_json("https://x.test/submit", payload={}, timeout=5)


def test_network_error_raises_httperror():
    from urllib.error import URLError

    with patch("linkedin.enrichment.http.request.urlopen",
               side_effect=URLError("connection refused")):
        with pytest.raises(HttpError):
            get_json("https://x.test/poll", timeout=5)


def test_non_json_body_raises_httperror():
    resp = MagicMock()
    resp.read.return_value = b"<html>not json</html>"
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    with patch("linkedin.enrichment.http.request.urlopen", return_value=resp):
        with pytest.raises(HttpError):
            get_json("https://x.test/poll", timeout=5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/enrichment/test_http.py -v`
Expected: FAIL — `ModuleNotFoundError: linkedin.enrichment.http`.

- [ ] **Step 3: Create the HTTP helper**

Create `linkedin/enrichment/http.py`:

```python
"""urllib JSON helper for the enrichment providers.

Mirrors linkedin/notifications/slack.py's stdlib-only HTTP approach — no new
dependency. Transport-level failures (network error, non-2xx, timeout,
non-JSON body) raise HttpError; providers catch it and convert to an
API_FAILURE EnrichmentResult, which drives waterfall failover.
"""
from __future__ import annotations

import json
from urllib import error, request


class HttpError(Exception):
    """A provider HTTP call failed at the transport layer (network error,
    non-2xx status, timeout, or a non-JSON response body)."""
    pass


def _request_json(url, *, method, headers=None, payload=None, timeout):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        raise HttpError(f"{method} {url} -> HTTP {exc.code}") from exc
    except (error.URLError, TimeoutError, OSError) as exc:
        raise HttpError(f"{method} {url} -> {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HttpError(f"{method} {url} -> non-JSON response body") from exc


def post_json(url, *, headers=None, payload=None, timeout):
    """POST a JSON body, return the parsed JSON response. Raises HttpError."""
    return _request_json(
        url, method="POST", headers=headers, payload=payload, timeout=timeout,
    )


def get_json(url, *, headers=None, timeout):
    """GET a URL, return the parsed JSON response. Raises HttpError."""
    return _request_json(url, method="GET", headers=headers, timeout=timeout)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/enrichment/test_http.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add linkedin/enrichment/http.py tests/enrichment/test_http.py
git commit -m "Add enrichment HTTP helper"
```

---

### Task 6: BetterContact provider

> **Before implementing:** the request/response field names below are the
> documented BetterContact v2 async API as of this plan. Make one real
> `curl` call with a test `BETTERCONTACT_API_KEY` and confirm the submit
> response carries `id` and the terminated poll response carries
> `data[0].contact_phone_number`. Adjust the constants in Step 3 if the live
> contract differs — the control flow does not change.

**Files:**
- Create: `linkedin/enrichment/providers/__init__.py` (empty)
- Create: `linkedin/enrichment/providers/bettercontact.py`
- Test: `tests/enrichment/test_bettercontact.py`

- [ ] **Step 1: Write the failing test**

Create `linkedin/enrichment/providers/__init__.py` (empty), then create `tests/enrichment/test_bettercontact.py`:

```python
"""Tests for the BetterContact provider."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from linkedin.enrichment.base import EnrichmentStatus
from linkedin.enrichment.http import HttpError
from linkedin.enrichment.providers.bettercontact import BetterContactProvider


def _lead(**over):
    base = dict(
        id=1, first_name="Ada", last_name="Lovelace",
        company_name="Analytical Engines",
        linkedin_url="https://www.linkedin.com/in/ada/",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _task(request_id=""):
    saved = {}
    task = SimpleNamespace(payload={"bettercontact_request_id": request_id})
    task.save = lambda **kw: saved.update(kw)
    return task


def test_missing_api_key_returns_api_failure(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "",
    )
    result = BetterContactProvider().enrich(_lead(), _task())
    assert result.status == EnrichmentStatus.API_FAILURE


def test_missing_last_name_short_circuits_to_api_failure(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "key",
    )
    with patch("linkedin.enrichment.providers.bettercontact.post_json") as mock_post:
        result = BetterContactProvider().enrich(_lead(last_name=""), _task())
    assert result.status == EnrichmentStatus.API_FAILURE
    mock_post.assert_not_called()  # no API call when required fields missing


def test_submit_then_poll_terminated_found(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "key",
    )
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_POLL_INTERVAL_SECONDS", 0,
    )
    task = _task()
    with patch("linkedin.enrichment.providers.bettercontact.post_json",
               return_value={"id": "req-123"}), \
         patch("linkedin.enrichment.providers.bettercontact.get_json",
               return_value={"status": "terminated",
                             "data": [{"contact_phone_number": "+14155550199"}]}):
        result = BetterContactProvider().enrich(_lead(), task)
    assert result.status == EnrichmentStatus.FOUND
    assert result.phone == "+14155550199"
    assert task.payload["bettercontact_request_id"] == "req-123"


def test_poll_terminated_no_phone_is_not_found(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "key",
    )
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_POLL_INTERVAL_SECONDS", 0,
    )
    with patch("linkedin.enrichment.providers.bettercontact.post_json",
               return_value={"id": "req-1"}), \
         patch("linkedin.enrichment.providers.bettercontact.get_json",
               return_value={"status": "terminated",
                             "data": [{"contact_phone_number": None}]}):
        result = BetterContactProvider().enrich(_lead(), _task())
    assert result.status == EnrichmentStatus.NOT_FOUND


def test_resume_skips_submit_when_request_id_present(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "key",
    )
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_POLL_INTERVAL_SECONDS", 0,
    )
    with patch("linkedin.enrichment.providers.bettercontact.post_json") as mock_post, \
         patch("linkedin.enrichment.providers.bettercontact.get_json",
               return_value={"status": "terminated",
                             "data": [{"contact_phone_number": "+1999"}]}):
        result = BetterContactProvider().enrich(_lead(), _task(request_id="resumed-id"))
    mock_post.assert_not_called()  # resumed — no re-submit, no re-billing
    assert result.status == EnrichmentStatus.FOUND


def test_poll_timeout_returns_api_failure(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "key",
    )
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_POLL_INTERVAL_SECONDS", 0,
    )
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.ENRICHMENT_MAX_DURATION_SECONDS", 0,
    )
    with patch("linkedin.enrichment.providers.bettercontact.post_json",
               return_value={"id": "req-1"}), \
         patch("linkedin.enrichment.providers.bettercontact.get_json",
               return_value={"status": "in_progress"}):
        result = BetterContactProvider().enrich(_lead(), _task())
    assert result.status == EnrichmentStatus.API_FAILURE


def test_http_error_returns_api_failure(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.bettercontact.BETTERCONTACT_API_KEY", "key",
    )
    with patch("linkedin.enrichment.providers.bettercontact.post_json",
               side_effect=HttpError("502")):
        result = BetterContactProvider().enrich(_lead(), _task())
    assert result.status == EnrichmentStatus.API_FAILURE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/enrichment/test_bettercontact.py -v`
Expected: FAIL — `ModuleNotFoundError: ...providers.bettercontact`.

- [ ] **Step 3: Create the provider**

Create `linkedin/enrichment/providers/bettercontact.py`:

```python
"""BetterContact phone-enrichment provider (async submit → poll).

BetterContact is itself a 20+ provider waterfall, so its NOT_FOUND is
authoritative — that is why it sits first in PROVIDER_CHAIN. Its submit
endpoint requires first + last name + company; linkedin_url is only a hint.
When the lead lacks last_name/company_name we short-circuit to API_FAILURE
(graceful failover) rather than calling the API or crashing.
"""
from __future__ import annotations

import logging
import time

from linkedin.conf import (
    BETTERCONTACT_API_KEY,
    BETTERCONTACT_POLL_INTERVAL_SECONDS,
    ENRICHMENT_HTTP_TIMEOUT_SECONDS,
    ENRICHMENT_MAX_DURATION_SECONDS,
)
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.http import HttpError, get_json, post_json
from linkedin.exceptions import EnrichmentError

logger = logging.getLogger(__name__)

_BASE = "https://app.bettercontact.rocks/api/v2"


class BetterContactProvider:
    name = "bettercontact"

    def enrich(self, lead, task) -> EnrichmentResult:
        if not BETTERCONTACT_API_KEY:
            logger.warning("BetterContact: no API key configured — API_FAILURE")
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)

        if not lead.last_name or not lead.company_name:
            logger.warning(
                "BetterContact: lead %s missing last_name/company — API_FAILURE",
                lead.id,
            )
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)

        request_id = (task.payload or {}).get("bettercontact_request_id") or ""
        try:
            if not request_id:
                request_id = self._submit(lead)
                task.payload["bettercontact_request_id"] = request_id
                task.save(update_fields=["payload"])
            return self._poll(request_id)
        except HttpError as exc:
            logger.warning("BetterContact API failure: %s", exc)
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)

    def _submit(self, lead) -> str:
        resp = post_json(
            f"{_BASE}/async?api_key={BETTERCONTACT_API_KEY}",
            payload={
                "data": [{
                    "first_name": lead.first_name,
                    "last_name": lead.last_name,
                    "company": lead.company_name,
                    "linkedin_url": lead.linkedin_url,
                }],
                "enrich_email_address": False,
                "enrich_phone_number": True,
            },
            timeout=ENRICHMENT_HTTP_TIMEOUT_SECONDS,
        )
        request_id = resp.get("id")
        if not request_id:
            raise EnrichmentError(f"BetterContact submit returned no id: {resp}")
        return str(request_id)

    def _poll(self, request_id: str) -> EnrichmentResult:
        deadline = time.monotonic() + ENRICHMENT_MAX_DURATION_SECONDS
        while True:
            resp = get_json(
                f"{_BASE}/async/{request_id}?api_key={BETTERCONTACT_API_KEY}",
                timeout=ENRICHMENT_HTTP_TIMEOUT_SECONDS,
            )
            if resp.get("status") == "terminated":
                return self._parse_terminated(resp)
            if time.monotonic() >= deadline:
                logger.warning("BetterContact poll timed out for %s", request_id)
                return EnrichmentResult(
                    status=EnrichmentStatus.API_FAILURE, provider=self.name, raw=resp,
                )
            time.sleep(BETTERCONTACT_POLL_INTERVAL_SECONDS)

    def _parse_terminated(self, resp: dict) -> EnrichmentResult:
        data = resp.get("data")
        if not isinstance(data, list) or not data:
            raise EnrichmentError(f"BetterContact terminated with no data: {resp}")
        phone = data[0].get("contact_phone_number")
        if phone:
            return EnrichmentResult(
                status=EnrichmentStatus.FOUND, provider=self.name,
                phone=str(phone), raw=resp,
            )
        return EnrichmentResult(
            status=EnrichmentStatus.NOT_FOUND, provider=self.name, raw=resp,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/enrichment/test_bettercontact.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add linkedin/enrichment/providers/__init__.py linkedin/enrichment/providers/bettercontact.py tests/enrichment/test_bettercontact.py
git commit -m "Add BetterContact enrichment provider"
```

---

### Task 7: LeadMagic provider

> **Before implementing:** confirm with one `curl` call that LeadMagic's
> `POST /mobile-finder` takes a `profile_url` body with an `X-API-Key` header
> and returns the number under `mobile_number`. Adjust Step 3 if the live
> contract differs.

**Files:**
- Create: `linkedin/enrichment/providers/leadmagic.py`
- Test: `tests/enrichment/test_leadmagic.py`

- [ ] **Step 1: Write the failing test**

Create `tests/enrichment/test_leadmagic.py`:

```python
"""Tests for the LeadMagic provider."""
from types import SimpleNamespace
from unittest.mock import patch

from linkedin.enrichment.base import EnrichmentStatus
from linkedin.enrichment.http import HttpError
from linkedin.enrichment.providers.leadmagic import LeadMagicProvider


def _lead():
    return SimpleNamespace(
        id=1, first_name="Ada", last_name="Lovelace", company_name="AE",
        linkedin_url="https://www.linkedin.com/in/ada/",
    )


def test_missing_api_key_returns_api_failure(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.leadmagic.LEADMAGIC_API_KEY", "",
    )
    result = LeadMagicProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.API_FAILURE


def test_found(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.leadmagic.LEADMAGIC_API_KEY", "key",
    )
    with patch("linkedin.enrichment.providers.leadmagic.post_json",
               return_value={"mobile_number": "+14155550199"}):
        result = LeadMagicProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.FOUND
    assert result.phone == "+14155550199"


def test_not_found(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.leadmagic.LEADMAGIC_API_KEY", "key",
    )
    with patch("linkedin.enrichment.providers.leadmagic.post_json",
               return_value={"mobile_number": None}):
        result = LeadMagicProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.NOT_FOUND


def test_http_error_returns_api_failure(monkeypatch):
    monkeypatch.setattr(
        "linkedin.enrichment.providers.leadmagic.LEADMAGIC_API_KEY", "key",
    )
    with patch("linkedin.enrichment.providers.leadmagic.post_json",
               side_effect=HttpError("500")):
        result = LeadMagicProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.API_FAILURE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/enrichment/test_leadmagic.py -v`
Expected: FAIL — `ModuleNotFoundError: ...providers.leadmagic`.

- [ ] **Step 3: Create the provider**

Create `linkedin/enrichment/providers/leadmagic.py`:

```python
"""LeadMagic phone-enrichment provider (synchronous, LinkedIn-URL native)."""
from __future__ import annotations

import logging

from linkedin.conf import ENRICHMENT_HTTP_TIMEOUT_SECONDS, LEADMAGIC_API_KEY
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.http import HttpError, post_json

logger = logging.getLogger(__name__)

_URL = "https://api.leadmagic.io/mobile-finder"


class LeadMagicProvider:
    name = "leadmagic"

    def enrich(self, lead, task) -> EnrichmentResult:
        if not LEADMAGIC_API_KEY:
            logger.warning("LeadMagic: no API key configured — API_FAILURE")
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)
        try:
            resp = post_json(
                _URL,
                headers={"X-API-Key": LEADMAGIC_API_KEY},
                payload={"profile_url": lead.linkedin_url},
                timeout=ENRICHMENT_HTTP_TIMEOUT_SECONDS,
            )
        except HttpError as exc:
            logger.warning("LeadMagic API failure: %s", exc)
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)

        phone = resp.get("mobile_number")
        if phone:
            return EnrichmentResult(
                status=EnrichmentStatus.FOUND, provider=self.name,
                phone=str(phone), raw=resp,
            )
        return EnrichmentResult(
            status=EnrichmentStatus.NOT_FOUND, provider=self.name, raw=resp,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/enrichment/test_leadmagic.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add linkedin/enrichment/providers/leadmagic.py tests/enrichment/test_leadmagic.py
git commit -m "Add LeadMagic enrichment provider"
```

---

### Task 8: Prospeo provider

> **Before implementing:** the verification pass found Prospeo retired
> `POST /mobile-finder` on 2026-03-01. The current endpoint is
> `POST /enrich-person` with an `X-KEY` header, an
> `{"only_verified_mobile": true, "data": {"linkedin_url": ...}}` body, and
> the mobile number at `person.mobile.mobile`. Confirm with one `curl` call
> and adjust Step 3 if the live contract differs.

**Files:**
- Create: `linkedin/enrichment/providers/prospeo.py`
- Test: `tests/enrichment/test_prospeo.py`

- [ ] **Step 1: Write the failing test**

Create `tests/enrichment/test_prospeo.py`:

```python
"""Tests for the Prospeo provider."""
from types import SimpleNamespace
from unittest.mock import patch

from linkedin.enrichment.base import EnrichmentStatus
from linkedin.enrichment.http import HttpError
from linkedin.enrichment.providers.prospeo import ProspeoProvider


def _lead():
    return SimpleNamespace(
        id=1, first_name="Ada", last_name="Lovelace", company_name="AE",
        linkedin_url="https://www.linkedin.com/in/ada/",
    )


def test_missing_api_key_returns_api_failure(monkeypatch):
    monkeypatch.setattr("linkedin.enrichment.providers.prospeo.PROSPEO_API_KEY", "")
    result = ProspeoProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.API_FAILURE


def test_found(monkeypatch):
    monkeypatch.setattr("linkedin.enrichment.providers.prospeo.PROSPEO_API_KEY", "key")
    with patch("linkedin.enrichment.providers.prospeo.post_json",
               return_value={"error": False,
                             "response": {"person": {"mobile": {"mobile": "+14155550199"}}}}):
        result = ProspeoProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.FOUND
    assert result.phone == "+14155550199"


def test_not_found_when_no_mobile(monkeypatch):
    monkeypatch.setattr("linkedin.enrichment.providers.prospeo.PROSPEO_API_KEY", "key")
    with patch("linkedin.enrichment.providers.prospeo.post_json",
               return_value={"error": False,
                             "response": {"person": {"mobile": None}}}):
        result = ProspeoProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.NOT_FOUND


def test_error_flag_returns_api_failure(monkeypatch):
    monkeypatch.setattr("linkedin.enrichment.providers.prospeo.PROSPEO_API_KEY", "key")
    with patch("linkedin.enrichment.providers.prospeo.post_json",
               return_value={"error": True, "message": "rate limited"}):
        result = ProspeoProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.API_FAILURE


def test_http_error_returns_api_failure(monkeypatch):
    monkeypatch.setattr("linkedin.enrichment.providers.prospeo.PROSPEO_API_KEY", "key")
    with patch("linkedin.enrichment.providers.prospeo.post_json",
               side_effect=HttpError("503")):
        result = ProspeoProvider().enrich(_lead(), None)
    assert result.status == EnrichmentStatus.API_FAILURE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/enrichment/test_prospeo.py -v`
Expected: FAIL — `ModuleNotFoundError: ...providers.prospeo`.

- [ ] **Step 3: Create the provider**

Create `linkedin/enrichment/providers/prospeo.py`:

```python
"""Prospeo phone-enrichment provider (synchronous — last resort in the chain).

Uses POST /enrich-person; Prospeo retired the older /mobile-finder endpoint
on 2026-03-01.
"""
from __future__ import annotations

import logging

from linkedin.conf import ENRICHMENT_HTTP_TIMEOUT_SECONDS, PROSPEO_API_KEY
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.http import HttpError, post_json

logger = logging.getLogger(__name__)

_URL = "https://api.prospeo.io/enrich-person"


class ProspeoProvider:
    name = "prospeo"

    def enrich(self, lead, task) -> EnrichmentResult:
        if not PROSPEO_API_KEY:
            logger.warning("Prospeo: no API key configured — API_FAILURE")
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)
        try:
            resp = post_json(
                _URL,
                headers={"X-KEY": PROSPEO_API_KEY},
                payload={
                    "only_verified_mobile": True,
                    "data": {"linkedin_url": lead.linkedin_url},
                },
                timeout=ENRICHMENT_HTTP_TIMEOUT_SECONDS,
            )
        except HttpError as exc:
            logger.warning("Prospeo API failure: %s", exc)
            return EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider=self.name)

        if resp.get("error"):
            logger.warning("Prospeo returned error flag: %s", resp.get("message"))
            return EnrichmentResult(
                status=EnrichmentStatus.API_FAILURE, provider=self.name, raw=resp,
            )

        person = (resp.get("response") or {}).get("person") or {}
        mobile = person.get("mobile") or {}
        phone = mobile.get("mobile")
        if phone:
            return EnrichmentResult(
                status=EnrichmentStatus.FOUND, provider=self.name,
                phone=str(phone), raw=resp,
            )
        return EnrichmentResult(
            status=EnrichmentStatus.NOT_FOUND, provider=self.name, raw=resp,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/enrichment/test_prospeo.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add linkedin/enrichment/providers/prospeo.py tests/enrichment/test_prospeo.py
git commit -m "Add Prospeo enrichment provider"
```

---

### Task 9: Waterfall orchestrator

**Files:**
- Create: `linkedin/enrichment/waterfall.py`
- Test: `tests/enrichment/test_waterfall.py`

- [ ] **Step 1: Write the failing test**

Create `tests/enrichment/test_waterfall.py`:

```python
"""Tests for run_waterfall escalation logic."""
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.waterfall import run_waterfall


class _FakeProvider:
    def __init__(self, name, status, phone=None):
        self.name = name
        self._status = status
        self._phone = phone
        self.called = False

    def enrich(self, lead, task):
        self.called = True
        return EnrichmentResult(status=self._status, provider=self.name, phone=self._phone)


def test_found_stops_chain():
    p1 = _FakeProvider("a", EnrichmentStatus.FOUND, phone="+1")
    p2 = _FakeProvider("b", EnrichmentStatus.FOUND, phone="+2")
    result = run_waterfall(None, None, chain=[p1, p2])
    assert result.status == EnrichmentStatus.FOUND
    assert result.provider == "a"
    assert p2.called is False  # short-circuited


def test_not_found_stops_chain_without_escalating():
    p1 = _FakeProvider("a", EnrichmentStatus.NOT_FOUND)
    p2 = _FakeProvider("b", EnrichmentStatus.FOUND, phone="+2")
    result = run_waterfall(None, None, chain=[p1, p2])
    assert result.status == EnrichmentStatus.NOT_FOUND
    assert p2.called is False  # NOT_FOUND is authoritative — no escalation


def test_api_failure_escalates_to_next():
    p1 = _FakeProvider("a", EnrichmentStatus.API_FAILURE)
    p2 = _FakeProvider("b", EnrichmentStatus.FOUND, phone="+2")
    result = run_waterfall(None, None, chain=[p1, p2])
    assert result.status == EnrichmentStatus.FOUND
    assert result.provider == "b"
    assert p1.called is True


def test_all_failed_returns_last_api_failure():
    p1 = _FakeProvider("a", EnrichmentStatus.API_FAILURE)
    p2 = _FakeProvider("b", EnrichmentStatus.API_FAILURE)
    result = run_waterfall(None, None, chain=[p1, p2])
    assert result.status == EnrichmentStatus.API_FAILURE
    assert result.provider == "b"


def test_default_chain_has_three_providers():
    from linkedin.enrichment.waterfall import PROVIDER_CHAIN

    assert [p.name for p in PROVIDER_CHAIN] == ["bettercontact", "leadmagic", "prospeo"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/enrichment/test_waterfall.py -v`
Expected: FAIL — `ModuleNotFoundError: linkedin.enrichment.waterfall`.

- [ ] **Step 3: Create the waterfall**

Create `linkedin/enrichment/waterfall.py`:

```python
"""Phone-enrichment provider waterfall.

Iterates PROVIDER_CHAIN in order. FOUND or NOT_FOUND is terminal — return
immediately (a NOT_FOUND from BetterContact, itself a 20+ provider waterfall,
is authoritative). API_FAILURE escalates to the next provider. If every
provider fails, the last API_FAILURE result is returned.
"""
from __future__ import annotations

import logging

from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.providers.bettercontact import BetterContactProvider
from linkedin.enrichment.providers.leadmagic import LeadMagicProvider
from linkedin.enrichment.providers.prospeo import ProspeoProvider

logger = logging.getLogger(__name__)

# Order matters — see docs/superpowers/specs/2026-05-17-phone-enrichment-design.md.
# To add a provider: implement the PhoneProvider protocol and append it here.
PROVIDER_CHAIN = [
    BetterContactProvider(),
    LeadMagicProvider(),
    ProspeoProvider(),
]


def run_waterfall(lead, task, chain=None) -> EnrichmentResult:
    """Run the provider chain for one lead. `chain` is injectable for tests."""
    providers = chain if chain is not None else PROVIDER_CHAIN
    last = EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider="none")
    for provider in providers:
        result = provider.enrich(lead, task)
        if result.status in (EnrichmentStatus.FOUND, EnrichmentStatus.NOT_FOUND):
            logger.info(
                "Enrichment %s via %s", result.status.value, provider.name,
            )
            return result
        logger.warning("Provider %s failed — escalating", provider.name)
        last = result
    logger.warning("All enrichment providers failed")
    return last
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/enrichment/test_waterfall.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add linkedin/enrichment/waterfall.py tests/enrichment/test_waterfall.py
git commit -m "Add enrichment provider waterfall"
```

---

### Task 10: `notify_phone_enriched` Slack notification

**Files:**
- Modify: `linkedin/notifications/slack.py` (append a function; update the module docstring)
- Test: `tests/test_slack_notify.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_slack_notify.py`:

```python
class TestNotifyPhoneEnriched:
    def _lead(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            first_name="Ada", last_name="Lovelace", company_name="Analytical Engines",
            linkedin_url="https://www.linkedin.com/in/ada/", public_identifier="ada",
        )

    def test_noop_when_webhook_unset(self, monkeypatch):
        from linkedin.notifications.slack import notify_phone_enriched
        from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus

        monkeypatch.setattr("linkedin.notifications.slack.SLACK_WEBHOOK_URL", "")
        # urlopen must never be called when the webhook is unset.
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            notify_phone_enriched(
                lead=self._lead(),
                result=EnrichmentResult(
                    status=EnrichmentStatus.FOUND, provider="leadmagic", phone="+1",
                ),
            )
        mock_open.assert_not_called()

    def test_found_posts_phone_and_provider(self, monkeypatch):
        from linkedin.notifications.slack import notify_phone_enriched
        from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus

        monkeypatch.setattr(
            "linkedin.notifications.slack.SLACK_WEBHOOK_URL", "https://hooks.test/x",
        )
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            notify_phone_enriched(
                lead=self._lead(),
                result=EnrichmentResult(
                    status=EnrichmentStatus.FOUND, provider="leadmagic",
                    phone="+14155550199",
                ),
            )
        body = mock_open.call_args[0][0].data.decode("utf-8")
        assert "+14155550199" in body
        assert "leadmagic" in body

    def test_not_found_posts_no_number(self, monkeypatch):
        from linkedin.notifications.slack import notify_phone_enriched
        from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus

        monkeypatch.setattr(
            "linkedin.notifications.slack.SLACK_WEBHOOK_URL", "https://hooks.test/x",
        )
        with patch("linkedin.notifications.slack.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            notify_phone_enriched(
                lead=self._lead(),
                result=EnrichmentResult(
                    status=EnrichmentStatus.NOT_FOUND, provider="prospeo",
                ),
            )
        body = mock_open.call_args[0][0].data.decode("utf-8")
        assert "No phone number found" in body
```

If `tests/test_slack_notify.py` has no `from unittest.mock import patch` at the top, add it.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_slack_notify.py::TestNotifyPhoneEnriched -v`
Expected: FAIL — `ImportError: cannot import name 'notify_phone_enriched'`.

- [ ] **Step 3: Add the function**

In `linkedin/notifications/slack.py`, append after `notify_message_received` (before `notify_error`, ~line 185):

```python
def notify_phone_enriched(*, lead, result) -> None:
    """Post a 'phone enriched' notification. No-op if SLACK_WEBHOOK_URL unset.

    `result` is an enrichment EnrichmentResult. A FOUND result renders the
    number and the winning provider; a NOT_FOUND renders 'no number found'.
    API_FAILURE never reaches here — the enrichment worker marks the task
    failed without notifying.
    """
    if not SLACK_WEBHOOK_URL:
        return

    full_name = (
        f"{lead.first_name or ''} {lead.last_name or ''}".strip()
        or lead.public_identifier
        or "Unknown lead"
    )
    profile_url = lead.linkedin_url or ""
    name_md = f"<{profile_url}|{full_name}>" if profile_url else full_name

    if result.phone:
        action_line = f":telephone_receiver: Phone found for *{name_md}*: `{result.phone}`"
        fallback = f":telephone_receiver: Phone found for {full_name}: {result.phone}"
    else:
        action_line = f":telephone_receiver: No phone number found for *{name_md}*"
        fallback = f":telephone_receiver: No phone number found for {full_name}"

    elements: list[dict] = []
    if lead.company_name:
        elements.append({"type": "mrkdwn", "text": f"*Company:* {lead.company_name}"})
    elements.append({"type": "mrkdwn", "text": f"*Provider:* {result.provider}"})

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": action_line}},
        {"type": "context", "elements": elements},
    ]
    payload = {"text": fallback, "blocks": blocks}

    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        SLACK_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning(
                    "Slack phone-enriched webhook returned %d for %s",
                    resp.status, full_name,
                )
    except (URLError, TimeoutError) as e:
        logger.warning("Slack phone-enriched webhook failed for %s: %s", full_name, e)
```

Also update the module docstring's numbered list of surfaces to add a 4th: `notify_phone_enriched` — fires when the enrichment worker finishes a lead.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_slack_notify.py::TestNotifyPhoneEnriched -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add linkedin/notifications/slack.py tests/test_slack_notify.py
git commit -m "Add notify_phone_enriched Slack notification"
```

---

### Task 11: `handle_enrich_phone` task handler

**Files:**
- Create: `linkedin/tasks/enrich_phone.py`
- Test: `tests/tasks/test_enrich_phone.py`

- [ ] **Step 1: Write the failing test**

Create `tests/tasks/test_enrich_phone.py`:

```python
"""Tests for the enrich_phone task handler."""
from unittest.mock import patch

import pytest
from django.utils import timezone

from crm.models import Lead
from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.models import Task
from linkedin.tasks.enrich_phone import handle_enrich_phone


def _lead(**over):
    base = dict(
        first_name="Ada", last_name="Lovelace", company_name="AE",
        linkedin_url="https://www.linkedin.com/in/ada/",
    )
    base.update(over)
    return Lead.objects.create(**base)


def _task(lead):
    return Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        scheduled_at=timezone.now(),
        payload={"lead_id": lead.id, "bettercontact_request_id": ""},
    )


@pytest.mark.django_db
def test_found_writes_phone_and_stamps():
    lead = _lead()
    task = _task(lead)
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="leadmagic", phone="+14155550199",
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=found), \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched") as mock_notify:
        result = handle_enrich_phone(task)
    lead.refresh_from_db()
    assert lead.phone == "+14155550199"
    assert lead.phone_enriched_at is not None
    assert result.status == EnrichmentStatus.FOUND
    mock_notify.assert_called_once()


@pytest.mark.django_db
def test_not_found_stamps_but_leaves_phone_empty():
    lead = _lead()
    task = _task(lead)
    nf = EnrichmentResult(status=EnrichmentStatus.NOT_FOUND, provider="prospeo")
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=nf), \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched") as mock_notify:
        handle_enrich_phone(task)
    lead.refresh_from_db()
    assert lead.phone == ""
    assert lead.phone_enriched_at is not None
    mock_notify.assert_called_once()


@pytest.mark.django_db
def test_api_failure_does_not_stamp_and_does_not_notify():
    lead = _lead()
    task = _task(lead)
    fail = EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider="prospeo")
    with patch("linkedin.tasks.enrich_phone.run_waterfall", return_value=fail), \
         patch("linkedin.tasks.enrich_phone.notify_phone_enriched") as mock_notify:
        result = handle_enrich_phone(task)
    lead.refresh_from_db()
    assert lead.phone == ""
    assert lead.phone_enriched_at is None  # next reply re-attempts
    assert result.status == EnrichmentStatus.API_FAILURE
    mock_notify.assert_not_called()


@pytest.mark.django_db
def test_already_enriched_lead_is_skipped():
    lead = _lead(phone_enriched_at=timezone.now())
    task = _task(lead)
    with patch("linkedin.tasks.enrich_phone.run_waterfall") as mock_wf:
        result = handle_enrich_phone(task)
    assert result is None
    mock_wf.assert_not_called()


@pytest.mark.django_db
def test_disqualified_lead_is_skipped():
    lead = _lead(disqualified=True)
    task = _task(lead)
    with patch("linkedin.tasks.enrich_phone.run_waterfall") as mock_wf:
        result = handle_enrich_phone(task)
    assert result is None
    mock_wf.assert_not_called()


@pytest.mark.django_db
def test_missing_lead_is_skipped():
    task = Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        scheduled_at=timezone.now(),
        payload={"lead_id": 999999, "bettercontact_request_id": ""},
    )
    with patch("linkedin.tasks.enrich_phone.run_waterfall") as mock_wf:
        result = handle_enrich_phone(task)
    assert result is None
    mock_wf.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/tasks/test_enrich_phone.py -v`
Expected: FAIL — `ModuleNotFoundError: linkedin.tasks.enrich_phone`.

- [ ] **Step 3: Create the handler**

Create `linkedin/tasks/enrich_phone.py`:

```python
"""enrich_phone task handler — runs the phone-enrichment waterfall.

Unlike the daemon-loop handlers (handle_connect / handle_follow_up) this
takes NO `session` argument: it runs in the EnrichmentWorker thread, does
HTTP only, and never touches the browser. The EnrichmentWorker sets the
task's final status from the returned EnrichmentResult.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.waterfall import run_waterfall
from linkedin.notifications.slack import notify_phone_enriched

logger = logging.getLogger(__name__)


def handle_enrich_phone(task) -> EnrichmentResult | None:
    """Enrich one lead's phone number.

    Returns the waterfall EnrichmentResult, or None when the task was a
    no-op skip (lead missing / already enriched / disqualified). The
    EnrichmentWorker treats None and FOUND/NOT_FOUND as `completed`, and
    API_FAILURE as `failed`.
    """
    from crm.models import Lead

    lead_id = task.payload.get("lead_id")
    lead = Lead.objects.filter(pk=lead_id).first()
    if lead is None:
        logger.warning("enrich_phone: lead %s not found — skipping", lead_id)
        return None
    if lead.phone_enriched_at is not None:
        logger.info("enrich_phone: lead %s already enriched — skipping", lead_id)
        return None
    if lead.disqualified:
        logger.info("enrich_phone: lead %s disqualified — skipping", lead_id)
        return None

    result = run_waterfall(lead, task)

    if result.status == EnrichmentStatus.FOUND:
        lead.phone = result.phone or ""
        lead.phone_enriched_at = timezone.now()
        lead.save(update_fields=["phone", "phone_enriched_at"])
        notify_phone_enriched(lead=lead, result=result)
    elif result.status == EnrichmentStatus.NOT_FOUND:
        # Stamp so we never re-enrich a confirmed empty result.
        lead.phone_enriched_at = timezone.now()
        lead.save(update_fields=["phone_enriched_at"])
        notify_phone_enriched(lead=lead, result=result)
    else:  # API_FAILURE — do NOT stamp; the lead's next reply re-attempts.
        logger.warning(
            "enrich_phone: all providers failed for lead %s — not stamping", lead_id,
        )

    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/tasks/test_enrich_phone.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add linkedin/tasks/enrich_phone.py tests/tasks/test_enrich_phone.py
git commit -m "Add enrich_phone task handler"
```

---

### Task 12: `next_enrichment` queryset method + exclude `ENRICH_PHONE` from the outbound loop

**Files:**
- Modify: `linkedin/models.py` (`TaskQuerySet.claim_next`, `seconds_to_next`; add `next_enrichment`)
- Test: `tests/tasks/test_claim_filter.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/tasks/test_claim_filter.py`:

```python
@pytest.mark.django_db
def test_claim_next_excludes_enrich_phone():
    """The outbound loop must never claim an enrichment task — that is the
    EnrichmentWorker's job."""
    enrich = Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        status=Task.Status.PENDING,
        scheduled_at=dj_tz.now() - timedelta(seconds=60),
        payload={"lead_id": 1},
    )
    # Even with nothing else queued and the task overdue, claim_next skips it.
    assert Task.objects.claim_next() is None
    assert Task.objects.claim_next(operator="Arian") is None
    # And it does not dictate the outbound loop's sleep.
    assert Task.objects.seconds_to_next() is None
    # But the dedicated query finds it.
    assert Task.objects.next_enrichment().pk == enrich.pk


@pytest.mark.django_db
def test_next_enrichment_returns_none_when_not_due():
    Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        status=Task.Status.PENDING,
        scheduled_at=dj_tz.now() + timedelta(hours=1),
        payload={"lead_id": 1},
    )
    assert Task.objects.next_enrichment() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/tasks/test_claim_filter.py::test_claim_next_excludes_enrich_phone -v`
Expected: FAIL — `claim_next` returns the enrichment task / `AttributeError: next_enrichment`.

- [ ] **Step 3: Update `TaskQuerySet`**

In `linkedin/models.py`, in `TaskQuerySet.claim_next`, change the `qs = self.due()` line (line 219) to:

```python
        qs = self.due().exclude(task_type=Task.TaskType.ENRICH_PHONE)
```

In `seconds_to_next`, change the `qs = self.pending().only(...)` line (line 240) to:

```python
        qs = self.pending().exclude(task_type=Task.TaskType.ENRICH_PHONE).only(
            "scheduled_at", "task_type", "payload",
        )
```

Add a new method to `TaskQuerySet` (after `seconds_to_next`, before the closing of the class, ~line 253):

```python
    def next_enrichment(self) -> "Task | None":
        """The next due ENRICH_PHONE task — the EnrichmentWorker's claim query.

        Separate from `claim_next` (which excludes ENRICH_PHONE) so the
        outbound task loop and the single enrichment worker thread never
        compete for the same row. NOTE: this is a plain ordered read, not a
        locking claim — safe only because exactly one worker thread calls it.
        """
        return self.due().filter(task_type=Task.TaskType.ENRICH_PHONE).first()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/tasks/test_claim_filter.py -v`
Expected: PASS (all — existing tests plus the 2 new ones).

- [ ] **Step 5: Commit**

```bash
git add linkedin/models.py tests/tasks/test_claim_filter.py
git commit -m "Add next_enrichment query and exclude ENRICH_PHONE from outbound loop"
```

---

### Task 13: `EnrichmentWorker` thread

**Files:**
- Create: `linkedin/enrichment/worker.py`
- Test: `tests/enrichment/test_worker.py`

- [ ] **Step 1: Write the failing test**

Create `tests/enrichment/test_worker.py`:

```python
"""Tests for the EnrichmentWorker.

The threaded loop is not exercised under the `db` fixture (a worker thread
would not see the test transaction). Logic is tested via `_run_once`, which
is pure DB + HTTP. The start/stop lifecycle is smoke-tested with `_run_once`
patched out so the thread never touches the DB.
"""
from unittest.mock import patch

import pytest
from django.utils import timezone

from linkedin.enrichment.base import EnrichmentResult, EnrichmentStatus
from linkedin.enrichment.worker import EnrichmentWorker
from linkedin.models import Task


def _enrich_task(status=Task.Status.PENDING, scheduled_offset_s=-1):
    from datetime import timedelta

    return Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        status=status,
        scheduled_at=timezone.now() + timedelta(seconds=scheduled_offset_s),
        payload={"lead_id": 1, "bettercontact_request_id": ""},
    )


@pytest.mark.django_db
def test_run_once_no_task_returns_false():
    assert EnrichmentWorker()._run_once() is False


@pytest.mark.django_db
def test_run_once_found_marks_task_completed():
    task = _enrich_task()
    found = EnrichmentResult(
        status=EnrichmentStatus.FOUND, provider="leadmagic", phone="+1",
    )
    with patch("linkedin.enrichment.worker.handle_enrich_phone", return_value=found):
        handled = EnrichmentWorker()._run_once()
    task.refresh_from_db()
    assert handled is True
    assert task.status == Task.Status.COMPLETED


@pytest.mark.django_db
def test_run_once_api_failure_marks_task_failed():
    task = _enrich_task()
    fail = EnrichmentResult(status=EnrichmentStatus.API_FAILURE, provider="prospeo")
    with patch("linkedin.enrichment.worker.handle_enrich_phone", return_value=fail):
        EnrichmentWorker()._run_once()
    task.refresh_from_db()
    assert task.status == Task.Status.FAILED


@pytest.mark.django_db
def test_run_once_skip_result_none_marks_completed():
    task = _enrich_task()
    with patch("linkedin.enrichment.worker.handle_enrich_phone", return_value=None):
        EnrichmentWorker()._run_once()
    task.refresh_from_db()
    assert task.status == Task.Status.COMPLETED


@pytest.mark.django_db
def test_run_once_handler_exception_marks_failed_and_notifies():
    task = _enrich_task()
    with patch("linkedin.enrichment.worker.handle_enrich_phone",
               side_effect=RuntimeError("boom")), \
         patch("linkedin.enrichment.worker.notify_error") as mock_err:
        EnrichmentWorker()._run_once()
    task.refresh_from_db()
    assert task.status == Task.Status.FAILED
    mock_err.assert_called_once()


@pytest.mark.django_db
def test_reclaim_stale_resets_running_enrich_tasks():
    task = _enrich_task(status=Task.Status.RUNNING)
    EnrichmentWorker()._reclaim_stale()
    task.refresh_from_db()
    assert task.status == Task.Status.PENDING


@pytest.mark.django_db
def test_start_stop_lifecycle_does_not_hang():
    worker = EnrichmentWorker(poll_interval=0.01)
    with patch.object(EnrichmentWorker, "_run_once", return_value=False):
        worker.start()
        assert worker._thread is not None
        worker.stop()
        assert worker._thread is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/enrichment/test_worker.py -v`
Expected: FAIL — `ModuleNotFoundError: linkedin.enrichment.worker`.

- [ ] **Step 3: Create the worker**

Create `linkedin/enrichment/worker.py`:

```python
"""EnrichmentWorker — the daemon's phone-enrichment task loop.

Runs as a SINGLE background thread spawned by run_daemon. Claims
ENRICH_PHONE tasks (the outbound loop excludes them), runs the waterfall via
handle_enrich_phone, and sets each task's final status.

Single-threaded by design: Task.objects.next_enrichment is a plain ordered
read, not a locking claim — a second worker would double-process tasks (and
double-bill providers). Do not scale this without select_for_update.

Crash recovery: the daemon has no clean SIGTERM shutdown, so a killed worker
leaves its task RUNNING. `start()` reclaims stale RUNNING enrich_phone tasks
back to PENDING — that, plus the persisted bettercontact_request_id, is the
real crash-safety net.
"""
from __future__ import annotations

import logging
import threading
import traceback

logger = logging.getLogger(__name__)

from linkedin.notifications.slack import notify_error


class EnrichmentWorker:
    def __init__(self, poll_interval: float = 10.0):
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Reclaim stale tasks, then spawn the worker thread. Idempotent."""
        if self._thread is not None:
            return
        self._reclaim_stale()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="enrichment-worker", daemon=True,
        )
        self._thread.start()
        logger.info("Enrichment worker started")

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop to exit and join the thread. Idempotent, never raises."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
            logger.info("Enrichment worker stopped")

    def _reclaim_stale(self) -> None:
        from linkedin.models import Task

        reclaimed = Task.objects.filter(
            task_type=Task.TaskType.ENRICH_PHONE,
            status=Task.Status.RUNNING,
        ).update(status=Task.Status.PENDING)
        if reclaimed:
            logger.info(
                "Enrichment worker reclaimed %d stale running task(s)", reclaimed,
            )

    def _run(self) -> None:
        from django.db import connection

        while not self._stop.is_set():
            # Connections are thread-local. close() is thread-scoped (unlike
            # connections.close_all(), which would also close the daemon main
            # thread's connection). Recycle so a Neon idle-timeout drop is
            # never reused.
            connection.close()
            handled = self._run_once()
            if not handled:
                self._stop.wait(self._poll_interval)

    def _run_once(self) -> bool:
        """Claim and process one enrichment task. Returns True if one ran.

        Pure DB + HTTP — safe to call directly from tests (no thread, no
        connection recycling)."""
        from linkedin.enrichment.base import EnrichmentStatus
        from linkedin.models import Task
        from linkedin.tasks.enrich_phone import handle_enrich_phone

        task = Task.objects.next_enrichment()
        if task is None:
            return False

        task.mark_running()
        try:
            result = handle_enrich_phone(task)
        except Exception as exc:
            logger.exception("enrich_phone task %s failed", task.id)
            task.mark_failed(traceback.format_exc())
            notify_error(
                "daemon:enrich_phone", exc,
                context={"task_id": task.id, "payload": task.payload},
            )
            return True

        if result is not None and result.status == EnrichmentStatus.API_FAILURE:
            task.mark_failed(
                f"All enrichment providers failed (last={result.provider})",
            )
        else:
            task.mark_completed()
        return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/enrichment/test_worker.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add linkedin/enrichment/worker.py tests/enrichment/test_worker.py
git commit -m "Add EnrichmentWorker thread"
```

---

### Task 14: Daemon wiring — spawn the worker, guard `heal_tasks` and the exit path

**Files:**
- Modify: `linkedin/daemon.py` (`heal_tasks` stale-reset; `run_daemon` worker spawn + exit guard)
- Test: `tests/test_daemon_resilience.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_daemon_resilience.py`:

```python
def test_heal_tasks_does_not_reset_running_enrich_phone(fake_session):
    """heal_tasks resets all stale RUNNING tasks to PENDING — but it must
    leave ENRICH_PHONE tasks alone, because the EnrichmentWorker (which
    spawns after heal_tasks) owns reclaiming those itself. Resetting them
    here would yank an in-flight enrichment away from the worker."""
    from django.utils import timezone

    from linkedin.daemon import heal_tasks
    from linkedin.models import Task

    enrich = Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        payload={"lead_id": 1},
    )
    other = Task.objects.create(
        task_type=Task.TaskType.CONNECT,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        payload={"campaign_id": fake_session.campaign.pk},
    )

    heal_tasks(fake_session)

    enrich.refresh_from_db()
    other.refresh_from_db()
    assert enrich.status == Task.Status.RUNNING   # left for the worker
    assert other.status == Task.Status.PENDING    # reset as before
```

`test_daemon_resilience.py` already uses the `fake_session` fixture for other tests; if its imports differ, match the file's existing style.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_daemon_resilience.py::test_heal_tasks_does_not_reset_running_enrich_phone -v`
Expected: FAIL — `enrich.status` is `PENDING` (the global reset caught it).

- [ ] **Step 3: Guard the `heal_tasks` stale reset**

In `linkedin/daemon.py`, in `heal_tasks`, change the stale-recovery block (lines 192-196):

```python
    # 1. Recover stale running tasks. ENRICH_PHONE is excluded — the
    # EnrichmentWorker (spawned after heal_tasks) reclaims its own stale
    # RUNNING tasks at start(); resetting them here would race the worker.
    stale_count = (
        Task.objects.filter(status=Task.Status.RUNNING)
        .exclude(task_type=Task.TaskType.ENRICH_PHONE)
        .update(status=Task.Status.PENDING)
    )
    if stale_count:
        logger.info("Recovered %d stale running tasks", stale_count)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_daemon_resilience.py::test_heal_tasks_does_not_reset_running_enrich_phone -v`
Expected: PASS.

- [ ] **Step 5: Wire the worker into `run_daemon`**

In `linkedin/daemon.py`:

(a) Add `ENABLE_PHONE_ENRICHMENT` to the `from linkedin.conf import (...)` block (lines 16-28):

```python
    ENABLE_PHONE_ENRICHMENT,
```

(b) In `run_daemon`, just after the listener supervisor is created (lines 441-442):

```python
    # Realtime listener supervisor — owns the listener child process.
    from linkedin.realtime.supervisor import ListenerSupervisor
    listener_supervisor = ListenerSupervisor()

    # Phone-enrichment worker — a background thread claiming enrich_phone
    # tasks. HTTP-only, so (unlike the listener) it is NOT gated on active
    # hours; it runs whenever the daemon is up.
    from linkedin.enrichment.worker import EnrichmentWorker
    enrichment_worker = EnrichmentWorker()
    if ENABLE_PHONE_ENRICHMENT:
        enrichment_worker.start()
```

(c) In the `while True:` loop, the queue-empty branch (lines 467-472) — guard the `return` so the daemon does not exit while enrichment work is outstanding:

```python
        task = Task.objects.claim_next(operator=our_operator)
        if task is None:
            wait = Task.objects.seconds_to_next(operator=our_operator)
            if wait is None:
                if ENABLE_PHONE_ENRICHMENT and Task.objects.filter(
                    task_type=Task.TaskType.ENRICH_PHONE,
                    status__in=[Task.Status.PENDING, Task.Status.RUNNING],
                ).exists():
                    logger.info("Outbound queue empty — waiting on enrichment worker")
                    connections.close_all()
                    time.sleep(LISTENER_PUMP_SLICE_SECONDS)
                    continue
                logger.info("Queue empty — nothing to do")
                listener_supervisor.stop()
                enrichment_worker.stop()
                return
```

`LISTENER_PUMP_SLICE_SECONDS` is already imported in `daemon.py` (it is in the existing `conf` import block — verify; if not, add it alongside `ENABLE_PHONE_ENRICHMENT`).

- [ ] **Step 6: Run the full daemon test file to verify nothing regressed**

Run: `.venv/bin/python -m pytest tests/test_daemon_resilience.py -v`
Expected: PASS (all).

- [ ] **Step 7: Commit**

```bash
git add linkedin/daemon.py tests/test_daemon_resilience.py
git commit -m "Wire EnrichmentWorker into the daemon and guard heal_tasks"
```

---

### Task 15: Listener enqueues `enrich_phone` on inbound replies

**Files:**
- Modify: `linkedin/realtime/handler.py`
- Test: `tests/realtime/test_handler.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/realtime/test_handler.py`:

```python
def test_inbound_enqueues_enrichment_when_enabled(db):
    from linkedin.models import Task

    lead = _seed_lead(db)
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=_inbound(lead)), \
         patch("linkedin.realtime.handler.notify_message_received"), \
         patch("linkedin.realtime.handler.ENABLE_PHONE_ENRICHMENT", True):
        handle_realtime_event({"data": "x"}, operator="Arian")

    tasks = Task.objects.filter(task_type=Task.TaskType.ENRICH_PHONE)
    assert tasks.count() == 1
    assert tasks.first().payload["lead_id"] == lead.id


def test_inbound_does_not_enqueue_when_disabled(db):
    from linkedin.models import Task

    lead = _seed_lead(db)
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=_inbound(lead)), \
         patch("linkedin.realtime.handler.notify_message_received"), \
         patch("linkedin.realtime.handler.ENABLE_PHONE_ENRICHMENT", False):
        handle_realtime_event({"data": "x"}, operator="Arian")

    assert Task.objects.filter(task_type=Task.TaskType.ENRICH_PHONE).count() == 0


def test_enrichment_not_enqueued_for_already_enriched_lead(db):
    from django.utils import timezone as _tz
    from linkedin.models import Task

    lead = _seed_lead(db)
    lead.phone_enriched_at = _tz.now()
    lead.save(update_fields=["phone_enriched_at"])
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=_inbound(lead)), \
         patch("linkedin.realtime.handler.notify_message_received"), \
         patch("linkedin.realtime.handler.ENABLE_PHONE_ENRICHMENT", True):
        handle_realtime_event({"data": "x"}, operator="Arian")

    assert Task.objects.filter(task_type=Task.TaskType.ENRICH_PHONE).count() == 0


def test_enrichment_deduped_against_existing_task(db):
    """A second inbound message before the worker runs must not enqueue a
    duplicate task (which would double-bill the provider)."""
    from django.utils import timezone as _tz
    from linkedin.models import Task

    lead = _seed_lead(db)
    Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        status=Task.Status.PENDING,
        scheduled_at=_tz.now(),
        payload={"lead_id": lead.id, "bettercontact_request_id": ""},
    )
    # A new inbound event with a fresh entity_urn so persist_thread accepts it.
    second = ParsedRealtimeMessage(
        entity_urn="urn:li:msg:rt2", conversation_urn=CONV,
        sender_name=f"{lead.first_name} {lead.last_name}".strip(),
        sender_member_urn="urn:li:fsd_profile:LEAD1",
        text="ping again", timestamp="2026-05-16 14:35",
    )
    with patch("linkedin.realtime.handler.parse_realtime_event", return_value=second), \
         patch("linkedin.realtime.handler.notify_message_received"), \
         patch("linkedin.realtime.handler.ENABLE_PHONE_ENRICHMENT", True):
        handle_realtime_event({"data": "x"}, operator="Arian")

    assert Task.objects.filter(task_type=Task.TaskType.ENRICH_PHONE).count() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/realtime/test_handler.py -k enrichment -v`
Expected: FAIL — `AttributeError: ...handler... ENABLE_PHONE_ENRICHMENT` / no task enqueued.

- [ ] **Step 3: Add the enqueue hook**

In `linkedin/realtime/handler.py`:

(a) Add imports at the top, after the existing `from __future__` / `import logging` lines:

```python
from django.utils import timezone

from linkedin.conf import ENABLE_PHONE_ENRICHMENT
```

(b) Add the helper function after `handle_realtime_event` (before `_handle`):

```python
def _maybe_enqueue_enrichment(lead) -> None:
    """Enqueue a phone-enrichment task for a freshly-replied lead.

    Gated by ENABLE_PHONE_ENRICHMENT. Skipped when the lead is already
    enriched, disqualified, or already has a PENDING/RUNNING enrich_phone
    task — the last guard prevents duplicate provider billing when a lead
    sends several messages before the EnrichmentWorker runs (the
    phone_enriched_at check alone cannot catch that — it is still None for
    both events).
    """
    if not ENABLE_PHONE_ENRICHMENT:
        return
    if lead.phone_enriched_at is not None or lead.disqualified:
        return

    from linkedin.models import Task

    already = Task.objects.filter(
        task_type=Task.TaskType.ENRICH_PHONE,
        status__in=[Task.Status.PENDING, Task.Status.RUNNING],
        payload__lead_id=lead.id,
    ).exists()
    if already:
        logger.debug("Phone enrichment already queued for %s — skipping", lead)
        return

    Task.objects.create(
        task_type=Task.TaskType.ENRICH_PHONE,
        scheduled_at=timezone.now(),
        payload={"lead_id": lead.id, "bettercontact_request_id": ""},
    )
    logger.info("Enqueued phone enrichment for %s", lead)
```

(c) In `_handle`, the INBOUND branch (lines 73-77), add the call:

```python
    if msg.direction == Message.Direction.INBOUND:
        logger.info("Realtime inbound message persisted for %s", lead)
        notify_message_received(lead=lead, text=parsed.text, operator=operator)
        _maybe_enqueue_enrichment(lead)
    else:
        logger.debug("Realtime outbound echo persisted for %s — no notify", lead)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/realtime/test_handler.py -v`
Expected: PASS (all — existing handler tests plus the 4 new ones).

- [ ] **Step 5: Commit**

```bash
git add linkedin/realtime/handler.py tests/realtime/test_handler.py
git commit -m "Enqueue enrich_phone task on realtime inbound replies"
```

---

### Task 16: Documentation sync

**Files:**
- Modify: `CLAUDE.md`
- Modify: `ARCHITECTURE.md`

- [ ] **Step 1: Update `CLAUDE.md`**

In `CLAUDE.md`, in the "Architecture (quick reference)" section:

(a) In the **Task queue** bullet, change the task-types sentence to include `enrich_phone`:

> Types: `connect`, `follow_up`, `sweep_connections`, `enrich_phone`.

(b) Add a new bullet after the **Realtime listener** bullet:

> - **Phone enrichment**: `linkedin/enrichment/` — when the realtime listener detects an inbound reply it enqueues an `enrich_phone` Task (gated by `ENABLE_PHONE_ENRICHMENT`, default off; deduped against an existing pending/running task per lead). A single `EnrichmentWorker` background thread in the daemon process claims those tasks (the outbound loop excludes `ENRICH_PHONE`) and runs a provider failover waterfall — BetterContact → LeadMagic → Prospeo (`linkedin/enrichment/waterfall.py`) — stopping on the first `FOUND`/`NOT_FOUND` and escalating only on `API_FAILURE`. A hit writes `Lead.phone` + `Lead.phone_enriched_at` and posts a separate Slack message (`notify_phone_enriched`); an all-providers-failed run leaves `phone_enriched_at` unstamped so the lead's next reply re-attempts. One attempt per confirmed result — `phone_enriched_at` set = never re-enrich. Worker is HTTP-only (no browser), so it is not gated on active hours; stale `RUNNING` tasks are reclaimed on `start()` (the daemon has no clean shutdown). Providers implement the `PhoneProvider` protocol in `linkedin/enrichment/base.py` — adding one is a new file plus one line in `PROVIDER_CHAIN`.

(c) In the **Config** bullet, add the new keys to the `.env` list:

> `ENABLE_PHONE_ENRICHMENT`, `ENRICHMENT_MAX_DURATION_SECONDS`, `ENRICHMENT_HTTP_TIMEOUT_SECONDS`, `BETTERCONTACT_POLL_INTERVAL_SECONDS`, `BETTERCONTACT_API_KEY`, `LEADMAGIC_API_KEY`, `PROSPEO_API_KEY`

- [ ] **Step 2: Update `ARCHITECTURE.md`**

Add a new section to `ARCHITECTURE.md` after the realtime-listener section (match the file's existing heading depth and prose style):

```markdown
## Phone Enrichment (`linkedin/enrichment/`)

Background phone-number enrichment, gated by `ENABLE_PHONE_ENRICHMENT`
(`conf.py`, default off).

**Trigger.** The realtime listener's handler (`linkedin/realtime/handler.py`),
on a persisted inbound reply, enqueues an `enrich_phone` `Task` —
`payload={lead_id, bettercontact_request_id}`. It skips leads already
enriched (`phone_enriched_at` set), disqualified, or with an existing
`PENDING`/`RUNNING` `enrich_phone` task (dedup — prevents double provider
billing when a lead sends several messages before the worker runs).

**Worker.** `EnrichmentWorker` (`worker.py`) is a single background thread
`run_daemon` spawns alongside the listener supervisor. It claims
`enrich_phone` tasks via `Task.objects.next_enrichment()` — the outbound loop
excludes `ENRICH_PHONE` from `claim_next`/`seconds_to_next`, and `heal_tasks`
excludes it from the stale-`RUNNING` reset, so the two never race. The worker
reclaims its own stale `RUNNING` tasks at `start()` (the daemon has no clean
shutdown — this is the crash-recovery path). HTTP-only, so it is not gated on
active hours. Single-threaded is load-bearing: `next_enrichment` is a plain
read, not a locking claim.

**Waterfall.** `run_waterfall` (`waterfall.py`) iterates `PROVIDER_CHAIN` —
BetterContact → LeadMagic → Prospeo. `FOUND`/`NOT_FOUND` is terminal
(BetterContact's `NOT_FOUND` is authoritative — it is itself a 20+ provider
waterfall); `API_FAILURE` escalates. BetterContact is async (submit → poll,
resumable via the persisted `bettercontact_request_id`) and short-circuits to
`API_FAILURE` when the lead lacks the `last_name`/`company_name` its submit
needs. LeadMagic and Prospeo are synchronous and LinkedIn-URL native.
Providers implement the `PhoneProvider` protocol (`base.py`); transport
failures raise `HttpError` (→ `API_FAILURE`), malformed responses raise
`EnrichmentError`.

**Outcome.** `handle_enrich_phone` (`linkedin/tasks/enrich_phone.py`) writes
`Lead.phone` + stamps `phone_enriched_at` on `FOUND`; stamps only on
`NOT_FOUND`; writes nothing on all-`API_FAILURE` (so the next reply
re-attempts). `FOUND`/`NOT_FOUND` post a Slack message via
`notify_phone_enriched`; all-failed posts nothing and marks the task `failed`.
```

- [ ] **Step 3: Verify docs reference real symbols**

Run: `grep -rn "enrich_phone\|EnrichmentWorker\|ENABLE_PHONE_ENRICHMENT" CLAUDE.md ARCHITECTURE.md`
Expected: matches in both files; spot-check the named modules/functions exist.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md ARCHITECTURE.md
git commit -m "Document phone enrichment in CLAUDE.md and ARCHITECTURE.md"
```

---

### Task 17: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the whole test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — all pre-existing tests plus every test added by this plan. Investigate and fix any failure before considering the plan complete.

- [ ] **Step 2: Confirm migrations are consistent**

Run: `.venv/bin/python manage.py makemigrations --check --dry-run`
Expected: `No changes detected`.

- [ ] **Step 3: Smoke-check imports**

Run: `.venv/bin/python -c "import django; django.setup(); from linkedin.enrichment.waterfall import run_waterfall, PROVIDER_CHAIN; from linkedin.enrichment.worker import EnrichmentWorker; print([p.name for p in PROVIDER_CHAIN])"`
Expected: prints `['bettercontact', 'leadmagic', 'prospeo']` with no import error.

(If `django.setup()` needs the settings module: `DJANGO_SETTINGS_MODULE=linkedin.django_settings`.)

---

## Self-Review

**Spec coverage** — every section of `2026-05-17-phone-enrichment-design.md` maps to a task:
- Schema (two migrations) → Tasks 1, 2.
- Task type / payload (`lead_id`, `bettercontact_request_id`, no `operator`) → Task 2 (enum), Task 11/15 (payload shape).
- `enrichment/` package: `base.py` → T4; `http.py` (per-call auth headers) → T5; `bettercontact.py` → T6; `leadmagic.py` → T7; `prospeo.py` (`/enrich-person`) → T8; `waterfall.py` → T9; `worker.py` (single thread, `connection.close()`, stale reclaim) → T13.
- `tasks/enrich_phone.py` (no `session` arg) → T11.
- Daemon wiring (spawn/stop, `claim_next`/`seconds_to_next` exclusion, `heal_tasks` exclusion, exit guard, not active-hours-gated) → Tasks 12, 14.
- Slack `notify_phone_enriched` (keyword-only) → T10.
- Config (`conf.py` constants, bool idiom, API keys) + `EnrichmentError` → Tasks 3, 4.
- Error handling / outcome→persistence table → T11 (handler) + T13 (worker task-status mapping).
- Listener enqueue with dedup → T15.
- Docs → T16. Full verification → T17.

**Placeholder scan** — no TBD/TODO; every code step shows complete code; every test step shows the test body; commands have expected output.

**Type consistency** — `EnrichmentStatus` / `EnrichmentResult` (T4) used identically in T6–T13; `EnrichmentResult(status=, provider=, phone=, raw=)` keyword order consistent; `PhoneProvider.enrich(lead, task)` signature matches all three providers and the `run_waterfall` call; `notify_phone_enriched(*, lead, result)` keyword-only at definition (T10) and every call site (T11); `Task.objects.next_enrichment()` defined in T12, used in T13; `handle_enrich_phone(task)` returns `EnrichmentResult | None`, consumed exactly that way by the worker in T13.

**Provider API contracts** — BetterContact/LeadMagic/Prospeo request/response field names are the documented contracts as of 2026-05-17; Tasks 6–8 each open with a `curl` verification step because external APIs can drift (Prospeo already retired one endpoint). The waterfall/worker/handler control flow does not depend on those field names.
