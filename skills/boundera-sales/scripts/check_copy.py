#!/usr/bin/env python3
"""Check Boundera sales copy against the skill's measurable house budgets."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Budget:
    unit: str
    target_min: int
    target_max: int
    ceiling: int
    sentence_min: int | None = None
    sentence_max: int | None = None


BUDGETS = {
    "email-cold": Budget("words", 50, 90, 100, 3, 4),
    "email-warm": Budget("words", 30, 80, 120, 2, 4),
    "email-reply": Budget("words", 20, 75, 100),
    "email-recap": Budget("words", 80, 150, 180),
    "email-bump": Budget("words", 5, 25, 35, 1, 1),
    "linkedin-connect": Budget("characters", 120, 180, 200, 1, 2),
    "linkedin-first": Budget("characters", 200, 400, 500, 2, 4),
    "linkedin-nudge": Budget("words", 15, 50, 80, 1, 3),
    "sms-slack": Budget("words", 10, 45, 70, 1, 3),
}


def word_count(text: str) -> int:
    """Return an approximate reader-visible word count."""
    cleaned = re.sub(r"https?://\S+", "URL", text)
    return len(re.findall(r"\b[\w’'-]+\b", cleaned, flags=re.UNICODE))


def sentence_count(text: str) -> int:
    """Count non-empty sentence-like units split by punctuation or paragraphs."""
    cleaned = re.sub(r"https?://\S+", "URL", text.strip())
    if not cleaned:
        return 0

    lines = cleaned.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if re.fullmatch(
            r"\s*(?:hi|hello|hey|dear)\b[^.!?]{0,80},?\s*",
            line,
            flags=re.IGNORECASE,
        ):
            lines[index] = ""
        break
    cleaned = "\n".join(lines)

    abbreviations = (
        (r"\ba\.m\.", "am"),
        (r"\bp\.m\.", "pm"),
        (r"\be\.g\.", "eg"),
        (r"\bi\.e\.", "ie"),
        (r"\bU\.S\.", "US"),
        (r"\bMr\.", "Mr"),
        (r"\bMs\.", "Ms"),
        (r"\bDr\.", "Dr"),
        (r"\bProf\.", "Prof"),
        (r"\bInc\.", "Inc"),
        (r"\bLtd\.", "Ltd"),
        (r"\bvs\.", "vs"),
    )
    for pattern, replacement in abbreviations:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)

    count = 0
    for paragraph in re.split(r"\n\s*\n+", cleaned):
        if not paragraph.strip():
            continue
        count += len(
            [
                part
                for part in re.split(r"[.!?]+(?=\s|$)", paragraph)
                if part.strip()
            ]
        )
    return count


def evaluate(profile: str, text: str) -> tuple[list[str], bool]:
    budget = BUDGETS[profile]
    words = word_count(text)
    characters = len(text.strip())
    sentences = sentence_count(text)
    measured = words if budget.unit == "words" else characters

    lines = [
        f"profile: {profile}",
        f"words: {words}",
        f"characters: {characters}",
        f"sentences: {sentences}",
        (
            f"{budget.unit}: {measured} "
            f"(target {budget.target_min}-{budget.target_max}; "
            f"Boundera ceiling {budget.ceiling})"
        ),
    ]

    warnings: list[str] = []
    if measured < budget.target_min:
        warnings.append(f"below the {budget.unit} target")
    elif measured > budget.target_max:
        warnings.append(f"above the {budget.unit} target")

    if budget.sentence_min is not None and sentences < budget.sentence_min:
        warnings.append("below the sentence target")
    if budget.sentence_max is not None and sentences > budget.sentence_max:
        warnings.append("above the sentence target")

    ceiling_exceeded = measured > budget.ceiling
    if ceiling_exceeded:
        warnings.append(f"exceeds the Boundera {budget.unit} ceiling")

    lines.append("status: " + ("ceiling exceeded" if ceiling_exceeded else "within ceiling"))
    lines.append("warnings: " + ("; ".join(warnings) if warnings else "none"))
    return lines, ceiling_exceeded


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=sorted(BUDGETS))
    parser.add_argument(
        "--text",
        help="Copy to check. If omitted, read the copy from standard input.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    text = args.text if args.text is not None else sys.stdin.read()
    if not text.strip():
        print("error: no copy supplied", file=sys.stderr)
        return 2

    lines, ceiling_exceeded = evaluate(args.profile, text)
    print("\n".join(lines))
    return 1 if ceiling_exceeded else 0


if __name__ == "__main__":
    raise SystemExit(main())
