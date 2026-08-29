# FedRAMP 20x Copy Reference

Last verified against official FedRAMP sources: 2026-08-29.

Use this as a language reference, not a substitute for checking the exact rule,
class, path, audience, and effective date. For current requirements, the
structured [`FedRAMP/rules`](https://github.com/FedRAMP/rules) data is the
machine-readable source of truth and the
[Consolidated Rules site](https://www.fedramp.gov/2026/rules/) is the primary
human-readable guide. Check the
[official changelog](https://www.fedramp.gov/2026/changelog/) before publishing
terminology-sensitive copy.

Browse official sources before putting a current status, deadline,
mandatory/optional claim, class/path detail, or direct quotation into
prospect-facing copy. Do not use 2025 pilot RFCs as current requirements.

## What 20x Is

FedRAMP's current definition distinguishes Rev5 as based primarily on documented
plans and 20x as based primarily on measured outcomes. FedRAMP describes 20x as
a different cloud-assurance model focused on security decisions and outcomes
rather than treating compliance as a point-in-time document exercise.

The core writing idea is:

> Security decisions are documented, their implementation is verified, their
> effectiveness is validated, and the resulting certification data stays
> current enough to support ongoing decisions.

Do not simplify that to `FedRAMP is now automated` or `documentation is gone`.

Use `FedRAMP 20x Certification` for the type or process. `FedRAMP Validated
(20x)` is a current Marketplace designation, not a generic replacement for the
word Certification. A provider's FedRAMP Certification remains distinct from an
agency's ATO.

Current 20x uses the Program Certification path and does not require an agency
sponsor. Treat path rules as time-sensitive and verify them before using that
point as a sales claim.

## Concepts to Use Correctly

### Key Security Indicators

20x expresses FedRAMP Practices as Key Security Indicators, supplemented by
other FedRAMP rules. KSIs summarize important security outcomes and relate to
NIST controls; do not say they eliminate or replace NIST controls.

Providers explain the measures used to demonstrate each applicable KSI, verify
that the measures are appropriate, and validate that they are accurately
produced and working as intended.

Copy-safe phrasing:

- `KSI measures and supporting evidence`
- `verification that the implementation is appropriate`
- `validation that the measure is working as intended`
- `current KSI results and historical metrics`

Avoid `KSI checklist`, `automatic pass`, or claims that one screenshot proves a
KSI.

### Verification and Validation

- Verification uses objective evidence to support that FedRAMP Practices are
  fulfilled.
- Validation uses objective evidence to support that implemented capabilities
  and data suit their intended use and support the expected outcomes.
- Independent assessment supplies an outside review of implementation and
  effectiveness. The current term is `FedRAMP Recognized independent assessment
  service`; `formerly 3PAO` may be added once for a market audience that knows
  the historical term.

Plain-language copy can say `show what is implemented and whether it is
working`. Do not use verification and validation as interchangeable labels.

### Persistent Validation

20x expects security state and measures to be maintained and reviewed as part
of normal engineering and security work. `Persistent` does not necessarily mean
uninterrupted, continuous, or real time. Cycles may have gaps when they are
intentional, understood, documented, and the status is known.

Automation is encouraged where appropriate, while non-machine evidence and
human decisions still exist. Deterministic telemetry must come from an
authoritative, reproducible source; generative output is not factual system-state
telemetry by itself.

Good copy focuses on keeping evidence and validation current, reducing audit
scrambles, and making drift or failures visible. Do not promise real-time
coverage for every measure or say AI generates proof.

### Security Decision Record

Under the Consolidated Rules for 2026, the Security Decision Record replaces
the traditional provider System Security Plan for applicable 20x workflows. It
is a persistently maintained, verified, and validated record of the provider's
security decisions. Required portions are available in human-readable and JSON
forms and cover applicable rules, verification, validation, independent
assessment information, clarifications, artifacts, KSI summaries, and metrics.

Say `provider Security Decision Record` when the distinction matters. Do not say
`20x eliminated SSPs`; agencies still document their own use, configuration,
responsibilities, and authorization decisions in an agency-system SSP.

### Certification Data and Trust Centers

A 20x package is maintained certification data rather than a static folder
reviewed once. Current guidance describes a Certification Package Overview,
Security Decision Record, KSI summaries and measures, Secure Configuration
Guide, independent assessment information where required, and ongoing
certification data.

The package may be delivered through a FedRAMP-compatible trust center,
documentation portal, files, APIs, or a combination. A compatible trust center
is a controlled, definitive source of certification data governed by the
data-sharing rules; a normal marketing trust page is not automatically
compatible. `Machine-readable` does not always mean JSON, although official JSON
schemas apply to specified records and reports.

Copy-safe phrasing:

- `current human- and machine-readable certification data`
- `a maintained record rather than a one-time document handoff`
- `traceable evidence and assessor review through controlled sharing workflows`

Do not claim that a trust center or JSON export alone makes a package compliant.
Do not describe CPO, SDR, trust centers, or machine-readable data as exclusive to
20x; the Consolidated Rules also introduce several of them into Rev5.

### Vulnerability Detection and Response

The current rules treat vulnerability work as an ongoing lifecycle: detection,
evaluation, mitigation or remediation, validation, and reporting. Requirements
and cadences vary by class and resource type. Use this concept only when it
matches the recipient's problem; do not reduce it to `monthly scans` or claim it
universally eliminates POA&Ms.

## Current Classes and Status

The official 20x overview currently describes Classes A, B, and C as finalized
and Class D as future. Classes describe differences in certification-data and
assurance depth; they are not overall security scores. Avoid simplistic
equations with historical impact levels.

Class availability, pipeline dates, eligibility, assessment depth, automation
expectations, and exact requirements are time-sensitive. Verify them live before
using them in copy.

## Useful Copy Angles

Choose one per short message:

- Turn live cloud/security implementation into reviewable KSI evidence.
- Show both what is implemented and whether the measure is working.
- Keep the Security Decision Record and certification data current.
- Give assessors clearer traceability from a measure to evidence and history.
- Surface validation failures and route remediation before an assessment
  scramble.
- Keep human-readable and machine-readable records aligned where FedRAMP schemas
  apply.

These are FedRAMP concepts, not automatically Boundera capabilities. Before
connecting one to the product, inspect the current FedRampGPT product
documentation, implementation, and tests for the exact named behavior. Existing
outbound copy is not product evidence.

## Claims to Avoid

- `20x is just a faster or lighter Rev5.`
- `FedRAMP in 30 days.` FedRAMP's processing target begins only after a complete
  package is submitted; it is not an end-to-end certification promise.
- `20x eliminates assessors, agencies, SSPs everywhere, or documentation.`
- `KSIs replace NIST controls with a simple checklist.`
- `Everything must be fully automated, continuous, or real time.`
- `A trust center or JSON export is the final Certification.`
- `FedRAMP Certification is the same as an agency ATO.`
- `Class A/B/C means the product itself is more or less secure.`
- `FedRAMP approved Boundera` without documented official proof.

## Official Sources

- [FedRAMP definitions](https://www.fedramp.gov/2026/definitions/)
- [FedRAMP 20x overview](https://www.fedramp.gov/20x/)
- [Choosing a Certification path](https://www.fedramp.gov/2026/providers/start/path/)
- [Security Decision Record rules](https://www.fedramp.gov/2026/providers/20x/rules/security-decision-record/)
- [Independent verification and validation rules](https://www.fedramp.gov/2026/providers/20x/rules/independent-verification-and-validation/)
- [Using 20x Certification packages](https://www.fedramp.gov/2026/agencies/use/packages/20x/)
- [Certification data sharing rules](https://www.fedramp.gov/2026/reference/20x/c/certification-data-sharing/)
- [Vulnerability detection and response rules](https://www.fedramp.gov/2026/providers/20x/rules/vulnerability-detection-and-response/)
- [Official FedRAMP schemas](https://www.fedramp.gov/schemas/)
- [Marketplace designation guidance](https://www.fedramp.gov/brand/fedramp-marketplace/marketplace-designations/)
