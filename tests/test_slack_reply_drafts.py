"""No live Slack/model calls. Real job SQL is tested only on disposable Postgres."""
import asyncio
from copy import deepcopy
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

import httpx
import pytest
from django.db import connection

from api import slack_enrich as api, slack_reply_drafts as drafts
from api.exceptions import DraftRuntimeUnavailable, DraftServiceError, DraftViewError


def test_deployment_requirements_match_canonical_slack_dependencies():
    root = Path(__file__).resolve().parents[1]

    def dependencies(path):
        return [line.strip() for line in path.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")]

    canonical = dependencies(root / "requirements/slack.txt")
    assert dependencies(root / "api/requirements.txt") == canonical
    assert not any(line.startswith("-r") for line in canonical)


def payload(*, context=False):
    metadata = {"lead_id": 42, "operator": "Arian", "thread_external_id": "thread-a",
                "channel_id": "C1", "message_ts": "1.2", "blocks": [], "thread_blocks": []}
    action = api._LEAD_CONTEXT_DRAFT_ACTION_ID if context else api._REPLY_DRAFT_ACTION_ID
    return {"type": "block_actions", "actions": [{"action_id": action, "value": json.dumps(metadata)}],
            "view": {"type": "modal", "id": "V1", "hash": "h1",
                     "callback_id": api._LEAD_CONTEXT_MODAL_CALLBACK_ID if context else api._REPLY_MODAL_CALLBACK_ID,
                     "title": {"type": "plain_text", "text": "Reply"},
                     "private_metadata": json.dumps(metadata),
                     "blocks": [] if context else api.render_reply_modal_blocks(metadata=metadata, initial_reply="My text"),
                     "state": {"values": {"linkedin_reply_message": {api._REPLY_BODY_ACTION_ID: {"value": "New typed text"}}}}}}


def encode(value):
    return urlencode({"payload": json.dumps(value)})


def job(*, context=False):
    return drafts.parse_job(encode(payload(context=context)), api, lead_context=context)


def with_instructions(value, text):
    value["view"]["state"]["values"]["linkedin_reply_draft_instructions"] = {
        api._REPLY_INSTRUCTIONS_ACTION_ID: {"value": text},
    }
    return value


def test_optional_guidance_is_separate_from_manual_reply_and_thread_preview():
    value = with_instructions(payload(), "Keep it casual; include my 20-minute meeting link.")
    blocks = value["view"]["blocks"]
    field = next(b for b in blocks if b.get("element", {}).get("action_id") == api._REPLY_INSTRUCTIONS_ACTION_ID)
    assert field["optional"] is True
    assert field["element"]["max_length"] == 500
    assert api._reply_thread_blocks_from_view(blocks) == []
    data = drafts.parse_job(encode(value), api)
    assert data["draft_instructions"].startswith("Keep it casual")
    assert data["request_key"] != job()["request_key"]
    assert data["request_key"] == drafts.parse_job(encode(value), api)["request_key"]
    value["type"] = "view_submission"
    submitted = api.parse_reply_modal_submission(encode(value))
    assert submitted["message"] == "New typed text"
    assert "draft_instructions" not in submitted


@pytest.mark.parametrize("instructions", [None, "", "   "])
def test_blank_optional_guidance_is_normal_drafting(instructions):
    data = drafts.parse_job(encode(with_instructions(payload(), instructions)), api)
    assert data["draft_instructions"] == ""
    assert data["request_key"] == job()["request_key"]


@pytest.mark.parametrize("instructions", ["x" * 501, ["invalid"]])
def test_invalid_guidance_rejected_before_background_work(instructions):
    with pytest.raises(ValueError, match="instructions"):
        drafts.parse_job(encode(with_instructions(payload(), instructions)), api)


@pytest.mark.parametrize("state", ["loading", "error", "ready"])
def test_guidance_editor_is_preserved_across_all_updates(state):
    data = drafts.parse_job(encode(with_instructions(payload(), "No pitch, just answer their question.")), api)
    before = next(b for b in data["view"]["blocks"] if b.get("element", {}).get("action_id") == api._REPLY_INSTRUCTIONS_ACTION_ID)
    result = drafts.modal(data, api, state=state, content="Generated reply")
    after = next(b for b in result["blocks"] if b.get("element", {}).get("action_id") == api._REPLY_INSTRUCTIONS_ACTION_ID)
    assert after == before  # Stable Slack IDs preserve the live typed guidance.


def test_recovery_keeps_guidance_but_an_edit_creates_a_new_job():
    data = drafts.parse_job(encode(with_instructions(payload(), "Include my 20-minute link")), api)
    data["job_known"] = True
    view = drafts.modal(data, api, state="error")
    value = payload()
    value["view"] = {**view, "id": "V1", "hash": "h2"}
    value["actions"] = view["blocks"][-1]["elements"]
    recovered = drafts.parse_job(encode(value), api)
    assert recovered["retry"]
    assert recovered["request_key"] == data["request_key"]
    assert recovered["draft_instructions"] == data["draft_instructions"]
    value["view"]["state"] = {"values": {}}
    with_instructions(value, "Use the deep dive link instead")
    edited = drafts.parse_job(encode(value), api)
    assert not edited["retry"]
    assert edited["request_key"] != recovered["request_key"]
    assert edited["draft_instructions"] == "Use the deep dive link instead"
    with_instructions(value, None)
    assert not drafts.parse_job(encode(value), api)["retry"]


def test_instruction_retry_button_fits_slack_value_limit():
    value = with_instructions(payload(), '"' * api._REPLY_INSTRUCTIONS_LIMIT)
    data = drafts.parse_job(encode(value), api)
    data.update(operator="s" * 80, thread_external_id="t" * 512, job_known=True)
    view = drafts.modal(data, api, state="error")
    assert len(view["blocks"][-1]["elements"][0]["value"]) <= 2000


def test_background_passes_guidance_and_verified_catalog_to_model(monkeypatch):
    worker(monkeypatch)
    drafts.fetch_context.return_value = {"lead": {}, "operator": "Arian", "messages": []}
    data = drafts.parse_job(encode(with_instructions(payload(), "Include my 20-minute meeting link")), api)
    asyncio.run(drafts.run(data, api))
    prompt = drafts.generate.call_args.args[0]
    context = json.loads(prompt[prompt.index('{"lead":'):])
    assert context["operator_instructions"] == data["draft_instructions"]
    assert context["booking_links"]["owner"] == "Arian"
    quick = next(e for e in context["booking_links"]["events"] if e["key"] == "quick_chat")
    assert quick["duration_minutes"] == 20
    assert quick["url"].endswith("/quick-chat-boundera")
    assert "Never invent a URL" in prompt


def test_missing_duration_is_visible_and_never_replaces_reply_or_calls_model(monkeypatch):
    calls = worker(monkeypatch)
    drafts.fetch_context.return_value = {"lead": {}, "operator": "Arian", "messages": []}
    data = drafts.parse_job(encode(with_instructions(payload(), "Attach my meeting link for 15 mins")), api)
    asyncio.run(drafts.run(data, api))
    drafts.generate.assert_not_called()
    drafts.save_result.assert_not_called()
    assert "No saved meeting link matches that duration" in json.dumps(calls[-1])
    assert calls[-1]["blocks"][0]["element"]["initial_value"] == "My text"
    drafts.fail_job.assert_called_once()


@pytest.mark.parametrize("operator,instructions,available", [
    ("Arian", "", True),
    ("Chuka", "Include my 30-minute link", False),
    ("Chuka", "Include Arian's 30-minute intro link", True),
    ("Chuka", "Arianna asked for a link", False),
])
def test_calendar_ownership_is_explicit(operator, instructions, available):
    prompt = api.reply_draft_prompt({"lead": {}, "operator": operator}, instructions=instructions)
    context = json.loads(prompt[prompt.index('{"lead":'):])
    assert bool(context["booking_links"].get("events")) is available


def test_request_identity_and_explicit_retry_scope():
    data = job()
    assert data["request_key"] == job()["request_key"]
    other = payload()
    other["view"]["hash"] = "h2"
    assert drafts.parse_job(encode(other), api)["request_key"] != data["request_key"]
    other["actions"][0]["value"] = json.dumps({"draft_job_key": data["request_key"]})
    retry = drafts.parse_job(encode(other), api)
    assert retry["request_key"] == data["request_key"] and retry["retry"]


@pytest.mark.parametrize("field", ["id", "hash", "callback_id"])
def test_missing_view_identity_rejected_before_background_work(field):
    value = payload()
    value["view"].pop(field)
    with pytest.raises(ValueError):
        drafts.parse_job(encode(value), api)


@pytest.mark.parametrize("state", ["loading", "error"])
def test_loading_and_failed_updates_preserve_editor_ids_and_initial_values(state):
    data = job()
    before = deepcopy(data)
    view = drafts.modal(data, api, state=state, content="Suggested text")
    field = next(b for b in view["blocks"] if b.get("type") == "input")
    assert field == next(b for b in data["view"]["blocks"] if b.get("type") == "input")
    assert data == before
    assert json.loads(view["private_metadata"]) == json.loads(data["view"]["private_metadata"])
    assert all("{role}" not in json.dumps(b) for b in view["blocks"])


def test_background_status_replaces_itself_and_keeps_unrelated_actions():
    data = job(context=True)
    data["view"]["blocks"] = [{"type": "actions", "block_id": "other", "elements": [
        {"type": "button", "action_id": api._LEAD_CONTEXT_AI_ACTION_ID}]}]
    data["view"] = drafts.modal(data, api, state="loading")
    result = drafts.modal(data, api, state="ready", content="Ready")
    assert len([b for b in result["blocks"] if b.get("block_id") == drafts.PREFIX + "status"]) == 1
    assert result["blocks"][0]["elements"][0]["action_id"] == api._LEAD_CONTEXT_AI_ACTION_ID
    assert "Use draft" not in json.dumps(result)


def worker(monkeypatch, *, cached=None):
    calls = []
    monkeypatch.setattr(drafts, "claim", MagicMock(return_value=("" if cached else "lease", cached)))
    monkeypatch.setattr(drafts, "fetch_context", MagicMock(return_value={"lead": {}, "messages": []}))
    monkeypatch.setattr(drafts, "generate", AsyncMock(return_value="Draft result"))
    monkeypatch.setattr(drafts, "save_result", MagicMock(return_value=True))
    monkeypatch.setattr(drafts, "fail_job", MagicMock())
    async def update(data, view, api):
        calls.append(deepcopy(view))
        data["view"], data["view_hash"] = view, "h" + str(len(calls) + 1)
    monkeypatch.setattr(drafts, "update", update)
    return calls


def test_background_generation_only_after_ack_and_never_enqueues(monkeypatch):
    scheduled = []
    monkeypatch.setattr(drafts, "schedule", lambda data, api, **kwargs: scheduled.append(data))
    db = MagicMock(side_effect=AssertionError("I/O before ACK"))
    monkeypatch.setattr(api.psycopg, "connect", db)
    send = MagicMock(side_effect=AssertionError("must never enqueue"))
    monkeypatch.setattr(api, "enqueue_manual_reply_task", send)
    h = object.__new__(api.handler)
    h.wfile = io.BytesIO()
    h._respond_text = MagicMock()
    h._handle_reply_draft(encode(payload()))
    h._respond_text.assert_called_once_with(200, "")
    db.assert_not_called()
    calls = worker(monkeypatch)
    asyncio.run(drafts.run(scheduled[0], api))
    assert len(calls) == 2
    drafts.generate.assert_awaited_once()
    drafts.save_result.assert_called_once()
    drafts.fail_job.assert_not_called()
    send.assert_not_called()


def test_duplicate_active_job_never_calls_slack_or_model(monkeypatch):
    calls = worker(monkeypatch)
    drafts.claim.return_value = ("", None)
    asyncio.run(drafts.run(job(), api))
    assert not calls
    drafts.generate.assert_not_called()


def test_interrupted_job_without_loading_view_exposes_explicit_retry(monkeypatch):
    calls = worker(monkeypatch)
    drafts.claim.side_effect = DraftServiceError("retry_required")
    data = job()
    asyncio.run(drafts.run(data, api))
    assert "Retry draft" in json.dumps(calls[-1])
    assert data["request_key"] in json.dumps(calls[-1])
    drafts.generate.assert_not_called()


def test_retry_completed_job_reuses_saved_result_without_model_call(monkeypatch):
    calls = worker(monkeypatch, cached="Previously generated")
    asyncio.run(drafts.run(job(), api))
    assert "Previously generated" in json.dumps(calls[-1])
    field = next(b for b in calls[-1]["blocks"] if b.get("type") == "input")
    assert field["element"]["initial_value"] == "Previously generated"
    assert "Suggested reply" not in json.dumps(calls[-1])
    assert "Use draft" not in json.dumps(calls[-1])
    drafts.generate.assert_not_called()
    drafts.save_result.assert_not_called()


@pytest.mark.parametrize("code", ["hash_conflict", "not_found"])
def test_stale_or_closed_modal_stops_before_generation(monkeypatch, code):
    worker(monkeypatch)
    monkeypatch.setattr(drafts, "update", AsyncMock(side_effect=DraftViewError(code)))
    asyncio.run(drafts.run(job(), api))
    drafts.generate.assert_not_called()
    drafts.fail_job.assert_called_once()


def test_saved_copy_survives_failed_slack_delivery(monkeypatch):
    worker(monkeypatch)
    monkeypatch.setattr(drafts, "update", AsyncMock(side_effect=[None, httpx.ReadTimeout("timeout"), None]))
    asyncio.run(drafts.run(job(), api))
    drafts.save_result.assert_called_once()
    drafts.fail_job.assert_not_called()
    assert "Retry draft" in json.dumps(drafts.update.call_args.args[1])


def test_superseded_lease_never_publishes_late_copy(monkeypatch):
    calls = worker(monkeypatch)
    drafts.save_result.return_value = False
    asyncio.run(drafts.run(job(), api))
    assert len(calls) == 1
    drafts.fail_job.assert_called_once()


def test_unexpected_errors_are_not_silenced(monkeypatch):
    worker(monkeypatch)
    drafts.generate.side_effect = ValueError("malformed response")
    with pytest.raises(ValueError, match="malformed response"):
        asyncio.run(drafts.run(job(), api))
    drafts.fail_job.assert_called_once()


def test_job_deadline_preserves_editor_and_enables_retry(monkeypatch):
    calls = worker(monkeypatch)
    async def slow(*args):
        await asyncio.sleep(1)
    monkeypatch.setattr(drafts, "generate", slow)
    monkeypatch.setattr(drafts, "JOB_BUDGET", .025)
    asyncio.run(drafts.run(job(), api))
    assert "Retry draft" in json.dumps(calls[-1])
    assert next(b for b in calls[-1]["blocks"] if b.get("type") == "input")["element"]["initial_value"] == "My text"
    drafts.fail_job.assert_called_once()


def test_success_replaces_input_directly_but_submission_still_requires_operator(monkeypatch):
    data = job()
    calls = worker(monkeypatch)
    enqueue = MagicMock()
    monkeypatch.setattr(api, "enqueue_manual_reply_task", enqueue)
    asyncio.run(drafts.run(data, api))
    view = calls[-1]
    field = next(b for b in view["blocks"] if b.get("type") == "input")
    assert field["block_id"].startswith("linkedin_reply_message:")
    assert field["block_id"] != next(b for b in calls[0]["blocks"] if b.get("type") == "input")["block_id"]
    assert field["element"]["action_id"] == api._REPLY_BODY_ACTION_ID
    assert field["element"]["initial_value"] == "Draft result"
    assert "Use draft" not in json.dumps(view)
    assert "Suggested reply" not in json.dumps(view)
    assert not any(b.get("block_id") == drafts.PREFIX + "status" for b in view["blocks"])
    enqueue.assert_not_called()
    view["state"] = {"values": {field["block_id"]: {api._REPLY_BODY_ACTION_ID: {"value": "Operator edited"}}}}
    assert api.parse_reply_modal_submission(encode({"type": "view_submission", "view": view}))["message"] == "Operator edited"
    view["state"]["values"][field["block_id"]][api._REPLY_BODY_ACTION_ID]["value"] = ""
    h = object.__new__(api.handler)
    h._respond_json = MagicMock()
    h._handle_reply_submission(encode({"type": "view_submission", "view": view}))
    assert field["block_id"] in h._respond_json.call_args.args[0]["errors"]
    enqueue.assert_not_called()


def test_draft_again_replaces_successfully_but_preserves_existing_draft_on_failure():
    data = job()
    first = drafts.modal(data, api, state="ready", content="First draft")
    data["view"] = first
    original = next(b for b in first["blocks"] if b.get("type") == "input")
    for state in ("loading", "error"):
        next_view = drafts.modal(data, api, state=state)
        assert next(b for b in next_view["blocks"] if b.get("type") == "input") == original
    second = drafts.modal(data, api, state="ready", content="Second draft")
    replaced = next(b for b in second["blocks"] if b.get("type") == "input")
    assert replaced["block_id"] != original["block_id"]
    assert replaced["element"]["initial_value"] == "Second draft"


def transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(drafts.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(api, "LLM_API_KEY", "synthetic")
    monkeypatch.setattr(api, "AI_MODEL", "synthetic-model")


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), httpx.ConnectError("reset"), 429, 503])
def test_transient_model_failures_retry_once(monkeypatch, failure):
    calls = []
    def respond(req):
        calls.append(req)
        if len(calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure, headers={"retry-after": "0"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Draft"}}]})
    transport(monkeypatch, respond)
    assert asyncio.run(drafts.generate("Prompt", api)) == "Draft"
    assert len(calls) == 2


@pytest.mark.parametrize("status, header", [(302, "0"), (401, "0"), (400, "0"), (429, "120"), (429, "Thu, 01 Jan 2026 00:00:00 GMT")])
def test_permanent_or_long_backoff_response_is_not_retried(monkeypatch, status, header):
    calls = []
    def respond(req):
        calls.append(req)
        return httpx.Response(status, headers={"retry-after": header})
    transport(monkeypatch, respond)
    with pytest.raises(DraftServiceError):
        asyncio.run(drafts.generate("Prompt", api))
    assert len(calls) == 1


def test_two_slow_model_attempts_have_real_deadlines(monkeypatch):
    calls = []
    async def respond(req):
        calls.append(req)
        await asyncio.sleep(1)
    transport(monkeypatch, respond)
    monkeypatch.setattr(drafts, "MODEL_ATTEMPT", .02)
    with pytest.raises(TimeoutError):
        asyncio.run(drafts.generate("Prompt", api))
    assert len(calls) == 2


def test_provider_slower_than_old_eight_second_limit_can_finish(monkeypatch):
    async def respond(req):
        await asyncio.sleep(8.1)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Completed slowly"}}]})
    transport(monkeypatch, respond)
    assert asyncio.run(drafts.generate("Prompt", api)) == "Completed slowly"


def test_slack_retry_reuses_original_hash_after_uncertain_update(monkeypatch):
    calls = []
    def respond(req):
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            raise httpx.ReadTimeout("response lost after success")
        return httpx.Response(200, json={"ok": False, "error": "hash_conflict"})
    transport(monkeypatch, respond)
    data = job()
    with pytest.raises(DraftViewError, match="hash_conflict"):
        asyncio.run(drafts.update(data, drafts.modal(data, api, state="loading"), api))
    assert [c["hash"] for c in calls] == ["h1", "h1"]
    assert data["view_hash"] == "h1"


def test_sdk_registration_does_not_run_work_before_ack(monkeypatch):
    # Use the installed public SDK and runtime collector when testing deployment deps.
    context = pytest.importorskip("vercel.cache.context")
    runtime = pytest.importorskip("vercel_runtime.wait_until")
    monkeypatch.setenv("VERCEL_IPC_PATH", "/synthetic/runtime")
    events = []
    async def work(*args):
        events.append("work")
    monkeypatch.setattr(drafts, "run", work)
    collector = runtime.begin_wait_until()
    h = object.__new__(api.handler)
    h.wfile = io.BytesIO()
    h._respond_text = lambda *args: events.append("ack")
    try:
        h._handle_reply_draft(encode(payload()))
        assert events == ["ack"]
        runtime.finish_wait_until(collector)
        assert events == ["ack", "work"]
    finally:
        context.set_context(wait_until=None)


def test_runtime_without_background_support_fails_before_ack(monkeypatch):
    context = pytest.importorskip("vercel.cache.context")
    context.set_context(wait_until=None)
    with pytest.raises(DraftRuntimeUnavailable, match="background runtime"):
        drafts.schedule(job(), api)


@pytest.fixture
def postgres(monkeypatch, transactional_db):
    if connection.vendor != "postgresql":
        pytest.skip("Run scripts/qa_campaigns.py --suite slack-drafts for real SQL checks")
    from tests.factories import LeadFactory
    from linkedin.models import Task
    lead = LeadFactory()
    params = connection.get_connection_params()
    params.pop("context", None)
    params.pop("cursor_factory", None)
    monkeypatch.setattr(drafts, "connect", lambda api: api.psycopg.connect(**params))
    data = job()
    data["lead_id"] = lead.pk
    before = Task.objects.count()
    yield data
    assert Task.objects.count() == before


def test_job_sql_deduplicates_and_recovers_saved_copy(postgres):
    data = postgres
    token, content = drafts.claim(data, api)
    assert token and content is None
    assert drafts.claim(data, api) == ("", None)
    assert drafts.save_result(data, token, "Saved copy", api)
    assert drafts.claim(data, api) == ("", "Saved copy")


def test_job_sql_concurrent_clicks_have_exactly_one_lease(postgres):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: drafts.claim(postgres, api), range(4)))
    assert sum(bool(token) for token, content in claims) == 1


def test_job_sql_failed_job_retries_only_on_operator_click(postgres):
    data = postgres
    token, _ = drafts.claim(data, api)
    drafts.fail_job(data, token, "model_unavailable", api)
    with pytest.raises(DraftServiceError, match="retry_required"):
        drafts.claim(data, api)
    replacement, _ = drafts.claim({**data, "retry": True}, api)
    assert replacement and replacement != token


def test_job_sql_rejects_other_sender_or_view(postgres):
    data = postgres
    token, _ = drafts.claim(data, api)
    assert drafts.save_result(data, token, "Private", api)
    for field, value in [("operator", "Chuka"), ("view_id", "OTHER"), ("thread_external_id", "other")]:
        changed = {**data, field: value, "retry": True}
        with pytest.raises(DraftServiceError):
            drafts.claim(changed, api)


def test_job_sql_expired_lease_requires_explicit_retry_and_rejects_old_result(postgres):
    from datetime import timedelta
    from django.utils import timezone
    from linkedin.models import SlackReplyDraftJob
    data = postgres
    old_token, _ = drafts.claim(data, api)
    SlackReplyDraftJob.objects.filter(pk=data["request_key"]).update(lease_until=timezone.now() - timedelta(seconds=1))
    with pytest.raises(DraftServiceError, match="retry_required"):
        drafts.claim(data, api)
    token, _ = drafts.claim({**data, "retry": True}, api)
    assert token and token != old_token
    assert not drafts.save_result(data, old_token, "Late copy", api)
    assert drafts.save_result(data, token, "New copy", api)
    drafts.fail_job(data, old_token, "late_error", api)
    assert drafts.claim(data, api) == ("", "New copy")


def test_job_sql_old_job_cannot_overwrite_newer_saved_artifact(postgres):
    from linkedin.models import SlackLeadContextArtifact
    data = postgres
    token, _ = drafts.claim(data, api)
    newer = {**data, "request_key": "f" * 64}
    other_token, _ = drafts.claim(newer, api)
    assert drafts.save_result(newer, other_token, "Newer", api)
    assert drafts.save_result(data, token, "Older", api)
    assert SlackLeadContextArtifact.objects.get(lead_id=data["lead_id"], kind="draft_reply").content == "Newer"


def test_new_job_model_matches_migration(postgres):
    from django.core.management import call_command
    call_command("makemigrations", "linkedin", check=True, dry_run=True, verbosity=0)
