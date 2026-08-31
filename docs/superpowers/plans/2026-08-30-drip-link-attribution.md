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
- FedRampGPT recognizes a valid OpenOutreach reference, keeps it for the
  current browser-tab session, and removes it from the visible URL.
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
capturing it on the agreed conversion surfaces, and resolving it with the two
read-only commands. Work outside that path is not part of this plan.

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

- a browser UTM/ref tracker;
- the production Django request-demo endpoint;
- durable rules-alert subscribers;
- durable gap-assessment leads;
- durable resource downloads.

The missing shared behavior is small: validate and retain `oo_*`, attach it to
intentional submissions, and record one normalized conversion event.

The existing generic browser behavior maps `ref` into traffic-source fields.
V1 must special-case valid `oo_*` references so they are not reported as a
human-readable source name. Non-`oo_*` referral behavior stays unchanged.

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

### 7.1 Add a small browser attribution helper

Add a tested helper under `website/lib/` and call it from the current ref/UTM
tracking surface. It must:

- recognize only syntactically valid `oo_*` values;
- treat every `ref` beginning with `oo_` as reserved: if it is malformed,
  remove it, do not store it, and do not pass it to generic referral handling;
- store the most recent valid value in `sessionStorage` for the current tab;
- clear that stored value if a later reserved `oo_` landing is malformed or
  ambiguous, preventing stale attribution from being attached to a conversion;
- remove only that `ref` from the visible URL with `history.replaceState`;
- preserve the path, other query parameters, and fragment;
- return the stored value to the in-scope marketing forms;
- ignore and scrub malformed values without breaking the page;
- keep existing generic referral behavior only for values that do not begin
  with `oo_`; and
- never register or emit the exact value as an analytics property.

This is a targeted change, not an analytics-provider rewrite. The current
page-view component captures its query from render state, so
`history.replaceState` alone cannot guarantee the first page-view path is
clean. Make one narrow sanitization change to the page-view path builder so any
reserved `oo_*` `ref` is omitted regardless of effect order. Do not change
PostHog initialization, consent behavior, destinations, or unrelated events.

### 7.2 Add one generic conversion ledger

Add one append-only model in `backend/marketing/models.py`:

```text
MarketingConversion
    event_id           client idempotency key
    kind               constrained conversion type
    outreach_ref       validated oo_* value or blank
    email              normalized email or blank
    name               bounded name or blank
    company            bounded company or blank
    source_object_type allowlisted label or blank
    source_object_id   existing record identifier or blank
    details            small per-kind allowlisted JSON object
    occurred_at
```

Use a unique constraint on `(kind, event_id)`. The `details` field never stores
an arbitrary request body; each conversion kind supplies an explicit allowlist.
Blank attribution is valid so organic submissions remain unchanged.

Add one shared recording service that:

- validates or rejects a nonblank malformed outreach reference;
- normalizes common fields;
- enforces per-kind detail allowlists;
- returns the existing row on a retry with the same kind/event ID; and
- rejects an idempotency conflict when the same kind/event ID is reused with a
  different reference, email, domain object, or allowlisted details; and
- never calls OpenOutreach.

Initial kinds:

- `request_demo_submitted`
- `rules_alerts_subscribed`
- `gap_assessment_lead`
- `gap_assessment_completed`
- `gap_assessment_expert_requested`
- `resource_downloaded`

### 7.3 Wire only meaningful conversion submissions

Each relevant browser form sends:

- one event ID generated for the submission attempt and reused across retries;
- the current valid outreach reference or blank; and
- its existing validated form data.

Ordinary CTA clicks and page views do not call the conversion service.

#### Request demo

Change the actual marketing form and canonical Django `/api/request-demo/`
handler. Record the allowlisted name, email, company, and existing message/
interest fields before or independently of the internal notification attempt.
The conversion row is the durable request-demo record. Team email remains an
at-least-once side effect: an exact retry reuses the row and attempts the alert
again, accepting a possible duplicate alert after a lost successful response
instead of adding an outbox to V1.

Do not redesign the similarly named Next route in V1. Tests must exercise the
route production nginx actually sends to Django.

#### Rules alerts

Keep `RulesAlertSubscriber` authoritative. After a valid subscribe operation,
record a conversion referencing the subscriber row. A later submission with a
new event ID is distinct history and does not rewrite an earlier conversion.

#### Gap assessment

Keep `GapAssessmentLead` as the current assessment state. Record separate lead,
completion, and expert-request milestone conversions. `(kind, event_id)` makes
retries idempotent while allowing multiple kinds during one assessment.

#### Resource downloads

Pass event ID and outreach reference through the existing Next download proxy
to Django. Record `resource_downloaded` in the same database transaction as the
existing `ResourceDownload` row.

### 7.4 Add one narrow review command

Add a read-only FedRampGPT command:

```text
python manage.py review_outreach_conversions --reference oo_<token>
```

It prints the matched conversion kind/time, submitted email, and referenced
domain object. It performs no export, synchronization, or mutation.

V1 verification consists of running this command and OpenOutreach's
`resolve_drip_reference` with the same token.

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

- Missing reference continues as a normal organic conversion.
- A malformed browser query value is ignored and scrubbed/not retained, and
  clears any earlier tab-scoped outreach reference so attribution fails closed.
- A nonblank malformed value sent directly to a backend endpoint is rejected.
- A duplicate form retry returns/reuses the same conversion event.
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

- one small browser helper plus the existing UTM/ref tracker integration;
- one narrow page-view path sanitization change, with no analytics
  initialization, consent, destination, or event redesign;
- `backend/marketing/models.py`, one migration, and one recording helper;
- the existing request-demo, rules-alert, gap-assessment, and resource-download
  form/handler paths;
- the existing resource proxy;
- one read-only management command; and
- focused tests/documentation.

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

1. Add the browser reference helper and special-case `oo_*` in the existing
   tracker without changing analytics initialization or consent.
2. Sanitize the existing page-view path so it cannot include a reserved
   `oo_*` reference.
3. Add `MarketingConversion`, migration, and recording service.
4. Wire request demo, rules alerts, gap assessment, and resource downloads.
5. Add the read-only conversion review command.
6. Run focused and regression tests.

### Phase 3: Cross-repository QA

1. Generate a tracked URL through OpenOutreach without sending.
2. Visit it on local/staging FedRampGPT and verify capture plus URL scrubbing.
3. Submit each conversion surface with controlled data.
4. Use both read-only commands to prove the exact reference resolves on each
   side.
5. Repeat submissions with the same event IDs and prove idempotency.
6. Test organic, malformed, unknown, unsent, and forwarded-email cases.

### Rollout gate: Controlled live QA

1. Publish a private one-Lead drip version with one tracked Gmail link.
2. Send it through the real drip Gmail handler to a controlled inbox.
3. Verify the received plain-text URL is exact and compact.
4. Complete one controlled FedRampGPT conversion.
5. Resolve the same token in both repositories.
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

- valid `oo_*` capture and current-tab persistence;
- malformed `oo_*` removal without storage, analytics emission, or generic
  referral fallback;
- visible URL scrubbing with other query values/fragments preserved;
- exact reference is not mapped to `utm_source`;
- exact reference is not emitted to existing analytics helpers;
- non-`oo_*` ref behavior remains unchanged;
- initial and later page-view paths omit every reserved `oo_*` reference;
- CTA forms receive valid reference or blank.

### FedRampGPT backend

- valid or blank attribution accepted for every conversion kind;
- malformed nonblank backend attribution rejected consistently;
- `(kind, event_id)` retry idempotency;
- same kind/event ID with different attribution or payload rejected as an
  idempotency conflict;
- per-kind details allowlist enforcement;
- request-demo persistence independent of notification outcome;
- rules/gap/resource conversion references point to existing domain rows;
- resource row and conversion commit atomically;
- review command is read-only and reference-scoped.

### Manual contract QA

- identical valid/invalid token fixtures pass in both repositories;
- a real reference resolves one OpenOutreach link and expected FedRampGPT
  conversions;
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

This attribution branch remains independent from
`codex/linkedin-message-media`. If both branches are later selected for the
same target, their two `0003` migrations and manifest/reconciliation edits must
be integrated and retested at that merge boundary. That integration is not a
V1 attribution deliverable.

## 14. Completion checklist

- [x] Clean base commits recorded for both repositories.
- [x] OpenOutreach link model, validator, manifest schema, materialization, and
      pre-send invariant complete.
- [x] OpenOutreach read-only reference lookup works.
- [x] FedRampGPT browser capture/scrub and narrow page-view sanitization work
      without analytics initialization, consent, destination, or event changes.
- [x] FedRampGPT generic conversion ledger and helper complete.
- [x] Request demo, rules alerts, gap assessment, and resource download store
      the optional reference.
- [x] FedRampGPT read-only conversion lookup works.
- [x] Focused and relevant regression tests pass in both repositories.
- [x] Controlled no-send cross-repository QA resolves the same exact token
      manually in both commands.
- [x] No current-outbound, Gmail-threading, stop-policy, or LinkedIn regression
      in the focused regression suite.

Production rollout follows the separate controlled live-email QA in Section
13; it is not additional V1 implementation scope.
