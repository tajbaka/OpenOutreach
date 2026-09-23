# PDF field contract

The renderer supports only these exact two-page letter-size files in OpenOutreach's `docs/outbound-collateral/`. Its source-hash constants are the contract: a changed template must be inspected and deliberately remapped, never accepted merely by updating a hash.

| template_key | Source | Current listing | Background bullets |
| --- | --- | --- | --- |
| initial-20x | initial-20x.pdf | Initial Implementation, 20x | 3 |
| rev5-authorized | rev5-auth-20x.pdf | Ongoing Certification, FedRAMP Certified, Rev5, Class C | 2 |
| rev5-ready | rev5-ready-to-20x.pdf | Legacy FedRAMP Ready, Rev5, Class C | 3 |

Background bullet order: company/market; published baseline or relevant offering context; exact offering/status and path caveat. Authorized has two bullets: company/market, then exact offering/class. Aim for 90-125 characters per bullet and no more than three rendered lines. Layout is the actual limit, not a character-count guarantee. Use ASCII hyphens in newly written copy.

## Brief fields

One JSON object, inside the chosen company's `research/` folder:

- `schema_version`: 1.
- `company`: concise verified display name and exact company-directory name.
- `slug`: safe lowercase hyphenated output stem, unique per revision.
- `template_key`: one of the three table entries.
- `offering`: exact offering name or a verified short form; full name belongs in the sources.
- `logo`: company-relative path to the reviewed high-resolution PNG, e.g. `assets/logo.png`.
- `logo_background`: optional `#ffffff` default or a reviewed contrasting hex color.
- `logo_source_url`: original official asset URL; for an inline SVG, its official page URL.
- `marketplace`: object with `package_id`, `url`, `verified_at` (timezone-aware ISO timestamp), `type` (`20x`/`Rev5`), exact `phase` and `status`, `path`, `class` (`B`, `C`, `Unknown`), optional `target_class`.
- `paragraphs`: array of two or three original, source-grounded strings.
- `sources`: array of `{url, finding}` objects; include Marketplace, company-fact pages and the logo source/discovery page. Findings are concise paraphrases, not copied articles.
- `paragraph_sources`: parallel array of source-URL arrays, one per paragraph.
- `next_path`: Ready only: `20x` with current evidence, or `Unconfirmed`. If unconfirmed, that word must appear in the background text.
- `source_conflicts`: empty array only after resolving material conflicts; do not suppress inconvenient evidence.
- `review_only`: true.
- `limits`: nonempty array of source gaps, unknowns and applicable inherited-template caveats.
- `lead_context`: optional private selection receipt, exact sender, lead/campaign IDs, evidence time and why selected. It never renders in the PDF.

Unknown Initial Implementation class is allowed only as a review comparison with a recorded limitation. Explicit Class D/High targets are held. Fresh source evidence must be less than seven days old; refresh it on a later run.

The script only performs local PDF filling and file validation. It does not browse, choose leads, verify that a citation semantically supports a claim, or approve sending. Those are agent/user review steps.

## Preserved-copy caveats

Carry applicable caveats into each review, without silently rewriting the templates:

- Initial Implementation already has a Marketplace listing; the statement that listing exists only after assessment is not correct for that phase.
- Four-week readiness, 2-3 month versus 12-18 month timelines, cost ranges and first-mover revenue outcomes are not substantiated guarantees.
- Universal statements about agency purchasing, mandatory migration and removal of documentation need qualification.
- Rev5 deadline/penalty summaries need ruleset-specific obtain/maintain/grace dates, not one universal date. Check the official Rev5 deadlines page before any send-ready claim.
- Ready does not establish a selected 20x path; Moderate equivalency is not certification.
- The Rev5 Authorized template is primarily about CR26 updates, not proof that the company is migrating to 20x.

Current official starting points:

- https://www.fedramp.gov/2026/providers/updating/deadlines/rev5/
- https://www.fedramp.gov/2026/timeline/
- https://www.fedramp.gov/2026/providers/start/path/

Research and visual approval are distinct from approval of the inherited general copy.
