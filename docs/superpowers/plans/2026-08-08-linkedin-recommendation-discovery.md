# LinkedIn Recommendation-Based Discovery — Replacement Plan

**Date:** 2026-08-08

**Branch:** `codex/recommendation-discovery-plan`

**Status:** Implemented and verified in this branch; pending user review

**Verification:** The final implementation passed all 79 discovery tests,
`manage.py check`, migration drift checking, and `start_discovery --dry-run`.
Athena's saved-session probe verified two live My Network sections, one exact
section-scoped Show All overlay, an opened profile's `More profiles for you`
rail, and its browse-map overlay without database or outbound writes. A final
one-task Athena run screened 22 My Network cards, opened one of four plausible
matches, saved one full structured profile under Athena and enabled non-CMMC
ICP `CSPs`, and left Lead, Deal, Message, ActionLog, and non-discovery Task
counts unchanged.

**Supersedes:** The People-search source and search-query cursor portions of
`docs/superpowers/plans/2026-07-29-linkedin-profile-discovery.md`

## Goal

Replace the current keyword-based LinkedIn People-search discovery source with
LinkedIn's own personalized recommendation surfaces.

The workflow should:

1. Open the logged-in sender's `/mynetwork/grow/` page.
2. Collect profiles from relevant recommendation sections such as
   `Suggestions for you` and `People you may know ...`.
3. Compare each visible recommendation card with that sender's enabled ICP
   descriptions in `linkedin/icp_messages.json`.
4. Open only plausible matches.
5. Fetch the structured LinkedIn profile and save it to
   `LinkedInDiscoveryLead`, including the sender and potential ICP.
6. Optionally use the opened profile's `More profiles for you` rail as one
   additional bounded source of recommendations.
7. Stop when the sender reaches `DISCOVERY_DAILY_LIMIT` or an independent
   browsing safety cap.

This remains a collection-only lane. It must not create CRM Leads, Deals,
Messages, connection requests, follow-ups, or campaign assignments.

## Why Replace People Search

The current implementation rotates through static keyword queries and pages.
That has several weaknesses:

- The same queries tend to return the same ranked people.
- Query and page cursors add state without creating meaningful variety.
- Search results are less personalized to the sender's real LinkedIn network
  and recent activity.
- Search usage can encounter LinkedIn commercial-use/search limits.
- `search_queries` duplicate sourcing intent across every sender/ICP block even
  though ICP validation is already described by `discovery.profile`.
- The source does not take advantage of LinkedIn's continuously refreshed
  recommendation graph.

LinkedIn recommendations are a better discovery feed because LinkedIn already
varies them using the sender's network, profile, geography, employer, mutual
connections, and recent activity. Our code should decide only whether a card
may fit an enabled ICP and whether to store the full profile.

## Live Athena UI Findings

The following was verified read-only in Athena's existing signed-in persistent
Chromium profile on 2026-08-08. No connect, follow, message, accept, ignore, or
remove action was clicked.

### `/mynetwork/grow/`

The page contains multiple independent recommendation sections. The exact
sections are personalized, so code must discover supported sections by their
heading and structure instead of assuming a fixed order.

Observed examples:

- `People you may know from <company>`
- `People you may know in <location>`
- `Suggestions for you`

Each recommendation card exposes:

- a canonical `/in/<public-identifier>/` profile link,
- a name,
- a headline or role context,
- sometimes a company, location, or mutual-connection context,
- outbound controls such as Invite/Connect, Follow, Message, or Remove.

The outbound controls are not needed and must never be targeted.

`Show all suggestions for <section heading>` opens an in-page modal containing
more cards for that specific section. It does not reliably navigate to a
standalone search/results page. The collector therefore needs section-scoped
Show All behavior, modal extraction, and modal dismissal.

`Suggestions for you` was already a long card list in the primary page during
the live inspection and did not expose a Show All control in that rendering.
The collector must support both inline-only sections and sections with a
Show All modal.

### Profile recommendation rail

An opened profile contains a right-rail section named `More profiles for you`.
The rail exposes several profile links plus a `Show all more profiles for you`
link. That link opens:

```text
/in/<public-identifier>/overlay/browsemap-recommendations/
```

The overlay exposes a larger list of related profiles. Its adjacent action can
vary per relationship—Connect, Follow, or Message—so extraction must be based
only on `/in/` profile anchors inside the recommendation container.

## Product Rules to Preserve

- Discovery stays behind `ENABLE_PROFILE_DISCOVERY`.
- A sender participates only when at least one of their ICPs has an enabled
  `discovery` block in `linkedin/icp_messages.json`.
- All current non-CMMC discovery ICPs remain enabled; CMMC remains omitted.
- Weekday discovery still starts only after that sender's daemon has parked or
  closed its connection-request lane for the day.
- Rest-day discovery remains eligible without waiting for weekday connection
  work.
- `DISCOVERY_DAILY_LIMIT` remains the authoritative per-sender limit on newly
  saved profiles.
- The discovery table remains globally deduplicated by canonical LinkedIn
  identity.
- The first sender that saves a profile remains the row's storing sender.
- Existing CRM Leads, existing discovery rows, self-profile, malformed URLs,
  and suppressed profiles are skipped before a profile visit.
- Card screening remains lightweight and chooses only a `potential_icp`.
- A full profile is saved only after the profile is opened and fetched.
- Rejected recommendation cards are not accumulated in a new database table.
- No discovery code may click Connect, Invite, Follow, Message, Accept, Ignore,
  Dismiss suggestion, or Remove suggestion controls.

## Target Flow

```text
discovery becomes eligible for sender
                |
                v
load sender's enabled discovery ICP descriptions
                |
                v
open /mynetwork/grow/
                |
                v
find supported recommendation sections
                |
                +--> extract inline cards
                |
                +--> open section-scoped Show All modal when present
                |         |
                |         +--> extract/scroll modal cards
                |
                v
canonicalize and deduplicate cards
                |
                v
lightweight card-to-ICP screen
          no <--+--> plausible match
                          |
                          v
                 open and fetch profile
                          |
                          +--> enqueue one-hop More profiles for you cards
                          |
                          v
        save LinkedInDiscoveryLead(sender, potential_icp, profile)
                          |
                          v
              daily limit or safety cap reached?
                    no ---+--- yes --> stop
```

## Source Architecture

### 1. My Network recommendation source

Add `linkedin/discovery/sources/mynetwork_recommendations.py`.

Responsibilities:

- Navigate directly to `https://www.linkedin.com/mynetwork/grow/`.
- Detect authentication/checkpoint/challenge pages using the existing expected
  discovery exceptions.
- Locate recommendation sections under primary content.
- Accept headings matching a small normalized allowlist/pattern set:
  - exact `Suggestions for you`,
  - headings beginning with `People you may know`,
  - additional headings only after a live fixture and test are added.
- Explicitly exclude Invitations, People who viewed your profile, ads,
  Premium-profile upsells, games, and general navigation links.
- Extract profile cards only from the current accepted section container.
- Carry source metadata in each card:
  - `source_kind=mynetwork_recommendation`,
  - normalized section heading,
  - inline or Show All modal origin,
  - visible card context.
- Find Show All relative to its section heading/container. Never click the
  first generic Show All link on the page.
- Extract the opened modal, scroll it in bounded increments, and dismiss it
  before continuing to another section.
- Return canonical, de-duplicated `DiscoveryCard` values without clicking any
  card action buttons.

### 2. Profile recommendation source

Add `linkedin/discovery/sources/profile_recommendations.py`.

Responsibilities:

- Run only after discovery already opened a plausible profile.
- Locate the profile Aside section headed `More profiles for you`.
- Extract its visible `/in/` profile anchors.
- Optionally open the exact
  `/overlay/browsemap-recommendations/` Show All link for that profile.
- Extract only anchors inside the More profiles overlay.
- Return related cards with:
  - `source_kind=profile_recommendation`,
  - `source_profile_public_identifier`,
  - `recommendation_depth=1`,
  - visible card context.
- Never expand a depth-1 card into another profile recommendation list in the
  first release.

### 3. Bounded one-hop traversal

Use a small queue, not an unbounded recursive crawl:

- My Network cards are depth 0 seeds.
- Related cards harvested from a visited profile are depth 1.
- Maximum recommendation depth is 1.
- Process depth 0 before depth 1 so personalized network suggestions remain
  the primary source.
- Keep a `seen_public_identifiers` set in the discovery task payload for the
  current local-day run.
- Deduplicate again through existing CRM/discovery/suppression checks before a
  visit and through the unique database constraint on save.
- Do not create a rejected-card database ledger. Repeated rejected cards on a
  later day are acceptable because LinkedIn's recommendation feed can change
  and every run is bounded.

## ICP Configuration Simplification

Keep discovery configuration in `linkedin/icp_messages.json`, but simplify the
shape to the information recommendation discovery actually needs:

```json
{
  "Athena": {
    "CSPs": {
      "discovery": {
        "enabled": true,
        "profile": "Executives, security, compliance, and public-sector leaders at cloud software providers with possible FedRAMP relevance."
      }
    }
  }
}
```

Required changes:

- Remove every `search_queries` array from `linkedin/icp_messages.json`.
- Remove `search_queries` from `DiscoveryTarget`.
- Remove `discovery_search_queries()`.
- Change strict discovery validation to allow only `enabled` and `profile`.
- Continue requiring a non-empty `profile` for every enabled block.
- Keep missing discovery blocks disabled and CMMC blocks absent.
- Keep configuration loaded fresh for each run.

The ICP description answers “does this recommendation look plausibly relevant?”
It does not drive navigation or become final qualification.

## Discovery Card and Task Payload

Extend `DiscoveryCard` with optional source metadata rather than coupling the
collector to a specific page:

- `source_kind`
- `source_section`
- `source_profile_public_identifier`
- `recommendation_depth`

Replace the search-specific discovery task state.

Remove:

- `source=people_search`
- `query_index`
- `page`
- `query_pages`
- `exhausted_query_indexes`
- query/page advancement logic
- `pages_scanned`
- `queries_exhausted` and `page_limit_reached` stop reasons

Add:

- `source=mynetwork_recommendations`
- `section_cursor`
- `sections_scanned`
- `scroll_rounds`
- `consecutive_scrolls_without_new_cards`
- `pending_cards`
- `seen_public_identifiers`
- `recommendation_depth`
- existing `cards_scanned`, `profile_visits`, `consecutive_no_matches`,
  `saved`, `run_started_at`, and `stop_after_pending`

Payload validation must fail loudly for malformed cursor state. Startup
reconciliation may reset an old pending People-search payload to a fresh
recommendation payload because the CRM models are owned by this project and no
backward-compatibility shim is required.

## Limits and Scheduling

Keep:

- `DISCOVERY_DAILY_LIMIT`
- `DISCOVERY_MAX_CARDS_PER_RUN`
- `DISCOVERY_MAX_PROFILE_VISITS_PER_RUN`
- `DISCOVERY_MAX_CONSECUTIVE_NO_MATCHES`
- `DISCOVERY_MAX_RUN_MINUTES`
- profile delay minimum/maximum

Replace:

- `DISCOVERY_MAX_PAGES_PER_RUN`

With:

- `DISCOVERY_MAX_SECTIONS_PER_RUN`
- `DISCOVERY_MAX_SCROLL_ROUNDS_PER_RUN`
- `DISCOVERY_MAX_CONSECUTIVE_EMPTY_SCROLLS`
- `DISCOVERY_MAX_PROFILE_RECOMMENDATIONS_PER_VISIT`

All must be positive and strictly validated. The collector stops at the first
reached condition. A scroll counts as productive only when it reveals at least
one previously unseen canonical public identifier.

The scheduling/gating logic in `linkedin/discovery/config.py` remains intact
except for mapping the new limit names. This plan does not reintroduce start or
end hours and does not change the weekday post-outbound/rest-day behavior.

## Code Removal

Delete the inferior source instead of retaining it as a hidden fallback:

- Delete `linkedin/discovery/sources/people_search.py`.
- Delete `tests/discovery/test_people_search.py`.
- Remove `linkedin.actions.search.search_people` from this discovery lane.
- Remove the People-search imports and branch from
  `linkedin/discovery/collector.py`.
- Remove all query/page cursor helpers and telemetry.
- Remove `search_queries` loading/validation/flattening from
  `linkedin/icp_outbound.py`.
- Remove all `search_queries` values from `linkedin/icp_messages.json`.
- Remove `DISCOVERY_MAX_PAGES_PER_RUN` from `linkedin/conf.py`,
  `linkedin/env_spec.py`, tests, and docs.
- Remove People-search wording from `AGENTS.md`, `ARCHITECTURE.md`, management
  command output, logs, and the earlier plan's implemented-source status.
- Do not silently fall back to People Search when recommendation selectors
  fail. Raise `DiscoverySurfaceError`, save diagnostics, and stop the bounded
  run so selector drift is visible.

People Search may still exist for unrelated explicit search actions elsewhere
in the product; only the standalone profile-discovery dependency is removed.

## Implementation Phases

### Phase 1: Read-only source probe and fixtures

Add a read-only management command such as:

```bash
.venv/bin/python manage.py probe_discovery_recommendations \
  --handle athenaaghdami \
  --output artifacts/discovery/athena-recommendations.json
```

The probe should:

- reuse the sender's existing persistent browser session,
- verify the authenticated LinkedIn identity,
- inspect `/mynetwork/grow/`, one scoped Show All modal, one profile rail, and
  one More profiles overlay,
- emit sanitized selector/structure fixtures and summary counts,
- make no database writes,
- create no Tasks,
- click no outbound or suggestion-removal controls,
- close/dismiss only overlays it opened.

Use the probe output to create deterministic HTML/JSON fixtures with names and
profile identifiers anonymized. The live Athena inspection summarized above is
evidence for the design, but checked-in fixtures are required before selectors
are implemented.

### Phase 2: Source adapters

- Implement the My Network adapter.
- Implement the profile recommendation adapter.
- Keep selector roots section/modal-specific.
- Add structured diagnostics when `/in/` links exist but the expected section
  or card root no longer matches.
- Confirm authentication and LinkedIn limit/error pages fail using typed
  expected exceptions.

### Phase 3: Collector replacement

- Replace query/page progression with the source/section/scroll queue.
- Run existing deterministic skips before LLM screening.
- Batch-screen only new cards against the sender's enabled ICP descriptions.
- Queue plausible cards with their potential ICP.
- Visit one profile per task unit as today.
- Harvest a capped depth-1 recommendation batch from the visited profile.
- Save only the full Voyager profile in `LinkedInDiscoveryLead`.
- Preserve atomic daily-save-limit enforcement.
- Preserve continuation delays and sender ownership.

### Phase 4: Configuration and documentation cleanup

- Simplify every non-CMMC discovery block.
- Replace env limit declarations and validation.
- Update command output to report enabled ICP count, section/scroll caps,
  capacity remaining, and gate state—not search-query count.
- Update `AGENTS.md` and `ARCHITECTURE.md` in the same implementation commit(s).
- Mark the old plan's People-search-specific implementation as superseded by
  this plan rather than leaving contradictory operational docs.

### Phase 5: Controlled Athena verification

1. Stop Athena's daemon or otherwise guarantee a single owner of her
   persistent Chromium profile.
2. Run the read-only probe and inspect its artifact.
3. Run unit and discovery integration tests.
4. Run one bounded live task:

   ```bash
   .venv/bin/python manage.py run_discovery_once \
     --handle athenaaghdami \
     --max-tasks 1
   ```

5. Confirm the run opened only allowed recommendation/profile surfaces.
6. Confirm no Connect/Invite/Follow/Message/Accept/Ignore/Remove action log or
   Task was created.
7. Confirm any new `LinkedInDiscoveryLead` has:
   - canonical URL/public identifier,
   - full structured profile JSON,
   - `stored_by_operator=Athena`,
   - Athena's account username,
   - one enabled non-CMMC `potential_icp`.
8. Increase the bounded task count only after the one-task live run passes.

## Test Plan

### Source unit tests

- Finds exact `Suggestions for you`.
- Finds dynamic `People you may know ...` headings.
- Rejects Invitations, ads, Premium upsells, and unrelated `/in/` links.
- Extracts canonical cards from inline sections.
- Resolves Show All relative to the correct section.
- Extracts and scrolls a Show All modal.
- Stops after consecutive empty scrolls.
- Extracts `More profiles for you` rail cards.
- Extracts browse-map overlay cards.
- Never targets action controls, including when their label is the only text
  near a profile link.
- Raises `DiscoverySurfaceError` on selector drift instead of broad page link
  harvesting.
- Raises the expected authentication error on login/checkpoint/challenge pages.

### Configuration tests

- Enabled discovery requires `profile`, not `search_queries`.
- Unknown discovery keys fail validation.
- CMMC remains absent/disabled for every sender.
- All intended non-CMMC ICPs remain enabled for each sender.
- New section/scroll/profile-recommendation limits reject zero or negative
  values.

### Collector/task tests

- Fresh payload contains no search query/page state.
- Old pending search payload is reset during reconciliation.
- Section and scroll counters persist across task continuations.
- Duplicate cards across sections and profile rails are screened once per run.
- Existing CRM/discovery/self/suppressed profiles are skipped.
- Depth-1 cards never expand further.
- Daily saved-row limit remains per sender and atomic.
- Card, visit, no-match, scroll, time, and daily limits each stop cleanly.
- A saved row records sender and selected potential ICP.
- No Lead, Deal, Message, or outbound Task is created.
- The discovery-only runner still claims no other task type.

### Required verification commands

```bash
.venv/bin/python -m pytest tests/discovery -q
.venv/bin/python manage.py check
.venv/bin/python manage.py start_discovery --dry-run
```

Then perform the bounded Athena live verification described above.

## Acceptance Criteria

- Discovery no longer performs keyword People searches.
- `linkedin/icp_messages.json` contains no discovery `search_queries`.
- Athena's signed-in `/mynetwork/grow/` recommendations can seed a bounded run.
- Section-scoped Show All modals are supported without ambiguous global clicks.
- Opened profiles can contribute one bounded hop from `More profiles for you`.
- The same profile is not repeatedly screened within one run even if multiple
  sections recommend it.
- Only card-level plausible ICP matches are opened.
- Only fully fetched profiles are stored.
- Stored rows contain the originating sender and potential ICP.
- Daily saved-profile limits and independent browsing caps stop the workflow.
- The lane remains read/collect-only on LinkedIn and isolated from outbound CRM
  automation.
- Selector drift fails visibly; it never degrades into harvesting arbitrary
  `/in/` links from the page.

## Recommended First Release Boundary

Ship the My Network source plus one-hop profile recommendations together, but
keep the first live rollout conservative:

- Athena only,
- one discovery task,
- recommendation depth 1,
- low daily save limit,
- no People-search fallback,
- inspect stored rows and browser telemetry before enabling the second sender.

This validates the personalized graph approach without changing any of the
already-correct sender gating, ICP validation, storage, or daily-limit rules.
