"""Conservative one-time import from legacy sender Followups tabs.

Legacy rows have no canonical Action ID.  This module therefore accepts a row
only when stable evidence (email, exact LinkedIn profile URL, or a stored
message-thread URL) resolves to one Lead and that Lead has exactly one current
OpportunityAction for the explicitly supplied owner.  Names are never used.

The worksheet is read with formula values and is never mutated.  Each accepted
row is committed in its own database transaction.  No messages are sent and
``sent_at`` is never synthesized.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from gspread.exceptions import APIError
from gspread.utils import ValueRenderOption

from linkedin.exceptions import SheetsError
from linkedin.notifications import sheets


_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
_QUOTED_FORMULA_VALUE_RE = re.compile(r'"((?:""|[^"])*)"')
_URL_RE = re.compile(r"https?://[^\s\"')]+", re.IGNORECASE)

_EMAIL_HEADERS = (
    sheets.COL_EMAILS,
    "Email",
    "Email address",
    "Email Address",
    sheets.FU_COL_EMAIL_LINK,
)
_LINK_HEADERS = (
    sheets.COL_LINKEDIN_URL,
    "LinkedIn profile",
    "LinkedIn Profile",
    sheets.FU_COL_LINKEDIN_MSG_URL,
)
_CURRENT_ACTION_STATUSES = ("open", "waiting")


@dataclass(frozen=True)
class LegacyFollowupSkip:
    row_number: int
    reason: str
    material: bool = False


@dataclass
class LegacyFollowupMigrationReport:
    dry_run: bool
    rows_seen: int = 0
    rows_resolved: int = 0
    rows_changed: int = 0
    rows_unchanged: int = 0
    actions_marked_sent: int = 0
    leads_disqualified: int = 0
    drafts_imported: int = 0
    drafts_preserved: int = 0
    skips: list[LegacyFollowupSkip] = field(default_factory=list)

    def skip(
        self,
        row_number: int,
        reason: str,
        *,
        material: bool = False,
    ) -> None:
        self.skips.append(LegacyFollowupSkip(row_number, reason, material))

    @property
    def material_rows_skipped(self) -> int:
        return len({item.row_number for item in self.skips if item.material})

    @property
    def material_skip_reasons(self) -> dict[str, int]:
        reasons = Counter(item.reason for item in self.skips if item.material)
        return dict(sorted(reasons.items()))

    def counts(self) -> dict[str, Any]:
        reasons = Counter(item.reason for item in self.skips)
        return {
            "dry_run": self.dry_run,
            "rows_seen": self.rows_seen,
            "rows_resolved": self.rows_resolved,
            "rows_changed": self.rows_changed,
            "rows_unchanged": self.rows_unchanged,
            "actions_marked_sent": self.actions_marked_sent,
            "leads_disqualified": self.leads_disqualified,
            "drafts_imported": self.drafts_imported,
            "drafts_preserved": self.drafts_preserved,
            "skipped": len(self.skips),
            "skip_reasons": dict(sorted(reasons.items())),
            "material_rows_skipped": self.material_rows_skipped,
            "material_skip_reasons": self.material_skip_reasons,
            "skip_rows": [
                {
                    "row": item.row_number,
                    "reason": item.reason,
                    "material": item.material,
                }
                for item in self.skips
            ],
        }


@dataclass(frozen=True)
class _IdentityIndex:
    email_to_lead_ids: Mapping[str, frozenset[int]]
    profile_to_lead_ids: Mapping[str, frozenset[int]]
    thread_to_lead_ids: Mapping[str, frozenset[int]]


@dataclass(frozen=True)
class _RowPlan:
    row_number: int
    lead_id: int
    action_id: Any
    sent_channels: tuple[str, ...]
    disqualify: bool
    draft_channel: str
    draft: str


def migrate_legacy_followup_tab(
    worksheet,
    *,
    owner: Any,
    desired_rows: Iterable[Mapping[str, Any]] | None = None,
    dry_run: bool = True,
) -> LegacyFollowupMigrationReport:
    """Import safe legacy row state without changing the legacy worksheet.

    ``owner`` must be a concrete SalesOwner instance or exact owner handle.
    When refresh orchestration already has canonical sender rows, pass them as
    ``desired_rows``; the resolved current Action must also be the sole Action
    ID published for that Lead/owner. The returned report contains row
    numbers/reasons but no names, emails, profile URLs, draft text, or PII.
    """
    from crm.models import Lead, OpportunityAction

    resolved_owner = _resolve_owner(owner)
    rows = _read_formula_rows(worksheet)
    report = LegacyFollowupMigrationReport(dry_run=dry_run, rows_seen=len(rows))
    identity_index = _build_identity_index()
    desired_action_ids = _desired_action_ids_by_lead(
        desired_rows,
        owner_handle=resolved_owner.handle,
    )

    plans: list[_RowPlan] = []
    for row_number, row in rows:
        sent_channels = tuple(
            channel
            for channel, column in (
                ("email", sheets.FU_COL_SENT_EMAIL),
                ("linkedin", sheets.FU_COL_SENT_LINKEDIN),
            )
            if _truthy_toggle(row.get(column, ""))
        )
        disqualify = _is_disqualify(row.get(sheets.FU_COL_QUALIFY, ""))
        drafts = [
            (channel, str(row.get(column, "") or "").strip())
            for channel, column in (
                ("email", sheets.FU_COL_DRAFT_EMAIL),
                ("linkedin", sheets.FU_COL_DRAFT_LINKEDIN),
            )
            if str(row.get(column, "") or "").strip()
        ]
        material = bool(sent_channels or disqualify or drafts)
        if not material:
            report.skip(row_number, "no_explicit_changes")
            continue
        if len(sent_channels) > 1:
            report.skip(row_number, "multiple_sent_channels", material=True)
            continue
        draft_channel = ""
        draft = ""
        if not sent_channels:
            if len(drafts) > 1:
                report.skip(row_number, "multiple_unsent_drafts", material=True)
                continue
            if drafts:
                draft_channel, draft = drafts[0]

        lead_id, identity_error = _resolve_lead_id(row, identity_index)
        if identity_error:
            report.skip(row_number, identity_error, material=True)
            continue

        action_ids = _current_action_ids(
            lead_id=lead_id,
            owner_id=resolved_owner.id,
        )
        if not action_ids:
            report.skip(row_number, "no_current_action", material=True)
            continue
        if len(action_ids) > 1:
            report.skip(row_number, "ambiguous_current_action", material=True)
            continue
        if desired_action_ids is not None:
            published_ids = desired_action_ids.get(lead_id, frozenset())
            if not published_ids:
                report.skip(row_number, "action_not_in_desired_rows", material=True)
                continue
            if len(published_ids) > 1:
                report.skip(row_number, "ambiguous_desired_action", material=True)
                continue
            if action_ids[0] not in published_ids:
                report.skip(row_number, "action_not_in_desired_rows", material=True)
                continue
        plans.append(_RowPlan(
            row_number=row_number,
            lead_id=lead_id,
            action_id=action_ids[0],
            sent_channels=sent_channels,
            disqualify=disqualify,
            draft_channel=draft_channel,
            draft=draft,
        ))

    duplicate_action_ids = {
        action_id
        for action_id, count in Counter(plan.action_id for plan in plans).items()
        if count > 1
    }
    unique_plans = []
    for plan in plans:
        if plan.action_id in duplicate_action_ids:
            report.skip(plan.row_number, "duplicate_action_rows", material=True)
        else:
            unique_plans.append(plan)

    for plan in unique_plans:
        with transaction.atomic():
            # Lock canonical records and revalidate the mapping so a concurrent
            # recalculation cannot turn a safe plan into a write to stale work.
            action = OpportunityAction.objects.select_for_update().get(pk=plan.action_id)
            lead = Lead.objects.select_for_update().get(pk=plan.lead_id)
            if not _action_is_current_for_lead_owner(
                action_id=action.id,
                lead_id=lead.id,
                owner_id=resolved_owner.id,
            ):
                report.skip(
                    plan.row_number,
                    "action_changed_after_planning",
                    material=True,
                )
                continue

            report.rows_resolved += 1
            action_fields: set[str] = set()
            system_action_fields: set[str] = set()
            lead_changed = False
            marked_sent = False
            draft_imported = False
            noop_reason = ""

            if action.target_lead_id is None:
                # The row was resolved through exact stable evidence and the
                # current action mapping was revalidated as unambiguous.  Make
                # that recipient durable before importing any human state.
                action.target_lead_id = lead.id
                system_action_fields.add("target_lead")

            if plan.sent_channels:
                target_channel = ",".join(plan.sent_channels)
                if action.channel != target_channel:
                    action.channel = target_channel
                    action_fields.add("channel")
                if action.disposition != OpportunityAction.Disposition.SENT:
                    action.disposition = OpportunityAction.Disposition.SENT
                    action_fields.add("disposition")
                if action.status != OpportunityAction.Status.COMPLETED:
                    action.status = OpportunityAction.Status.COMPLETED
                    action.completed_at = action.completed_at or timezone.now()
                    action_fields.update({"status", "completed_at"})
                marked_sent = bool(action_fields)
            elif plan.draft:
                if action.draft:
                    report.drafts_preserved += 1
                    noop_reason = "existing_draft"
                elif action.channel and action.channel != plan.draft_channel:
                    noop_reason = "existing_channel"
                else:
                    if action.channel != plan.draft_channel:
                        action.channel = plan.draft_channel
                        action_fields.add("channel")
                    action.draft = plan.draft
                    action_fields.add("draft")
                    draft_imported = True

            if plan.disqualify and not lead.disqualified:
                lead.disqualified = True
                lead_changed = True

            if action_fields:
                action.human_revision += 1
                action_fields.add("human_revision")
            # Target linkage is system-owned canonical identity. Persisting it
            # must not manufacture a human revision or make an otherwise
            # unresolved legacy row look successfully imported.
            changed = bool(action_fields or lead_changed)
            if dry_run:
                # No model save occurs. The transaction is still scoped per
                # row so this path exercises the same locks/revalidation.
                pass
            else:
                save_fields = action_fields | system_action_fields
                if save_fields:
                    action.save(update_fields={*save_fields, "updated_at"})
                if lead_changed:
                    lead.save(update_fields={"disqualified", "update_date"})

            if noop_reason:
                # A pre-existing canonical draft/channel is preserved, but the
                # legacy material remains unresolved and must block archival.
                report.skip(plan.row_number, noop_reason, material=True)
            if changed:
                report.rows_changed += 1
                report.actions_marked_sent += int(marked_sent)
                report.leads_disqualified += int(lead_changed)
                report.drafts_imported += int(draft_imported)
            else:
                report.rows_unchanged += 1
                if not noop_reason:
                    report.skip(plan.row_number, "already_applied")
    return report


def _read_formula_rows(worksheet) -> list[tuple[int, dict[str, str]]]:
    try:
        try:
            values = worksheet.get_all_values(
                value_render_option=ValueRenderOption.formula,
            )
        except TypeError:
            values = worksheet.get_all_values()
    except APIError as exc:
        raise SheetsError(
            f"failed reading legacy Followups tab {getattr(worksheet, 'title', '')!r}: {exc}"
        ) from exc
    if not values:
        return []
    headers = [str(value or "").strip() for value in values[0]]
    duplicates = [
        header
        for header, count in Counter(item for item in headers if item).items()
        if count > 1
    ]
    if duplicates:
        raise SheetsError(f"legacy Followups tab has duplicate headers: {duplicates}")
    if not any(header in headers for header in (*_EMAIL_HEADERS, *_LINK_HEADERS)):
        raise SheetsError("legacy Followups tab has no stable identity evidence column")

    rows = []
    for row_number, raw in enumerate(values[1:], start=2):
        if not any(str(value or "").strip() for value in raw):
            continue
        row = {
            header: str(raw[index] or "") if index < len(raw) else ""
            for index, header in enumerate(headers)
            if header
        }
        rows.append((row_number, row))
    return rows


def _resolve_owner(owner: Any):
    from crm.models import SalesOwner

    if isinstance(owner, SalesOwner):
        queryset = SalesOwner.objects.filter(pk=owner.pk, active=True)
    else:
        handle = str(owner or "").strip()
        if not handle:
            raise ValueError("explicit owner is required")
        queryset = SalesOwner.objects.filter(handle__iexact=handle, active=True)
    matches = list(queryset[:2])
    if len(matches) != 1:
        raise ValueError("explicit owner must resolve to exactly one active SalesOwner")
    return matches[0]


def _build_identity_index() -> _IdentityIndex:
    from crm.models import Lead, Message

    emails: dict[str, set[int]] = defaultdict(set)
    profiles: dict[str, set[int]] = defaultdict(set)
    threads: dict[str, set[int]] = defaultdict(set)
    for lead_id, email, linkedin_url in Lead.objects.values_list(
        "id", "email", "linkedin_url"
    ):
        normalized_email = _normalize_email(email)
        normalized_profile = _normalize_profile_url(linkedin_url)
        if normalized_email:
            emails[normalized_email].add(lead_id)
        if normalized_profile:
            profiles[normalized_profile].add(lead_id)

    for lead_id, thread_external_id, raw in Message.objects.values_list(
        "lead_id", "thread_external_id", "raw"
    ):
        candidates = set(_stored_thread_urls(raw))
        external_id = str(thread_external_id or "").strip()
        if external_id:
            if external_id.startswith(("http://", "https://")):
                candidates.add(external_id)
            else:
                candidates.add(sheets.linkedin_thread_url(external_id))
        for candidate in candidates:
            normalized = _normalize_url(candidate)
            if normalized:
                threads[normalized].add(lead_id)
    return _IdentityIndex(
        email_to_lead_ids={key: frozenset(value) for key, value in emails.items()},
        profile_to_lead_ids={key: frozenset(value) for key, value in profiles.items()},
        thread_to_lead_ids={key: frozenset(value) for key, value in threads.items()},
    )


def _resolve_lead_id(
    row: Mapping[str, str],
    index: _IdentityIndex,
) -> tuple[int | None, str | None]:
    emails: set[str] = set()
    urls: set[str] = set()
    for header in _EMAIL_HEADERS:
        emails.update(_emails_from_cell(row.get(header, "")))
    for header in _LINK_HEADERS:
        urls.update(_urls_from_cell(row.get(header, "")))

    evidence_sets: list[frozenset[int]] = []
    unmatched = False
    for email in sorted(emails):
        matches = index.email_to_lead_ids.get(email, frozenset())
        unmatched = unmatched or not matches
        if matches:
            evidence_sets.append(matches)
    for url in sorted(urls):
        profile = _normalize_profile_url(url)
        if profile:
            matches = index.profile_to_lead_ids.get(profile, frozenset())
        else:
            matches = index.thread_to_lead_ids.get(_normalize_url(url), frozenset())
        unmatched = unmatched or not matches
        if matches:
            evidence_sets.append(matches)

    if not emails and not urls:
        return None, "no_stable_identity"
    if unmatched:
        return None, "identity_evidence_unmatched"
    if not evidence_sets:
        return None, "identity_evidence_unmatched"
    candidates = set(evidence_sets[0])
    for matches in evidence_sets[1:]:
        candidates.intersection_update(matches)
    if not candidates:
        return None, "conflicting_identity"
    if len(candidates) != 1:
        return None, "ambiguous_identity"
    return next(iter(candidates)), None


def _current_action_ids(*, lead_id: int, owner_id: Any) -> list[Any]:
    from crm.models import OpportunityAction

    candidates = list(
        OpportunityAction.objects.filter(
            opportunity__owner_id=owner_id,
            status__in=_CURRENT_ACTION_STATUSES,
        )
        .filter(
            Q(target_lead_id=lead_id)
            | Q(trigger_message__lead_id=lead_id)
            | Q(trigger_meeting__lead_id=lead_id)
            | Q(opportunity__contacts__lead_id=lead_id)
        )
        .select_related("trigger_message", "trigger_meeting", "target_lead")
        .prefetch_related("opportunity__contacts")
        .distinct()[:10]
    )
    return [
        action.id
        for action in candidates
        if _action_targets_lead(action, lead_id=lead_id)
    ][:3]


def _desired_action_ids_by_lead(
    desired_rows: Iterable[Mapping[str, Any]] | None,
    *,
    owner_handle: str,
) -> Mapping[int, frozenset[Any]] | None:
    if desired_rows is None:
        return None
    from uuid import UUID

    from linkedin.notifications import crm_sheets

    by_lead: dict[int, set[Any]] = defaultdict(set)
    for row in desired_rows:
        row_owner = str(row.get(crm_sheets.COL_OWNER, "") or "").strip()
        if row_owner and row_owner.casefold() != owner_handle.casefold():
            continue
        try:
            lead_id = int(str(row.get(crm_sheets.COL_LEAD_ID, "") or "").strip())
            action_id = UUID(
                str(row.get(crm_sheets.COL_ACTION_ID, "") or "").strip()
            )
        except (TypeError, ValueError):
            continue
        by_lead[lead_id].add(action_id)
    return {key: frozenset(value) for key, value in by_lead.items()}


def _action_is_current_for_lead_owner(
    *,
    action_id: Any,
    lead_id: int,
    owner_id: Any,
) -> bool:
    from crm.models import OpportunityAction

    action = OpportunityAction.objects.filter(
        pk=action_id,
        opportunity__owner_id=owner_id,
        status__in=_CURRENT_ACTION_STATUSES,
    ).select_related(
        "trigger_message",
        "trigger_meeting",
        "target_lead",
    ).prefetch_related("opportunity__contacts").first()
    return bool(action is not None and _action_targets_lead(action, lead_id=lead_id))


def _action_targets_lead(action, *, lead_id: int) -> bool:
    if action.target_lead_id is not None:
        return action.target_lead_id == lead_id
    if action.trigger_message_id is not None:
        return action.trigger_message.lead_id == lead_id
    if action.trigger_meeting_id is not None:
        return action.trigger_meeting.lead_id == lead_id
    contact_ids = {
        contact.lead_id
        for contact in action.opportunity.contacts.all()
    }
    return contact_ids == {lead_id}


def _formula_strings(value: Any) -> tuple[str, ...]:
    text = str(value or "").strip()
    if not text.startswith("="):
        return ()
    return tuple(
        match.replace('""', '"')
        for match in _QUOTED_FORMULA_VALUE_RE.findall(text)
    )


def _emails_from_cell(value: Any) -> set[str]:
    text = str(value or "").strip()
    candidates = [text, unquote(text), *_formula_strings(text)]
    emails = set()
    for candidate in candidates:
        for match in _EMAIL_RE.findall(unquote(candidate)):
            normalized = _normalize_email(match)
            if normalized:
                emails.add(normalized)
    return emails


def _urls_from_cell(value: Any) -> set[str]:
    text = str(value or "").strip()
    candidates = [text, *_formula_strings(text)]
    urls = set()
    for candidate in candidates:
        urls.update(_URL_RE.findall(candidate))
    return urls


def _normalize_email(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return text if _EMAIL_RE.fullmatch(text) else ""


def _normalize_profile_url(value: Any) -> str:
    normalized = _normalize_url(value)
    if not normalized:
        return ""
    parts = urlsplit(normalized)
    host = (parts.hostname or "").casefold()
    if host not in {"linkedin.com", "www.linkedin.com"}:
        return ""
    path = parts.path.rstrip("/")
    if not path.casefold().startswith("/in/") or len(path.split("/")) != 3:
        return ""
    return f"https://www.linkedin.com{path}"


def _normalize_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        return ""
    host = parts.hostname.casefold()
    if host == "linkedin.com":
        host = "www.linkedin.com"
    path = parts.path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    fragment = f"#{parts.fragment}" if parts.fragment else ""
    return f"https://{host}{path}{query}{fragment}"


def _stored_thread_urls(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key).replace("_", "").casefold()
            if normalized_key == "threadurl" and isinstance(item, str):
                yield item
            elif isinstance(item, (Mapping, list, tuple)):
                yield from _stored_thread_urls(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _stored_thread_urls(item)


def _truthy_toggle(value: Any) -> bool:
    return str(value or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "y",
        "sent",
        "checked",
        "✓",
        "x",
    }


def _is_disqualify(value: Any) -> bool:
    return str(value or "").strip().casefold().startswith("disq")
