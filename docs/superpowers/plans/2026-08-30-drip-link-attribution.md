# Lean V1 Drip Email Link Attribution Plan

**Date:** 2026-08-30

**Status:** Implemented and locally verified on isolated feature branches; no
live send, deployment, or production data change has been made

**Repositories:**

- OpenOutreach owns the generated reference and its exact drip-delivery mapping.
- FedRampGPT stores that reference with meaningful conversions.

**Implementation bases:**

- OpenOutreach: `d49757c116ea3db6eb6b7dc133d82c1697f018e9`
- FedRampGPT: `a93594d11e7da4b509e96e16d1480fc8234c6251`

**Implemented FedRampGPT feature commit:**

- `ecef75a3ebc2420f36001fd1478d74b2a81af3cb`

**Scope rule:** V1 is deliberately a small, manual-review integration. It does
not build a cross-repository attribution platform.

## 1. Goal

Allow a reviewed Gmail drip step to send a clean first-party link such as:

```text
https://boundera.io/fedramp-automation?ref=oo_EjRWeJCrze8SNFZ4kKvN7w
```

The opaque `ref` identifies the exact generated link without exposing a Lead
ID, email, sender, campaign name, or other business data. OpenOutreach stores
the immutable mapping to the drip delivery. FedRampGPT carries the same value
into meaningful conversion submissions and stores it durably.

For V1, attribution is reviewed by looking up the same exact reference in the
two systems. There is no automatic transfer or imported conversion state.

## 2. V1 success criteria

- A Gmail drip step can declare zero or one tracked first-party link.
- The received email contains one short `ref=oo_*` value rather than a long UTM
  query string.
- OpenOutreach can resolve that reference to the exact delivery, Lead,
  campaign version, sender, theme, and step.
- Retries and Task rematerialization reuse the same frozen URL.
- FedRampGPT's central Next proxy recognizes a valid OpenOutreach reference,
  stores it in a host-only HttpOnly session cookie, and redirects to the same
  page without the reserved reference in the visible URL.
- FedRampGPT does not treat an `oo_*` value as `utm_source` and does not send
  the exact value as an analytics property.
- Meaningful conversions store the reference when one exists and continue to
  work normally when it does not.
- Request-demo information becomes durable rather than existing only in the
  notification email.
- Existing durable rules-alert, gap-assessment, and resource-download records
  continue to be authoritative for their own workflows.
- Attribution adds no new Gmail worker, LinkedIn worker, Task type, campaign
  stop condition, or synchronous call between repositories.

## 3. Scope boundary

V1 ends at generating the clean link, retaining its exact delivery mapping,
capturing it on the agreed conversion surfaces, and manually joining the
OpenOutreach lookup output to FedRampGPT conversion rows by the exact
reference. Work outside that path is not part of this plan.

## 4. Current-code findings that drive the design

### 4.1 OpenOutreach already owns the required delivery graph

The durable attribution chain already exists:

```text
DripCampaign
    -> DripCampaignVersion
    -> DripEnrollment
    -> DripLane
    -> DripDelivery
```

`DripDelivery` is the correct parent for a generated tracked link. A `Task` is
not: Tasks can be retired and rematerialized while the delivery remains the
authoritative intent and send record.

`drip/services/reconciliation.py:_materialize_delivery` is the correct creation
boundary because it renders and freezes the delivery body before the Task is
created. `drip/tasks/gmail.py` sends that frozen body, and `gmail/client.py`
already sends it as plain text.

### 4.2 FedRampGPT already has the conversion surfaces

The repository currently has:

- a central Next request proxy that can intercept marketing-page landings
  before rendering;
- the production Django request-demo endpoint;
- durable rules-alert subscribers;
- durable gap-assessment leads;
- durable resource downloads.

The missing shared behavior is small: validate and retain `oo_*` in one
first-party session cookie, let accepted backend submissions read it, and
record a compact conversion row. Because the proxy redirects before the page
renders, reserved references never reach the existing browser analytics/ref
path. Non-`oo_*` referral behavior stays unchanged.

## 5. Public reference contract

### 5.1 Format

```text
oo_<base64url-token>
```

Rules:

- prefix exactly `oo_`;
- 128 random bits encoded as unpadded URL-safe Base64;
- exactly 22 token characters after the prefix;
- exact, case-sensitive comparison;
- bounded total length;
- no encoded IDs or personally identifiable data; and
- the same valid and invalid fixture values tested in both repositories.

The reference is an origin/delivery key, not proof of the converter's identity.

### 5.2 Destination URL rules

OpenOutreach accepts only reviewed destinations that:

- use `https`;
- use an explicitly configured first-party Boundera marketing host;
- contain no credentials or ambiguous authority syntax; and
- do not already contain a `ref` parameter.

The builder preserves existing non-attribution query parameters and fragments,
then adds exactly one canonical `ref` value. It does not rewrite arbitrary URLs
found in message prose.

## 6. OpenOutreach changes

### 6.1 Add `DripTrackedLink`

Add one immutable model in `drip/models.py`:

```text
DripTrackedLink
    reference          unique indexed string
    delivery           protected FK to DripDelivery
    link_key           reviewed stable slug
    destination_url    canonical URL without outreach ref
    attributed_url     canonical URL with outreach ref
    created_at
```

Invariants:

- unique `(delivery, link_key)`;
- only a Gmail-lane delivery may own one;
- V1 permits at most one row per delivery;
- `attributed_url` contains exactly the row's reference;
- destination and attributed URLs pass the first-party validator; and
- persisted fields cannot be edited through normal application paths.

The token is random; the database relationship supplies campaign, sender,
step, and Lead context.

### 6.2 Add a structured Gmail manifest link

Publish new tracked-link campaigns using manifest schema version 3. Existing
persisted no-link campaign snapshots continue running from their immutable
database manifest without republishing.

```json
{
  "delay_days": 1,
  "subject": "A clearer way to see the gap",
  "body": "Thought this view might be useful: {tracked_link}",
  "link": {
    "key": "fedramp_automation",
    "url": "https://boundera.io/fedramp-automation"
  }
}
```

Validation rules:

- `link` is optional and Gmail-only;
- a link requires exactly one `{tracked_link}` placeholder;
- a placeholder without a link fails publication;
- multiple placeholders fail publication;
- raw URLs remain ordinary untracked copy and are never silently rewritten;
- `{our_website_url}` remains unchanged; and
- LinkedIn message/media behavior remains unchanged.

### 6.3 Generate and freeze during materialization

Add a small `drip/link_attribution.py` module for reference generation,
validation, and URL construction.

During `_materialize_delivery`:

1. Read the structured link from the frozen campaign version.
2. Generate the opaque reference.
3. Build the exact attributed URL.
4. Render it into `{tracked_link}`.
5. Create the `DripDelivery` and `DripTrackedLink` in the existing transaction.
6. Create the Task only after both durable objects exist.

A unique-key collision may be regenerated only before persistence. Any other
failure rolls back the delivery, link, and Task together.

### 6.4 Preserve send behavior

The Gmail handler continues receiving a completely frozen body. It does not
generate or decorate URLs.

Before provider submission, its existing reservation path verifies:

- the link row belongs to this Gmail delivery;
- the exact `attributed_url` occurs once in `frozen_body`; and
- no different `ref=oo_*` appears in the body.

A mismatch fails before Gmail submission. Successful `crm.Message.raw`
evidence adds the reference and link key, while the delivery/link rows remain
authoritative.

### 6.5 Add one narrow lookup command

Add a read-only command:

```text
.venv/bin/python manage.py resolve_drip_reference oo_<token>
```

It prints the reference, link key, delivery status/time, Lead, sender,
campaign/version, theme, and step. It performs no writes and exposes no public
HTTP resolver.

## 7. FedRampGPT changes

### 7.1 Capture and scrub in the central Next proxy

Add the shared token helper under `website/lib/` and handle reserved references
in the root `website/proxy.ts` request boundary before a marketing page renders.
The proxy:

- runs only for `GET` and `HEAD` requests covered by the marketing-page matcher;
- treats every `ref` whose trimmed, case-insensitive prefix is `oo_` as
  reserved, while accepting only the exact canonical token syntax;
- redirects a tracked `www.boundera.io` landing to the apex host first so the
  eventual cookie remains host-only;
- on the apex host, accepts exactly one canonical reserved reference, sets it
  as the `boundera_outreach_ref` cookie, and redirects to the scrubbed URL;
- makes that cookie HttpOnly, SameSite=Lax, host-only, path-wide, Secure on an
  HTTPS request, and session-scoped by omitting a persistent expiry;
- removes every reserved `oo_*` `ref` from the redirect while preserving the
  path, ordinary `ref` values, all other query values, and the fragment;
- clears an earlier outreach cookie when a reserved reference is malformed or
  ambiguous; and
- leaves requests with no reserved reference untouched.

This server-side redirect means the reserved value is gone before application
rendering and browser analytics. There is no `sessionStorage`, client form
field, `history.replaceState`, page-view sanitizer, or analytics-provider
change. The HttpOnly cookie is deliberately unavailable to browser JavaScript.

### 7.2 Add one generic conversion ledger

Add one compact model in `backend/marketing/models.py`:

```text
MarketingConversion
    kind               constrained conversion type
    outreach_ref       validated oo_* value or blank
    email              normalized email or blank
    company            bounded company or blank
    context            bounded source/slug text or blank
    occurred_at
```

Index `outreach_ref` with descending `occurred_at` for manual lookup. There is
no event ID, uniqueness constraint, JSON payload, source-object relation, or
application-level immutability contract. Blank attribution is valid for the
request-demo ledger so organic submissions remain durable.

Add one shared recording service that:

- accepts only the exact canonical reference from the first-party cookie;
- permits the trusted Next resource-download bridge to forward that same exact
  value in POST data when the Django request has no cookie;
- treats an absent or invalid value as blank rather than trusting it;
- lowercases and bounds email and bounds company/context;
- creates one row per accepted call without client idempotency; and
- never calls OpenOutreach.

The service defaults to recording attributed conversions only. Request demo
opts into organic rows as well; rules alerts, gap-assessment milestones, and
resource downloads create a conversion row only when a valid outreach
reference exists.

Initial kinds:

- `request_demo_submitted`
- `rules_alerts_subscribed`
- `gap_assessment_lead`
- `gap_assessment_completed`
- `gap_assessment_expert_requested`
- `resource_downloaded`

### 7.3 Wire only meaningful conversion submissions

The normal Django-backed forms need no attribution field: the browser sends the
HttpOnly first-party cookie with their existing requests and the backend reads
it. Ordinary CTA clicks and page views do not call the conversion service. The
one exception is the resource-download Next route, which validates its cookie
server-side and forwards the reference to Django through the existing trusted
form-encoded bridge.

#### Request demo

Change the canonical Django `/api/request-demo/` handler. After the honeypot and
required-email checks, record email and company before the internal
notification attempt. This is the durable request-demo record for both organic
and attributed submissions. Team email remains an independent side effect; a
retry may create another conversion row and another alert because V1 adds no
event ID or outbox.

#### Rules alerts

Keep `RulesAlertSubscriber` authoritative. After a valid subscribe operation,
record an attributed conversion with the email and existing source string.
Organic signups create no extra `MarketingConversion` row, and repeat
attributed submissions may create distinct history rows.

#### Gap assessment

Keep `GapAssessmentLead` as the current assessment state. Record separate
attributed lead, completion, and expert-request milestone conversions with the
email, company, and source context. Repeat submissions are not deduplicated by
the conversion ledger.

#### Resource downloads

The existing Next resource-download route reads and validates the HttpOnly
cookie, then forwards only a canonical reference to Django. After the existing
`ResourceDownload` row is created, Django records an attributed
`resource_downloaded` row with email and the resource slug as context. V1 does
not add cross-row atomicity or a client event ID.

### 7.4 Review through the stored rows

FedRampGPT adds no conversion-review management command. Manual verification
uses ordinary trusted database/admin access to filter `MarketingConversion` by
`outreach_ref`, then compares that value with OpenOutreach's read-only
`resolve_drip_reference` output.

## 8. Identity and campaign semantics

The reference proves which generated delivery originated the link. It does not
prove who completed the conversion.

During manual review:

- a successfully sent delivery plus an exact recipient-email match can be
  described as a direct match;
- a missing or different submitted email is influenced/forwarded attribution;
- an unknown token stays unresolved; and
- a token whose delivery never sent is not reported as a delivered campaign
  conversion.

These are review labels only. Manual review does not mutate a Lead, Deal, Task,
lane, stop state, or sales stage.

## 9. Failure and safety behavior

### OpenOutreach

- Invalid link configuration fails campaign publication.
- Link generation failure leaves no delivery or Task.
- Retry/rematerialization reuses the existing delivery body and reference.
- A pre-send body/ledger mismatch fails before Gmail submission.
- An unsent link remains distinguishable through delivery status.
- Current Gmail follow-up and LinkedIn handlers remain untouched.

### FedRampGPT

- A missing reference leaves every existing form workflow organic; request
  demo still receives its durable conversion row with blank attribution.
- A malformed or ambiguous reserved query value is scrubbed by redirect and
  clears any earlier outreach session cookie so attribution fails closed.
- A malformed cookie or trusted forwarded value is treated as blank, never as
  an attributed reference.
- A duplicate form retry may create another conversion row; V1 deliberately
  has no client event ID or deduplication layer.
- The conversion is durable even if a later notification attempt fails.
- No public request waits for or calls OpenOutreach.
- A page visit or scanner request creates no conversion row.

## 10. Expected implementation surface

### OpenOutreach

Expected focused changes:

- `drip/models.py` and one migration;
- `drip/manifest.py`;
- new `drip/link_attribution.py`;
- `drip/services/reconciliation.py`;
- `drip/tasks/gmail.py` for the pre-send invariant/evidence only;
- one read-only management command;
- focused tests; and
- `AGENTS.md`/`ARCHITECTURE.md` plus the drip manifest documentation.

No shared Gmail-client rewrite and no new worker or Task type are expected.

### FedRampGPT

Expected focused changes:

- one shared reference helper and the central Next `proxy.ts` capture/scrub
  redirect;
- `backend/marketing/models.py`, one migration, and one recording helper;
- the existing Django request-demo, rules-alert, gap-assessment, and
  resource-download handlers;
- the existing Next resource-download proxy bridge; and
- focused proxy and Django tests.

## 11. Implementation phases

### Phase 0: Isolate clean branches

1. Keep this plan separate from the current LinkedIn-media implementation.
2. Create a clean OpenOutreach attribution branch from the intended main base.
3. Create a clean FedRampGPT attribution branch/worktree rather than using its
   current dirty checkout.
4. Record both base commits before code changes.

### Phase 1: OpenOutreach reference generation

1. Add model/migration and URL/reference helper.
2. Add schema-v3 structured Gmail link validation.
3. Generate and freeze the URL during delivery materialization.
4. Add pre-send consistency validation and Message evidence.
5. Add the read-only reference lookup command.
6. Run focused and regression tests.

### Phase 2: FedRampGPT durable capture

1. Add the shared token helper and central Next proxy capture/scrub redirect.
2. Store a valid reference in the host-only HttpOnly session cookie and clear
   it for malformed or ambiguous reserved landings.
3. Add the simple `MarketingConversion` model, migration, and recording
   service.
4. Wire request demo, rules alerts, gap assessment, and resource downloads,
   using the trusted Next-to-Django bridge only for the resource route.
5. Run focused and regression tests.

### Phase 3: Cross-repository QA

1. Generate a tracked URL through OpenOutreach without sending.
2. Visit it on local/staging FedRampGPT and verify the redirect, clean URL, and
   HttpOnly session cookie.
3. Submit each conversion surface with controlled data.
4. Run OpenOutreach's lookup command and query the FedRampGPT conversion rows
   to prove the exact reference joins across repositories.
5. Repeat controlled submissions and verify the lean ledger records each
   accepted call rather than claiming idempotency.
6. Test organic, malformed, ambiguous, unknown, unsent, and forwarded-email
   cases.

### Rollout gate: Controlled live QA

1. Publish a private one-Lead drip version with one tracked Gmail link.
2. Send it through the real drip Gmail handler to a controlled inbox.
3. Verify the received plain-text URL is exact and compact.
4. Complete one controlled FedRampGPT conversion.
5. Resolve the token in OpenOutreach and find it in the FedRampGPT conversion
   table.
6. Verify no drip state changed because of the visit or conversion.

The V1 implementation ends after Phase 3. This controlled verification is a
production-rollout gate, not another implementation phase.

## 12. Automated test matrix

### OpenOutreach

- reference format, entropy boundary, and invalid-value rejection;
- URL host/scheme/ref validation and query/fragment preservation;
- model uniqueness and Gmail-only immutability invariants;
- manifest link/placeholder validation;
- atomic delivery/link/Task creation and rollback;
- retry and Task-rematerialization reference stability;
- pre-send rejection of missing, duplicated, or foreign references;
- successful Message evidence matching the frozen body and link row;
- persisted older no-link campaign compatibility; and
- no current Gmail-follow-up or LinkedIn regression.

### FedRampGPT browser

- one valid `oo_*` reference produces an apex-host redirect, a clean URL, and
  a host-only HttpOnly SameSite=Lax session cookie;
- malformed or multiple reserved references are removed and clear an earlier
  cookie;
- tracked `www` landings move to the apex before capture;
- other query values, fragments, and ordinary non-`oo_*` referrals survive;
- requests without a reserved reference pass through unchanged; and
- the resource-download bridge forwards only a canonical cookie value.

### FedRampGPT backend

- exact canonical cookie/forwarded-reference validation;
- request-demo rows for both organic and attributed accepted submissions;
- honeypot submissions create no conversion;
- rules alerts create only attributed conversion rows;
- gap-assessment lead, complete, and expert phases create separate attributed
  kinds;
- resource download accepts the valid reference forwarded by the trusted Next
  bridge; and
- the simple row contains only kind, reference, email, company, context, and
  occurrence time.

### Manual contract QA

- identical valid/invalid token fixtures pass in both repositories;
- a real reference resolves one OpenOutreach link and matches expected
  FedRampGPT conversion rows through trusted database/admin access;
- matching submitted email is distinguishable from a forwarded/mismatched one;
- unknown and unsent references are not misrepresented; and
- no conversion changes outreach execution state.

## 13. Rollout and rollback

1. Deploy FedRampGPT capture and durable conversion support first.
2. Verify organic forms still work with blank attribution.
3. Publish a new OpenOutreach schema-v3 campaign version only after FedRampGPT
   accepts the reference.
4. Run one controlled live delivery and conversion.
5. Expand tracked-link authoring only after the manual join succeeds.

Existing manifests and deliveries are never rewritten. Both database
migrations are additive. If FedRampGPT capture is disabled, forms continue
organically. If OpenOutreach link materialization fails, no Gmail Task is
created.

The OpenOutreach attribution and LinkedIn-media branches are now integrated.
Their existing `0003` migration identities remain intact and an empty `0004`
merge migration joins the graph. The shared schema-v3 manifest and
reconciliation path support Gmail tracked links and LinkedIn media as
independent channel features.

## 14. Completion checklist

- [x] Clean base commits recorded for both repositories.
- [x] OpenOutreach link model, validator, manifest schema, materialization, and
      pre-send invariant complete.
- [x] OpenOutreach read-only reference lookup works.
- [x] FedRampGPT central proxy captures one valid reserved reference in a
      host-only HttpOnly session cookie and redirects to a scrubbed URL.
- [x] FedRampGPT generic conversion ledger and helper complete.
- [x] Request demo, rules alerts, gap assessment, and resource download store
      the optional reference.
- [x] Focused and relevant regression tests pass in both repositories.
- [ ] Controlled no-send cross-repository QA resolves the same exact token
      through OpenOutreach's command and a direct FedRampGPT conversion-row
      query.
- [x] No current-outbound, Gmail-threading, stop-policy, or LinkedIn regression
      in the focused regression suite.

Production rollout follows the separate controlled live-email QA in Section
13; it is not additional V1 implementation scope.
