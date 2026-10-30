"""Stage only the existing Vercel functions; never upload a working checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "api/slack_feed_like.py", "api/slack_feed_context.py", "api/rb2b_webhook.py",
    "api/exceptions.py", "api/slack_feed_comment.py", "api/slack_feed_common.py",
    "api/slack_enrich.py", "api/slack_reply_drafts.py", "api/requirements.txt", "vercel.json",
    "linkedin/__init__.py", "linkedin/calendar_links.py", "linkedin/feed_like_slack_status.py",
    "linkedin/feed_slack_status.py",
)


def stage(project_file, *, root=ROOT):
    project = json.loads(Path(project_file).read_text(encoding="utf-8"))
    if not str(project.get("projectId", "")).startswith("prj_") or not str(project.get("orgId", "")).startswith("team_"):
        raise ValueError("An existing Vercel project/team link is required")
    output = Path(tempfile.mkdtemp(prefix="openoutreach-rb2b-release-"))
    manifest = []
    for name in FILES:
        content = (root / name).read_bytes()
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        manifest.append({"path": name, "sha256": hashlib.sha256(content).hexdigest()})
    (output / ".vercel").mkdir()
    (output / ".vercel/project.json").write_text(json.dumps({
        "projectId": project["projectId"], "orgId": project["orgId"],
    }), encoding="utf-8")
    (output / "source-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"directory": str(output), "source_files": len(manifest), "manifest": str(output / "source-manifest.json")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-json", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(stage(args.project_json), indent=2))
