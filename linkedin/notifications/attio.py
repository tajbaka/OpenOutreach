"""Attio CRM sync via REST API.

Mirrors Deal state into the Sales list's Stage column. Each Lead with an
active Deal becomes:
- one Company record (by name)
- one Person record (name + LinkedIn URL + job_title + company ref)
- one Sales list entry (Company-parented, Stage = mapped from Deal.state,
  Main point of contact = the Person)

The IDs returned by create are persisted on Lead (attio_*_id) so subsequent
syncs PATCH the entry rather than re-creating. Idempotent — running twice
is a no-op for already-seeded leads.

Failure mode: raises AttioError on any non-2xx. Callers (sync_attio) decide
whether to skip-and-continue or abort.
"""
from __future__ import annotations

import json
import logging
from urllib import request
from urllib.error import HTTPError, URLError

from linkedin.conf import ATTIO_API_KEY, ATTIO_SALES_LIST_ID
from linkedin.exceptions import AttioError

logger = logging.getLogger(__name__)

API_BASE = "https://api.attio.com/v2"


# ----------------------------------------------------------------------
# Stage names + ranking. Stages above Meeting are human-managed; sync_attio
# uses STAGE_RANK to avoid pushing entries backwards once a human moves
# them to Proposal+.
# ----------------------------------------------------------------------
STAGE_PROSPECTING = "Prospecting"
STAGE_QUALIFICATION = "Qualification"
STAGE_MEETING = "Meeting"
STAGE_WON = "Won"
STAGE_LOST = "Lost"

# Funnel-progression rank, used to pick the "furthest along" lead at a
# company when we aggregate per-lead stages into a single company-level
# Stage. Lost is excluded — it's terminal-negative, not progress.
PROGRESSION_RANK = {
    STAGE_PROSPECTING: 1,
    STAGE_QUALIFICATION: 2,
    STAGE_MEETING: 3,
    STAGE_WON: 4,
}


def deal_to_stage(deal) -> str:
    """Compute the Sales list Stage for a Deal.

    Auto-managed stages: Prospecting, Qualification, Won, Lost. Meeting is
    human-driven (sync_attio's don't-downgrade rule preserves it).
    """
    from linkedin.enums import ProfileState

    state = deal.state
    if state == ProfileState.COMPLETED:
        return STAGE_WON
    if state == ProfileState.FAILED:
        return STAGE_LOST
    if state == ProfileState.CONNECTED and deal.last_reply_at is not None:
        return STAGE_QUALIFICATION
    return STAGE_PROSPECTING


def aggregate_company_stage(stages: list[str]) -> str:
    """Pick the company-level Stage from per-lead stages.

    Rule: Won wins outright; otherwise pick the furthest-along active
    stage (Meeting > Qualification > Prospecting). If all leads are Lost,
    the company is Lost. Mixing Lost with active leads ignores the Lost
    ones — one disqualified contact doesn't doom the company-level deal.
    """
    if not stages:
        return STAGE_PROSPECTING
    if STAGE_WON in stages:
        return STAGE_WON
    active = [s for s in stages if s in PROGRESSION_RANK]
    if not active:
        return STAGE_LOST  # all Lost
    return max(active, key=lambda s: PROGRESSION_RANK[s])


def should_patch_stage(current: str, target: str) -> bool:
    """Decide whether to PATCH an existing entry's Stage.

    - Won always overrides (terminal positive from our DB beats anything).
    - Don't downgrade active stages (Meeting -> Qualification, etc.) —
      humans manually advance to Meeting+, we should never pull back.
    - Lost is overridable (a revived deal can transition out of Lost).
    """
    if current == target:
        return False
    if target == STAGE_WON:
        return True
    cur_rank = PROGRESSION_RANK.get(current, 0)
    tgt_rank = PROGRESSION_RANK.get(target, 0)
    if cur_rank > tgt_rank and current != STAGE_LOST:
        return False
    return True


# ----------------------------------------------------------------------
# Per-person Outreach status (custom select on People object).
# Tracks where each individual lead is in the conversation flow,
# independent of the company-level Stage on the Sales list entry.
# ----------------------------------------------------------------------
STATUS_INVITE_SENT = "Invite Sent"
STATUS_CONNECTED = "Connected"
STATUS_REPLIED = "Replied"
STATUS_MEETING_BOOKED = "Meeting Booked"
STATUS_WON = "Won"
STATUS_LOST = "Lost"


def deal_to_outreach_status(deal) -> str:
    """Compute Person.outreach_status from a Deal.

    Meeting Booked is human-driven (we don't know about meetings without
    Calendar integration); the script leaves it alone if a human has set
    it via `should_patch_outreach_status`.
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
    STATUS_INVITE_SENT: 1,
    STATUS_CONNECTED: 2,
    STATUS_REPLIED: 3,
    STATUS_MEETING_BOOKED: 4,
    STATUS_WON: 5,
}


def should_patch_outreach_status(current: str, target: str) -> bool:
    """Same don't-downgrade rule as stages, applied per-person.

    Won overrides anything. Don't pull back from Meeting Booked since the
    human set that. Lost is overridable.
    """
    if current == target:
        return False
    if target == STATUS_WON:
        return True
    cur_rank = OUTREACH_RANK.get(current, 0)
    tgt_rank = OUTREACH_RANK.get(target, 0)
    if cur_rank > tgt_rank and current != STATUS_LOST:
        return False
    return True


# ----------------------------------------------------------------------
# HTTP plumbing
# ----------------------------------------------------------------------


def _request(method: str, path: str, body: dict | None = None) -> dict:
    if not ATTIO_API_KEY:
        raise AttioError("ATTIO_API_KEY is not set in .env")
    url = f"{API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {ATTIO_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8")
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise AttioError(
            f"{method} {path} -> HTTP {e.code}: {body_text[:500]}"
        ) from e
    except (URLError, TimeoutError) as e:
        raise AttioError(f"{method} {path} -> network error: {e}") from e

    if not payload:
        return {}
    try:
        return json.loads(payload)
    except json.JSONDecodeError as e:
        raise AttioError(f"{method} {path} -> invalid JSON: {payload[:200]}") from e


def _extract_id(resp: dict, key: str) -> str:
    """Pull `id.<key>` from an Attio create-response."""
    rid = (((resp.get("data") or {}).get("id") or {}).get(key))
    if not rid:
        raise AttioError(f"missing id.{key} in response: {json.dumps(resp)[:300]}")
    return rid


# ----------------------------------------------------------------------
# Object writes
# ----------------------------------------------------------------------


def create_company(name: str) -> str:
    """Create a Company record. Returns record_id."""
    body = {"data": {"values": {"name": name}}}
    resp = _request("POST", "/objects/companies/records", body)
    return _extract_id(resp, "record_id")


def create_person(
    *,
    first_name: str,
    last_name: str,
    linkedin_url: str = "",
    job_title: str = "",
    company_id: str = "",
) -> str:
    """Create a Person record. Returns record_id."""
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
    resp = _request("POST", "/objects/people/records", body)
    return _extract_id(resp, "record_id")


# ----------------------------------------------------------------------
# Sales list writes
# ----------------------------------------------------------------------


def _require_list_id() -> str:
    if not ATTIO_SALES_LIST_ID:
        raise AttioError("ATTIO_SALES_LIST_ID is not set in .env")
    return ATTIO_SALES_LIST_ID


def create_sales_entry(*, company_id: str, person_id: str, stage: str) -> str:
    """Add a Company to the Sales list with Main point of contact + Stage."""
    list_id = _require_list_id()
    body = {
        "data": {
            "parent_record_id": company_id,
            "parent_object": "companies",
            "entry_values": {
                "stage": stage,
                "main_point_of_contact": [{
                    "target_object": "people",
                    "target_record_id": person_id,
                }],
            },
        },
    }
    resp = _request("POST", f"/lists/{list_id}/entries", body)
    return _extract_id(resp, "entry_id")


def get_sales_entry_state(entry_id: str) -> dict:
    """Fetch a Sales list entry's current Stage + Main point of contact.

    Returns {"stage": str, "mpoc_id": str} — empty strings when unset.
    Used by sync_attio to decide whether to PATCH stage and/or MPOC.
    """
    list_id = _require_list_id()
    resp = _request("GET", f"/lists/{list_id}/entries/{entry_id}")
    values = ((resp.get("data") or {}).get("entry_values") or {})

    stage_values = values.get("stage") or []
    stage = ""
    if stage_values:
        first = stage_values[0] if isinstance(stage_values, list) else stage_values
        status = first.get("status") if isinstance(first, dict) else None
        if isinstance(status, dict):
            stage = status.get("title") or ""

    mpoc_values = values.get("main_point_of_contact") or []
    mpoc_id = ""
    if mpoc_values:
        first = mpoc_values[0] if isinstance(mpoc_values, list) else mpoc_values
        target = first.get("target_record") if isinstance(first, dict) else None
        if isinstance(target, dict):
            mpoc_id = target.get("record_id") or ""
        else:
            # Some Attio responses inline target_record_id directly on the value
            mpoc_id = first.get("target_record_id") if isinstance(first, dict) else ""
            mpoc_id = mpoc_id or ""

    return {"stage": stage, "mpoc_id": mpoc_id}


def patch_sales_entry_stage(entry_id: str, stage: str) -> None:
    """Update the Stage on an existing Sales list entry."""
    list_id = _require_list_id()
    body = {"data": {"entry_values": {"stage": stage}}}
    _request("PATCH", f"/lists/{list_id}/entries/{entry_id}", body)


def patch_sales_entry_mpoc(entry_id: str, person_id: str) -> None:
    """Update Main point of contact on an existing Sales list entry."""
    list_id = _require_list_id()
    body = {
        "data": {
            "entry_values": {
                "main_point_of_contact": [{
                    "target_object": "people",
                    "target_record_id": person_id,
                }],
            },
        },
    }
    _request("PATCH", f"/lists/{list_id}/entries/{entry_id}", body)


def set_person_outreach_status(person_id: str, status: str) -> None:
    """Set the per-person Outreach status (custom select field on People)."""
    body = {"data": {"values": {"outreach_status": status}}}
    _request("PATCH", f"/objects/people/records/{person_id}", body)


def get_person_outreach_status(person_id: str) -> str:
    """Fetch the current Outreach status title, or '' if unset."""
    resp = _request("GET", f"/objects/people/records/{person_id}")
    values = ((resp.get("data") or {}).get("values") or {})
    sel = values.get("outreach_status") or []
    if not sel:
        return ""
    first = sel[0] if isinstance(sel, list) else sel
    if isinstance(first, dict):
        opt = first.get("option")
        if isinstance(opt, dict):
            return opt.get("title") or ""
    return ""


def patch_person_company(person_id: str, company_id: str) -> None:
    """Re-link a Person record to a different Company. Used for dedup."""
    body = {
        "data": {
            "values": {
                "company": [{
                    "target_object": "companies",
                    "target_record_id": company_id,
                }],
            },
        },
    }
    _request("PATCH", f"/objects/people/records/{person_id}", body)


def delete_company(record_id: str) -> None:
    _request("DELETE", f"/objects/companies/records/{record_id}")


def delete_person(record_id: str) -> None:
    _request("DELETE", f"/objects/people/records/{record_id}")


def delete_sales_entry(entry_id: str) -> None:
    list_id = _require_list_id()
    _request("DELETE", f"/lists/{list_id}/entries/{entry_id}")
