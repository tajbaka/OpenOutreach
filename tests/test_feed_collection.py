from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dt_timezone

import pytest
from django.core.management import call_command
from django.utils import timezone

from linkedin.feed_collection import (
    CollectionResult,
    FeedPostRecord,
    _collect_from_page,
    claim_due_collection_job,
    catchup_start_date,
    collection_cutoff_for_job,
    collection_window_end_for_job,
    content_hash_for,
    ensure_backfill_collection_jobs,
    ensure_collection_jobs,
    extract_posts_from_page,
    is_specific_post_url,
    mark_job_completed,
    mark_job_failed,
    parse_relative_timestamp,
    post_url_for_activity_urn,
    scheduled_for_local_day,
    upsert_feed_record,
)
from linkedin.models import (
    LinkedInFeedCollectionJob,
    LinkedInFeedObservation,
    LinkedInFeedPost,
)


def _record(**overrides) -> FeedPostRecord:
    data = {
        "activity_urn": "urn:li:activity:123",
        "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:123/?trk=x",
        "author_name": "Pete Strouse",
        "author_headline": "FedRAMP advisor",
        "author_profile_url": "https://www.linkedin.com/in/pete/?mini=true",
        "post_text": "I have an interesting FedRAMP advisory opportunity.",
        "timestamp_text": "1h",
        "posted_at": timezone.now() - timedelta(hours=1),
        "raw_payload": {"source": "test"},
    }
    data.update(overrides)
    return FeedPostRecord(**data)


@pytest.mark.django_db
def test_claim_due_collection_job_creates_and_claims_today_job(monkeypatch):
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_HOUR", 17)
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_MINUTE", 0)
    now = datetime(2026, 7, 3, 22, 30, tzinfo=dt_timezone.utc)

    job = claim_due_collection_job(
        operator="Arian",
        account_username="arian@example.com",
        now=now,
    )

    assert job is not None
    assert job.status == LinkedInFeedCollectionJob.Status.RUNNING
    assert job.collection_date.isoformat() == "2026-07-03"
    assert job.scheduled_for == scheduled_for_local_day(job.collection_date)


@pytest.mark.django_db
def test_claim_due_collection_job_noops_before_scheduled_time(monkeypatch):
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_HOUR", 17)
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_MINUTE", 0)
    now = datetime(2026, 7, 3, 20, 30, tzinfo=dt_timezone.utc)

    job = claim_due_collection_job(
        operator="Arian",
        account_username="arian@example.com",
        now=now,
    )

    assert job is None
    stored = LinkedInFeedCollectionJob.objects.get()
    assert stored.status == LinkedInFeedCollectionJob.Status.PENDING


@pytest.mark.django_db
def test_ensure_backfill_collection_jobs_creates_oldest_to_newest(monkeypatch):
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_HOUR", 17)
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_MINUTE", 0)
    now = datetime(2026, 7, 6, 23, 0, tzinfo=dt_timezone.utc)

    jobs = ensure_backfill_collection_jobs(
        operator="Arian",
        account_username="arian@example.com",
        days=3,
        now=now,
    )

    assert [job.collection_date for job in jobs] == [
        date(2026, 7, 4),
        date(2026, 7, 5),
        date(2026, 7, 6),
    ]
    assert [job.scheduled_for for job in jobs] == [
        scheduled_for_local_day(date(2026, 7, 4)),
        scheduled_for_local_day(date(2026, 7, 5)),
        scheduled_for_local_day(date(2026, 7, 6)),
    ]


def test_catchup_start_date_defaults_to_two_week_window(monkeypatch):
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_CATCHUP_DAYS", 14)
    now = datetime(2026, 7, 14, 18, 0, tzinfo=dt_timezone.utc)

    assert catchup_start_date(now) == date(2026, 7, 1)


@pytest.mark.django_db
def test_claim_due_collection_job_ignores_jobs_older_than_catchup_window(monkeypatch):
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_HOUR", 17)
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_MINUTE", 0)
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_CATCHUP_DAYS", 14)
    now = datetime(2026, 7, 14, 22, 30, tzinfo=dt_timezone.utc)
    LinkedInFeedCollectionJob.objects.create(
        operator="Arian",
        account_username="arian@example.com",
        collection_date=date(2026, 6, 20),
        scheduled_for=datetime(2026, 6, 20, 21, 0, tzinfo=dt_timezone.utc),
    )
    recent = LinkedInFeedCollectionJob.objects.create(
        operator="Arian",
        account_username="arian@example.com",
        collection_date=date(2026, 7, 13),
        scheduled_for=datetime(2026, 7, 13, 21, 0, tzinfo=dt_timezone.utc),
    )

    job = claim_due_collection_job(
        operator="Arian",
        account_username="arian@example.com",
        now=now,
    )

    assert job is not None
    assert job.pk == recent.pk


@pytest.mark.django_db
def test_collection_window_end_caps_historical_jobs_at_scheduled_time():
    now = datetime(2026, 7, 6, 23, 0, tzinfo=dt_timezone.utc)
    job = LinkedInFeedCollectionJob.objects.create(
        operator="Arian",
        account_username="arian@example.com",
        collection_date=date(2026, 7, 5),
        scheduled_for=datetime(2026, 7, 5, 21, 0, tzinfo=dt_timezone.utc),
    )

    assert collection_window_end_for_job(job, now=now) == job.scheduled_for


@pytest.mark.django_db
def test_mark_completed_schedules_next_day_job():
    now = timezone.now()
    job = ensure_collection_jobs(
        operator="Arian",
        account_username="arian@example.com",
        now=now,
    )
    result = CollectionResult(
        posts_seen=10,
        posts_created=8,
        observations_created=9,
        repeated_observations=1,
    )

    mark_job_completed(job, result)

    job.refresh_from_db()
    assert job.status == LinkedInFeedCollectionJob.Status.COMPLETED
    assert job.posts_seen == 10
    assert LinkedInFeedCollectionJob.objects.filter(
        operator="Arian",
        account_username="arian@example.com",
        collection_date=job.collection_date + timedelta(days=1),
    ).exists()


@pytest.mark.django_db
def test_collection_cutoff_uses_previous_completed_job_with_overlap(monkeypatch):
    monkeypatch.setattr(
        "linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_CUTOFF_OVERLAP_MINUTES",
        1,
    )
    previous_scheduled = datetime(2026, 7, 3, 21, 0, tzinfo=dt_timezone.utc)
    LinkedInFeedCollectionJob.objects.create(
        operator="Arian",
        account_username="arian@example.com",
        collection_date=date(2026, 7, 3),
        status=LinkedInFeedCollectionJob.Status.COMPLETED,
        scheduled_for=previous_scheduled,
    )
    job = LinkedInFeedCollectionJob.objects.create(
        operator="Arian",
        account_username="arian@example.com",
        collection_date=date(2026, 7, 4),
        scheduled_for=datetime(2026, 7, 4, 21, 0, tzinfo=dt_timezone.utc),
    )

    assert collection_cutoff_for_job(job) == previous_scheduled + timedelta(minutes=1)


@pytest.mark.django_db
def test_collection_cutoff_first_job_uses_previous_day_schedule(monkeypatch):
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_HOUR", 17)
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_MINUTE", 0)
    monkeypatch.setattr(
        "linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_CUTOFF_OVERLAP_MINUTES",
        1,
    )
    job = LinkedInFeedCollectionJob.objects.create(
        operator="Arian",
        account_username="arian@example.com",
        collection_date=date(2026, 7, 4),
        scheduled_for=datetime(2026, 7, 4, 21, 0, tzinfo=dt_timezone.utc),
    )

    assert collection_cutoff_for_job(job) == datetime(
        2026, 7, 3, 21, 1, tzinfo=dt_timezone.utc,
    )


@pytest.mark.django_db
def test_mark_failed_keeps_same_job_retryable(monkeypatch):
    monkeypatch.setattr("linkedin.feed_collection.LINKEDIN_FEED_COLLECTION_RETRY_MINUTES", 30)
    now = timezone.now()
    job = ensure_collection_jobs(
        operator="Arian",
        account_username="arian@example.com",
        now=now,
    )

    mark_job_failed(job, "cdp unavailable")

    job.refresh_from_db()
    assert job.status == LinkedInFeedCollectionJob.Status.FAILED
    assert "cdp unavailable" in job.error
    assert job.scheduled_for > now
    assert job.scheduled_for <= timezone.now() + timedelta(minutes=31)


def test_parse_relative_timestamp():
    reference = datetime(2026, 7, 4, 21, 0, tzinfo=dt_timezone.utc)

    assert parse_relative_timestamp("5h •", reference=reference) == (
        reference - timedelta(hours=5)
    )
    assert parse_relative_timestamp("2mo", reference=reference) == (
        reference - timedelta(days=60)
    )
    assert parse_relative_timestamp("now", reference=reference) == reference
    assert parse_relative_timestamp("Promoted", reference=reference) is None


def test_specific_post_url_detection_rejects_author_posts_listing():
    assert is_specific_post_url("https://www.linkedin.com/feed/update/urn:li:activity:123/")
    assert is_specific_post_url("https://www.linkedin.com/posts/petestrouse_post-123-abc/")
    assert not is_specific_post_url("https://www.linkedin.com/company/scotiabank/posts/")
    assert not is_specific_post_url("https://www.linkedin.com/in/person/recent-activity/all/")


def test_extract_posts_from_page_uses_activity_urn_permalink_when_post_url_is_listing():
    class FakePage:
        def evaluate(self, *_args):
            return [{
                "dataUrn": "",
                "dataId": "",
                "descendantActivityUrn": "urn:li:activity:987",
                "postUrl": "https://www.linkedin.com/company/scotiabank/posts/",
                "authorName": "Santiago Negret Rey",
                "authorHeadline": "MBA Candidate",
                "authorProfileUrl": "https://www.linkedin.com/in/santiago-negret-rey/",
                "postText": "I'm joining Scotiabank as an intern.",
                "timestampText": "5h",
                "text": "Feed post\nSantiago Negret Rey\n5h\nI'm joining Scotiabank.",
            }]

    records = extract_posts_from_page(FakePage())

    assert len(records) == 1
    assert records[0].activity_urn == "urn:li:activity:987"
    assert records[0].post_url == post_url_for_activity_urn("urn:li:activity:987")


def test_extract_posts_from_page_uses_share_urn_permalink_when_available():
    class FakePage:
        def evaluate(self, *_args):
            return [{
                "dataUrn": "",
                "dataId": "",
                "descendantActivityUrn": "urn:li:share:7475780780439314432",
                "postUrl": "https://www.linkedin.com/feed/",
                "authorName": "Matt Bruggeman",
                "authorHeadline": "Director of Federal GTM",
                "authorProfileUrl": "https://www.linkedin.com/in/matt-bruggeman/",
                "postText": "Have a SOC 2 and always wanted FedRAMP?",
                "timestampText": "2h",
                "text": "Feed post\nMatt Bruggeman\n2h\nHave a SOC 2 and always wanted FedRAMP?",
            }]

    records = extract_posts_from_page(FakePage())

    assert len(records) == 1
    assert records[0].activity_urn == "urn:li:share:7475780780439314432"
    assert records[0].post_url == post_url_for_activity_urn("urn:li:share:7475780780439314432")


def test_extract_posts_from_page_uses_menu_urn_permalink_when_card_has_no_post_link():
    class FakePage:
        def evaluate(self, *_args):
            return [{
                "dataUrn": "",
                "dataId": "",
                "descendantActivityUrn": "",
                "menuPostUrn": (
                    "https://www.linkedin.com/preload/embed-modal/"
                    "?targetUrn=urn:li:share:7483212390117879809"
                ),
                "postUrl": (
                    "https://www.linkedin.com/preload/embed-modal/"
                    "?targetUrn=urn:li:share:7483212390117879809"
                ),
                "authorName": "Sean Doherty",
                "authorHeadline": "CEO at GovDash",
                "authorProfileUrl": "https://www.linkedin.com/in/sean-doherty/",
                "postText": "Agents on GovDash continue to go exponential.",
                "timestampText": "1m",
                "text": "Feed post\nSean Doherty\n1m\nAgents on GovDash continue to go exponential.",
            }]

    records = extract_posts_from_page(FakePage())

    assert len(records) == 1
    assert records[0].activity_urn == "urn:li:share:7483212390117879809"
    assert records[0].post_url == post_url_for_activity_urn("urn:li:share:7483212390117879809")


def test_extract_posts_from_page_drops_non_specific_post_listing_without_activity_urn():
    class FakePage:
        def evaluate(self, *_args):
            return [{
                "dataUrn": "",
                "dataId": "",
                "descendantActivityUrn": "",
                "postUrl": "https://www.linkedin.com/company/scotiabank/posts/",
                "authorName": "Santiago Negret Rey",
                "authorHeadline": "MBA Candidate",
                "authorProfileUrl": "https://www.linkedin.com/in/santiago-negret-rey/",
                "postText": "I'm joining Scotiabank as an intern.",
                "timestampText": "5h",
                "text": "Feed post\nSantiago Negret Rey\n5h\nI'm joining Scotiabank.",
            }]

    records = extract_posts_from_page(FakePage())

    assert records == []


@pytest.mark.django_db
def test_upsert_feed_record_dedupes_post_and_counts_observation():
    job = ensure_collection_jobs(
        operator="Arian",
        account_username="arian@example.com",
        now=timezone.now(),
    )
    record = _record()

    assert upsert_feed_record(record, job=job) == (True, True)
    assert upsert_feed_record(record, job=job) == (False, False)

    assert LinkedInFeedPost.objects.count() == 1
    obs = LinkedInFeedObservation.objects.get()
    assert obs.seen_count == 2
    assert obs.operator == "Arian"
    assert LinkedInFeedPost.objects.get().posted_at is not None


@pytest.mark.django_db
def test_collect_from_page_stops_at_cutoff_before_saving_old_post(monkeypatch):
    now = timezone.now()
    job = ensure_collection_jobs(
        operator="Arian",
        account_username="arian@example.com",
        now=now,
    )

    fresh = _record(activity_urn="urn:li:activity:111", posted_at=now - timedelta(hours=1))
    old = _record(activity_urn="urn:li:activity:222", posted_at=now - timedelta(hours=3))
    monkeypatch.setattr(
        "linkedin.feed_collection.extract_posts_from_page",
        lambda page: [fresh, old],
    )

    class FakeMouse:
        def wheel(self, *_args):
            pass

    class FakePage:
        mouse = FakeMouse()

        def goto(self, *_args, **_kwargs):
            pass

        def wait_for_timeout(self, *_args):
            pass

    result = _collect_from_page(
        FakePage(),
        job=job,
        cutoff_at=now - timedelta(hours=2),
        max_posts=10,
        stop_after_seen=10,
        stop_after_stale=1,
        scroll_pause_seconds=0,
    )

    assert result.posts_seen == 1
    assert LinkedInFeedPost.objects.count() == 1
    assert LinkedInFeedPost.objects.get().activity_urn == "urn:li:activity:111"


@pytest.mark.django_db
def test_collect_from_page_keeps_scrolling_after_out_of_order_old_post(monkeypatch):
    now = timezone.now()
    job = ensure_collection_jobs(
        operator="Arian",
        account_username="arian@example.com",
        now=now,
    )

    old = _record(activity_urn="urn:li:activity:111", posted_at=now - timedelta(hours=3))
    fresh = _record(activity_urn="urn:li:activity:222", posted_at=now - timedelta(hours=1))
    calls = {"count": 0}

    def fake_extract(_page):
        calls["count"] += 1
        if calls["count"] == 1:
            return [old]
        return [old, fresh]

    monkeypatch.setattr("linkedin.feed_collection.extract_posts_from_page", fake_extract)

    class FakePage:
        def goto(self, *_args, **_kwargs):
            pass

        def wait_for_timeout(self, *_args):
            pass

        def evaluate(self, *_args):
            pass

    result = _collect_from_page(
        FakePage(),
        job=job,
        cutoff_at=now - timedelta(hours=2),
        max_posts=10,
        stop_after_seen=10,
        stop_after_stale=3,
        scroll_pause_seconds=0,
    )

    assert result.posts_seen == 1
    assert LinkedInFeedPost.objects.count() == 1
    assert LinkedInFeedPost.objects.get().activity_urn == "urn:li:activity:222"


@pytest.mark.django_db
def test_collect_from_page_skips_posts_newer_than_window_end(monkeypatch):
    now = timezone.now()
    job = ensure_collection_jobs(
        operator="Arian",
        account_username="arian@example.com",
        now=now,
    )

    newer = _record(activity_urn="urn:li:activity:111", posted_at=now - timedelta(hours=1))
    in_window = _record(activity_urn="urn:li:activity:222", posted_at=now - timedelta(days=2))
    old = _record(activity_urn="urn:li:activity:333", posted_at=now - timedelta(days=4))
    monkeypatch.setattr(
        "linkedin.feed_collection.extract_posts_from_page",
        lambda page: [newer, in_window, old],
    )

    class FakePage:
        def goto(self, *_args, **_kwargs):
            pass

        def wait_for_timeout(self, *_args):
            pass

    result = _collect_from_page(
        FakePage(),
        job=job,
        cutoff_at=now - timedelta(days=3),
        window_end_at=now - timedelta(days=1),
        max_posts=10,
        stop_after_seen=10,
        stop_after_stale=1,
        scroll_pause_seconds=0,
    )

    assert result.posts_seen == 1
    assert LinkedInFeedPost.objects.count() == 1
    assert LinkedInFeedPost.objects.get().activity_urn == "urn:li:activity:222"


@pytest.mark.django_db
def test_upsert_feed_record_rejects_record_without_specific_post_url():
    job = ensure_collection_jobs(
        operator="Arian",
        account_username="arian@example.com",
        now=timezone.now(),
    )

    with pytest.raises(ValueError, match="specific LinkedIn post URL"):
        upsert_feed_record(_record(activity_urn="", post_url=""), job=job)

    assert LinkedInFeedPost.objects.count() == 0
    assert LinkedInFeedObservation.objects.count() == 0


@pytest.mark.django_db
def test_upsert_feed_record_repairs_legacy_hash_match_when_url_becomes_available():
    job = ensure_collection_jobs(
        operator="Arian",
        account_username="arian@example.com",
        now=timezone.now(),
    )
    first = _record(activity_urn="", post_url="")
    legacy_post = LinkedInFeedPost.objects.create(
        content_hash=first.content_hash,
        author_name=first.author_name,
        post_text=first.post_text,
        post_url="",
    )
    LinkedInFeedObservation.objects.create(
        post=legacy_post,
        job=job,
        operator=job.operator,
        account_username=job.account_username,
    )
    later = _record(
        activity_urn="urn:li:activity:456",
        post_url="https://www.linkedin.com/feed/update/urn:li:activity:456/",
    )

    assert upsert_feed_record(later, job=job) == (False, False)

    legacy_post.refresh_from_db()
    assert legacy_post.activity_urn == "urn:li:activity:456"
    assert legacy_post.post_url == later.post_url
    assert LinkedInFeedObservation.objects.get().seen_count == 2


@pytest.mark.django_db
def test_upsert_feed_record_fills_blank_url_from_existing_share_urn():
    job = ensure_collection_jobs(
        operator="Arian",
        account_username="arian@example.com",
        now=timezone.now(),
    )
    record = _record(
        activity_urn="urn:li:share:7482977423408660480",
        post_url="",
        post_text="JOIN OUR TEAM! We're growing and looking for talented people.",
    )

    assert upsert_feed_record(record, job=job) == (True, True)
    post = LinkedInFeedPost.objects.get(activity_urn="urn:li:share:7482977423408660480")
    post.post_url = ""
    post.save(update_fields=["post_url"])

    assert upsert_feed_record(record, job=job) == (False, False)

    post.refresh_from_db()
    assert post.post_url == post_url_for_activity_urn("urn:li:share:7482977423408660480")


@pytest.mark.django_db
def test_collect_linkedin_feed_backfill_days_runs_daily_windows(
    fake_session,
    monkeypatch,
):
    fake_session.linkedin_profile.linkedin_username = "arian@example.com"
    fake_session.linkedin_profile.save(update_fields=["linkedin_username"])
    monkeypatch.setenv("LINKEDIN_USERNAME", "arian@example.com")

    class FakeGuard:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self):
            pass

        def release(self):
            pass

    calls = []

    def fake_collect(job, **kwargs):
        calls.append((job.collection_date, kwargs["cutoff_at"], kwargs["window_end_at"]))
        return CollectionResult(
            posts_seen=1,
            posts_created=1,
            observations_created=1,
            repeated_observations=0,
        )

    monkeypatch.setattr("linkedin.single_instance.SingleInstanceGuard", FakeGuard)
    monkeypatch.setattr("linkedin.feed_collection.collect_feed_for_job", fake_collect)

    call_command("collect_linkedin_feed", backfill_days=2)

    assert len(calls) == 2
    assert calls[0][0] < calls[1][0]
    assert LinkedInFeedCollectionJob.objects.filter(
        account_username="arian@example.com",
        status=LinkedInFeedCollectionJob.Status.COMPLETED,
    ).count() == 2
