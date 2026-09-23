# Outbound collateral

The three user-designated final reference PDFs, copied unchanged from Desktop on September 21, 2026:

| Listing cohort | Document |
| --- | --- |
| 20x Initial Implementation | [initial-20x.pdf](initial-20x.pdf) |
| Rev5 Authorized | [rev5-auth-20x.pdf](rev5-auth-20x.pdf) |
| Rev5 Ready, considering 20x | [rev5-ready-to-20x.pdf](rev5-ready-to-20x.pdf) |

These are reference assets only. Adding them does not attach them to campaigns or send them to leads. The original Desktop files are unchanged.

## Personalized output location

Keep these source templates here. Save generated, company-specific PDFs and videos together under the repo-root `custom_media/<Company Name>/` directory. Reuse the same company directory for both media types; do not create separate top-level PDF and video output trees.

Initial examples use `custom_media/OpenClinica/`, `custom_media/Qualys/`, and `custom_media/Bonterra/`. Skill validation adds MaintainX, BMC Helix and GovDash. All company-specific generation and research support belongs in the same company directory: logos and downloaded assets in `assets/`, sources and research notes in `research/`, rendered reviews in `previews/`, and scratch files or intermediate exports in `working/`. Keep final PDFs and videos together at the company-directory root. The entire `custom_media/` tree is Git-ignored, including all support files and future videos.

Shared batch-only generation tools, local dependencies and cross-company review evidence also stay inside `custom_media/`, under `.generation/`. The existing three-company prototype and review packet are in `custom_media/.generation/pdf-personalization-2026-09-21/`; its company-specific scratch assets have been moved into the respective company directories. Do not leave new personalization research or generation artifacts in a separate `artifacts/` or `output/` tree.

The reusable skill is [boundera-personalized-collateral](../../skills/boundera-personalized-collateral/SKILL.md). It reads these templates without modifying them and writes private media/support under `custom_media/`. Its field contract documents allowed replacements, required research and inherited general-copy caveats. Run its input check before generation and visually inspect both pages afterward; passing the renderer is not factual or sending approval. A company directory is an output location, not approval to generate, attach or send anything.
