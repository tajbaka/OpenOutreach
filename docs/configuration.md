# Configuration

Configuration is split between environment variables (`.env` file), Django models (managed via interactive
onboarding or Django Admin), and hardcoded defaults in `linkedin/conf.py`.

## LLM Configuration (`.env`)

LLM settings are stored in `.env` (project root). Any
OpenAI-compatible provider works. These are prompted during interactive onboarding if missing.

| Variable | Description | Default |
|:---------|:------------|:--------|
| `LLM_API_KEY` | API key for an OpenAI-compatible provider. | (required) |
| `AI_MODEL` | Model identifier for qualification, follow-up, and search keyword generation. | (required) |
| `LLM_API_BASE` | Base URL for the API endpoint. | (none) |

These can also be set as environment variables directly.

## Campaign Settings (Django Model)

Campaign data is stored in the `Campaign` Django model (with `name` and `users` M2M), managed via
Django Admin (`/admin/`) or created during interactive onboarding.

| Field | Type | Description |
|:------|:-----|:------------|
| `product_docs` | text | Product/service description. Used by LLM qualification, follow-up agent, and search keyword generation. |
| `campaign_objective` | text | Campaign goal. Used by LLM qualification, follow-up agent, and search keyword generation. |
| `booking_link` | string | URL included in follow-up messages when suggesting a meeting. |
| `is_freemium` | boolean | Whether this is a freemium campaign (uses KitQualifier instead of BayesianQualifier). |
| `action_fraction` | float | Target fraction of total connections for freemium campaigns. |

## Account Settings (Django Model)

Account data is stored in the `LinkedInProfile` Django model (1:1 with `auth.User`), managed via
Django Admin or created during interactive onboarding.

| Field | Type | Description | Default |
|:------|:-----|:------------|:--------|
| `linkedin_username` | string | LinkedIn login email. | (required) |
| `linkedin_password` | string | LinkedIn password. | (required) |
| `active` | boolean | Enable/disable this account. | `true` |
| `subscribe_newsletter` | boolean | Receive OpenOutreach updates. | `true` |
| `connect_daily_limit` | integer | Max connection requests per day. | `20` |
| `connect_weekly_limit` | integer | Max connection requests per week. | `100` |
| `follow_up_daily_limit` | integer | Max follow-up messages per day. | `30` |
| `discovery_daily_limit` | integer | Max newly saved discovery profiles per discovery-local day; `0` disables discovery for this sender. | `25` |
| `legal_accepted` | boolean | Whether the user accepted the legal notice. | `false` |

Rate limiting is enforced by `LinkedInProfile` methods (`can_execute()`, `record_action()`,
`mark_exhausted()`) backed by the `ActionLog` model, surviving daemon restarts.

### GDPR Location Detection

On the first run, the daemon checks the logged-in user's LinkedIn country code against a static set of
ISO-2 codes for jurisdictions with opt-in email marketing laws (EU/EEA, UK, Switzerland, Canada, Brazil,
Australia, Japan, South Korea, New Zealand).

- **Non-GDPR location**: `subscribe_newsletter` is auto-set to `true` for that account.
- **GDPR-protected location**: the existing value is preserved (no override).
- **Unknown/empty location**: defaults to GDPR-protected (errs on the side of caution).

This check runs once per account (a database sentinel record prevents re-runs).

## Standalone Profile Discovery

Profile discovery is a separate, disabled-by-default daemon lane. It runs only
after normal outbound hours or in the configured rest-day window. One bounded
task scans a LinkedIn People-search page, compares visible cards with the
logged-in sender's enabled ICP descriptions, opens plausible profiles, and
saves structured profile data to `LinkedInDiscoveryLead`.

It does not create `crm.Lead`, `crm.Deal`, connection requests, messages, or
other outbound state.

### Sender/ICP configuration

Discovery metadata lives inside each sender's existing
`linkedin/icp_messages.json` ICP block:

```json
{
  "Arian": {
    "CSPs": {
      "discovery": {
        "enabled": true,
        "profile": "Security, compliance, and public-sector leaders at cloud software providers with possible FedRAMP relevance.",
        "search_queries": [
          "FedRAMP SaaS founder",
          "public sector cloud CISO"
        ]
      },
      "linkedin_connect_note": ["..."],
      "linkedin_connect_followup": ["..."]
    }
  }
}
```

A missing block or `"enabled": false` disables that sender/ICP combination.
Enabled blocks require a non-empty `profile` and at least one explicit
`search_queries` value. The ICP Messages Sheet pull preserves this JSON-only
metadata. `LLM_API_KEY` and `AI_MODEL` must also be configured before the
feature can be enabled; startup validation fails before browser activity when
either is missing.

### Discovery environment settings

| Variable | Default | Description |
|:---------|:--------|:------------|
| `ENABLE_PROFILE_DISCOVERY` | `false` | Global feature gate. `ENABLE_ACTIVE_HOURS` must also remain enabled. |
| `DISCOVERY_TIMEZONE` | `America/Toronto` | Timezone for windows and per-sender daily counts; must match `ACTIVE_TIMEZONE`. |
| `DISCOVERY_WEEKDAY_START_HOUR` / `DISCOVERY_WEEKDAY_END_HOUR` | `18` / `21` | Weekday window; start must be at or after `ACTIVE_END_HOUR`. |
| `DISCOVERY_RUN_ON_REST_DAYS` | `true` | Permit the separate rest-day window. |
| `DISCOVERY_REST_DAY_START_HOUR` / `DISCOVERY_REST_DAY_END_HOUR` | `11` / `16` | Rest-day discovery window. |
| `DISCOVERY_MAX_CARDS_PER_RUN` | `200` | Maximum result cards scanned in one sender run. |
| `DISCOVERY_MAX_PAGES_PER_RUN` | `10` | Maximum search pages scanned in one sender run. |
| `DISCOVERY_MAX_PROFILE_VISITS_PER_RUN` | `40` | Maximum profiles opened in one sender run. |
| `DISCOVERY_MAX_CONSECUTIVE_NO_MATCHES` | `75` | Sparse-result stop condition. |
| `DISCOVERY_MAX_RUN_MINUTES` | `120` | Wall-clock run cap. |
| `DISCOVERY_PROFILE_DELAY_MIN_SECONDS` / `DISCOVERY_PROFILE_DELAY_MAX_SECONDS` | `20` / `45` | Randomized delay between bounded task units. |

The `LinkedInProfile.discovery_daily_limit` database field is authoritative
for saved volume. Duplicates do not consume it. Once reached, the next
discovery task is scheduled for the next eligible local day. The independent
card/page/profile/no-match/time caps still stop runs that save nothing.

Inspect configuration without writing queue state:

```bash
.venv/bin/python manage.py start_discovery --dry-run
```

Enqueue the next eligible task for all active profiles or one Django handle:

```bash
.venv/bin/python manage.py start_discovery
.venv/bin/python manage.py start_discovery --handle arian
```

For a controlled live check that cannot claim any other daemon lane, keep one
sender browser open for a bounded discovery batch and then exit:

```bash
.venv/bin/python manage.py run_discovery_once --handle arian --max-tasks 3
```

## Hardcoded Defaults (`conf.py:CAMPAIGN_CONFIG`)

Timing and ML defaults are hardcoded in `linkedin/conf.py`. These are not user-configurable.

| Key | Value | Description |
|:----|:------|:------------|
| `check_pending_recheck_after_hours` | `24` | Base interval (hours) before first pending check. Doubles per profile via exponential backoff. |
| `enrich_min_interval` | `1` | Floor (seconds) between enrichment API calls during auto-discovery. |
| `min_action_interval` | `120` | Minimum seconds between major actions. |
| `qualification_n_mc_samples` | `100` | Monte Carlo samples for BALD computation. |
| `min_ready_to_connect_prob` | `0.9` | GP probability threshold for promoting QUALIFIED to READY_TO_CONNECT. |
| `min_positive_pool_prob` | `0.20` | P(f > 0.5) threshold for positive pool check in exploit mode. |
| `embedding_model` | `BAAI/bge-small-en-v1.5` | FastEmbed model for 384-dim profile embeddings. |
| `connect_delay_seconds` | `10` | Delay between connect tasks. |
| `connect_no_candidate_delay_seconds` | `300` | Delay when candidate pool is empty. |
| `check_pending_jitter_factor` | `0.2` | Multiplicative jitter factor for backoff. |

Other constants: `MIN_DELAY` (5s) / `MAX_DELAY` (8s) for human-like wait timing.

See [Templating](./templating.md) for follow-up messaging configuration.
