---
name: posthog-analytics
description: Read Boundera PostHog analytics, referrers, campaign UTMs, visitor journeys and booking attribution, or create and update requested dashboards and insights. Use the existing read key for data retrieval and the separate write key for dashboard changes. Prefer direct API access over browser navigation.
---

# PostHog analytics and dashboards

## Connection

- Run from OpenOutreach with `.venv/bin/python`; `requests` and `python-dotenv`
  are already available. No Django setup is required for PostHog reads.
- For data retrieval, use `POSTHOG_PERSONAL_KEY` from OpenOutreach's repo-root
  `.env`. The user authorized a local copy on 2026-10-19. The original remains in
  `/Users/admin/Desktop/Projects/Boundera-website/.env.local`; use that same-account
  fallback only if the local value is absent, not to bypass an authentication failure.
- For requested dashboard or insight creation and updates, use
  `POSTHOG_PERSONAL_WRITE_KEY` from
  `/Users/admin/Desktop/Projects/Boundera-website/.env.local`, where the user saved
  it. Do not look for the previously proposed `POSTHOG_DASHBOARD_KEY` name.
- Choose the key by the operation, not the HTTP method: a POST to `/query/`
  that retrieves data still uses `POSTHOG_PERSONAL_KEY`. In a dashboard task,
  use the read key for discovery, query previews and readback, and a separate
  write-key session for the requested dashboard/insight mutations. Never retry
  a failed read with the write key or substitute the read key for a missing write key.
- Read the selected value with `dotenv_values`; do not load or display the entire
  environment. Keep the key only in ignored credential files; never include it
  in tracked files, artifacts or command-line arguments.
  `NEXT_PUBLIC_POSTHOG_KEY` and `POSTHOG_PROJECT_TOKEN` are capture
  tokens, not substitutes for either personal API key.
- Use `https://us.posthog.com`, project **404775**, named **Default project**
  (verified 2026-10-19). Verify project access with a GET before reporting.
  Another requested project requires resolving its exact identity first.
- Default to the API, not a PostHog browser tab, plugin installation, new login
  or API-key creation. Honor an explicitly requested browser workflow. If the
  credential file is missing, access is denied or the key is revoked, report
  the blocker without exposing the secret or silently switching accounts.

Minimal read-key connection check:

```python
from pathlib import Path
from dotenv import dotenv_values
import requests

credential_file = Path('.env')  # Run from the OpenOutreach repo root.
key = dotenv_values(credential_file).get('POSTHOG_PERSONAL_KEY')
if not key:
    credential_file = Path('/Users/admin/Desktop/Projects/Boundera-website/.env.local')
    key = dotenv_values(credential_file).get('POSTHOG_PERSONAL_KEY')
if not key:
    raise SystemExit('PostHog personal key is missing from the configured file.')
session = requests.Session()
session.headers['Authorization'] = f'Bearer {key}'
base = 'https://us.posthog.com/api/projects/404775'
response = session.get(f'{base}/', timeout=25, allow_redirects=False)
if response.status_code != 200:
    raise SystemExit(f'PostHog project access failed: HTTP {response.status_code}')
project = response.json()
print({field: project.get(field) for field in ('id', 'name', 'timezone')})
```

## Read-only queries

Use `POSTHOG_PERSONAL_KEY` for `POST {base}/query/` with
`{"query": {"kind": "HogQLQuery", "query": sql}}` for read-only SELECT queries.
Use a 40–45 second timeout and disable redirects. Inspect status before parsing;
do not dump raw error bodies or request headers. Consult the official
[query documentation](https://posthog.com/docs/sql) if the API shape changes.

- Always constrain event queries by a timestamp range and a result limit.
  Begin with the booking window or requested reporting interval. For visitor
  history, expand deliberately to a stated lookback, not an unbounded scan.
- On a query timeout, narrow the window or select fewer fields before one
  bounded retry. Do not poll or retry indefinitely.
- Select only relevant properties, such as `$current_url`, `$referrer`,
  `$referring_domain`, `$session_entry_url`, `$session_entry_referrer`,
  `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `ad_id`, `fbclid`,
  `gclid`, `event_id`, `cta_label`, `source_location`, and `$session_id`.
  Never print complete event properties: they can contain tokens, IP addresses
  and unrelated personal data. Redact credentials and sensitive URL parameters
  from any selected values before displaying or saving them.
- Respect query limits/pagination; describe any incomplete window or truncated
  result set rather than calling it exhaustive. Convert UTC to the user's
  timezone for reporting, retaining exact UTC bounds for reproducibility.

## Requested dashboards and insights

- A request to create or update a dashboard authorizes that scoped work; a request
  to pull data does not. Resolve the intended project and existing dashboard IDs
  with the read key, and preview the underlying queries before saving insights.
- Load `POSTHOG_PERSONAL_WRITE_KEY` from the website's `.env.local` into a
  separate `requests.Session` using the same Bearer authentication. Keep the
  reporting session on `POSTHOG_PERSONAL_KEY`; do not replace its headers globally.
- Consult the current official [dashboard](https://posthog.com/docs/api/dashboards)
  and [insight](https://posthog.com/docs/api/insights) API documentation for the
  requested operation. Create or update only the requested dashboards and their
  supporting insights, preserving unrelated tiles, filters and sharing settings.
- Verify saved IDs, queries and dashboard membership through read-key GETs. If a
  mutation times out, check for the saved object before retrying to avoid duplicates.
  If required access is denied, report the exact blocked operation without exposing
  the key, expanding its scopes or silently switching credentials.
- Having a write key does not authorize tracking changes, feature flags, public
  sharing, deletion, campaign changes or other unrelated mutations.

## Booking and campaign attribution

1. For a named booking, obtain its exact Cal.com booking UID and timestamp from
   the booking record or confirmation. If using Gmail, first read
   `../gmail-oauth/SKILL.md` and `../gmail-read/SKILL.md` and use the correct mailbox.
2. Website booking events can include `demo_booked`, `demo_requested` and
   `lead_form_submitted`. Match `properties.event_id = 'cal-<booking UID>'`.
   Multiple event names with the same booking ID are one booking, not multiple
   conversions. An anonymous PostHog visitor can still be matched by this ID.
3. Follow the matched `distinct_id` and `$session_id` to the entry page,
   referrer, UTMs and CTA. Time proximity, company geography or an ad's aggregate
   conversion count alone does not identify a person.
4. For Meta attribution, inspect recorded tags/click IDs, then compare exact
   campaign/ad IDs using the existing Meta connector. Verify its selected
   account before reporting. A Meta click ID alone does not identify a specific
   paid campaign; use the available campaign evidence.
5. Separate the **observed booking-session source** from **original discovery**.
   Google referral with no ad tags is consistent with organic search, not proof
   the person never saw a Meta ad. State the searched lookback and gaps from
   consent, different browsers/devices or missing identity links. Do not infer
   a search term that PostHog did not record.
6. When asked about outbound, separately query exact Lead identities, company
   aliases/domains, campaign membership and positive send/message records.
   An account research CSV is not evidence of enrollment or contact. Attribute
   a coworker referral only with evidence, not merely a shared employer.

Dashboard and insight changes require the user's request for that work. This skill
does not authorize sends, CRM/Sheet updates, tracking changes, feature-flag changes,
campaign activation or recurring monitoring.
