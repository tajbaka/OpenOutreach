"""Rigid ICP-keyed outbound message templates for high-volume cohorts.

Companion to `linkedin.notifications.sheets.read_icp_goals()` —
that helper reads operator-editable per-ICP goals from the `ICP Goals`
Sheets tab, which guide the Claude-run warm-cohort drafting flow while
the followup drafter fills with an AI-generated 1-2 sentence hook per
lead. That path is right for warm cohorts (ball-on-us, met, cold-thread,
pre-meeting) where personalization is the lift, but it's overkill for
the high-volume Connected/No-Reply cohort the daemon DMs rigidly via
`linkedin/tasks/follow_up.py` — same canonical pitch blasted out with
only the first name filled in.

This module is the rigid alternative. Templates live in
`linkedin/icp_messages.json` (checked into the repo). The legacy channel
shape is `{sender: {icp: {channel: [variant1, variant2, ...]}}}`. Follow-up
channels can also use step objects:
`{channel: [{"delay_days": 0, "variants": [...]}, ...]}`. The top level is
keyed by the operator's canonical handle (`linkedin.operators.resolve_operator`,
e.g. "Arian" / "Chuka") so each sender gets a fully independent template block.
Under each sender, variants stay nested inside the step so the batch doesn't
look templated when scanned top-to-bottom. The only substitution is
`{first_name}`; product name, URLs, signature, everything else is hardcoded
literally in the message body. To change the wording, edit the JSON.

There is no shared default sender: a sender absent from the JSON raises
`SheetsError` rather than falling back, so a misconfigured operator
handle fails loud instead of blasting another operator's copy.

Variant selection is seeded on `lead.id` so the same lead always gets
the same variant across re-runs — useful when the operator edits a
draft mid-run and re-renders. Lead `42` always lands on variant 0 (or
whatever `42 % len(variants)` resolves to), not a random new one.

Routing: ROLE → ICP via the same `FU_ROLE_TO_ICP` mapping the Sheets
path uses, so a lead the followup workflow classifies as `ROLE=CSP`
gets the `CSPs` template here. Channel rolls into Advisors, Assessor
rolls into 3PAOs/Assessors (same as Sheets).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from linkedin.conf import ROOT_DIR
from linkedin.exceptions import SheetsError
from linkedin.name_utils import greeting_first_name
from linkedin.notifications.sheets import FU_ROLE_TO_ICP
from linkedin.operators import resolve_operator

logger = logging.getLogger(__name__)

_MESSAGES_PATH = Path(__file__).parent / "icp_messages.json"

# `{add <filename>}` placeholders attach a file to the send. The
# placeholder text is stripped from the rendered body. Multiple
# attachments per template are supported. Resolution order:
#   1. If the value contains a path separator → resolve relative to
#      ROOT_DIR verbatim (e.g. `{add assets/followup/demo.gif}`).
#   2. Else → search `_ATTACH_SEARCH_DIRS` in order; first match wins
#      (e.g. `{add demo.gif}` finds `assets/followup/demo.gif`).
# Missing files log a warning and are silently dropped so a stale
# template reference doesn't block the whole send.
_ATTACH_RE = re.compile(r"\{\s*add\s+([^\}]+?)\s*\}")
# Both spellings supported — `assets/follow_up` (snake_case, matches the
# Python `follow_up` task module) and `assets/followup` (no separator).
# Trailing "" = ROOT_DIR itself for legacy callers.
_ATTACH_SEARCH_DIRS = ("assets/follow_up", "assets/followup", "")
ICP_MESSAGES_HEADERS = ["ICP", "Connect Message", "Followup Message"]
ICP_MESSAGES_SHEET_BUCKETS = ("CSPs", "3PAOs/Assessors", "Advisors")
UNKNOWN_COMPANY_NAMES = {"unknown company", "unknown", "n/a", "none"}


@dataclass
class FilledMessage:
    """Body + resolved attachment paths from rendering a rigid ICP template.

    Acts string-ish for backward compatibility: `str(filled)`, `"X" in filled`,
    `filled == "..."` all delegate to `.body`. New callers should prefer
    explicit `.body` / `.attachments` access for clarity.
    """
    body: str
    attachments: list[Path] = field(default_factory=list)

    def __str__(self) -> str:
        return self.body

    def __contains__(self, item) -> bool:
        return item in self.body

    def __eq__(self, other) -> bool:
        if isinstance(other, str):
            return self.body == other
        if isinstance(other, FilledMessage):
            return self.body == other.body and self.attachments == other.attachments
        return NotImplemented

    def __ne__(self, other) -> bool:
        eq = self.__eq__(other)
        return NotImplemented if eq is NotImplemented else not eq

    def __hash__(self) -> int:
        return hash((self.body, tuple(self.attachments)))


@dataclass(frozen=True)
class TemplateStep:
    delay_days: int
    variants: list[str]


def is_unknown_company_name(company_name: str | None) -> bool:
    """True when `company_name` is a placeholder sentinel, not real data."""
    normalized = re.sub(r"\s+", " ", (company_name or "").strip().lower())
    return normalized in UNKNOWN_COMPANY_NAMES


def safe_company_name(company_name: str | None, *, fallback: str = "your team") -> str:
    """Return a template-safe company string.

    Some import paths use "Unknown Company" as a data sentinel. Rendering that
    into outreach copy exposes the automation, so rigid templates get a
    grammatical generic fallback instead.
    """
    cleaned = (company_name or "").strip()
    return fallback if is_unknown_company_name(cleaned) else cleaned


# ROLE classification — used by the daemon's follow_up Task handler to
# pick which ICP template fires for a given Lead. The followup-sheet
# workflow runs a fancier per-lead classifier inside its drafting
# subagents (Phase 5 of `docs/followup-generation-workflow.md`); this
# is the lighter-weight pure-Python version for the always-on daemon
# path, where we can't afford an LLM call per send. Misclassification
# downside is small — CSP is the default and the three templates all
# read fine to anyone in the FedRAMP universe; the bucket only tunes
# the angle (referral program vs assessor-portal vs CSP pitch).
TIER1_3PAO_FIRMS = {
    "coalfire", "schellman", "prescient", "a-lign", "alignsec",
    "barr advisory", "barr", "kratos", "knowx", "fortreum",
    "kpmg", "ey ", "deloitte", "pwc",  # big-four auditors that 3PAO
}
ADVISOR_SIGNALS = (
    "advisor", "consultant", "vciso", "ciso advisor",
    "managed service", "managed compliance", "compliance services",
    "fractional", "principal", "managing director",
    "compliance partner", "grc partner",
)


def resolve_icp(lead) -> str:
    """Return the canonical ICP bucket for a lead, populating Lead.icp if blank.

    Single source of truth used by both the connect-note picker and the
    follow-up template path. Two write paths converge here:

      1. `add_seeds` stamps `Lead.icp` at import from the CSV `ICP`
         column (operator's sourcing intent, normalized via
         `CSV_ICP_TO_LEAD_ICP`). This is the primary path for new leads.
      2. This function backfills `Lead.icp` from `classify_role(lead)`
         when it's still blank — typically a legacy lead imported before
         the field existed, or imported via a path that didn't stamp it.
         The result is cached on the row so subsequent calls are a free
         attribute read.

    Returns "" when the lead has no signal at all (no CSV ICP, no
    scrape data yet). Callers should treat "" as "no template routing
    available, fall back to env-var defaults". This is intentional —
    sending under a wrong-bucket pitch is worse than sending under a
    generic one.
    """
    if lead.icp:
        return lead.icp

    role = classify_role(lead)
    icp = FU_ROLE_TO_ICP.get(role, "")
    if icp:
        lead.icp = icp
        try:
            lead.save(update_fields=["icp"])
        except Exception as e:
            # Don't fail the caller if the save bounces — they still get
            # the resolved ICP. Next call will retry the save.
            logger.warning("resolve_icp: save failed for lead=%s → %s", lead.pk, e)
    return icp


def classify_role(lead) -> str:
    """Map a Lead to one of `FU_ROLES` based on company + headline signals.

    Deterministic, no LLM. Order matters: 3PAO check first (strong signal
    from the firm name), then Advisor (headline / summary signal), then
    fall back to CSP as the broad default. Channel and Assessor aren't
    auto-detected here — the daemon's deterministic path doesn't have
    enough signal to distinguish a Channel/reseller role from a CSP
    employee from the LinkedIn description alone, and Channel rolls into
    the Advisor template anyway via `FU_ROLE_TO_ICP`. If the operator
    needs a Channel-specific draft they can override via the followup
    sheet, which is what the bespoke per-lead drafter is for.
    """
    co = (lead.company_name or "").lower()
    if any(t in co for t in TIER1_3PAO_FIRMS):
        return "3PAO"

    headline_summary = ""
    if lead.description:
        try:
            prof = json.loads(lead.description)
            headline_summary = (
                f"{prof.get('headline', '')} {prof.get('summary', '')}".lower()
            )
        except (json.JSONDecodeError, TypeError):
            pass

    if any(s in headline_summary for s in ADVISOR_SIGNALS):
        return "Advisor"
    return "CSP"


def load_icp_messages(sender: str) -> dict[str, dict[str, list[str]]]:
    """Return one sender's `{icp: {channel: [variant, ...]}}` block.

    The JSON file is `{sender: {icp: {channel: [...]}}}` — each operator
    (canonical handle from `linkedin.operators.resolve_operator`) gets a
    full, independent template block. There is no shared default: an
    unknown `sender` raises `SheetsError` per the project's no-silent-
    fallback rule — sending under another operator's copy is a worse
    outcome than crashing the run.

    Loaded fresh on every call so an operator edit of the file takes
    effect on the next followup run without needing a process restart.
    """
    by_sender = json.loads(_MESSAGES_PATH.read_text())
    if sender not in by_sender:
        raise SheetsError(
            f"icp_outbound: sender {sender!r} has no template block in "
            f"{_MESSAGES_PATH.name} (known senders: {sorted(by_sender)})"
        )
    return by_sender[sender]


def known_senders() -> set[str]:
    """Return the set of operator handles with a block in icp_messages.json.

    Just the top-level keys of the JSON file. Used by the daemon's
    startup check to verify a LinkedIn account has templates before the
    task loop begins.
    """
    return set(json.loads(_MESSAGES_PATH.read_text()))


def icp_messages_rows(sender: str) -> list[list[str]]:
    """Flatten one sender's core JSON block into one row per ICP.

    The sheet is intentionally operator-friendly rather than lossless:
    it surfaces the three core ICP buckets only, and only the first
    variant for the connect note / follow-up. Extra ICPs (e.g. Channel)
    and extra variants remain in JSON and are preserved on pull.
    Sequenced follow-up channels are JSON-only because the single-cell
    Sheet view cannot round-trip multiple steps safely.
    """
    rows: list[list[str]] = [list(ICP_MESSAGES_HEADERS)]
    messages = load_icp_messages(sender)
    for icp in ICP_MESSAGES_SHEET_BUCKETS:
        channels = messages.get(icp, {})
        followup = channels.get("linkedin_connect_followup") or [""]
        if followup and isinstance(followup[0], dict):
            raise SheetsError(
                f"ICP messages sync cannot push sequenced follow-up templates "
                f"for {sender}.{icp}. Edit linkedin/icp_messages.json directly."
            )
        rows.append([
            icp,
            ((channels.get("linkedin_connect_note") or [""])[0] or "").strip(),
            (followup[0] or "").strip(),
        ])
    return rows


def parse_icp_messages_rows(rows: list[list[str]]) -> dict[str, dict[str, list[str]]]:
    """Parse sheet rows back into one sender's core JSON block."""
    if not rows:
        raise SheetsError("ICP messages tab is empty")
    header_lower = [str(h).strip().lower() for h in rows[0]]

    def col_idx(name: str) -> int:
        try:
            return header_lower.index(name.lower())
        except ValueError as e:
            raise SheetsError(
                f"ICP messages tab missing required {name!r} column"
            ) from e

    icp_idx = col_idx("ICP")
    connect_idx = col_idx("Connect Message")
    followup_idx = col_idx("Followup Message")

    out: dict[str, dict[str, list[str]]] = {}
    for row_num, row in enumerate(rows[1:], start=2):
        def cell(idx: int) -> str:
            return row[idx].strip() if idx < len(row) and row[idx] is not None else ""

        icp = cell(icp_idx)
        connect_message = cell(connect_idx)
        followup_message = cell(followup_idx)
        if not any((icp, connect_message, followup_message)):
            continue
        if not icp or not connect_message or not followup_message:
            raise SheetsError(
                f"ICP messages row {row_num} must include ICP, Connect Message, and Followup Message"
            )
        if icp not in ICP_MESSAGES_SHEET_BUCKETS:
            raise SheetsError(
                f"ICP messages row {row_num} has unsupported ICP {icp!r}; "
                f"expected one of {list(ICP_MESSAGES_SHEET_BUCKETS)}"
            )
        if icp in out:
            raise SheetsError(f"ICP messages row {row_num} duplicates ICP {icp!r}")
        out[icp] = {
            "linkedin_connect_note": [connect_message],
            "linkedin_connect_followup": [followup_message],
        }
    if not out:
        raise SheetsError("ICP messages tab has no message rows")
    return out


def save_icp_messages(sender: str, block: dict[str, dict[str, list[str]]]) -> None:
    """Merge one sender's edited core ICPs into `icp_messages.json`.

    Only buckets present in `block` are replaced. Other ICPs and extra
    variants for untouched buckets remain as-is. Existing sequenced
    `linkedin_connect_followup` channels are preserved on pull rather
    than flattened back to the Sheet's single follow-up cell.
    """
    by_sender = json.loads(_MESSAGES_PATH.read_text())
    existing = by_sender.get(sender, {})
    merged = dict(existing)
    for icp, channels in block.items():
        existing_channels = existing.get(icp, {})
        merged_channels = dict(existing_channels)
        for channel, value in channels.items():
            current = existing_channels.get(channel)
            if (
                channel == "linkedin_connect_followup"
                and isinstance(current, list)
                and current
                and isinstance(current[0], dict)
            ):
                # The Sheets tab is single-cell/legacy only for follow-up
                # copy. Do not flatten a real multi-step sequence on pull.
                continue
            merged_channels[channel] = value
        merged[icp] = merged_channels
    by_sender[sender] = merged
    _MESSAGES_PATH.write_text(json.dumps(by_sender, indent=2) + "\n")


def missing_sender_block(linkedin_username: str) -> str | None:
    """Return the resolved sender handle if it has *no* template block.

    `linkedin_username` is a `LinkedInProfile.linkedin_username`; it is
    run through `resolve_operator` exactly as the send paths do. Returns
    None when the sender is covered — the account is safe to run
    outbound for. A non-None result is the handle the operator must add
    to `icp_messages.json` (and ideally alias in `linkedin/operators.py`)
    before follow-up DMs will work for that account.
    """
    sender = resolve_operator(linkedin_username)
    return None if sender in known_senders() else sender


def channel_steps(*, sender: str, icp: str, channel: str) -> list[TemplateStep]:
    """Return normalized steps for one sender × ICP × channel.

    Backward-compatible shapes:
      - legacy: ["variant a", "variant b"] → one step with variants
      - sequence: [{"delay_days": 0, "variants": ["..."]}, ...]
    """
    messages = load_icp_messages(sender)
    if icp not in messages:
        raise SheetsError(
            f"icp_outbound: ICP {icp!r} has no rigid template "
            f"(known: {sorted(messages)})"
        )
    raw = messages[icp].get(channel)
    if not raw:
        raise SheetsError(
            f"icp_outbound: ICP {icp!r} has no {channel!r} channel "
            f"variants (known channels: {sorted(messages[icp])})"
        )

    if all(isinstance(v, str) for v in raw):
        return [TemplateStep(delay_days=0, variants=list(raw))]

    steps: list[TemplateStep] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SheetsError(
                f"icp_outbound: {icp!r}.{channel!r} mixes legacy variants "
                f"and step objects at index {idx}"
            )
        variants = item.get("variants")
        if not isinstance(variants, list) or not variants or not all(
            isinstance(v, str) and v.strip() for v in variants
        ):
            raise SheetsError(
                f"icp_outbound: {icp!r}.{channel!r} step {idx} must include "
                "a non-empty string variants list"
            )
        delay_days = item.get("delay_days", 0)
        try:
            delay_days = int(delay_days)
        except (TypeError, ValueError) as e:
            raise SheetsError(
                f"icp_outbound: {icp!r}.{channel!r} step {idx} has invalid "
                f"delay_days={delay_days!r}"
            ) from e
        if delay_days < 0:
            raise SheetsError(
                f"icp_outbound: {icp!r}.{channel!r} step {idx} has negative "
                f"delay_days={delay_days}"
            )
        steps.append(TemplateStep(delay_days=delay_days, variants=variants))
    return steps


def _icp_for_role(role: str) -> str:
    icp = FU_ROLE_TO_ICP.get(role)
    if not icp:
        raise SheetsError(
            f"icp_outbound: ROLE {role!r} has no ICP mapping in "
            f"FU_ROLE_TO_ICP (known ROLEs: {sorted(FU_ROLE_TO_ICP)})"
        )
    return icp


def channel_steps_for_lead(*, sender: str, role: str, channel: str) -> list[TemplateStep]:
    return channel_steps(sender=sender, icp=_icp_for_role(role), channel=channel)


def fill_message(
    *,
    sender: str,
    icp: str,
    channel: str,
    first_name: str,
    last_name: str = "",
    company_name: str = "",
    my_name: str = "",
    lead_id: int | None = None,
    variant_index: int | None = None,
    step_index: int = 0,
) -> FilledMessage:
    """Pick a variant from the rigid template and substitute placeholders.

    Mechanical substitutions only (no LLM generation):
      - `{first_name}` — lead's first name
      - `{last_name}`  — lead's last name (rarely used in current templates)
      - `{company_name}` — lead's company (CSP template uses this)
      - `{my_name}` — operator's display name (email signatures only)
      - `{our_company_name}` — our product/company name (from `.env`
        `OUR_COMPANY_NAME`). Lets templates reference the product without
        hardcoding a string the operator can change in one place.
      - `{our_website_url}` — our website URL (from `.env`
        `OUR_WEBSITE_URL`). Same rationale as `{our_company_name}`.

    Variant selection precedence:
      1. Explicit `variant_index` (if provided) — caller controls.
      2. `lead_id` mod len(variants) — stable across re-runs for a lead.
      3. Index 0 — fallback when neither is supplied.

    `sender` is the operator's canonical handle (`linkedin.operators.
    resolve_operator`) — it selects the top-level template block.

    Missing sender, ICP, or channel raises `SheetsError` per the
    project's no-silent-fallback rule; sending a wrong-bucket message in
    production is a worse outcome than crashing the run.
    """
    from linkedin.conf import OUR_COMPANY_NAME, OUR_WEBSITE_URL

    steps = channel_steps(sender=sender, icp=icp, channel=channel)
    if step_index < 0 or step_index >= len(steps):
        raise SheetsError(
            f"icp_outbound: ICP {icp!r} channel {channel!r} has no "
            f"step {step_index} (steps: {len(steps)})"
        )
    variants = steps[step_index].variants

    if variant_index is not None:
        idx = variant_index % len(variants)
    elif lead_id is not None:
        idx = lead_id % len(variants)
    else:
        idx = 0

    # IMPORTANT: extract `{add <filename>}` placeholders BEFORE str.format —
    # otherwise format() sees them as named placeholders and raises KeyError
    # on "add demo.gif" since there's no matching kwarg.
    template = variants[idx]
    stripped, attachments = _extract_attachments(template)
    body = stripped.format(
        first_name=greeting_first_name(first_name),
        last_name=last_name or "",
        company_name=safe_company_name(company_name),
        my_name=my_name or "",
        our_company_name=OUR_COMPANY_NAME,
        our_website_url=OUR_WEBSITE_URL,
    )
    # Collapse the blank line the stripped placeholder leaves behind.
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return FilledMessage(body=body, attachments=attachments)


def _extract_attachments(template: str) -> tuple[str, list[Path]]:
    """Strip `{add <filename>}` from `template` and resolve each filename.

    Returns `(template_without_attach_placeholders, resolved_paths)`.
    Missing files are logged-and-dropped so a stale reference doesn't
    block the send.
    """
    attachments: list[Path] = []

    def _swap(match):
        filename = match.group(1).strip()
        candidate = _resolve_attachment(filename)
        if candidate:
            attachments.append(candidate)
        else:
            logger.warning(
                "icp_outbound: attachment %r referenced in template but not found "
                "(searched %s)",
                filename,
                [str(ROOT_DIR / d / filename) for d in _ATTACH_SEARCH_DIRS],
            )
        return ""

    return _ATTACH_RE.sub(_swap, template), attachments


def _resolve_attachment(filename: str) -> Path | None:
    """Search ROOT_DIR-relative paths for an attachment. Returns None on miss."""
    # Explicit path → resolve verbatim relative to ROOT_DIR.
    if "/" in filename or "\\" in filename:
        path = ROOT_DIR / filename
        return path if path.exists() else None
    # Bare filename → search known asset dirs in order.
    for subdir in _ATTACH_SEARCH_DIRS:
        path = ROOT_DIR / subdir / filename if subdir else ROOT_DIR / filename
        if path.exists():
            return path
    return None


def fill_for_lead(
    *,
    sender: str,
    role: str,
    channel: str,
    lead,
    my_name: str = "",
    step_index: int = 0,
) -> FilledMessage:
    """Convenience wrapper — resolves ROLE → ICP and pulls lead fields.

    Equivalent to `fill_message(sender=sender, icp=FU_ROLE_TO_ICP[role],
    channel=channel, first_name=lead.first_name, last_name=lead.last_name,
    company_name=lead.company_name, my_name=my_name,
    lead_id=lead.id)`. Used by the daemon's follow_up handler so
    callers can stay in lead-space without thinking about the ICP key
    or variant rotation.

    `sender` is the operator's canonical handle ("Arian" / "Chuka") —
    it selects the top-level template block in `icp_messages.json`.
    `my_name` is the same handle, but used only by email-channel
    templates (signature block); LinkedIn templates have no signature,
    so passing it has no effect there.
    """
    icp = _icp_for_role(role)
    return fill_message(
        sender=sender,
        icp=icp,
        channel=channel,
        first_name=lead.first_name or "",
        last_name=getattr(lead, "last_name", "") or "",
        company_name=getattr(lead, "company_name", "") or "",
        my_name=my_name,
        lead_id=getattr(lead, "id", None),
        step_index=step_index,
    )
