"""Personal post ledger, Social Studio's inserts, commands and one worker run (SQLite, no browser)."""
import hashlib
import io
import json
from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from linkedin import env_spec, personal_posts as posts
from linkedin.exceptions import (
    LinkedInPostImmutableError,
    LinkedInPostValidationError,
    LinkedInPostWorkerRefused,
    PostBrowserUnavailableError,
    PostCommentaryMismatchError,
    PostMediaMismatchError,
    PostMediaUnavailableError,
    PostUncertainError,
)
from linkedin.models import LinkedInPost, LinkedInProfile, LinkedInRepost
from tests.factories import UserFactory

NEW = "https://www.linkedin.com/feed/update/urn:li:share:7300000000000000009/"
CHUKA_LOGIN = "eddy@boundera.io"
ARIAN_LOGIN = "arian@boundera.io"
PDF = b"%PDF-1.7\nslides\n%%EOF\n"
PDF_SHA = hashlib.sha256(PDF).hexdigest()
PDF_URL = f"https://cdn.sanity.io/files/9z0vtvpx/production/{'a' * 40}.pdf"
PNG = b"\x89PNG\r\n\x1a\nimage"
PNG_SHA = hashlib.sha256(PNG).hexdigest()
PNG_URL = f"https://cdn.sanity.io/files/9z0vtvpx/production/{'b' * 40}.png"


def _command(name, *args):
    out = StringIO()
    call_command(name, *args, stdout=out)
    return json.loads(out.getvalue())


def _document(**extra):
    return {"media_kind": "document", "media_url": PDF_URL, "media_sha256": PDF_SHA, "media_bytes": len(PDF),
            "media_name": "carousel.pdf", "document_title": "Quick 411 on VDR Response", **extra}


# --------------------------------------------------------------------------- Social Studio's inserts


def _studio_insert(**fields):
    """The row Social Studio's dashboard writes: named columns only, the rest from database defaults."""
    values = {"external_ref": "studio-1", "operator": "Arian", "commentary": "", "media_kind": "", "media_url": "",
              "media_sha256": "", "media_bytes": None, "media_name": "", "document_title": "", **fields}
    names = list(values) + ["status", "scheduled_for", "result_url", "error_type", "created_at", "updated_at"]
    now = timezone.now()
    params = list(values.values()) + ["queued", now + timedelta(hours=1), "", "", now, now]
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO linkedin_linkedinpost ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))})", params,
        )


@pytest.mark.django_db
def test_social_studio_insert_of_a_text_post_uses_database_defaults():
    _studio_insert(commentary="Posting from my own profile.")
    row = LinkedInPost.objects.get()
    assert (row.status, row.retry_count, row.media_kind, row.result_url) == ("queued", 0, "", "")
    assert posts.validate_row(row)["kind"] == ""


@pytest.mark.django_db
def test_social_studio_can_omit_every_optional_column():
    now = timezone.now()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO linkedin_linkedinpost (external_ref, operator, commentary, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s)", ["bare", "Chuka", "Just text.", now, now],
        )
    row = LinkedInPost.objects.get()
    assert (row.status, row.media_kind, row.document_title, row.retry_count, row.scheduled_for) == ("queued", "", "", 0, None)


@pytest.mark.django_db
def test_social_studio_insert_of_a_document_post():
    _studio_insert(commentary="Slides attached.", **_document())
    row = LinkedInPost.objects.get()
    media = posts.validate_row(row)
    assert (media["kind"], media["title"], media["size"]) == ("document", "Quick 411 on VDR Response", len(PDF))


@pytest.mark.django_db
@pytest.mark.parametrize("fields", [
    {"commentary": ""},  # nothing to post
    {"commentary": "x", "media_kind": "video"},
    {"commentary": "x", "media_kind": "image", "media_url": "", "media_sha256": PNG_SHA, "media_name": "i.png"},
    {"commentary": "x", **_document(document_title="")},
])
def test_database_refuses_incomplete_rows(fields):
    with pytest.raises(IntegrityError), transaction.atomic():
        _studio_insert(**fields)


@pytest.mark.django_db
def test_sent_needs_a_permalink_and_payload_is_immutable():
    row, _ = posts.enqueue_post(operator="Arian", external_ref="p-1", commentary="Words.")
    with pytest.raises(IntegrityError), transaction.atomic():
        LinkedInPost.objects.filter(pk=row.pk).update(status="sent", result_url="")
    for field, value in [("commentary", "Other words."), ("media_url", PDF_URL), ("document_title", "T"), ("operator", "Chuka")]:
        with pytest.raises(LinkedInPostImmutableError):
            LinkedInPost.objects.filter(pk=row.pk).update(**{field: value})
        changed = LinkedInPost.objects.get(pk=row.pk)
        setattr(changed, field, value)
        with pytest.raises(LinkedInPostImmutableError):
            changed.save()


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize("media, message", [
    ({"kind": "image", "url": "https://example.com/a.png", "sha256": PNG_SHA, "size": 5, "name": "a.png", "title": ""}, "cdn.sanity.io"),
    ({"kind": "image", "url": PNG_URL, "sha256": "ABC", "size": 5, "name": "a.png", "title": ""}, "SHA-256"),
    ({"kind": "image", "url": PNG_URL, "sha256": PNG_SHA, "size": 5, "name": "a.pdf", "title": ""}, "ending in"),
    ({"kind": "image", "url": PNG_URL, "sha256": PNG_SHA, "size": 5, "name": "a.png", "title": "T"}, "Only a document"),
    ({"kind": "document", "url": PDF_URL, "sha256": PDF_SHA, "size": 5, "name": "c.pdf", "title": ""}, "needs its title"),
    ({"kind": "document", "url": PDF_URL, "sha256": PDF_SHA, "size": 5, "name": "c.pdf", "title": " padded"}, "whitespace"),
    ({"kind": "", "url": PDF_URL, "sha256": "", "size": None, "name": "", "title": ""}, "without media"),
    ({"kind": "document", "url": PDF_URL, "sha256": PDF_SHA, "size": 0, "name": "c.pdf", "title": "T"}, "size"),
])
def test_media_validation_refuses_anything_inexact(media, message):
    with pytest.raises(LinkedInPostValidationError, match=message):
        posts.validate_media(**media)


@pytest.mark.parametrize("text, message", [
    (" leading", "whitespace"), ("tab\there", "control characters"), ("x" * 3001, "3000"), ("bom﻿", "byte-order"),
])
def test_post_text_validation(text, message):
    with pytest.raises(LinkedInPostValidationError, match=message):
        posts.validate_commentary(text)
    assert posts.validate_commentary("Line one\n\nLine three") == "Line one\n\nLine three"


@pytest.mark.django_db
def test_enqueue_is_idempotent_and_refuses_a_different_payload(tmp_path):
    text = tmp_path / "post.txt"
    text.write_text("Approved words.", encoding="utf-8")
    first = _command("enqueue_linkedin_post", "--operator", "Eddy", "--external-ref", "p-1", "--text-file", str(text))
    assert first == {"external_ref": "p-1", "id": LinkedInPost.objects.get().pk, "operator": "Chuka", "status": "queued", "created": True}
    again = _command("enqueue_linkedin_post", "--operator", "chuka", "--external-ref", "p-1", "--text-file", str(text))
    assert again == {**first, "created": False}
    text.write_text("Different words.", encoding="utf-8")
    with pytest.raises(CommandError, match="different payload"):
        call_command("enqueue_linkedin_post", "--operator", "Eddy", "--external-ref", "p-1", "--text-file", str(text), stdout=StringIO())
    with pytest.raises(CommandError, match="text, media or both"):
        call_command("enqueue_linkedin_post", "--operator", "Eddy", "--external-ref", "p-2", stdout=StringIO())


# --------------------------------------------------------------------------- status, cancel, retry


@pytest.mark.django_db
def test_status_cancel_and_retry_mirror_reposts():
    posts.enqueue_post(operator="Arian", external_ref="p-1", commentary="Words.")
    assert _command("linkedin_post_status", "--external-ref", "p-1")["status"] == "queued"
    assert _command("cancel_linkedin_post", "--external-ref", "p-1") == {"external_ref": "p-1", "status": "skipped", "cancelled": True}
    with pytest.raises(CommandError, match="status is skipped"):
        call_command("cancel_linkedin_post", "--external-ref", "p-1", stdout=StringIO())

    posts.enqueue_post(operator="Arian", external_ref="p-2", commentary="Words.")
    for attempt in (1, 2):
        LinkedInPost.objects.filter(external_ref="p-2").update(status="failed", error_type="PostComposerError", completed_at=timezone.now())
        assert _command("retry_linkedin_post", "--external-ref", "p-2")["retry_count"] == attempt
    LinkedInPost.objects.filter(external_ref="p-2").update(status="failed", completed_at=timezone.now())
    with pytest.raises(CommandError, match="already retried 2 times"):
        call_command("retry_linkedin_post", "--external-ref", "p-2", stdout=StringIO())

    posts.enqueue_post(operator="Arian", external_ref="p-3", commentary="Words.")
    LinkedInPost.objects.filter(external_ref="p-3").update(status="failed", click_attempted_at=timezone.now())
    with pytest.raises(CommandError, match="never clicked"):
        call_command("retry_linkedin_post", "--external-ref", "p-3", stdout=StringIO())
    for status in ("uncertain", "sent", "running"):
        LinkedInPost.objects.filter(external_ref="p-3").update(status=status, result_url=NEW if status == "sent" else "")
        with pytest.raises(CommandError, match=f"status is {status}"):
            call_command("retry_linkedin_post", "--external-ref", "p-3", stdout=StringIO())


@pytest.mark.django_db
def test_a_running_row_past_its_bound_reads_as_uncertain():
    row, _ = posts.enqueue_post(operator="Arian", external_ref="p-1", commentary="Words.")
    LinkedInPost.objects.filter(pk=row.pk).update(status="running", started_at=timezone.now() - timedelta(hours=1))
    assert posts.post_status("p-1")["status"] == "uncertain"


# --------------------------------------------------------------------------- media download


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _media(kind="document", **extra):
    if kind == "document":
        return {"kind": "document", "url": PDF_URL, "sha256": PDF_SHA, "size": len(PDF), "name": "carousel.pdf", "title": "T", **extra}
    return {"kind": "image", "url": PNG_URL, "sha256": PNG_SHA, "size": len(PNG), "name": "image.png", "title": "", **extra}


def test_fetch_media_keeps_only_the_exact_file(tmp_path):
    path = posts.fetch_media(_media(), tmp_path / "media", opener=lambda url: FakeResponse(PDF))
    assert path.read_bytes() == PDF and path.name == f"{PDF_SHA}.pdf"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(PostMediaMismatchError):
        posts.fetch_media(_media(), tmp_path / "m2", opener=lambda url: FakeResponse(PDF + b"x"))
    assert not list((tmp_path / "m2").iterdir()), "a mismatched download is deleted"
    with pytest.raises(PostMediaMismatchError):
        posts.fetch_media(_media(size=len(PDF) + 1), tmp_path / "m3", opener=lambda url: FakeResponse(PDF))

    def down(url):
        raise OSError("network")

    with pytest.raises(PostMediaUnavailableError) as raised:
        posts.fetch_media(_media(), tmp_path / "m4", opener=down)
    assert raised.value.error_type == "OSError"
    with pytest.raises(PostMediaUnavailableError):
        posts.fetch_media(_media(url="https://example.com/x.pdf"), tmp_path / "m5", opener=lambda url: pytest.fail("never fetched"))


# --------------------------------------------------------------------------- worker


@pytest.fixture
def senders(db):
    return {
        "Chuka": LinkedInProfile.objects.create(user=UserFactory(), linkedin_username=CHUKA_LOGIN, linkedin_password="unused"),
        "Arian": LinkedInProfile.objects.create(user=UserFactory(), linkedin_username=ARIAN_LOGIN, linkedin_password="unused"),
    }


class FakeAttach:
    def __init__(self, error=None):
        self.error = error

    @contextmanager
    def __call__(self, port):
        if self.error:
            raise self.error
        yield object()


def _worker(tmp_path, perform, *, login=CHUKA_LOGIN, attach=None, notify=None, media_fetcher=None):
    return posts.run_once(
        linkedin_username=login, root=tmp_path, port=9222,
        attach=attach or FakeAttach(), perform=perform,
        page_opener=lambda browser, url, **hooks: pytest.fail("fake perform never opens pages"),
        notify=notify or (lambda row: None),
        media_fetcher=media_fetcher or (lambda media, directory: pytest.fail("no media expected")),
    )


def _queued(ref, operator, commentary="Approved words.", age_seconds=0, **media):
    row = LinkedInPost.objects.create(external_ref=ref, operator=operator, commentary=commentary, **media)
    LinkedInPost.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(seconds=age_seconds))
    return row


def test_worker_posts_its_own_operators_oldest_due_row(senders, tmp_path):
    _queued("arian-1", "Arian", age_seconds=300)
    _queued("chuka-new", "Chuka", age_seconds=10)
    _queued("chuka-old", "Chuka", commentary="Oldest.", age_seconds=200)
    later = _queued("chuka-later", "Chuka", age_seconds=500)
    LinkedInPost.objects.filter(pk=later.pk).update(scheduled_for=timezone.now() + timedelta(hours=1))
    calls = []

    def perform(open_page, *, commentary, media, on_click_attempt):
        calls.append((commentary, media))
        on_click_attempt()
        return NEW

    summary = _worker(tmp_path, perform)
    assert summary == {"operator": "Chuka", "swept": [], "claimed": "chuka-old", "status": "sent", "error_type": None, "result_url": NEW}
    assert calls == [("Oldest.", None)]
    rows = {row.external_ref: row for row in LinkedInPost.objects.all()}
    assert rows["chuka-old"].status == "sent" and rows["chuka-old"].result_url == NEW
    assert rows["chuka-old"].click_attempted_at is not None
    assert {rows[r].status for r in ("arian-1", "chuka-new", "chuka-later")} == {"queued"}


def test_worker_downloads_media_passes_it_and_deletes_it_after(senders, tmp_path):
    _queued("chuka-doc", "Chuka", **_document())
    seen = {}

    def fetch(media, directory):
        target = directory / "file.pdf"
        directory.mkdir(parents=True)
        target.write_bytes(PDF)
        seen["path"] = target
        return target

    def perform(open_page, *, commentary, media, on_click_attempt):
        assert media["path"].read_bytes() == PDF
        assert (media["kind"], media["title"]) == ("document", "Quick 411 on VDR Response")
        on_click_attempt()
        return NEW

    assert _worker(tmp_path, perform, media_fetcher=fetch)["status"] == "sent"
    assert not seen["path"].exists(), "the downloaded copy is removed after the run"
    assert "linkedin-posts" in str(seen["path"]), "a private directory of its own, not the repost one"


def test_media_mismatch_fails_before_any_click(senders, tmp_path):
    _queued("chuka-doc", "Chuka", **_document())

    def fetch(media, directory):
        raise PostMediaMismatchError("differs")

    summary = _worker(tmp_path, lambda *a, **k: pytest.fail("never opens LinkedIn"), media_fetcher=fetch)
    row = LinkedInPost.objects.get()
    assert (row.status, row.error_type, row.click_attempted_at) == ("failed", "PostMediaMismatchError", None)
    assert summary["status"] == "failed"


def test_an_invalid_row_written_by_hand_fails_before_opening_linkedin(senders, tmp_path):
    _queued("chuka-bad", "Chuka", commentary=" padded")
    summary = _worker(tmp_path, lambda *a, **k: pytest.fail("never opens LinkedIn"))
    assert LinkedInPost.objects.values_list("status", "error_type").get() == ("failed", "PostInvalidRowError")
    assert summary["error_type"] == "PostInvalidRowError"


def test_failure_before_click_is_failed_and_after_click_is_uncertain(senders, tmp_path):
    _queued("chuka-1", "Chuka")

    def mismatch(open_page, **kwargs):
        raise PostCommentaryMismatchError("page text must never be stored")

    _worker(tmp_path, mismatch)
    assert LinkedInPost.objects.values_list("status", "error_type").get() == ("failed", "PostCommentaryMismatchError")

    _queued("chuka-2", "Chuka")

    def lost(open_page, *, on_click_attempt, **kwargs):
        on_click_attempt()
        raise PostUncertainError("lost", error_type="TargetClosedError")

    _worker(tmp_path, lost)
    row = LinkedInPost.objects.get(external_ref="chuka-2")
    assert (row.status, row.error_type, row.result_url) == ("uncertain", "TargetClosedError", "")


def test_unverified_permalink_is_never_sent(senders, tmp_path):
    _queued("chuka-1", "Chuka")

    def perform(open_page, *, on_click_attempt, **kwargs):
        on_click_attempt()
        return "https://www.linkedin.com/feed/"

    summary = _worker(tmp_path, perform)
    assert LinkedInPost.objects.values_list("status", "error_type").get() == ("uncertain", "PostUnverifiedError")
    assert summary["result_url"] is None


def test_unexpected_error_after_click_is_uncertain_and_crashes(senders, tmp_path):
    _queued("chuka-1", "Chuka")

    def perform(open_page, *, on_click_attempt, **kwargs):
        on_click_attempt()
        raise KeyError("bug")

    with pytest.raises(KeyError):
        _worker(tmp_path, perform)
    assert LinkedInPost.objects.values_list("status", "error_type").get() == ("uncertain", "KeyError")


def test_click_attempt_is_refused_when_the_row_changed(senders, tmp_path):
    row = _queued("chuka-1", "Chuka")

    def perform(open_page, *, on_click_attempt, **kwargs):
        LinkedInPost.objects.filter(pk=row.pk).update(status="uncertain")
        on_click_attempt()
        pytest.fail("click must not proceed")

    _worker(tmp_path, perform)
    assert LinkedInPost.objects.values_list("status", "click_attempted_at").get() == ("uncertain", None)


def test_interrupted_rows_become_uncertain_are_announced_and_never_retried(senders, tmp_path):
    announced = []
    clicked = _queued("chuka-clicked", "Chuka")
    LinkedInPost.objects.filter(pk=clicked.pk).update(status="running", started_at=timezone.now(), click_attempted_at=timezone.now())
    summary = _worker(tmp_path, lambda *a, **k: pytest.fail("nothing queued"), notify=lambda row: announced.append((row.external_ref, row.status)))
    assert summary["swept"] == ["chuka-clicked"] and announced == [("chuka-clicked", "uncertain")]
    assert LinkedInPost.objects.get().error_type == "PostInterruptedError"


def test_worker_waits_while_a_post_or_a_repost_of_the_same_person_is_running(senders, tmp_path):
    live = _queued("chuka-live", "Chuka")
    LinkedInPost.objects.filter(pk=live.pk).update(status="running", started_at=timezone.now())
    _queued("chuka-next", "Chuka")
    assert _worker(tmp_path, lambda *a, **k: pytest.fail("must not claim beside a live row"))["claimed"] is None

    LinkedInPost.objects.filter(pk=live.pk).update(status="sent", result_url=NEW)
    LinkedInRepost.objects.create(
        external_ref="repost-live", operator="Chuka", status="running", started_at=timezone.now(),
        post_url="https://www.linkedin.com/feed/update/urn:li:activity:1/",
    )
    assert _worker(tmp_path, lambda *a, **k: pytest.fail("a repost is driving the browser"))["claimed"] is None
    assert LinkedInPost.objects.get(external_ref="chuka-next").status == "queued"


def test_unavailable_browser_leaves_the_row_queued(senders, tmp_path):
    _queued("chuka-1", "Chuka")
    with pytest.raises(PostBrowserUnavailableError):
        _worker(tmp_path, lambda *a, **k: pytest.fail("no browser"), attach=FakeAttach(PostBrowserUnavailableError(error_type="Error")))
    assert LinkedInPost.objects.get().status == "queued"


@pytest.mark.parametrize("change", [{"stop_requested": True}, {"active": False}])
def test_worker_refuses_stopped_or_inactive_sender(senders, tmp_path, change):
    LinkedInProfile.objects.filter(pk=senders["Chuka"].pk).update(**change)
    _queued("chuka-1", "Chuka")
    with pytest.raises(LinkedInPostWorkerRefused, match="stopped or inactive"):
        _worker(tmp_path, lambda *a, **k: pytest.fail("refused"))
    with pytest.raises(LinkedInPostWorkerRefused, match="not enabled"):
        _worker(tmp_path, None, login="leili.ash2011@yahoo.com")


def test_a_retried_row_is_posted_again_as_the_same_row(senders, tmp_path):
    row = _queued("chuka-1", "Chuka")
    LinkedInPost.objects.filter(pk=row.pk).update(status="failed", error_type="Error", completed_at=timezone.now())
    posts.retry_post("chuka-1")

    def perform(open_page, *, on_click_attempt, **kwargs):
        on_click_attempt()
        return NEW

    assert _worker(tmp_path, perform)["claimed"] == "chuka-1"
    assert LinkedInPost.objects.get(pk=row.pk).status == "sent"


def test_worker_closes_only_its_own_leftover_targets(senders, tmp_path, monkeypatch):
    from linkedin.reposts import RepostTargetReceipt

    closed = []
    monkeypatch.setattr("linkedin.feed_sampling.close_sample_target", lambda port, target: closed.append(target) or True)
    posts.PostTargetReceipt(tmp_path, CHUKA_LOGIN, 9222).add("post-left-behind")
    RepostTargetReceipt(tmp_path, CHUKA_LOGIN, 9222).add("repost-tab")
    _worker(tmp_path, lambda *a, **k: pytest.fail("nothing queued"))
    assert closed == ["post-left-behind"], "the repost worker's tabs are not the post worker's to close"


# --------------------------------------------------------------------------- Slack


def test_each_finished_post_sends_one_feed_message_and_slack_never_changes_the_outcome(senders, tmp_path, monkeypatch):
    sent = []
    _queued("chuka-1", "Chuka")

    def perform(open_page, *, on_click_attempt, **kwargs):
        on_click_attempt()
        return NEW

    _worker(tmp_path, perform, notify=lambda row: sent.append((row.external_ref, row.status)))
    assert sent == [("chuka-1", "sent")]

    def broken(**kwargs):
        raise RuntimeError("slack down")

    monkeypatch.setattr("linkedin.notifications.slack.notify_post_outcome", broken)
    posts.announce(LinkedInPost.objects.get())
    assert LinkedInPost.objects.get().status == "sent"


def test_post_slack_messages_go_to_the_feed_channel(monkeypatch):
    from linkedin.notifications import slack

    posted = []
    monkeypatch.setattr(slack, "_post_to_slack", lambda url, payload, label: posted.append((url, payload)))
    monkeypatch.setattr(slack, "SLACK_HIGH_SIGNAL_URL", "")
    slack.notify_post_outcome(operator="Chuka", status="sent", result_url=NEW)
    assert posted == []
    monkeypatch.setattr(slack, "SLACK_HIGH_SIGNAL_URL", "https://hooks.slack.test/feed")
    slack.notify_post_outcome(operator="Chuka", status="sent", result_url=NEW)
    slack.notify_post_outcome(operator="Arian", status="failed", error_type="PostComposerError")
    slack.notify_post_outcome(operator="Arian", status="uncertain", error_type="PostUnverifiedError")
    assert [url for url, _ in posted] == ["https://hooks.slack.test/feed"] * 3
    assert [payload["text"] for _, payload in posted] == [
        ":white_check_mark: Eddy posted on LinkedIn",
        ":warning: Arian's post didn't go out",
        ":warning: Arian's post may have posted",
    ]
    details = [payload["blocks"][1]["text"]["text"] for _, payload in posted]
    assert f"<{NEW}|View Eddy's post>" in details[0]
    assert "nothing was posted" in details[1] and "never sent twice" in details[2]


# --------------------------------------------------------------------------- flag and command


def test_run_command_refuses_when_flag_is_off(monkeypatch):
    monkeypatch.delenv("ENABLE_LINKEDIN_POSTS", raising=False)
    with pytest.raises(CommandError, match="ENABLE_LINKEDIN_POSTS is off"):
        call_command("run_linkedin_post_once", stdout=StringIO())


def test_run_command_prints_summary(monkeypatch, senders):
    monkeypatch.setenv("ENABLE_LINKEDIN_POSTS", "true")
    monkeypatch.setenv("LINKEDIN_USERNAME", CHUKA_LOGIN)
    monkeypatch.setattr("linkedin.management.commands.run_linkedin_post_once.signal.signal", lambda *a: None)
    seen = {}

    def fake_run_once(**kwargs):
        seen.update(kwargs)
        return {"operator": "Chuka", "swept": [], "claimed": None}

    monkeypatch.setattr("linkedin.management.commands.run_linkedin_post_once.run_once", fake_run_once)
    assert _command("run_linkedin_post_once") == {"operator": "Chuka", "swept": [], "claimed": None}
    assert seen["linkedin_username"] == CHUKA_LOGIN
    assert Path(seen["root"]) == Path(posts.__file__).resolve().parents[1]


def test_flag_is_registered_off_by_default_and_documented():
    var = next(var for var in env_spec.ENV_VARS if var.name == "ENABLE_LINKEDIN_POSTS")
    assert (var.required, var.secret, var.default, var.group) == (False, False, "false", "feature_flags")
    assert "ENABLE_LINKEDIN_POSTS=false" in Path(".env.example").read_text().splitlines()


def test_flag_parsing(monkeypatch):
    for value, expected in [(None, False), ("false", False), ("true", True), ("1", True), ("on", True)]:
        if value is None:
            monkeypatch.delenv("ENABLE_LINKEDIN_POSTS", raising=False)
        else:
            monkeypatch.setenv("ENABLE_LINKEDIN_POSTS", value)
        assert posts.posts_enabled() is expected


def test_budget_bounds_the_slowest_typing_and_the_supervisor_waits_longer():
    assert posts.budget_seconds("") == posts.BASE_BUDGET_SECONDS
    assert posts.SUPERVISOR_TIMEOUT_SECONDS > posts.budget_seconds("x" * 3000) + posts.PROCESS_GRACE_SECONDS


def test_error_type_is_class_name_shaped_only():
    assert posts.safe_error_type("PostComposerError") == "PostComposerError"
    assert posts.safe_error_type("Timeout <div>") == "PostError"
