---
name: boundera-personalized-collateral
description: Research a Boundera prospect's exact FedRAMP offering and personalize the existing Initial Implementation, Rev5 Authorized, or Rev5 Ready PDF with its official logo, company name and sourced background. Use for company-specific collateral and review PDFs, not general copy rewriting, ICP Sheet edits, campaign activation, sending, or video production.
---

# Company-personalized FedRAMP collateral

Create review copies from the three existing PDFs, preserving their design and general copy. This is an agent-guided research workflow plus a deterministic local renderer, not an unattended bulk-sending tool.

## Inputs and storage

Resolve the OpenOutreach repo from the current workspace or its saved project. Canonical source PDFs are under `docs/outbound-collateral/`; never use a previously personalized PDF as a template. Read that directory's README and this skill's [field contract](references/field-contract.md) before generation.

All private output and support goes under `custom_media/<Company Name>/`: PDFs and future videos at the company root, logos/downloads in `assets/`, source notes and briefs/manifests in `research/`, rendered page images in `previews/`, intermediate files in `working/`. Reuse the exact existing company directory. Shared batch-only work goes in `custom_media/.generation/`. Verify `/custom_media/` is Git-ignored; do not put customer research or secrets in the skill itself.

## Research and qualification

1. Resolve the requested lead/company and exact offering. If selecting from campaign engagement, use read-only, exact-sender evidence; distinguish accepted-only, a polite response and an actual collateral request. Read relevant replies before selecting. Do not change CRM, classifications, campaigns, Tasks, controls or delivery schedules. Never infer a buying role or consent from an acceptance. The existing repo analysis helper may be inspected for attribution logic; do not embed historical campaign IDs, IPs or customer records in this skill.
2. Check the current official Marketplace product page and record its package ID, phase, status, type, path, class and verification time. Use current official facts over old CSV/ICP labels. Select one matching template. Hold Class D/High offerings or targets, unclear offering identity, material conflicting sources, or a mismatched current stage. A company may have several separate offerings. Review its site for contradictory target-class/path claims too.
3. Ready is not Certified, Moderate equivalency is not FedRAMP certification, and Ready does not establish a chosen 20x path. For an unconfirmed next path, say so in the company background and mark the document discussion-only. For Initial Implementation with an unknown class, retain that limitation; the fixed Class C template is an illustrative planning comparison, not proof of the company's target. Do not imply an authorized Rev5 provider must migrate to 20x rather than update under Rev5.
4. Use the official company site for its description, customers/market and published security baseline. Browse the exact sources, then write short original paraphrases with a source mapping for every background paragraph. Do not invent revenue, agency deals, internal deficiencies, assessment timelines or audited boundary coverage. Read the repo's shared FedRAMP language references when drafting regulatory wording; verify time-sensitive claims against applicable official rules.
5. Retrieve the actual logo from the company site or an asset host linked by that site. Save its URL, discovery page and original asset; inspect identity, aspect ratio and light/dark contrast. A site's embedded SVG may be extracted unchanged as an asset. Do not redraw logos or use another customer's logo. Convert SVG to a high-resolution PNG with the workspace's bundled Sharp renderer; preserve colors and aspect ratio. The renderer accepts a background color for a white logo. Save downloads and conversions in that company's assets directory.

## Fill and verify

Prepare one company-local JSON brief following the field contract. The approved editable areas are the two top logo placeholders, company-name placeholders, listed-offering placeholder and company-background bullets. No redesign, new marketing claims or silent edits to the general template copy.

Use the PDF skill for its authoring marker, rendering and delivery requirements. The user's `custom_media/` convention overrides its generic output paths. Use the repo's `.venv/bin/python`. Optional isolated dependencies are in `requirements/collateral.txt`; install with `--target custom_media/.generation/deps` if needed, not into the application's runtime. Set `PYTHONPATH` to that isolated directory. Poppler (`pdftoppm`) and Arial regular/bold are required; alternate fonts require explicit CLI paths and visual review.

Run from the OpenOutreach repo root, substituting the resolved absolute skill path:

```sh
PYTHONPATH=custom_media/.generation/deps .venv/bin/python /absolute/skill/scripts/personalize.py --repo /absolute/OpenOutreach --brief "/absolute/OpenOutreach/custom_media/Company/research/brief.json" --check-only
PYTHONPATH=custom_media/.generation/deps .venv/bin/python /absolute/skill/scripts/personalize.py --repo /absolute/OpenOutreach --brief "/absolute/OpenOutreach/custom_media/Company/research/brief.json"
```

For SVG logos, use the bundled Node and Sharp paths returned by workspace dependency discovery with `node /absolute/skill/scripts/render_logo.cjs /absolute/company/assets/logo.svg /absolute/company/assets/logo.png /absolute/sharp-module`. This converts the original asset without changing its colors or proportions and refuses to overwrite an existing PNG.

After changing the renderer, run `PYTHONPATH=custom_media/.generation/deps .venv/bin/python /absolute/skill/scripts/test_personalize.py` from the repo root. Tests use synthetic fixtures under ignored `custom_media/.generation/`, never live leads or outreach.

The first command validates inputs without writing. The second verifies exact source hashes, field fit, page count, placeholder removal and image changes outside editable areas, then publishes review files without overwriting an earlier revision. It preserves scratch files on failure. If text overflows, shorten only sourced company-background wording or use a verified shorter company/offering display name; never silently shrink all type, change the template, clip text or weaken the verification.

Inspect every newly rendered page, not just extracted text: logos, all replacements, bullet spacing, footer, CTA and unedited content. Record visual results and the exact PDF hash in a separate company-local review note. The script's automated manifest deliberately remains `review_only_visual_review_pending`; automated checks cannot approve visual quality or factual claims. Show the PDFs for user review and summarize any qualification or template-copy limitations.

## Important review boundary

The current user-provided templates include unsubstantiated general cost/timing promises and broad rule/deadline statements. Read the field contract's caveats and carry them forward; successful personalization does not validate them. Do not label a PDF send-ready solely because rendering passes. Creation/testing never authorizes sending, campaign attachment, publication or bulk generation beyond the requested count.
