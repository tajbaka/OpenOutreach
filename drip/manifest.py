from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any, Mapping

from drip.exceptions import ManifestValidationError


SCHEMA_VERSION = 1
ALLOWED_PLACEHOLDERS = frozenset(
    {
        "first_name",
        "last_name",
        "company_name",
        "my_name",
        "our_company_name",
        "our_website_url",
    },
)
_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_FORMATTER = Formatter()


@dataclass(frozen=True)
class ValidatedManifest:
    normalized: dict[str, Any]
    content_hash: str

    @property
    def campaign_key(self) -> str:
        return self.normalized["campaign_key"]

    @property
    def name(self) -> str:
        return self.normalized["name"]


def _fail(path: str, detail: str) -> None:
    raise ManifestValidationError(f"{path}: {detail}")


def _expect_object(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


def _expect_exact_keys(
    value: Mapping[str, Any],
    *,
    path: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required - optional)
    if missing:
        _fail(path, f"missing required key(s): {', '.join(missing)}")
    if unknown:
        _fail(path, f"unknown key(s): {', '.join(unknown)}")


def _nonblank_string(value: Any, *, path: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be a non-empty string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) > max_length:
        _fail(path, f"must be at most {max_length} characters")
    return normalized


def _validate_key(value: Any, *, path: str) -> str:
    key = _nonblank_string(value, path=path, max_length=100)
    if not _KEY_RE.fullmatch(key):
        _fail(path, "must start with a lowercase letter and contain only a-z, 0-9, _ or -")
    return key


def _placeholder_names(value: str, *, path: str) -> set[str]:
    try:
        parsed = list(_FORMATTER.parse(value))
    except ValueError as exc:
        _fail(path, f"contains invalid braces: {exc}")
    names: set[str] = set()
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not field_name:
            _fail(path, "uses a positional placeholder")
        if any(separator in field_name for separator in (".", "[", "]")):
            _fail(path, f"uses unsupported placeholder expression {{{field_name}}}")
        if format_spec or conversion:
            _fail(path, f"uses unsupported formatting on {{{field_name}}}")
        names.add(field_name)
    unknown = sorted(names - ALLOWED_PLACEHOLDERS)
    if unknown:
        _fail(path, f"uses unsupported placeholder(s): {', '.join(unknown)}")
    return names


def _normalize_step(
    value: Any,
    *,
    path: str,
    channel: str,
    step_index: int,
    gmail_thread_subject: str | None = None,
) -> dict[str, Any]:
    step = _expect_object(value, path=path)
    required = {"delay_days", "body"}
    optional: set[str] = set()
    if channel == "gmail" and step_index == 0:
        required.add("subject")
    elif channel == "gmail":
        optional.add("subject")
    _expect_exact_keys(step, path=path, required=required, optional=optional)

    delay = step["delay_days"]
    if isinstance(delay, bool) or not isinstance(delay, (int, float)):
        _fail(f"{path}.delay_days", "must be a finite nonnegative number")
    numeric_delay = float(delay)
    if not math.isfinite(numeric_delay) or numeric_delay < 0:
        _fail(f"{path}.delay_days", "must be a finite nonnegative number")

    normalized: dict[str, Any] = {
        "delay_days": int(numeric_delay) if numeric_delay.is_integer() else numeric_delay,
    }
    if channel == "gmail" and step_index == 0:
        subject = _nonblank_string(
            step["subject"],
            path=f"{path}.subject",
            max_length=998,
        )
        _placeholder_names(subject, path=f"{path}.subject")
        normalized["subject"] = subject
    elif channel == "gmail" and "subject" in step:
        subject = _nonblank_string(
            step["subject"],
            path=f"{path}.subject",
            max_length=998,
        )
        _placeholder_names(subject, path=f"{path}.subject")
        if subject != gmail_thread_subject:
            _fail(
                f"{path}.subject",
                "must be omitted or exactly match the first Gmail step subject",
            )
    body_limit = 8_000 if channel == "linkedin" else 100_000
    body = _nonblank_string(step["body"], path=f"{path}.body", max_length=body_limit)
    _placeholder_names(body, path=f"{path}.body")
    normalized["body"] = body
    return normalized


def _normalize_rendition(value: Any, *, path: str, channel: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        _fail(path, "must be a non-empty list when present; omit it for not-applicable")
    normalized: list[dict[str, Any]] = []
    gmail_thread_subject: str | None = None
    for index, step in enumerate(value):
        normalized_step = _normalize_step(
            step,
            path=f"{path}[{index}]",
            channel=channel,
            step_index=index,
            gmail_thread_subject=gmail_thread_subject,
        )
        if channel == "gmail" and index == 0:
            gmail_thread_subject = normalized_step["subject"]
        normalized.append(normalized_step)
    return normalized


def _canonical_icps() -> frozenset[str]:
    from linkedin.notifications.sheets import LEAD_ICP_BUCKETS

    return frozenset(LEAD_ICP_BUCKETS)


def _canonical_operators() -> frozenset[str]:
    from linkedin.operators import CANONICAL_OPERATOR_HANDLES

    return CANONICAL_OPERATOR_HANDLES


def validate_manifest(payload: Any) -> ValidatedManifest:
    root = _expect_object(payload, path="manifest")
    _expect_exact_keys(
        root,
        path="manifest",
        required={"schema_version", "campaign_key", "name", "audiences"},
    )
    if root["schema_version"] != SCHEMA_VERSION:
        _fail("manifest.schema_version", f"must equal {SCHEMA_VERSION}")

    campaign_key = _validate_key(root["campaign_key"], path="manifest.campaign_key")
    name = _nonblank_string(root["name"], path="manifest.name", max_length=200)
    raw_audiences = _expect_object(root["audiences"], path="manifest.audiences")
    if not raw_audiences:
        _fail("manifest.audiences", "must contain at least one canonical ICP")

    canonical_icps = _canonical_icps()
    canonical_operators = _canonical_operators()
    normalized_audiences: dict[str, Any] = {}
    for icp in sorted(raw_audiences):
        if icp not in canonical_icps:
            _fail(f"manifest.audiences.{icp}", "is not a canonical ICP")
        audience_path = f"manifest.audiences.{icp}"
        audience = _expect_object(raw_audiences[icp], path=audience_path)
        _expect_exact_keys(audience, path=audience_path, required={"themes"})
        raw_themes = audience["themes"]
        if not isinstance(raw_themes, list) or not raw_themes:
            _fail(f"{audience_path}.themes", "must be a non-empty list")

        normalized_themes: list[dict[str, Any]] = []
        theme_keys: set[str] = set()
        expected_senders: set[str] | None = None
        gmail_subject_by_sender: dict[str, str] = {}
        for theme_index, raw_theme in enumerate(raw_themes):
            theme_path = f"{audience_path}.themes[{theme_index}]"
            theme = _expect_object(raw_theme, path=theme_path)
            _expect_exact_keys(
                theme,
                path=theme_path,
                required={"key", "intent", "senders"},
            )
            theme_key = _validate_key(theme["key"], path=f"{theme_path}.key")
            if theme_key in theme_keys:
                _fail(f"{theme_path}.key", f"duplicates theme key {theme_key!r}")
            theme_keys.add(theme_key)
            intent = _nonblank_string(theme["intent"], path=f"{theme_path}.intent", max_length=2_000)
            raw_senders = _expect_object(theme["senders"], path=f"{theme_path}.senders")
            if not raw_senders:
                _fail(f"{theme_path}.senders", "must contain at least one canonical sender")
            sender_keys = set(raw_senders)
            unknown_senders = sorted(sender_keys - canonical_operators)
            if unknown_senders:
                _fail(
                    f"{theme_path}.senders",
                    f"contains non-canonical sender(s): {', '.join(unknown_senders)}",
                )
            if expected_senders is None:
                expected_senders = sender_keys
            elif sender_keys != expected_senders:
                _fail(
                    f"{theme_path}.senders",
                    "must contain the same canonical sender set as every other theme in this audience",
                )

            normalized_senders: dict[str, Any] = {}
            for sender in sorted(raw_senders):
                sender_path = f"{theme_path}.senders.{sender}"
                sender_block = _expect_object(raw_senders[sender], path=sender_path)
                _expect_exact_keys(
                    sender_block,
                    path=sender_path,
                    required=set(),
                    optional={"linkedin", "gmail"},
                )
                if not sender_block:
                    _fail(sender_path, "must define LinkedIn, Gmail, or both")
                normalized_sender: dict[str, Any] = {}
                for channel in ("linkedin", "gmail"):
                    if channel in sender_block:
                        rendition = _normalize_rendition(
                            sender_block[channel],
                            path=f"{sender_path}.{channel}",
                            channel=channel,
                        )
                        if channel == "gmail":
                            subject = rendition[0]["subject"]
                            existing_subject = gmail_subject_by_sender.get(sender)
                            if existing_subject is not None and subject != existing_subject:
                                _fail(
                                    f"{sender_path}.gmail[0].subject",
                                    "must exactly match this sender's first Gmail "
                                    "subject in the audience because one lane uses "
                                    "one thread",
                                )
                            gmail_subject_by_sender.setdefault(sender, subject)
                        normalized_sender[channel] = rendition
                normalized_senders[sender] = normalized_sender

            normalized_themes.append(
                {
                    "key": theme_key,
                    "intent": intent,
                    "senders": normalized_senders,
                },
            )
        normalized_audiences[icp] = {"themes": normalized_themes}

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "campaign_key": campaign_key,
        "name": name,
        "audiences": normalized_audiences,
    }
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ValidatedManifest(
        normalized=normalized,
        content_hash=hashlib.sha256(encoded).hexdigest(),
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestValidationError(f"manifest: duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_manifest(path: str | Path) -> ValidatedManifest:
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise ManifestValidationError(f"Manifest file does not exist: {manifest_path}")
    try:
        payload = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except UnicodeDecodeError as exc:
        raise ManifestValidationError(f"Manifest is not valid UTF-8: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(
            f"Manifest is not valid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
        ) from exc
    return validate_manifest(payload)


def render_template(template: str, context: Mapping[str, Any]) -> str:
    names = _placeholder_names(template, path="template")
    missing = sorted(name for name in names if name not in context)
    if missing:
        raise ManifestValidationError(
            f"template: missing render value(s): {', '.join(missing)}",
        )
    rendered = template.format_map({name: str(context[name]) for name in names})
    if any(_placeholder_names(rendered, path="rendered template")):
        raise ManifestValidationError("rendered template still contains placeholders")
    return rendered
