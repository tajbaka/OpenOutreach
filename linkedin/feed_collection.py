from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone as dt_timezone
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from playwright.sync_api import sync_playwright

from linkedin.conf import (
    LINKEDIN_FEED_COLLECTION_CUTOFF_OVERLAP_MINUTES,
    LINKEDIN_FEED_COLLECTION_CATCHUP_DAYS,
    LINKEDIN_FEED_COLLECTION_HOUR,
    LINKEDIN_FEED_COLLECTION_MAX_POSTS,
    LINKEDIN_FEED_COLLECTION_MINUTE,
    LINKEDIN_FEED_COLLECTION_RETRY_MINUTES,
    LINKEDIN_FEED_COLLECTION_SCROLL_PAUSE_SECONDS,
    LINKEDIN_FEED_COLLECTION_STOP_AFTER_SEEN,
    LINKEDIN_FEED_COLLECTION_STOP_AFTER_STALE,
    LINKEDIN_FEED_COLLECTION_TIMEZONE,
    LISTENER_CDP_PORT,
)
from linkedin.models import (
    LinkedInFeedCollectionJob,
    LinkedInFeedObservation,
    LinkedInFeedPost,
)

logger = logging.getLogger(__name__)

FEED_URL = "https://www.linkedin.com/feed/"
_ACTIVITY_RE = re.compile(r"urn:li:(?:activity|share):\d+")
_RELATIVE_TIME_RE = re.compile(r"\b(now|(\d+)\s*(mo|yr|s|m|h|d|w|y))\b", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class FeedPostRecord:
    activity_urn: str
    post_url: str
    author_name: str
    author_headline: str
    author_profile_url: str
    post_text: str
    timestamp_text: str
    posted_at: datetime | None
    raw_payload: dict

    @property
    def content_hash(self) -> str:
        return content_hash_for(
            activity_urn=self.activity_urn,
            post_url=self.post_url,
            author_name=self.author_name,
            post_text=self.post_text,
        )


@dataclass(frozen=True)
class CollectionResult:
    posts_seen: int
    posts_created: int
    observations_created: int
    repeated_observations: int


def collection_timezone() -> ZoneInfo:
    return ZoneInfo(LINKEDIN_FEED_COLLECTION_TIMEZONE)


def scheduled_for_local_day(day: date) -> datetime:
    local = datetime.combine(
        day,
        dt_time(
            hour=LINKEDIN_FEED_COLLECTION_HOUR,
            minute=LINKEDIN_FEED_COLLECTION_MINUTE,
        ),
        tzinfo=collection_timezone(),
    )
    return local.astimezone(dt_timezone.utc)


def today_collection_date(now: datetime | None = None) -> date:
    now = now or timezone.now()
    return timezone.localtime(now, collection_timezone()).date()


def catchup_start_date(now: datetime | None = None) -> date:
    """Oldest local collection date the unattended collector should drain."""
    days = max(LINKEDIN_FEED_COLLECTION_CATCHUP_DAYS, 1)
    return today_collection_date(now) - timedelta(days=days - 1)


def collection_cutoff_for_job(job: LinkedInFeedCollectionJob) -> datetime:
    """Oldest post timestamp this job should collect, with a small overlap.

    A sender's feed jobs form sequential daily windows. Prefer the previous
    completed job's scheduled time; if this is the first job, fall back to the
    previous local day's scheduled collection time. The default one-minute
    overlap keeps boundary posts from being skipped if LinkedIn rounds labels.
    """
    previous = (
        LinkedInFeedCollectionJob.objects.filter(
            operator=job.operator,
            account_username=job.account_username,
            collection_date__lt=job.collection_date,
            status=LinkedInFeedCollectionJob.Status.COMPLETED,
        )
        .order_by("-collection_date", "-scheduled_for")
        .first()
    )
    base = (
        previous.scheduled_for
        if previous is not None
        else scheduled_for_local_day(job.collection_date - timedelta(days=1))
    )
    return base + timedelta(minutes=LINKEDIN_FEED_COLLECTION_CUTOFF_OVERLAP_MINUTES)


def ensure_collection_jobs(
    *,
    operator: str,
    account_username: str,
    now: datetime | None = None,
) -> LinkedInFeedCollectionJob:
    """Ensure today's job exists, and tomorrow's after today's completion."""
    now = now or timezone.now()
    local_day = today_collection_date(now)
    job, _ = LinkedInFeedCollectionJob.objects.get_or_create(
        operator=operator,
        account_username=account_username,
        collection_date=local_day,
        defaults={"scheduled_for": scheduled_for_local_day(local_day)},
    )
    if job.status == LinkedInFeedCollectionJob.Status.COMPLETED:
        tomorrow = local_day + timedelta(days=1)
        LinkedInFeedCollectionJob.objects.get_or_create(
            operator=operator,
            account_username=account_username,
            collection_date=tomorrow,
            defaults={"scheduled_for": scheduled_for_local_day(tomorrow)},
        )
    return job


def ensure_backfill_collection_jobs(
    *,
    operator: str,
    account_username: str,
    days: int,
    now: datetime | None = None,
) -> list[LinkedInFeedCollectionJob]:
    """Create one sender/day job for a historical bootstrap window.

    Backfill jobs are returned oldest-to-newest so each completed job becomes
    the previous cutoff for the next daily timeline.
    """
    if days <= 0:
        raise ValueError("days must be positive")
    now = now or timezone.now()
    end_day = today_collection_date(now)
    start_day = end_day - timedelta(days=days - 1)
    jobs: list[LinkedInFeedCollectionJob] = []
    for offset in range(days):
        collection_date = start_day + timedelta(days=offset)
        job, _ = LinkedInFeedCollectionJob.objects.get_or_create(
            operator=operator,
            account_username=account_username,
            collection_date=collection_date,
            defaults={"scheduled_for": scheduled_for_local_day(collection_date)},
        )
        jobs.append(job)
    return jobs


def collection_window_end_for_job(
    job: LinkedInFeedCollectionJob,
    *,
    now: datetime | None = None,
) -> datetime:
    """Latest post timestamp a historical job should claim.

    Normal daily collection has no explicit upper bound because it runs near
    scheduled time. A historical bootstrap may replay old sender/day windows
    much later, so cap older jobs at their scheduled collection time and cap
    today's job at the actual run time.
    """
    now = now or timezone.now()
    if job.collection_date >= today_collection_date(now):
        return now
    return job.scheduled_for


def claim_due_collection_job(
    *,
    operator: str,
    account_username: str,
    job_id: int | None = None,
    now: datetime | None = None,
) -> LinkedInFeedCollectionJob | None:
    now = now or timezone.now()
    ensure_collection_jobs(operator=operator, account_username=account_username, now=now)
    oldest_collection_date = catchup_start_date(now)
    with transaction.atomic():
        qs = LinkedInFeedCollectionJob.objects.select_for_update().filter(
            operator=operator,
            account_username=account_username,
            collection_date__gte=oldest_collection_date,
            status__in=[
                LinkedInFeedCollectionJob.Status.PENDING,
                LinkedInFeedCollectionJob.Status.FAILED,
            ],
            scheduled_for__lte=now,
        )
        if job_id is not None:
            qs = qs.filter(pk=job_id)
        job = qs.order_by("scheduled_for").first()
        if job is None:
            return None
        job.status = LinkedInFeedCollectionJob.Status.RUNNING
        job.started_at = now
        job.finished_at = None
        job.error = ""
        job.save(update_fields=["status", "started_at", "finished_at", "error", "updated_at"])
        return job


def mark_job_completed(job: LinkedInFeedCollectionJob, result: CollectionResult) -> None:
    now = timezone.now()
    job.status = LinkedInFeedCollectionJob.Status.COMPLETED
    job.finished_at = now
    job.posts_seen = result.posts_seen
    job.posts_created = result.posts_created
    job.observations_created = result.observations_created
    job.error = ""
    job.save(
        update_fields=[
            "status", "finished_at", "posts_seen", "posts_created",
            "observations_created", "error", "updated_at",
        ],
    )
    next_day = job.collection_date + timedelta(days=1)
    LinkedInFeedCollectionJob.objects.get_or_create(
        operator=job.operator,
        account_username=job.account_username,
        collection_date=next_day,
        defaults={"scheduled_for": scheduled_for_local_day(next_day)},
    )


def mark_job_failed(job: LinkedInFeedCollectionJob, error: str) -> None:
    now = timezone.now()
    retry_at = now + timedelta(minutes=LINKEDIN_FEED_COLLECTION_RETRY_MINUTES)
    job.status = LinkedInFeedCollectionJob.Status.FAILED
    job.finished_at = now
    job.scheduled_for = retry_at
    job.error = (error or "Unknown feed collection failure")[:4000]
    job.save(update_fields=["status", "finished_at", "scheduled_for", "error", "updated_at"])


def content_hash_for(
    *,
    activity_urn: str,
    post_url: str,
    author_name: str,
    post_text: str,
) -> str:
    del activity_urn, post_url
    basis = "\n".join(
        [
            _normalize_text(author_name).lower(),
            _normalize_text(post_text),
        ],
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def post_url_for_activity_urn(activity_urn: str) -> str:
    if not activity_urn:
        return ""
    return f"https://www.linkedin.com/feed/update/{activity_urn}/"


def is_specific_post_url(value: str) -> bool:
    parts = urlsplit(value or "")
    path = parts.path.rstrip("/")
    if not parts.netloc.endswith("linkedin.com"):
        return False
    if "/feed/update/" in path:
        return True
    if "urn:li:activity" in value:
        return True
    if "/posts/" not in path:
        return False
    if re.search(r"/(?:company|school|showcase)/[^/]+/posts$", path):
        return False
    suffix = path.split("/posts/", 1)[1]
    return bool(suffix)


def collect_feed_for_job(
    job: LinkedInFeedCollectionJob,
    *,
    cdp_port: int | None = None,
    cutoff_at: datetime | None = None,
    window_end_at: datetime | None = None,
    max_posts: int | None = None,
    stop_after_seen: int | None = None,
    stop_after_stale: int | None = None,
    scroll_pause_seconds: float | None = None,
) -> CollectionResult:
    cdp_port = LISTENER_CDP_PORT if cdp_port is None else cdp_port
    max_posts = LINKEDIN_FEED_COLLECTION_MAX_POSTS if max_posts is None else max_posts
    stop_after_seen = (
        LINKEDIN_FEED_COLLECTION_STOP_AFTER_SEEN
        if stop_after_seen is None else stop_after_seen
    )
    stop_after_stale = (
        LINKEDIN_FEED_COLLECTION_STOP_AFTER_STALE
        if stop_after_stale is None else stop_after_stale
    )
    scroll_pause_seconds = (
        LINKEDIN_FEED_COLLECTION_SCROLL_PAUSE_SECONDS
        if scroll_pause_seconds is None else scroll_pause_seconds
    )

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        if not browser.contexts:
            raise RuntimeError("no shared browser context available over CDP")
        context = browser.contexts[0]
        page = context.new_page()
        try:
            return _collect_from_page(
                page,
                job=job,
                cutoff_at=cutoff_at or collection_cutoff_for_job(job),
                window_end_at=window_end_at,
                max_posts=max_posts,
                stop_after_seen=stop_after_seen,
                stop_after_stale=stop_after_stale,
                scroll_pause_seconds=scroll_pause_seconds,
            )
        finally:
            try:
                page.close()
            except Exception:
                pass


def _collect_from_page(
    page,
    *,
    job: LinkedInFeedCollectionJob,
    cutoff_at: datetime,
    max_posts: int,
    stop_after_seen: int,
    stop_after_stale: int,
    scroll_pause_seconds: float,
    window_end_at: datetime | None = None,
) -> CollectionResult:
    page.goto(FEED_URL, wait_until="commit", timeout=45_000)
    page.wait_for_timeout(2000)

    processed: set[str] = set()
    posts_created = 0
    observations_created = 0
    repeated_observations = 0
    stale_posts_seen = 0
    posts_seen = 0
    idle_scrolls = 0

    while (
        posts_seen < max_posts
        and repeated_observations < stop_after_seen
        and stale_posts_seen < stop_after_stale
    ):
        before = len(processed)
        for record in extract_posts_from_page(page):
            identity = record.activity_urn or record.content_hash
            if not identity or identity in processed:
                continue
            if record.posted_at is not None and record.posted_at <= cutoff_at:
                processed.add(identity)
                stale_posts_seen += 1
                if stale_posts_seen >= stop_after_stale:
                    break
                continue
            processed.add(identity)
            if (
                window_end_at is not None
                and record.posted_at is not None
                and record.posted_at > window_end_at
            ):
                continue
            stale_posts_seen = 0
            posts_seen += 1
            post_created, observation_created = upsert_feed_record(record, job=job)
            posts_created += int(post_created)
            observations_created += int(observation_created)
            repeated_observations += int(not observation_created)

            if posts_seen >= max_posts or repeated_observations >= stop_after_seen:
                break

        if stale_posts_seen >= stop_after_stale:
            break

        if len(processed) == before:
            idle_scrolls += 1
        else:
            idle_scrolls = 0
        if idle_scrolls >= 10:
            break

        _scroll_feed_page(page)
        page.wait_for_timeout(int(scroll_pause_seconds * 1000))

    return CollectionResult(
        posts_seen=posts_seen,
        posts_created=posts_created,
        observations_created=observations_created,
        repeated_observations=repeated_observations,
    )


def _scroll_feed_page(page) -> None:
    page.evaluate(
        """
        async () => {
          const workspace = document.querySelector('main#workspace');
          if (workspace && workspace.scrollHeight > workspace.clientHeight) {
            workspace.scrollTop = Math.min(
              workspace.scrollTop + Math.floor(workspace.clientHeight * 0.9),
              workspace.scrollHeight
            );
            return;
          }
          window.scrollBy(0, Math.floor(window.innerHeight * 0.9));
        }
        """,
    )


def extract_posts_from_page(page) -> list[FeedPostRecord]:
    rows = page.evaluate(
        """
        async () => {
          const selectors = [
            'div.feed-shared-update-v2',
            'div[data-urn*="urn:li:activity"]',
            'div[data-id*="urn:li:activity"]',
            'div[data-urn*="urn:li:share"]',
            'div[data-id*="urn:li:share"]'
          ];
          const nodes = Array.from(document.querySelectorAll(selectors.join(',')));
          for (const node of document.querySelectorAll('[role="listitem"]')) {
            const text = (node.innerText || '').trim();
            if (text.startsWith('Feed post') || text.includes('\\nFeed post\\n')) {
              nodes.push(node);
            }
          }
          const unique = [];
          const seen = new Set();
          for (const node of nodes) {
            if (seen.has(node)) continue;
            seen.add(node);
            const text = (node.innerText || '').trim();
            if (!text || text.length < 20) continue;
            unique.push(node);
          }
          const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const findMenuPostUrn = async (node) => {
            const menuButton = node.querySelector('button[aria-label^="Open control menu for post by"]');
            if (!menuButton) return '';
            try {
              document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
              await wait(100);
              node.scrollIntoView({block: 'center', inline: 'nearest'});
              await wait(100);
              menuButton.click();
              await wait(500);
              const menuLinks = Array.from(document.querySelectorAll(
                '[role="menu"] a[href*="targetUrn="],'
                + '[role="menu"] a[href*="entityUrn="],'
                + '[role="menu"] a[href*="updateUrn="]'
              ));
              for (const link of menuLinks) {
                const href = decodeURIComponent((link.href || '').trim());
                if (href.includes('urn:li:activity') || href.includes('urn:li:share')) {
                  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
                  return href;
                }
              }
              document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
            } catch (_e) {
              document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
            }
            return '';
          };
          const rows = [];
          for (const node of unique) {
            const attr = (name) => node.getAttribute(name) || '';
            const pickText = (items) => {
              for (const sel of items) {
                const el = node.querySelector(sel);
                const value = (el && el.innerText || '').trim();
                if (value) return value;
              }
              return '';
            };
            const pickHref = (items) => {
              for (const sel of items) {
                const el = node.querySelector(sel);
                const href = (el && el.href || '').trim();
                if (href) return href;
              }
              return '';
            };
            const findAttr = (names) => {
              const nodes = [node, ...Array.from(node.querySelectorAll('*'))];
              for (const el of nodes) {
                for (const name of names) {
                  const value = (el.getAttribute && el.getAttribute(name) || '').trim();
                  if (
                    value
                    && (value.includes('urn:li:activity') || value.includes('urn:li:share'))
                  ) return value;
                }
              }
              return '';
            };
            const links = Array.from(node.querySelectorAll('a[href]')).map((el) => ({
              href: (el.href || '').trim(),
              text: (el.innerText || '').trim(),
              ariaLabel: (el.getAttribute('aria-label') || '').trim(),
              className: (el.className || '').toString()
            })).filter((link) => link.href);
            const isSpecificPostLink = (href) => {
              try {
                const url = new URL(href);
                const path = url.pathname.replace(/\\/+$/, '');
                if (!url.hostname.endsWith('linkedin.com')) return false;
                if (path.includes('/feed/update/')) return true;
                if (href.includes('urn:li:activity') || href.includes('urn:li:share')) return true;
                if (!path.includes('/posts/')) return false;
                if (new RegExp('/(?:company|school|showcase)/[^/]+/posts$').test(path)) return false;
                return path.split('/posts/')[1].length > 0;
              } catch (_e) {
                return false;
              }
            };
            const looksLikeTimestampLink = (link) => {
              const text = `${link.text} ${link.ariaLabel} ${link.className}`.toLowerCase();
              return (
                new RegExp('(^|\\s)(now|\\d+\\s*(s|m|h|d|w|mo|yr|y))(\\s|$)').test(text)
                || text.includes('actor__sub-description')
                || text.includes('feed-shared-actor__sub-description')
              );
            };
            const postLink = (
              links.find((link) => isSpecificPostLink(link.href) && looksLikeTimestampLink(link))
              || links.find((link) => isSpecificPostLink(link.href))
              || {href: pickHref([
                'a[href*="/feed/update/"]',
                'a[href*="urn:li:activity"]',
                'a[href*="urn:li:share"]'
              ])}
            ).href || '';
            const menuPostUrn = postLink ? '' : await findMenuPostUrn(node);
            const profileLink = pickHref([
              'a.update-components-actor__meta-link[href*="/in/"]',
              'a.feed-shared-actor__container-link[href*="/in/"]',
              'a[href*="/in/"]'
            ]);
            const postText = pickText([
              '.update-components-text',
              '.feed-shared-update-v2__description',
              '.feed-shared-text',
              '.break-words'
            ]);
            const timestamp = pickText([
              '.update-components-actor__sub-description',
              '.feed-shared-actor__sub-description',
              'time'
            ]);
            rows.push({
              dataUrn: attr('data-urn'),
              dataId: attr('data-id'),
              descendantActivityUrn: findAttr([
                'data-urn', 'data-id', 'data-activity-urn', 'data-chameleon-result-urn'
              ]),
              menuPostUrn,
              postUrl: postLink || menuPostUrn,
              candidateLinks: links.slice(0, 80),
              authorName: pickText([
                '.update-components-actor__name',
                '.feed-shared-actor__name',
                '.update-components-actor__title',
                'span[dir="ltr"]'
              ]),
              authorHeadline: pickText([
                '.update-components-actor__description',
                '.feed-shared-actor__description'
              ]),
              authorProfileUrl: profileLink,
              postText,
              timestampText: timestamp,
              text: node.innerText || ''
            });
          }
          return rows;
        }
        """,
    )
    records: list[FeedPostRecord] = []
    reference_time = timezone.now()
    for row in rows:
        rendered_text = row.get("text") or ""
        timestamp_text = _normalize_text(
            row.get("timestampText") or _extract_timestamp_from_rendered_text(rendered_text),
        )
        text = _normalize_text(
            row.get("postText") or _extract_post_text_from_rendered_text(rendered_text),
        )
        if not text:
            continue
        raw_url = row.get("postUrl") or ""
        raw_identity = " ".join(
            str(row.get(key) or "")
            for key in ("dataUrn", "dataId", "descendantActivityUrn", "menuPostUrn", "postUrl")
        )
        activity_urn = _extract_activity_urn(raw_identity)
        post_url = _normalize_url(raw_url)
        if not is_specific_post_url(post_url):
            post_url = post_url_for_activity_urn(activity_urn)
        records.append(
            FeedPostRecord(
                activity_urn=activity_urn,
                post_url=post_url,
                author_name=_normalize_text(
                    row.get("authorName") or _extract_author_from_rendered_text(rendered_text),
                ),
                author_headline=_normalize_text(
                    row.get("authorHeadline") or _extract_headline_from_rendered_text(rendered_text),
                ),
                author_profile_url=_normalize_url(row.get("authorProfileUrl") or ""),
                post_text=text,
                timestamp_text=timestamp_text,
                posted_at=parse_relative_timestamp(timestamp_text, reference=reference_time),
                raw_payload=row,
            ),
        )
    return records


def upsert_feed_record(
    record: FeedPostRecord,
    *,
    job: LinkedInFeedCollectionJob,
) -> tuple[bool, bool]:
    now = timezone.now()
    defaults = {
        "post_url": record.post_url,
        "author_name": record.author_name,
        "author_headline": record.author_headline,
        "author_profile_url": record.author_profile_url,
        "post_text": record.post_text,
        "posted_at": record.posted_at,
        "raw_payload": {
            **record.raw_payload,
            "timestamp_text": record.timestamp_text,
        },
        "last_seen_at": now,
    }
    post = None
    if record.activity_urn:
        post = LinkedInFeedPost.objects.filter(activity_urn=record.activity_urn).first()
    if post is None:
        post = LinkedInFeedPost.objects.filter(content_hash=record.content_hash).first()

    if post is None:
        post = LinkedInFeedPost.objects.create(
            activity_urn=record.activity_urn,
            content_hash=record.content_hash,
            first_seen_at=now,
            **defaults,
        )
        created = True
    else:
        created = False
        effective_activity_urn = record.activity_urn or post.activity_urn
        if not defaults["post_url"]:
            defaults["post_url"] = post.post_url or post_url_for_activity_urn(effective_activity_urn)
        if record.posted_at is None and post.posted_at is not None:
            defaults["posted_at"] = post.posted_at
        for field, value in defaults.items():
            setattr(post, field, value)
        if record.activity_urn and not post.activity_urn:
            post.activity_urn = record.activity_urn
        post.save(
            update_fields=[
                "activity_urn", "post_url", "author_name", "author_headline",
                "author_profile_url", "post_text", "posted_at", "raw_payload",
                "last_seen_at", "updated_at",
            ],
        )

    observation, observation_created = LinkedInFeedObservation.objects.get_or_create(
        post=post,
        operator=job.operator,
        account_username=job.account_username,
        defaults={
            "job": job,
            "first_seen_at": now,
            "last_seen_at": now,
            "seen_count": 1,
        },
    )
    if not observation_created:
        observation.job = job
        observation.last_seen_at = now
        observation.seen_count += 1
        observation.save(update_fields=["job", "last_seen_at", "seen_count"])

    return created, observation_created


def _extract_activity_urn(value: str) -> str:
    match = _ACTIVITY_RE.search(value or "")
    return match.group(0) if match else ""


def _normalize_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", (value or "").replace("\u200b", "").strip())


def _normalize_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _rendered_lines(value: str) -> list[str]:
    return [
        cleaned
        for line in (value or "").splitlines()
        if (cleaned := _normalize_text(line))
    ]


def _feed_lines_after_marker(value: str) -> list[str]:
    lines = _rendered_lines(value)
    try:
        index = lines.index("Feed post")
    except ValueError:
        return lines
    return lines[index + 1:]


def _extract_author_from_rendered_text(value: str) -> str:
    lines = _feed_lines_after_marker(value)
    while lines and (
        lines[0] == "Recommended for you"
        or " follow this Page" in lines[0]
        or " follows this Page" in lines[0]
    ):
        lines.pop(0)
    return lines[0] if lines else ""


def _extract_headline_from_rendered_text(value: str) -> str:
    lines = _feed_lines_after_marker(value)
    author = _extract_author_from_rendered_text(value)
    if author in lines:
        lines = lines[lines.index(author) + 1:]
    if not lines:
        return ""
    first = lines[0]
    if _looks_like_timestamp(first) or first in {"Promoted", "Follow"}:
        return ""
    return first


def _extract_post_text_from_rendered_text(value: str) -> str:
    lines = _feed_lines_after_marker(value)
    author = _extract_author_from_rendered_text(value)
    if author in lines:
        lines = lines[lines.index(author) + 1:]
    while lines and not _looks_like_post_body_start(lines[0]):
        lines.pop(0)
    stop_words = {
        "Like",
        "Comment",
        "Repost",
        "Send",
        "Follow",
        "Show more",
        "Unlock full document",
    }
    body: list[str] = []
    for line in lines:
        if line in stop_words:
            break
        if re.fullmatch(r"[\d,]+", line):
            break
        body.append(line)
    return "\n".join(body).strip()


def _extract_timestamp_from_rendered_text(value: str) -> str:
    lines = _feed_lines_after_marker(value)
    for line in lines:
        if _looks_like_timestamp(line):
            return line
    return ""


def parse_relative_timestamp(value: str, *, reference: datetime) -> datetime | None:
    """Parse LinkedIn's compact relative feed labels into an approximate time."""
    match = _RELATIVE_TIME_RE.search(value or "")
    if match is None:
        return None
    if match.group(1).lower() == "now":
        return reference
    amount = int(match.group(2))
    unit = match.group(3).lower()
    if unit == "s":
        delta = timedelta(seconds=amount)
    elif unit == "m":
        delta = timedelta(minutes=amount)
    elif unit == "h":
        delta = timedelta(hours=amount)
    elif unit == "d":
        delta = timedelta(days=amount)
    elif unit == "w":
        delta = timedelta(weeks=amount)
    elif unit == "mo":
        delta = timedelta(days=30 * amount)
    elif unit in {"yr", "y"}:
        delta = timedelta(days=365 * amount)
    else:
        return None
    return reference - delta


def _looks_like_timestamp(value: str) -> bool:
    return bool(_RELATIVE_TIME_RE.search(value or ""))


def _looks_like_post_body_start(value: str) -> bool:
    if not value:
        return False
    if value in {"Promoted", "Follow", "Recommended for you"}:
        return False
    if " followers" in value or " connections" in value:
        return False
    if _looks_like_timestamp(value):
        return False
    return len(value) > 12
