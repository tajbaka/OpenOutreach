"""Decrypt this one-off handoff locally; never connect to a service or import leads."""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DESTINATION = ROOT / "artifacts/leads/soc2-class-a-2026-10-12/handoff"
FILENAMES = {
    "campaign-candidates.csv",
    "campaign-definition.json",
    "campaign-plan.json",
    "creation-status.json",
    "message-previews.json",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, help="Ignored local file containing the separately supplied key.")
    parser.add_argument("--check-only", action="store_true", help="Authenticate and validate without extracting files.")
    args = parser.parse_args()
    key = (
        args.key_file.read_bytes().strip()
        if args.key_file
        else getpass.getpass("Handoff key (not echoed): ").strip().encode("ascii")
    )
    ciphertext = (HERE / "campaign-bundle.fernet").read_bytes()
    expected_hash = (HERE / "campaign-bundle.sha256").read_text(encoding="ascii").strip()
    if hashlib.sha256(ciphertext).hexdigest() != expected_hash:
        raise SystemExit("Encrypted bundle checksum mismatch; no files extracted.")
    try:
        plaintext = Fernet(key).decrypt(ciphertext)
    except InvalidToken:
        raise SystemExit("Wrong key or unauthenticated bundle; no files extracted.") from None
    payload = json.loads(plaintext)
    if payload.get("schema_version") != 1 or set(payload.get("files", {})) != FILENAMES:
        raise SystemExit("Unexpected handoff schema or filenames; no files extracted.")
    files = {}
    for name, item in payload["files"].items():
        content = base64.b64decode(item["content_base64"], validate=True)
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise SystemExit("Handoff member checksum mismatch; no files extracted.")
        files[name] = content
    if args.check_only:
        print("Authenticated and verified all five handoff files; no extraction or service access.")
        return
    # Validate all existing members first. A retry may recover identical files,
    # but it must never replace a receipt or locally edited candidate list.
    for name, content in files.items():
        target = DESTINATION / name
        if target.exists() and target.read_bytes() != content:
            raise SystemExit(f"Existing handoff file differs: {name}; nothing overwritten.")
    DESTINATION.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, content in files.items():
        target = DESTINATION / name
        if target.exists():
            continue
        with target.open("xb") as handle:
            handle.write(content)
        target.chmod(0o600)
    print(f"Verified and extracted five files to {DESTINATION}")
    print("No database writes, campaign activation, restart, or messages were performed.")


if __name__ == "__main__":
    main()
