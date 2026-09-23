"""Offline guard regressions. Run from OpenOutreach using its Python venv."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import fitz
from PIL import Image

import personalize as p


class PersonalizationTests(unittest.TestCase):
    def setUp(self):
        repo = Path.cwd()
        staging = repo / "custom_media/.generation"
        staging.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="collateral-test-", dir=staging)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        templates = self.root / "docs/outbound-collateral"
        templates.mkdir(parents=True)
        for spec in p.TEMPLATES.values():
            shutil.copyfile(repo / "docs/outbound-collateral" / spec["file"], templates / spec["file"])
        self.folder = self.root / "custom_media/Fixture Co"
        (self.folder / "research").mkdir(parents=True)
        (self.folder / "assets").mkdir()
        Image.new("RGB", (600, 100), "blue").save(self.folder / "assets/logo.png")
        self.brief = self.folder / "research/brief.json"
        url = "https://www.fedramp.gov/marketplace/products/FIXTURE123/"
        self.case = {
            "schema_version": 1, "company": "Fixture Co", "slug": "fixture",
            "template_key": "rev5-ready", "offering": "Fixture Offering",
            "logo": "assets/logo.png", "logo_source_url": "https://example.com/logo.svg",
            "marketplace": {
                "package_id": "FIXTURE123", "url": url,
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "type": "Rev5", "phase": "Legacy FedRAMP Ready", "status": "Legacy FedRAMP Ready",
                "path": "Agency", "class": "C",
            },
            "paragraphs": ["A synthetic company description.", "A synthetic security baseline.", "The next path is unconfirmed."],
            "sources": [{"url": url, "finding": "Synthetic test fixture, not real research."},
                        {"url": "https://example.com/logo.svg", "finding": "Synthetic test asset."}],
            "paragraph_sources": [[url], [url], [url]], "next_path": "Unconfirmed",
            "source_conflicts": [], "review_only": True, "limits": ["Synthetic test only."],
        }
        self.brief.write_text(json.dumps(self.case))

    def validate(self):
        return p.validate(self.case, self.root, self.brief)

    def test_all_three_supported_stages(self):
        for key, spec in p.TEMPLATES.items():
            with self.subTest(key=key):
                case = deepcopy(self.case)
                case["template_key"] = key
                case["marketplace"].update({k: spec[k] for k in ("type", "phase", "status")})
                case["marketplace"]["path"] = "Program"
                case["paragraphs"] = case["paragraphs"][:spec["paragraph_count"]]
                case["paragraph_sources"] = case["paragraph_sources"][:spec["paragraph_count"]]
                p.validate(case, self.root, self.brief)

    def test_reject_unsupported_class_or_target(self):
        for field, value in (("class", "D"), ("class", "High"), ("class", "Unknown"),
                             ("target_class", "D"), ("target_class", "High")):
            with self.subTest(field=field, value=value):
                case = deepcopy(self.case)
                case["marketplace"][field] = value
                with self.assertRaises(ValueError):
                    p.validate(case, self.root, self.brief)

    def test_reject_wrong_listing_fields(self):
        for field in ("type", "phase", "status", "path", "package_id", "url"):
            with self.subTest(field=field):
                case = deepcopy(self.case)
                case["marketplace"][field] = "Wrong"
                with self.assertRaises(ValueError):
                    p.validate(case, self.root, self.brief)

    def test_reject_stale_future_and_naive_dates(self):
        for stamp in ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),
                      (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), "2026-09-21"):
            with self.subTest(stamp=stamp):
                self.case["marketplace"]["verified_at"] = stamp
                with self.assertRaises(ValueError):
                    self.validate()

    def test_source_conflicts_hold(self):
        self.case["source_conflicts"] = ["Conflicting target class"]
        with self.assertRaisesRegex(ValueError, "Resolve source conflicts"):
            self.validate()

    def test_unmapped_or_unknown_sources_hold(self):
        for mapping in ([], [[], [], []], [["https://unknown.example.com/"]] * 3):
            self.case["paragraph_sources"] = mapping
            with self.assertRaises(ValueError):
                self.validate()

    def test_missing_fields_hold(self):
        for field in ("schema_version", "company", "offering", "logo", "sources", "limits", "review_only"):
            with self.subTest(field=field):
                case = deepcopy(self.case)
                del case[field]
                with self.assertRaises(ValueError):
                    p.validate(case, self.root, self.brief)

    def test_ready_unconfirmed_path_must_be_disclosed(self):
        self.case["paragraphs"][2] = "Ready on the Marketplace."
        with self.assertRaisesRegex(ValueError, "Disclose the unconfirmed"):
            self.validate()

    def test_reject_placeholders_and_ai_dashes(self):
        for text in ("[COMPANY]", "Drop client logo", "A claim — unverified", "A claim – unverified"):
            self.case["paragraphs"][0] = text
            with self.assertRaises(ValueError):
                self.validate()

    def test_company_and_slug_paths_cannot_escape(self):
        for field, value in (("company", "../Other"), ("company", "A/B"),
                             ("company", ".hidden"), ("slug", "../overwrite")):
            with self.subTest(field=field, value=value):
                case = deepcopy(self.case)
                case[field] = value
                with self.assertRaises(ValueError):
                    p.validate(case, self.root, self.brief)

    def test_company_symlink_escape(self):
        external = self.root / "external"
        external.mkdir()
        (self.root / "custom_media/Escape").symlink_to(external, target_is_directory=True)
        self.case["company"] = "Escape"
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.validate()

    def test_logo_escape(self):
        self.case["logo"] = "../../external.png"
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.validate()

    def test_tiny_logo_rejected(self):
        Image.new("RGB", (10, 10)).save(self.folder / "assets/logo.png")
        with self.assertRaisesRegex(ValueError, "resolution"):
            self.validate()

    def test_output_collision_preserves_file(self):
        output = self.folder / "fixture-review.pdf"
        output.write_bytes(b"existing")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.validate()
        self.assertEqual(output.read_bytes(), b"existing")

    def test_template_hash_change_rejected(self):
        source = self.root / "docs/outbound-collateral/rev5-ready-to-20x.pdf"
        source.write_bytes(source.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "Template changed"):
            self.validate()

    def test_long_word_rejected_even_after_short_word(self):
        font = fitz.Font(fontfile=p.DEFAULT_FONTS["normal"])
        for text in ("W" * 200, "Short " + "W" * 200):
            with self.assertRaisesRegex(ValueError, "Word does not fit"):
                p.wrap(text, font, 12, 200)

    def test_nbsp_text_equivalence(self):
        self.assertEqual("BMC Helix public-sector", p.normalized_text("BMC\u00a0Helix\npublic\u00adsector"))

    def test_overflow_publishes_nothing(self):
        self.case["paragraphs"][0] = "Long background detail " * 40
        with self.assertRaises(AssertionError):
            p.build(self.case, self.root, self.brief, p.DEFAULT_FONTS)
        self.assertFalse((self.folder / "fixture-review.pdf").exists())
        self.assertFalse(list((self.folder / "previews").iterdir()))

    def test_check_only_has_no_writes(self):
        before = sorted(str(x.relative_to(self.root)) for x in self.root.rglob("*"))
        result = subprocess.run([sys.executable, str(Path(p.__file__).resolve()),
                                 "--repo", str(self.root), "--brief", str(self.brief), "--check-only"],
                                check=True, capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(result.stdout)["writes"], False)
        after = sorted(str(x.relative_to(self.root)) for x in self.root.rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
