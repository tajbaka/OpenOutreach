"""Fill only reviewed company fields in Boundera's three canonical PDFs.

No network calls, Django imports, CRM writes, or sending APIs. Inputs are a
company-local research brief. Outputs remain review-only pending human QA.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlparse

import fitz
from PIL import Image, ImageChops, ImageDraw

TEMPLATES = {
    "initial-20x": {
        "file": "initial-20x.pdf",
        "sha256": "b30ecf0cffcfc2353bb9fb56cd8cdf5792cd7bafe4ce5c7136c2b18b31c8ae32",
        "type": "20x", "phase": "Initial Implementation", "status": "Not yet certified",
        "paragraph_count": 3, "last_baseline": 399,
    },
    "rev5-authorized": {
        "file": "rev5-auth-20x.pdf",
        "sha256": "ebab7329c02142bb9b6a9b825419316c24e4c8666e7f4a7bf00d3c07a5d886dc",
        "type": "Rev5", "phase": "Ongoing Certification", "status": "FedRAMP Certified",
        "paragraph_count": 2, "last_baseline": 391,
    },
    "rev5-ready": {
        "file": "rev5-ready-to-20x.pdf",
        "sha256": "9e02bf956b7d8ff3509f31d65a346f4b3406ed56a8b89e64ae18d420bb4746fb",
        "type": "Rev5", "phase": "Legacy FedRAMP Ready", "status": "Legacy FedRAMP Ready",
        "paragraph_count": 3, "last_baseline": 413,
    },
}
WHITE = (1, 1, 1)
CARD = (0.9843, 0.9882, 0.9922)
PLACEHOLDER = re.compile(r"\[[A-Z][A-Z 0-9/]*\]|Drop client|browse files")
DEFAULT_FONTS = {
    "normal": "/System/Library/Fonts/Supplemental/Arial.ttf",
    "bold": "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def inside(path, parent):
    resolved = path.resolve()
    require(resolved.is_relative_to(parent.resolve()), f"Path escapes company directory: {path}")
    return resolved


def validate(case, root, brief):
    require(case.get("schema_version") == 1, "Expected brief schema_version 1")
    company = case.get("company", "")
    require(isinstance(company, str) and company.strip() == company and bool(company), "Company name required")
    require(not any(c in company for c in "/\\") and not company.startswith("."),
            "Company must be one non-hidden directory name")
    media = root / "custom_media"
    require(not media.is_symlink(), "custom_media must not be a symlink")
    folder = inside(media / company, media)
    require(folder.parent == media.resolve(), "Company must be a direct child of custom_media")
    inside(brief, folder)
    key = case.get("template_key")
    require(key in TEMPLATES, "Unknown template_key")
    spec = TEMPLATES[key]
    mp = case.get("marketplace", {})
    require(all(mp.get(k) == spec[k] for k in ("type", "phase", "status")),
            "Marketplace phase/type/status does not fit this template")
    require(mp.get("path") in ({"Program"} if key == "initial-20x" else {"Program", "Agency"}),
            "Missing or inconsistent Marketplace path")
    require(mp.get("class") in {"B", "C", "Unknown"}, "Unsupported or missing Marketplace class")
    require(mp.get("target_class") not in {"D", "High"}, "Class D/High target is out of scope")
    if key != "initial-20x":
        require(mp["class"] == "C", "This Rev5 template is scoped to Class C (Moderate)")
    url = urlparse(mp.get("url", ""))
    require(url.scheme == "https" and url.hostname in {"www.fedramp.gov", "fedramp.gov", "marketplace.fedramp.gov"},
            "Exact official Marketplace URL required")
    require(bool(mp.get("package_id")) and mp["package_id"] in url.path, "Package ID must match source URL")
    checked = datetime.fromisoformat(mp.get("verified_at", ""))
    require(checked.tzinfo is not None, "verified_at must include timezone")
    require(0 <= (datetime.now(timezone.utc) - checked).total_seconds() <= 7 * 86400,
            "Recheck Marketplace facts; timestamp is future or older than seven days")
    require(case.get("source_conflicts") == [], "Resolve source conflicts before rendering")
    require(case.get("review_only") is True, "This helper creates review-only collateral")
    require(isinstance(case.get("limits"), list) and len(case["limits"]) > 0, "Record research/template limitations")
    paragraphs = case.get("paragraphs", [])
    require(len(paragraphs) == spec["paragraph_count"], "Wrong company-background paragraph count")
    sources = case.get("sources", [])
    require(isinstance(sources, list) and len(sources) > 0, "Source evidence required")
    source_urls = {x["url"] for x in sources}
    require(mp["url"] in source_urls, "Include Marketplace in source evidence")
    require(all(x.get("finding") and urlparse(x["url"]).scheme == "https" for x in sources),
            "Each source needs an HTTPS URL and a supported finding")
    require(len(case.get("paragraph_sources", [])) == len(paragraphs), "Map every paragraph to sources")
    require(all(urls and set(urls) <= source_urls for urls in case["paragraph_sources"]),
            "Every paragraph needs known source URLs")
    for value in [company, case.get("offering", ""), *paragraphs]:
        require(isinstance(value, str) and value.strip() and not PLACEHOLDER.search(value),
                "Empty text or unresolved company placeholders")
        require(not any(c in value for c in "\u2011\u2013\u2014"), "Use ASCII hyphens in new copy")
    if key == "rev5-ready":
        require(case.get("next_path") in {"20x", "Unconfirmed"}, "Ready next path must be 20x or Unconfirmed")
        if case["next_path"] == "Unconfirmed":
            require("unconfirmed" in " ".join(paragraphs).lower(), "Disclose the unconfirmed path in the PDF")
    slug = case.get("slug", "")
    require(bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)), "Use a safe lowercase output slug")
    logo = inside(folder / case.get("logo", ""), folder)
    require(logo.is_file() and logo.suffix.lower() == ".png", "A reviewed company-local PNG logo is required")
    require(case.get("logo_source_url") in source_urls, "Record the official logo source")
    bg = case.get("logo_background", "#ffffff")
    require(bool(re.fullmatch(r"#[0-9a-fA-F]{6}", bg)), "logo_background must be a hex color")
    with Image.open(logo) as im:
        require(im.width >= 400 and im.height >= 30, "Logo resolution is too small")
    source = root / "docs/outbound-collateral" / spec["file"]
    require(sha(source) == spec["sha256"], "Template changed; inspect and update its exact field contract first")
    output = folder / (slug + "-review.pdf")
    require(not output.exists(), "Output already exists; use a new revision slug, never overwrite")
    for name in ("previews", "working", "research", "assets"):
        inside(folder / name, folder)
    return spec, folder, logo


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_text(text):
    # Embedded Arial extracts visible spaces/hyphens as NBSP/soft hyphen.
    return ' '.join(text.replace('\u00ad', '-').split())


def wrap(text, font, size, width):
    lines = []
    line = ''
    for word in text.split():
        if font.text_length(word, fontsize=size) > width:
            raise ValueError(f'Word does not fit: {word}')
        trial = f'{line} {word}'.strip()
        if font.text_length(trial, fontsize=size) <= width:
            line = trial
        else:
            if not line:
                raise ValueError(f'Word does not fit: {word}')
            lines.append(line)
            line = word
    return lines + [line]


def image(page):
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    return Image.frombytes('RGB', (pix.width, pix.height), pix.samples)


def build(case, root, brief, font_files):
    spec, company_dir, logo = validate(case, root, brief)
    FONTS = {k: fitz.Font(fontfile=v) for k, v in font_files.items()}
    FONT_FILES = font_files
    case = dict(case, template=spec["file"], template_sha256=spec["sha256"])
    work_root = company_dir / "working"
    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=case["slug"] + "-", dir=work_root))
    final_output = company_dir / (case["slug"] + "-review.pdf")
    company_dir.mkdir(parents=True, exist_ok=True)
    (company_dir/'previews').mkdir(exist_ok=True)
    (company_dir/'assets').mkdir(exist_ok=True)
    source = root / 'docs/outbound-collateral' / case['template']
    assert sha(source) == case['template_sha256'], 'Template changed; inspect before editing'
    original = fitz.open(source)
    doc = fitz.open(source)
    assert len(doc) == 2 and all(tuple(p.rect) == (0, 0, 612, 792) for p in doc)
    page_receipts = []
    for page_idx, page in enumerate(doc):
        modifications = []
        edits = []
        logo_rect = fitz.Rect(192.5, 33.5, 307, 68.5)
        page.add_redact_annot(logo_rect, fill=WHITE)
        page.apply_redactions(images=0, graphics=1)
        modifications.append(logo_rect)
        background = case.get("logo_background", "#ffffff")
        page.draw_rect(logo_rect, color=None, fill=tuple(int(background[i:i+2], 16)/255 for i in (1, 3, 5)))
        page.insert_image(fitz.Rect(196, 37, 304, 65), filename=str(logo), keep_proportion=True)
        paragraphs = iter(case['paragraphs'])
        body_count = 0
        for block in page.get_text('dict')['blocks']:
            if 'lines' not in block:
                continue
            spans = [s for line in block['lines'] for s in line['spans']]
            block_text = '\n'.join(''.join(s['text'] for s in line['spans']) for line in block['lines'])
            if '[' not in block_text:
                continue
            bbox = fitz.Rect(block['bbox'])
            if page_idx == 1 and bbox.x0 < 100 and 250 < bbox.y0 < 380 and 'Bold' not in spans[0]['font']:
                text = next(paragraphs)
                body_count += 1
                first = spans[0]
                base = first['origin'][1]
                max_y = spec["last_baseline"] if body_count == spec["paragraph_count"] else base + 32
                rect = fitz.Rect(bbox.x0 - .4, bbox.y0 - .4, 284, max(max_y + 3, bbox.y1 + .4))
                text_lines = wrap(text, FONTS['normal'], first['size'], 284 - bbox.x0)
                assert base + (len(text_lines)-1)*15 <= max_y, (case['company'], text_lines)
                edits.append((rect, CARD, [(line, (bbox.x0, base+n*15), first['size'], 'normal', first['color']) for n,line in enumerate(text_lines)]))
            else:
                for line in block['lines']:
                    line_spans = line['spans']
                    text = ''.join(s['text'] for s in line_spans)
                    if '[' not in text:
                        continue
                    first = line_spans[0]
                    text = text.replace('[COMPANY]', case['company'])
                    text = text.replace('[LISTED OFFERING]', case['offering'])
                    # Exact name/offering substitution only; do not invent a shorter offering.
                    # If it does not fit, fail and request a verified shorter display name.
                    assert '[' not in text, text
                    bold = 'Bold' in first['font']
                    font_key = 'bold' if bold else 'normal'
                    size = first['size']
                    width = FONTS[font_key].text_length(text, fontsize=size)
                    x = (612 - width)/2 if first['bbox'][1] < 200 else first['origin'][0]
                    assert x >= 39 and x + width <= 573, (text, x, width)
                    old = fitz.Rect(line['bbox'])
                    new = fitz.Rect(x-.5, old.y0-.5, x+width+.5, old.y1+.5)
                    rect = old | new
                    color = CARD if 200 < old.y0 < 700 else WHITE
                    # Only this exact text region is erased; shapes and other copy survive.
                    edits.append((rect, color, [(text, (x, first['origin'][1]), size, font_key, first['color'])]))
        assert body_count == (len(case['paragraphs']) if page_idx == 1 else 0)
        for rect, fill, _ in edits:
            page.add_redact_annot(rect, fill=fill)
            modifications.append(rect)
        page.apply_redactions(images=0, graphics=0)
        for key,path in FONT_FILES.items():
            page.insert_font(fontname='Review'+key, fontfile=path)
        for _, _, lines in edits:
            for text, point, size, key, color in lines:
                rgb = tuple(((color >> shift) & 255)/255 for shift in (16, 8, 0))
                page.insert_text(point, text, fontname='Review'+key, fontsize=size, color=rgb)
        assert not re.search(r'\[[A-Z][A-Z 0-9/]*\]|Drop client|browse files', page.get_text())
        before, after = image(original[page_idx]), image(page)
        diff = ImageChops.difference(before, after)
        mask = Image.new('RGB', before.size, (255,255,255))
        pen = ImageDraw.Draw(mask)
        for r in modifications:
            pen.rectangle((int(r.x0*2)-2, int(r.y0*2)-2, int(r.x1*2)+2, int(r.y1*2)+2), fill=(0,0,0))
        outside = ImageChops.multiply(diff, mask)
        # MuPDF rewrites the existing logo's interpolation flag while applying
        # redactions. Allow only low-level resampling differences inside that
        # exact original image rectangle, never elsewhere on the page.
        source_image_boxes = [r for im in original[page_idx].get_images(full=True) for r in original[page_idx].get_image_rects(im[0])]
        assert len(source_image_boxes) == 1
        img_box = source_image_boxes[0]
        crop_box = (int(img_box.x0*2)-2, int(img_box.y0*2)-2, int(img_box.x1*2)+2, int(img_box.y1*2)+2)
        existing_logo_delta = max(hi for lo,hi in outside.crop(crop_box).getextrema())
        assert existing_logo_delta <= 16, ('Existing logo changed beyond resampling', existing_logo_delta)
        ImageDraw.Draw(outside).rectangle(crop_box,fill=(0,0,0))
        assert outside.getbbox() is None, ('Unexpected change outside allowed fields', case['slug'], page_idx, outside.getbbox())
        page_receipts.append({'page': page_idx+1, 'changed_rectangles': [list(r) for r in modifications], 'outside_allowed_regions_unchanged_except_existing_logo_resampling': True, 'existing_logo_max_channel_delta': existing_logo_delta})
    output = work / (case['slug'] + '-review.pdf')
    metadata = dict(doc.metadata)
    metadata['title'] = f"{case['company']} - {case['template'].removesuffix('.pdf')} - personalization review"
    metadata['subject'] = 'Review only. Company personalization, not approval of template claims or permission to send.'
    doc.set_metadata(metadata)
    doc.save(output, garbage=4, deflate=True)
    check = fitz.open(output)
    assert len(check) == 2
    assert all(normalized_text(case['company']) in normalized_text(p.get_text()) for p in check)
    extracted = ' '.join(normalized_text(p.get_text()) for p in check)
    assert normalized_text(case['offering']) in extracted, repr(extracted[-1800:])
    assert all(normalized_text(paragraph) in normalized_text(check[1].get_text()) for paragraph in case['paragraphs'])
    # Independent renderer for review images; avoid MuPDF's process-local
    # image cache interactions across multiple input documents.
    prefix = work / f"{case['slug']}-page"
    subprocess.run(['pdftoppm','-scale-to','1500','-png',str(output),str(prefix)],check=True,timeout=60)
    previews = [Path(f'{prefix}-{n}.png') for n in (1,2)]
    images = [Image.open(p) for p in previews]
    contact = Image.new('RGB', (1240, 820), '#e9edf3')
    for n,im in enumerate(images):
        im.thumbnail((600, 780))
        contact.paste(im, (10+n*620, 18))
    spread = work / (case['slug'] + '-spread.png')
    contact.save(spread)
    assert sha(source) == case['template_sha256']
    receipt = case | {
        "output": str(final_output), "output_sha256": sha(output),
        "template_sha256": spec["sha256"], "logo_sha256": sha(logo),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "review_only_visual_review_pending",
        "qa": page_receipts,
    }
    receipt_path = company_dir / "research" / (case["slug"] + "-manifest.json")
    require(not receipt_path.exists(), "Manifest already exists; use a new revision slug")
    # Exclusively publish validated files; never replace earlier media.
    for preview in [*previews, spread]:
        require(not (company_dir / "previews" / preview.name).exists(), "Preview already exists")
    for preview in [*previews, spread]:
        os.link(preview, company_dir / "previews" / preview.name)
    os.link(output, final_output)
    with receipt_path.open("x") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--brief", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--font-regular", default=DEFAULT_FONTS["normal"])
    parser.add_argument("--font-bold", default=DEFAULT_FONTS["bold"])
    args = parser.parse_args()
    require(__debug__, "Do not run template verification with Python optimization enabled")
    root = args.repo.resolve()
    brief = args.brief.resolve()
    case = json.loads(brief.read_text())
    validate(case, root, brief)
    if args.check_only:
        print(json.dumps({"validated": True, "company": case["company"], "writes": False}))
        return
    receipt = build(case, root, brief, {"normal": args.font_regular, "bold": args.font_bold})
    print(json.dumps({"output": receipt["output"], "status": receipt["status"], "pages_checked": len(receipt["qa"])}))


if __name__ == "__main__":
    main()
