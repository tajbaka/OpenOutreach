"""Repost ledger, management commands and one worker run (SQLite, no browser)."""
import json
from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from linkedin import env_spec, reposts
from linkedin.exceptions import (
    LinkedInRepostImmutableError,
    LinkedInRepostWorkerRefused,
    RepostBrowserUnavailableError,
    RepostCommentaryMismatchError,
    RepostUncertainError,
    RepostUnverifiedError,
)
from linkedin.models import LinkedInProfile, LinkedInRepost
from tests.factories import UserFactory

POST = "https://www.linkedin.com/feed/update/urn:li:activity:7300000000000000001/"
OTHER_POST = "https://www.linkedin.com/feed/update/urn:li:ugcPost:7300000000000000002/"
NEW = "https://www.linkedin.com/feed/update/urn:li:share:7300000000000000009/"
CHUKA_LOGIN = "eddy@boundera.io"
ARIAN_LOGIN = "arian@boundera.io"


def _command(name, *args):
    out = StringIO()
    call_command(name, *args, stdout=out)
    return json.loads(out.getvalue())


def _enqueue(*args):
    return _command("enqueue_linkedin_repost", *args)


@pytest.fixture
def commentary_file(tmp_path):
    def write(text: str | bytes):
        path = tmp_path / "commentary.txt"
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return str(path)
    return write


# --------------------------------------------------------------------------- enqueue


def test_enqueue_prints_contract_and_resolves_eddy_to_chuka():
    result = _enqueue("--operator", "Eddy", "--post-url", POST, "--external-ref", "bss:repost:1")
    row = LinkedInRepost.objects.get()
    assert result == {"external_ref": "bss:repost:1", "id": row.pk, "operator": "Chuka", "status": "queued", "created": True}
    assert row.commentary == "" and row.operator == "Chuka"


def test_enqueue_is_idempotent_for_identical_payload(commentary_file):
    path = commentary_file("Approved words.\nSecond line.")
    first = _enqueue("--operator", "arian", "--post-url", POST, "--external-ref", "r-1", "--commentary-file", path)
    LinkedInRepost.objects.filter(pk=first["id"]).update(status="sent", result_url=NEW)
    again = _enqueue("--operator", "Arian", "--post-url", POST, "--external-ref", "r-1", "--commentary-file", path)
    assert again == {**first, "status": "sent", "created": False}
    assert LinkedInRepost.objects.count() == 1


@pytest.mark.parametrize("change", ["operator", "post", "commentary"])
def test_enqueue_conflicting_payload_changes_nothing(commentary_file, change):
    path = commentary_file("Approved words.")
    _enqueue("--operator", "Arian", "--post-url", POST, "--external-ref", "r-1", "--commentary-file", path)
    before = list(LinkedInRepost.objects.values())
    args = {"operator": "Arian", "post": POST, "commentary": path}
    if change == "operator":
        args["operator"] = "Chuka"
    elif change == "post":
        args["post"] = OTHER_POST
    else:
        args["commentary"] = commentary_file("Different words.")
    with pytest.raises(CommandError, match="different payload"):
        _enqueue("--operator", args["operator"], "--post-url", args["post"], "--external-ref", "r-1",
                 "--commentary-file", args["commentary"])
    assert list(LinkedInRepost.objects.values()) == before


@pytest.mark.parametrize("operator", ["Leili", "Athena", "chukyjack@gmail.com", "", "Bob"])
def test_enqueue_refuses_other_operators(operator):
    with pytest.raises(CommandError, match="Arian, Chuka or Eddy"):
        _enqueue("--operator", operator, "--post-url", POST, "--external-ref", "r-1")
    assert not LinkedInRepost.objects.exists()


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/feed/update/urn:li:activity:73/?utm=1",
    "https://www.linkedin.com/feed/update/urn:li:activity:73",
    "http://www.linkedin.com/feed/update/urn:li:activity:73/",
    "https://linkedin.com/feed/update/urn:li:activity:73/",
    "https://www.linkedin.com/feed/update/urn:li:comment:73/",
    "https://www.linkedin.com/posts/boundera_activity-73",
    "https://www.linkedin.com.evil.example/feed/update/urn:li:activity:73/",
])
def test_enqueue_accepts_only_canonical_post_urls(url):
    with pytest.raises(CommandError, match="--post-url"):
        _enqueue("--operator", "Arian", "--post-url", url, "--external-ref", "r-1")


@pytest.mark.parametrize("ref", ["", "x" * 101, "has space", "-leading", "a/b"])
def test_enqueue_validates_external_ref(ref):
    with pytest.raises(CommandError, match="--external-ref"):
        _enqueue("--operator", "Arian", "--post-url", POST, "--external-ref", ref)


@pytest.mark.parametrize("text,message", [
    (" leading", "whitespace"),
    ("trailing\n", "whitespace"),
    ("tab\there", "control"),
    ("windows\r\nline", "control"),
    ("\ufeffbom", "byte-order"),
    ("x" * 3001, "3000"),
])
def test_enqueue_refuses_ambiguous_commentary(commentary_file, text, message):
    with pytest.raises(CommandError, match=message):
        _enqueue("--operator", "Arian", "--post-url", POST, "--external-ref", "r-1",
                 "--commentary-file", commentary_file(text))
    assert not LinkedInRepost.objects.exists()


def test_enqueue_refuses_non_utf8_and_missing_files(commentary_file, tmp_path):
    with pytest.raises(CommandError, match="UTF-8"):
        _enqueue("--operator", "Arian", "--post-url", POST, "--external-ref", "r-1",
                 "--commentary-file", commentary_file(b"\xff\xfe"))
    with pytest.raises(CommandError, match="could not be read"):
        _enqueue("--operator", "Arian", "--post-url", POST, "--external-ref", "r-1",
                 "--commentary-file", str(tmp_path / "missing.txt"))


def test_empty_commentary_file_is_a_plain_repost(commentary_file):
    result = _enqueue("--operator", "Arian", "--post-url", POST, "--external-ref", "r-1",
                      "--commentary-file", commentary_file(""))
    assert LinkedInRepost.objects.get(pk=result["id"]).commentary == ""


# --------------------------------------------------------------------------- model guards


def test_payload_is_immutable_through_save_and_queryset_update():
    row = LinkedInRepost.objects.create(external_ref="r-1", operator="Arian", post_url=POST)
    row.commentary = "changed"
    with pytest.raises(LinkedInRepostImmutableError):
        row.save()
    with pytest.raises(LinkedInRepostImmutableError):
        LinkedInRepost.objects.filter(pk=row.pk).update(post_url=OTHER_POST)
    row.refresh_from_db()
    row.status = "skipped"
    row.save()  # non-payload fields still change
    assert LinkedInRepost.objects.get(pk=row.pk).commentary == ""


def test_sent_rows_require_a_result_url_at_the_database():
    row = LinkedInRepost.objects.create(external_ref="r-1", operator="Arian", post_url=POST)
    with pytest.raises(IntegrityError), transaction.atomic():
        LinkedInRepost.objects.filter(pk=row.pk).update(status="sent")


def test_migration_matches_model_on_sqlite():
    call_command("makemigrations", "linkedin", check=True, dry_run=True, verbosity=0)


# --------------------------------------------------------------------------- status / cancel


def test_status_contract_and_stale_running_reads_as_uncertain():
    _enqueue("--operator", "Arian", "--post-url", POST, "--external-ref", "r-1")
    assert _command("linkedin_repost_status", "--external-ref", "r-1") == {
        "external_ref": "r-1", "status": "queued", "scheduled_for": None, "result_url": None, "error_type": None,
        "click_attempted_at": None, "completed_at": None,
    }
    started = timezone.now() - timedelta(seconds=reposts.budget_seconds("") + reposts.PROCESS_GRACE_SECONDS + 5)
    LinkedInRepost.objects.filter(external_ref="r-1").update(status="running", started_at=started)
    assert _command("linkedin_repost_status", "--external-ref", "r-1")["status"] == "uncertain"
    # Status is read-only.
    assert LinkedInRepost.objects.get(external_ref="r-1").status == "running"
    LinkedInRepost.objects.filter(external_ref="r-1").update(started_at=timezone.now())
    assert _command("linkedin_repost_status", "--external-ref", "r-1")["status"] == "running"


def test_status_reports_sent_result():
    now = timezone.now()
    LinkedInRepost.objects.create(external_ref="r-1", operator="Arian", post_url=POST, status="sent",
                                  result_url=NEW, click_attempted_at=now, completed_at=now)
    result = _command("linkedin_repost_status", "--external-ref", "r-1")
    assert result["status"] == "sent" and result["result_url"] == NEW
    assert result["click_attempted_at"] == now.isoformat()


def test_enqueue_post_at_sets_the_time_and_needs_a_zone():
    _enqueue("--operator", "Arian", "--post-url", POST, "--external-ref", "r-1", "--post-at", "2026-10-30T13:15:00Z")
    assert _command("linkedin_repost_status", "--external-ref", "r-1")["scheduled_for"] == "2026-10-30T13:15:00+00:00"
    for bad in ["2026-10-30T13:15:00", "tomorrow"]:
        with pytest.raises(CommandError, match="--post-at"):
            _enqueue("--operator", "Arian", "--post-url", POST, "--external-ref", "r-2", "--post-at", bad)
    assert not LinkedInRepost.objects.filter(external_ref="r-2").exists()


def test_status_of_unknown_ref_is_an_error():
    with pytest.raises(CommandError, match="No LinkedIn repost"):
        call_command("linkedin_repost_status", "--external-ref", "nope", stdout=StringIO())


def test_cancel_only_skips_queued_rows():
    _enqueue("--operator", "Arian", "--post-url", POST, "--external-ref", "r-1")
    assert _command("cancel_linkedin_repost", "--external-ref", "r-1") == {
        "external_ref": "r-1", "status": "skipped", "cancelled": True,
    }
    for status in ["skipped", "running", "sent", "failed", "uncertain"]:
        LinkedInRepost.objects.filter(external_ref="r-1").update(status=status, result_url=NEW)
        with pytest.raises(CommandError, match=f"status is {status}"):
            call_command("cancel_linkedin_repost", "--external-ref", "r-1", stdout=StringIO())
        assert LinkedInRepost.objects.get(external_ref="r-1").status == status


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
        self.ports = []

    @contextmanager
    def __call__(self, port):
        self.ports.append(port)
        if self.error:
            raise self.error
        yield object()


def _worker(tmp_path, perform, *, login=CHUKA_LOGIN, attach=None, notify=None):
    return reposts.run_once(
        linkedin_username=login, root=tmp_path, port=9222,
        attach=attach or FakeAttach(), perform=perform,
        page_opener=lambda browser, url, **hooks: pytest.fail("fake perform never opens pages"),
        notify=notify or (lambda row: None),
    )


def _queued(ref, operator, commentary="", age_seconds=0):
    row = LinkedInRepost.objects.create(external_ref=ref, operator=operator, post_url=POST, commentary=commentary)
    LinkedInRepost.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(seconds=age_seconds))
    return row


def test_worker_claims_only_its_own_operators_oldest_row(senders, tmp_path):
    _queued("arian-1", "Arian", age_seconds=300)
    _queued("chuka-new", "Chuka", age_seconds=10)
    _queued("chuka-old", "Chuka", commentary="Approved.", age_seconds=200)
    calls = []

    def perform(open_page, *, post_url, commentary, on_click_attempt):
        calls.append((post_url, commentary))
        on_click_attempt()
        return NEW

    summary = _worker(tmp_path, perform)
    assert summary == {"operator": "Chuka", "swept": [], "claimed": "chuka-old", "status": "sent",
                       "error_type": None, "result_url": NEW}
    assert calls == [(POST, "Approved.")]
    rows = {row.external_ref: row for row in LinkedInRepost.objects.all()}
    assert rows["chuka-old"].status == "sent" and rows["chuka-old"].result_url == NEW
    assert rows["chuka-old"].click_attempted_at is not None and rows["chuka-old"].completed_at is not None
    assert rows["chuka-new"].status == "queued"
    assert rows["arian-1"].status == "queued"


def test_worker_waits_for_each_rows_time_then_takes_the_earliest_due(senders, tmp_path):
    now = timezone.now()
    later = _queued("chuka-later", "Chuka", age_seconds=600)
    LinkedInRepost.objects.filter(pk=later.pk).update(scheduled_for=now + timedelta(minutes=15))
    assert reposts.has_queued_repost("Chuka") is False, "nothing is due yet, so the supervisor starts no child"
    calls = []

    def perform(open_page, *, post_url, commentary, on_click_attempt):
        calls.append(commentary)
        on_click_attempt()
        return NEW

    assert _worker(tmp_path, perform)["claimed"] is None
    assert calls == [] and LinkedInRepost.objects.get(pk=later.pk).status == "queued"

    due_late = _queued("chuka-due-late", "Chuka", commentary="Second.", age_seconds=10)
    LinkedInRepost.objects.filter(pk=due_late.pk).update(scheduled_for=now - timedelta(minutes=1))
    due_early = _queued("chuka-due-early", "Chuka", commentary="First.", age_seconds=5)
    LinkedInRepost.objects.filter(pk=due_early.pk).update(scheduled_for=now - timedelta(minutes=5))
    assert reposts.has_queued_repost("Chuka") is True
    assert _worker(tmp_path, perform)["claimed"] == "chuka-due-early", "earliest time first, not oldest row"
    assert reposts.claim_next("Chuka", now=now + timedelta(minutes=16)) is not None
    assert {r.external_ref: r.status for r in LinkedInRepost.objects.filter(operator="Chuka")} == {
        "chuka-later": "queued", "chuka-due-late": "running", "chuka-due-early": "sent",
    }, "a later row waits behind the due one; the next due row is claimed once the first is done"


def test_a_row_written_with_social_studios_columns_is_claimed_when_due(senders, tmp_path):
    """Social Studio's dashboard inserts rows with SQL; these are exactly the columns it writes."""
    from django.db import connection

    now = timezone.now()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO linkedin_linkedinrepost (external_ref, operator, post_url, commentary, status, scheduled_for,"
            " result_url, error_type, created_at, updated_at) VALUES (%s, %s, %s, %s, 'queued', %s, '', '', %s, %s)",
            ["social-studio-attempt-1", "Chuka", POST, "From the dashboard.", now - timedelta(seconds=30), now, now],
        )
    seen = []

    def perform(open_page, *, post_url, commentary, on_click_attempt):
        seen.append((post_url, commentary))
        on_click_attempt()
        return NEW

    assert _worker(tmp_path, perform)["status"] == "sent"
    assert seen == [(POST, "From the dashboard.")]
    assert _command("linkedin_repost_status", "--external-ref", "social-studio-attempt-1")["result_url"] == NEW


def test_each_finished_repost_sends_one_slack_message_and_slack_never_changes_the_outcome(senders, tmp_path, monkeypatch):
    from linkedin.exceptions import RepostSendError

    sent = []

    def notify(row):
        sent.append((row.external_ref, row.status))

    def posts(open_page, *, post_url, commentary, on_click_attempt):
        on_click_attempt()
        return NEW

    _queued("chuka-1", "Chuka")
    _worker(tmp_path, posts, notify=notify)
    assert sent == [("chuka-1", "sent")]

    _queued("chuka-2", "Chuka")

    def fails(open_page, *, post_url, commentary, on_click_attempt):
        raise RepostSendError("no control", error_type="RepostControlNotFound")

    _worker(tmp_path, fails, notify=notify)
    assert sent[-1] == ("chuka-2", "failed")

    interrupted = _queued("chuka-3", "Chuka")
    LinkedInRepost.objects.filter(pk=interrupted.pk).update(status="running", started_at=timezone.now(), click_attempted_at=timezone.now())
    _worker(tmp_path, lambda *a, **k: pytest.fail("nothing queued"), notify=notify)
    assert sent[-1] == ("chuka-3", "uncertain"), "an interrupted row swept to uncertain is announced too"
    assert len(sent) == 3

    # The real announcer: a broken Slack call is logged, never raised.
    def broken(**kwargs):
        raise RuntimeError("slack down")

    monkeypatch.setattr("linkedin.notifications.slack.notify_repost_outcome", broken)
    reposts.announce(LinkedInRepost.objects.get(external_ref="chuka-1"))
    assert LinkedInRepost.objects.get(external_ref="chuka-1").status == "sent"


def test_repost_slack_messages_go_to_the_feed_channel_not_ops(monkeypatch):
    from linkedin.notifications import slack

    posted = []
    monkeypatch.setattr(slack, "_post_to_slack", lambda url, payload, label: posted.append((url, payload, label)))
    monkeypatch.setattr(slack, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/ops")
    monkeypatch.setattr(slack, "SLACK_HIGH_SIGNAL_URL", "")
    slack.notify_repost_outcome(operator="Chuka", status="sent", post_url=POST, result_url=NEW)
    assert posted == [], "no feed webhook: nothing is sent, and never to ops instead"

    monkeypatch.setattr(slack, "SLACK_HIGH_SIGNAL_URL", "https://hooks.slack.test/feed")
    slack.notify_repost_outcome(operator="Chuka", status="sent", post_url=POST, result_url=NEW)
    slack.notify_repost_outcome(operator="Arian", status="failed", post_url=POST, error_type="RepostControlNotFound")
    slack.notify_repost_outcome(operator="Arian", status="uncertain", post_url=POST, error_type="RepostUnverifiedError")
    assert [url for url, _, _ in posted] == ["https://hooks.slack.test/feed"] * 3
    texts = [payload["text"] for _, payload, _ in posted]
    assert texts == [
        ":white_check_mark: Eddy reposted a Boundera post",
        ":warning: Arian's repost didn't go out",
        ":warning: Arian's repost may have posted",
    ]
    details = [payload["blocks"][1]["text"]["text"] for _, payload, _ in posted]
    assert f"<{NEW}|View Eddy's repost>" in details[0] and f"<{POST}|Original post>" in details[0]
    assert "nothing was posted" in details[1] and "RepostControlNotFound" in details[1]
    assert "never sent twice" in details[2]


def test_worker_marks_stale_running_rows_uncertain_and_never_retries_them(senders, tmp_path):
    now = timezone.now()
    clicked = _queued("chuka-clicked", "Chuka", age_seconds=600)
    LinkedInRepost.objects.filter(pk=clicked.pk).update(status="running", started_at=now, click_attempted_at=now)
    old = _queued("chuka-old-running", "Chuka", age_seconds=600)
    LinkedInRepost.objects.filter(pk=old.pk).update(status="running", started_at=now - timedelta(hours=1))
    arian = _queued("arian-running", "Arian")
    LinkedInRepost.objects.filter(pk=arian.pk).update(status="running", started_at=now, click_attempted_at=now)
    perform_calls = []

    summary = _worker(tmp_path, lambda *a, **k: perform_calls.append(k) or NEW)
    assert summary["swept"] == ["chuka-clicked", "chuka-old-running"]
    assert summary["claimed"] is None and perform_calls == []
    statuses = dict(LinkedInRepost.objects.values_list("external_ref", "status"))
    assert statuses == {"chuka-clicked": "uncertain", "chuka-old-running": "uncertain", "arian-running": "running"}
    assert set(LinkedInRepost.objects.filter(status="uncertain").values_list("error_type", flat=True)) == {"RepostInterruptedError"}
    # A second run does not resurrect them.
    assert _worker(tmp_path, lambda *a, **k: pytest.fail("nothing queued"))["claimed"] is None


def test_worker_waits_while_its_operator_has_a_live_running_row(senders, tmp_path):
    live = _queued("chuka-live", "Chuka")
    LinkedInRepost.objects.filter(pk=live.pk).update(status="running", started_at=timezone.now())
    _queued("chuka-next", "Chuka")
    summary = _worker(tmp_path, lambda *a, **k: pytest.fail("must not claim beside a live row"))
    assert summary["claimed"] is None
    assert LinkedInRepost.objects.get(external_ref="chuka-next").status == "queued"


def test_unavailable_browser_leaves_the_row_queued(senders, tmp_path):
    _queued("chuka-1", "Chuka")
    with pytest.raises(RepostBrowserUnavailableError):
        _worker(tmp_path, lambda *a, **k: pytest.fail("no browser"),
                attach=FakeAttach(RepostBrowserUnavailableError(error_type="Error")))
    assert LinkedInRepost.objects.get().status == "queued"


def test_failure_before_click_is_failed_with_class_name_only(senders, tmp_path):
    _queued("chuka-1", "Chuka", commentary="Approved.")

    def perform(open_page, **kwargs):
        raise RepostCommentaryMismatchError("page text must never be stored")

    summary = _worker(tmp_path, perform)
    row = LinkedInRepost.objects.get()
    assert (row.status, row.error_type, row.click_attempted_at) == ("failed", "RepostCommentaryMismatchError", None)
    assert summary["status"] == "failed"


def test_error_after_click_is_uncertain_and_keeps_cause_type(senders, tmp_path):
    _queued("chuka-1", "Chuka")

    def perform(open_page, *, on_click_attempt, **kwargs):
        on_click_attempt()
        raise RepostUncertainError("lost", error_type="TargetClosedError")

    _worker(tmp_path, perform)
    row = LinkedInRepost.objects.get()
    assert (row.status, row.error_type) == ("uncertain", "TargetClosedError")
    assert row.click_attempted_at is not None and row.result_url == ""


def test_unexpected_error_after_persisted_click_is_uncertain_and_crashes(senders, tmp_path):
    _queued("chuka-1", "Chuka")

    def perform(open_page, *, on_click_attempt, **kwargs):
        on_click_attempt()
        raise KeyError("bug")

    with pytest.raises(KeyError):
        _worker(tmp_path, perform)
    row = LinkedInRepost.objects.get()
    assert (row.status, row.error_type) == ("uncertain", "KeyError")


def test_unverified_permalink_is_never_sent(senders, tmp_path):
    _queued("chuka-1", "Chuka")

    def perform(open_page, *, on_click_attempt, **kwargs):
        on_click_attempt()
        return "https://www.linkedin.com/feed/"

    summary = _worker(tmp_path, perform)
    row = LinkedInRepost.objects.get()
    assert (row.status, row.error_type, row.result_url) == ("uncertain", "RepostUnverifiedError", "")
    assert (summary["status"], summary["error_type"], summary["result_url"]) == ("uncertain", "RepostUnverifiedError", None)


def test_unverified_error_from_action_is_uncertain(senders, tmp_path):
    _queued("chuka-1", "Chuka")

    def perform(open_page, *, on_click_attempt, **kwargs):
        on_click_attempt()
        raise RepostUnverifiedError()

    _worker(tmp_path, perform)
    assert LinkedInRepost.objects.values_list("status", "error_type").get() == ("uncertain", "RepostUnverifiedError")


def test_click_attempt_is_refused_when_row_changed(senders, tmp_path):
    row = _queued("chuka-1", "Chuka")

    def perform(open_page, *, on_click_attempt, **kwargs):
        LinkedInRepost.objects.filter(pk=row.pk).update(status="uncertain")
        on_click_attempt()
        pytest.fail("click must not proceed")

    _worker(tmp_path, perform)
    assert LinkedInRepost.objects.values_list("status", "click_attempted_at").get() == ("uncertain", None)


@pytest.mark.parametrize("change", [{"stop_requested": True}, {"active": False}])
def test_worker_refuses_stopped_or_inactive_sender(senders, tmp_path, change):
    LinkedInProfile.objects.filter(pk=senders["Chuka"].pk).update(**change)
    _queued("chuka-1", "Chuka")
    with pytest.raises(LinkedInRepostWorkerRefused, match="stopped or inactive"):
        _worker(tmp_path, lambda *a, **k: pytest.fail("refused"))
    assert LinkedInRepost.objects.get().status == "queued"


def test_worker_refuses_unknown_missing_or_ambiguous_sender(senders, tmp_path):
    with pytest.raises(LinkedInRepostWorkerRefused, match="not enabled"):
        _worker(tmp_path, None, login="leili.ash2011@yahoo.com")
    with pytest.raises(LinkedInRepostWorkerRefused, match="not configured"):
        _worker(tmp_path, None, login="")
    with pytest.raises(LinkedInRepostWorkerRefused, match="no profiles"):
        _worker(tmp_path, None, login="ariant@boundera.io")
    LinkedInProfile.objects.create(user=UserFactory(), linkedin_username=CHUKA_LOGIN.upper(), linkedin_password="unused")
    with pytest.raises(LinkedInRepostWorkerRefused, match="multiple"):
        _worker(tmp_path, None)


def test_worker_closes_leftover_repost_targets_only(senders, tmp_path, monkeypatch):
    closed = []
    monkeypatch.setattr("linkedin.feed_sampling.close_sample_target", lambda port, target: closed.append((port, target)) or True)
    receipt = reposts.RepostTargetReceipt(tmp_path, CHUKA_LOGIN, 9333)
    receipt.add("left-behind")
    _worker(tmp_path, lambda *a, **k: pytest.fail("nothing queued"))
    assert closed == [(9333, "left-behind")]
    assert not receipt.path.exists()


def test_run_command_refuses_when_flag_is_off(monkeypatch):
    monkeypatch.delenv("ENABLE_LINKEDIN_REPOSTS", raising=False)
    with pytest.raises(CommandError, match="ENABLE_LINKEDIN_REPOSTS is off"):
        call_command("run_linkedin_repost_once", stdout=StringIO())


def test_run_command_prints_summary(monkeypatch, senders):
    monkeypatch.setenv("ENABLE_LINKEDIN_REPOSTS", "true")
    monkeypatch.setenv("LINKEDIN_USERNAME", CHUKA_LOGIN)
    seen = {}
    monkeypatch.setattr("linkedin.management.commands.run_linkedin_repost_once.signal.signal", lambda *a: None)

    def fake_run_once(**kwargs):
        seen.update(kwargs)
        return {"operator": "Chuka", "swept": [], "claimed": None}

    monkeypatch.setattr("linkedin.management.commands.run_linkedin_repost_once.run_once", fake_run_once)
    assert _command("run_linkedin_repost_once") == {"operator": "Chuka", "swept": [], "claimed": None}
    assert seen["linkedin_username"] == CHUKA_LOGIN
    assert Path(seen["root"]) == Path(reposts.__file__).resolve().parents[1]


# --------------------------------------------------------------------------- env


def test_flag_is_registered_off_by_default_and_documented_in_example():
    var = next(var for var in env_spec.ENV_VARS if var.name == "ENABLE_LINKEDIN_REPOSTS")
    assert (var.required, var.secret, var.default, var.group) == (False, False, "false", "feature_flags")
    example = Path(".env.example").read_text().splitlines()
    assert "ENABLE_LINKEDIN_REPOSTS=false" in example


def test_flag_parsing(monkeypatch):
    for value, expected in [(None, False), ("false", False), ("0", False), ("true", True), ("1", True), ("on", True)]:
        if value is None:
            monkeypatch.delenv("ENABLE_LINKEDIN_REPOSTS", raising=False)
        else:
            monkeypatch.setenv("ENABLE_LINKEDIN_REPOSTS", value)
        assert reposts.reposts_enabled() is expected


def test_budget_bounds_the_slowest_typing():
    assert reposts.budget_seconds("") == reposts.BASE_BUDGET_SECONDS
    assert reposts.budget_seconds("x" * 3000) == reposts.BASE_BUDGET_SECONDS + 750
    assert reposts.SUPERVISOR_TIMEOUT_SECONDS > reposts.budget_seconds("x" * 3000) + reposts.PROCESS_GRACE_SECONDS


def test_error_type_is_class_name_shaped_only():
    assert reposts.safe_error_type("TargetClosedError") == "TargetClosedError"
    assert reposts.safe_error_type("Timeout 30000ms exceeded <div>") == "RepostError"
