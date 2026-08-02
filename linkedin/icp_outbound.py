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
`{channel: [{"delay_hours": 0, "variants": [...]}, ...]}`. An ICP block can
declare `"media": ["demo.gif"]`; templates in that block may reference
`{demo.gif}` to attach that file, resolved from `assets/follow_up/` or
`assets/followup/`. The legacy `{add demo.gif}` syntax still works. The top
level is keyed by the operator's canonical handle
(`linkedin.operators.resolve_operator`, e.g. "Arian" / "Chuka") so each sender
gets a fully independent template block. Under each sender, variants stay
nested inside the step so the batch doesn't look templated when scanned
top-to-bottom. The only substitution is
`{first_name}`; product name, URLs, signature, everything else is hardcoded
literally in the message body. To change the wording, edit the JSON.

There is no shared default sender: a sender absent from the JSON raises
`SheetsError` rather than falling back, so a misconfigured operator
handle fails loud instead of blasting another operator's copy.

Variant selection is seeded on `lead.id` so the same lead always gets
the same variant across re-runs — useful when the operator edits a
draft mid-run and re-renders. Lead `42` always lands on variant 0 (or
whatever `42 % len(variants)` resolves to), not a random new one.

Routing: callers should use `resolve_icp(lead)`, which returns stamped
`Lead.icp` first and uses the legacy ROLE → ICP classifier only to backfill
blank rows. Queued follow-up tasks freeze that result in `payload.icp` so a
later lead edit cannot change an in-process sequence. Channel has its own
partner/routing bucket, while Assessor rolls into 3PAOs/Assessors. CMMC
buckets are not inferred from generic profile text; stamp them from the CSV
`ICP` column at import so CMMC buyer/advisor leads do not receive FedRAMP copy.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path

from linkedin.conf import ROOT_DIR
from linkedin.exceptions import DiscoveryConfigurationError, SheetsError
from linkedin.name_utils import greeting_first_name
from linkedin.notifications.sheets import FU_ROLE_TO_ICP, LEAD_ICP_BUCKETS
from linkedin.operators import resolve_operator

logger = logging.getLogger(__name__)

_MESSAGES_PATH = Path(__file__).parent / "icp_messages.json"
_GMAIL_MESSAGES_PATH = ROOT_DIR / "gmail" / "icp_emails.json"

# `{add <filename>}` placeholders attach a file to the send. ICP blocks can
# also declare `"media": ["demo.gif"]`, allowing templates in that block to
# attach media with the shorter `{demo.gif}` token. Placeholder text is stripped
# from the rendered body. Multiple attachments per template are supported.
# Resolution order:
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
ICP_MESSAGES_BASE_HEADERS = ["ICP", "Connect Message"]
ICP_MESSAGES_SHEET_BUCKETS = LEAD_ICP_BUCKETS
# Follow-up sequences are arbitrary-length: the sheet grows one
# `Followup Message N` column per step, sized per sender to the longest
# sequence across its ICP buckets (always at least one column).
_FOLLOWUP_HEADER_RE = re.compile(r"^followup message(?:\s+(\d+))?$")
_EMAIL_SUBJECT_HEADER_RE = re.compile(r"^email subject(?:\s+(\d+))?$")
_EMAIL_BODY_HEADER_RE = re.compile(r"^email body(?:\s+(\d+))?$")


def _followup_header(n: int) -> str:
    return f"Followup Message {n}"


def _email_subject_header(n: int) -> str:
    return f"Email Subject {n}"


def _email_body_header(n: int) -> str:
    return f"Email Body {n}"


def icp_messages_headers(followup_steps: int, email_steps: int = 0) -> list[str]:
    """Full header row for the multi-channel post-accept cadence."""
    n = max(followup_steps, email_steps, 1)
    headers = list(ICP_MESSAGES_BASE_HEADERS)
    for idx in range(n):
        step = idx + 1
        headers.extend([
            _followup_header(step),
            _email_subject_header(step),
            _email_body_header(step),
        ])
    return headers


# Single-step default header; the push path computes the real width per sender.
ICP_MESSAGES_HEADERS = icp_messages_headers(1)


def _default_step_delay(idx: int) -> float:
    """Cadence used for a sheet-introduced step when JSON carries no delay.

    Step 0 fires on accept (0 hours); each later step defaults to 96 hours.
    Existing per-step delays in JSON are preserved on merge (see
    `_reconcile_followup`); this only fills genuinely new steps.
    """
    return 0 if idx == 0 else 96


def _default_email_step_delay(idx: int) -> float:
    """Default email offsets from accept when Sheets adds a Gmail step."""
    if idx == 0:
        return 0.33
    if idx == 1:
        return 192
    return 192 + ((idx - 1) * 168)
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
    delay_hours: float
    variants: list[str]


@dataclass(frozen=True)
class DiscoveryTarget:
    """One sender/ICP block that explicitly opts into profile discovery."""

    icp: str
    profile: str
    search_queries: tuple[str, ...]


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


# ROLE classification — fallback used by `resolve_icp` only when Lead.icp is
# blank. Normal daemon follow-up tasks freeze the resolved bucket in
# payload.icp when queued, so later row edits do not move an in-process
# sequence between template buckets.
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


def load_icp_messages(sender: str) -> dict[str, dict[str, object]]:
    """Return one sender's `{icp: {channel: [variant, ...], media: [...]}}` block.

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


def load_discovery_targets(sender: str) -> tuple[DiscoveryTarget, ...]:
    """Return strictly validated discovery-enabled ICPs for one sender.

    Discovery metadata lives alongside that sender's existing outbound
    channels in ``icp_messages.json``. A missing ``discovery`` block means
    disabled. Disabled blocks may omit a profile and queries; enabled blocks
    must provide both a non-empty profile and at least one explicit search
    query so the browser never invents an unbounded search.
    """
    messages = load_icp_messages(sender)
    targets: list[DiscoveryTarget] = []
    allowed_keys = {"enabled", "profile", "search_queries"}

    for icp, channels in messages.items():
        if not isinstance(channels, dict):
            raise DiscoveryConfigurationError(
                f"{sender}/{icp}: ICP block must be an object",
            )
        discovery = channels.get("discovery")
        if discovery is None:
            continue
        if not isinstance(discovery, dict):
            raise DiscoveryConfigurationError(
                f"{sender}/{icp}: discovery must be an object",
            )
        unknown = set(discovery) - allowed_keys
        if unknown:
            raise DiscoveryConfigurationError(
                f"{sender}/{icp}: unknown discovery keys {sorted(unknown)}",
            )

        enabled = discovery.get("enabled", False)
        if not isinstance(enabled, bool):
            raise DiscoveryConfigurationError(
                f"{sender}/{icp}: discovery.enabled must be a boolean",
            )

        profile = discovery.get("profile", "")
        if not isinstance(profile, str):
            raise DiscoveryConfigurationError(
                f"{sender}/{icp}: discovery.profile must be a string",
            )
        profile = profile.strip()

        raw_queries = discovery.get("search_queries", [])
        if not isinstance(raw_queries, list) or any(
            not isinstance(query, str) or not query.strip()
            for query in raw_queries
        ):
            raise DiscoveryConfigurationError(
                f"{sender}/{icp}: discovery.search_queries must contain "
                "only non-empty strings",
            )
        queries = tuple(dict.fromkeys(query.strip() for query in raw_queries))

        if not enabled:
            continue
        if not profile:
            raise DiscoveryConfigurationError(
                f"{sender}/{icp}: enabled discovery requires a non-empty profile",
            )
        if not queries:
            raise DiscoveryConfigurationError(
                f"{sender}/{icp}: enabled discovery requires search_queries",
            )
        targets.append(
            DiscoveryTarget(
                icp=icp,
                profile=profile,
                search_queries=queries,
            ),
        )

    return tuple(targets)


def discovery_search_queries(
    targets: tuple[DiscoveryTarget, ...],
) -> tuple[str, ...]:
    """Flatten sender targets into a stable, de-duplicated query sequence."""
    return tuple(
        dict.fromkeys(
            query
            for target in targets
            for query in target.search_queries
        ),
    )


def load_gmail_messages(sender: str) -> dict[str, list[dict[str, object]]]:
    """Return one sender's Gmail template block, or `{}` when absent."""
    if not _GMAIL_MESSAGES_PATH.exists():
        return {}
    by_sender = json.loads(_GMAIL_MESSAGES_PATH.read_text())
    return by_sender.get(sender, {})


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
    it surfaces the core ICP buckets only, and only the first variant
    for the connect note / follow-up step(s). Extra variants remain in
    JSON and are preserved on pull.
    Sequenced follow-ups render one step per `Followup Message N` column;
    the tab is sized to the longest sequence across the sender's buckets,
    so any number of steps round-trips.
    """
    messages = load_icp_messages(sender)
    gmail_messages = load_gmail_messages(sender)
    # Buckets may be sender-specific. Do not write label-only blank rows for
    # operators who are not participating in a campaign; the pull parser
    # correctly treats a non-empty ICP label with empty copy as malformed.
    sheet_icps = [
        icp for icp in ICP_MESSAGES_SHEET_BUCKETS
        if icp in messages or icp in gmail_messages
    ]
    steps_by_icp = {
        icp: _followup_step_firsts(messages.get(icp, {}).get("linkedin_connect_followup"))
        for icp in sheet_icps
    }
    email_steps_by_icp = {
        icp: _email_step_firsts(gmail_messages.get(icp))
        for icp in sheet_icps
    }
    n_linkedin = max((len(s) for s in steps_by_icp.values()), default=0)
    n_email = max((len(s) for s in email_steps_by_icp.values()), default=0)
    n_cols = max(n_linkedin, n_email, 1)
    rows: list[list[str]] = [icp_messages_headers(n_linkedin, n_email)]
    for icp in sheet_icps:
        channels = messages.get(icp, {})
        steps = steps_by_icp[icp]
        email_steps = email_steps_by_icp[icp]
        row = [
            icp,
            ((channels.get("linkedin_connect_note") or [""])[0] or "").strip(),
        ]
        for i in range(n_cols):
            subject, body = email_steps[i] if i < len(email_steps) else ("", "")
            row.extend([
                steps[i] if i < len(steps) else "",
                subject,
                body,
            ])
        rows.append(row)
    return rows


def _followup_step_firsts(raw) -> list[str]:
    """First variant of each follow-up step, for both JSON shapes.

    Legacy `["a", "b"]` → one step (`["a"]`). Sequenced
    `[{"variants": [...]}, ...]` → one entry per step. Empty/missing → `[]`.
    """
    if not raw:
        return []
    if isinstance(raw[0], dict):
        return [((item.get("variants") or [""])[0] or "").strip() for item in raw]
    return [(raw[0] or "").strip()]


def _email_step_firsts(raw) -> list[tuple[str, str]]:
    """First subject/body variant of each Gmail step."""
    if not raw:
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        subject = ((item.get("subject_variants") or [""])[0] or "").strip()
        body = ((item.get("body_variants") or [""])[0] or "").strip()
        out.append((subject, body))
    return out


def parse_icp_messages_sheet_rows(
    rows: list[list[str]],
) -> tuple[dict[str, dict[str, list[str]]], dict[str, list[dict[str, object]]]]:
    """Parse sheet rows back into LinkedIn and Gmail template blocks."""
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

    # Every `Followup Message [N]` column, ordered by step number. A bare
    # "Followup Message" header counts as step 1.
    followup_cols: list[tuple[int, int]] = []
    email_subject_cols: dict[int, int] = {}
    email_body_cols: dict[int, int] = {}
    for idx, header in enumerate(header_lower):
        m = _FOLLOWUP_HEADER_RE.match(header)
        if m:
            step_num = int(m.group(1)) if m.group(1) else 1
            followup_cols.append((step_num, idx))
            continue
        m = _EMAIL_SUBJECT_HEADER_RE.match(header)
        if m:
            step_num = int(m.group(1)) if m.group(1) else 1
            email_subject_cols[step_num] = idx
            continue
        m = _EMAIL_BODY_HEADER_RE.match(header)
        if m:
            step_num = int(m.group(1)) if m.group(1) else 1
            email_body_cols[step_num] = idx
    if not followup_cols:
        raise SheetsError("ICP messages tab missing required 'Followup Message' column")
    followup_idxs = [idx for _num, idx in sorted(followup_cols)]
    email_step_nums = sorted(set(email_subject_cols) | set(email_body_cols))
    has_email_columns = bool(email_step_nums)
    for step_num in email_step_nums:
        if step_num not in email_subject_cols or step_num not in email_body_cols:
            raise SheetsError(
                f"ICP messages tab must include both Email Subject {step_num} and Email Body {step_num}"
            )

    out: dict[str, dict[str, list[str]]] = {}
    gmail_out: dict[str, list[dict[str, object]]] = {}
    for row_num, row in enumerate(rows[1:], start=2):
        def cell(idx: int | None) -> str:
            if idx is None or idx >= len(row) or row[idx] is None:
                return ""
            return row[idx].strip()

        icp = cell(icp_idx)
        connect_message = cell(connect_idx)
        step_texts = [cell(i) for i in followup_idxs]
        email_steps = [
            (cell(email_subject_cols[step_num]), cell(email_body_cols[step_num]))
            for step_num in email_step_nums
        ]
        # Trailing empty step columns just mean this ICP's sequence is
        # shorter than the widest one on the tab.
        while step_texts and not step_texts[-1]:
            step_texts.pop()
        while email_steps and not email_steps[-1][0] and not email_steps[-1][1]:
            email_steps.pop()
        if not any((icp, connect_message, *step_texts, *[v for pair in email_steps for v in pair])):
            continue
        if not icp or not connect_message or not step_texts or not step_texts[0]:
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
        if len(step_texts) > 1:
            # Multiple steps → sequenced shape. Delays default here;
            # save_icp_messages() preserves existing per-step delays on merge.
            followup_value: list = [
                {"delay_hours": _default_step_delay(i), "variants": [text]}
                for i, text in enumerate(step_texts)
            ]
        else:
            followup_value = [step_texts[0]]
        out[icp] = {
            "linkedin_connect_note": [connect_message],
            "linkedin_connect_followup": followup_value,
        }
        rendered_email_steps = []
        for idx, (subject, body) in enumerate(email_steps):
            if not subject or not body:
                raise SheetsError(
                    f"ICP messages row {row_num} Email step {idx + 1} must include subject and body"
                )
            rendered_email_steps.append({
                "delay_hours": _default_email_step_delay(idx),
                "subject_variants": [subject],
                "body_variants": [body],
            })
        if rendered_email_steps:
            gmail_out[icp] = rendered_email_steps
        elif has_email_columns:
            # Blank email cells in an otherwise valid ICP row explicitly
            # disable that sender/ICP's Gmail lane on pull. Omitting the ICP
            # here would merge-preserve stale Gmail JSON copy.
            gmail_out[icp] = []
    if not out:
        raise SheetsError("ICP messages tab has no message rows")
    return out, gmail_out


def parse_icp_messages_rows(rows: list[list[str]]) -> dict[str, dict[str, list[str]]]:
    """Parse sheet rows back into one sender's LinkedIn JSON block."""
    return parse_icp_messages_sheet_rows(rows)[0]


def save_icp_messages(sender: str, block: dict[str, dict[str, list[str]]]) -> None:
    """Merge one sender's edited core ICPs into `icp_messages.json`.

    Only buckets present in `block` are replaced. Other ICPs and extra
    variants for untouched buckets remain as-is. Follow-up handling is
    delegated to `_reconcile_followup` so a sheet-edited sequence applies
    its text while preserving the existing JSON cadence, and a legacy
    single-cell pull never flattens a real multi-step sequence.
    """
    by_sender = json.loads(_MESSAGES_PATH.read_text())
    existing = by_sender.get(sender, {})
    merged = dict(existing)
    for icp, channels in block.items():
        existing_channels = existing.get(icp, {})
        merged_channels = dict(existing_channels)
        for channel, value in channels.items():
            if channel == "linkedin_connect_followup":
                value = _reconcile_followup(existing_channels.get(channel), value)
            merged_channels[channel] = value
        merged[icp] = merged_channels
    by_sender[sender] = merged
    _MESSAGES_PATH.write_text(json.dumps(by_sender, indent=2) + "\n")


def save_gmail_messages(sender: str, block: dict[str, list[dict[str, object]]]) -> None:
    """Merge one sender's edited Gmail templates into `gmail/icp_emails.json`.

    An explicit empty list means the Sheet intentionally disabled Gmail copy
    for that sender/ICP; preserve it so stale JSON does not keep sending.
    """
    if not _GMAIL_MESSAGES_PATH.exists():
        by_sender = {}
    else:
        by_sender = json.loads(_GMAIL_MESSAGES_PATH.read_text())
    existing = by_sender.get(sender, {})
    merged = dict(existing)
    for icp, value in block.items():
        if value == []:
            merged[icp] = []
        else:
            merged[icp] = _reconcile_gmail_followup(existing.get(icp), value)
    by_sender[sender] = merged
    _GMAIL_MESSAGES_PATH.write_text(json.dumps(by_sender, indent=2) + "\n")


def _reconcile_followup(current, new):
    """Merge a sheet-parsed follow-up value with the existing JSON value.

    `new` is what `parse_icp_messages_rows` produced — a legacy `[str]` when
    the sheet had one follow-up cell, or a sequenced `[{...}, ...]` when
    multiple `Followup Message N` cells were filled. The sheet never carries
    step delays, so:
      - sequenced sheet edit + sequenced JSON → keep new text, keep the
        existing per-step delays (sheet can't express cadence).
      - sequenced sheet edit + legacy/absent JSON → use new as-is (default
        delays from the parser).
      - legacy single cell + sequenced JSON → preserve the JSON sequence
        rather than flatten it (operator left the second cell blank).
      - otherwise → take new verbatim.
    """
    new_is_seq = isinstance(new, list) and bool(new) and isinstance(new[0], dict)
    cur_is_seq = isinstance(current, list) and bool(current) and isinstance(current[0], dict)
    if new_is_seq:
        if cur_is_seq:
            reconciled = []
            for idx, step in enumerate(new):
                if idx < len(current) and "delay_hours" in current[idx]:
                    delay = current[idx]["delay_hours"]
                else:
                    delay = step.get("delay_hours", 0)
                reconciled.append({"delay_hours": delay, "variants": step["variants"]})
            return reconciled
        return new
    if cur_is_seq:
        return current
    return new


def _reconcile_gmail_followup(current, new):
    cur_is_seq = isinstance(current, list) and bool(current) and isinstance(current[0], dict)
    reconciled = []
    for idx, step in enumerate(new):
        if cur_is_seq and idx < len(current) and "delay_hours" in current[idx]:
            delay = current[idx]["delay_hours"]
        else:
            delay = step.get("delay_hours", _default_email_step_delay(idx))
        reconciled.append({
            "delay_hours": delay,
            "subject_variants": step["subject_variants"],
            "body_variants": step["body_variants"],
        })
    return reconciled


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
      - sequence: [{"delay_hours": 0, "variants": ["..."]}, ...]
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
        return [TemplateStep(delay_hours=0, variants=list(raw))]

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
        raw_delay_hours = item.get("delay_hours", 0)
        try:
            delay_hours = float(raw_delay_hours)
        except (TypeError, ValueError) as e:
            raise SheetsError(
                f"icp_outbound: {icp!r}.{channel!r} step {idx} has invalid "
                f"delay_hours={raw_delay_hours!r}"
            ) from e
        if not isfinite(delay_hours):
            raise SheetsError(
                f"icp_outbound: {icp!r}.{channel!r} step {idx} has invalid "
                f"delay_hours={raw_delay_hours!r}"
            )
        if delay_hours < 0:
            raise SheetsError(
                f"icp_outbound: {icp!r}.{channel!r} step {idx} has negative "
                f"delay_hours={delay_hours}"
            )
        steps.append(TemplateStep(delay_hours=delay_hours, variants=variants))
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

    # IMPORTANT: extract media placeholders BEFORE str.format — otherwise
    # format() sees `{add demo.gif}` / `{demo.gif}` as named placeholders and
    # raises KeyError since there are no matching kwargs.
    template = variants[idx]
    stripped, attachments = _extract_attachments(
        template,
        media_names=_media_names_for_icp(sender=sender, icp=icp),
    )
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


def _media_names_for_icp(*, sender: str, icp: str) -> tuple[str, ...]:
    """Return the optional media registry for one sender × ICP block."""
    messages = load_icp_messages(sender)
    raw = messages.get(icp, {}).get("media", [])
    if raw in (None, ""):
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise SheetsError(
            f"icp_outbound: {icp!r}.media must be a list of filenames"
        )
    return tuple(item.strip() for item in raw if item.strip())


def _extract_attachments(
    template: str,
    *,
    media_names: tuple[str, ...] = (),
) -> tuple[str, list[Path]]:
    """Strip media placeholders from `template` and resolve each filename.

    Returns `(template_without_attach_placeholders, resolved_paths)`.
    Missing files are logged-and-dropped so a stale reference doesn't
    block the send.
    """
    attachments: list[Path] = []

    def _attach(filename: str) -> None:
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

    def _swap_add(match):
        _attach(match.group(1).strip())
        return ""

    stripped = _ATTACH_RE.sub(_swap_add, template)

    for filename in media_names:
        token_re = re.compile(r"\{\s*" + re.escape(filename) + r"\s*\}")

        def _swap_media(match):
            _attach(filename)
            return ""

        stripped = token_re.sub(_swap_media, stripped)

    return stripped, attachments


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
