"""Conservative two-way synchronization for the curated Trello pipeline.

The broad CRM radar deliberately stays in Google Sheets.  An Opportunity only
appears on Trello after a human gives it a nonblank ``pipeline_stage``.  Once a
card exists, its list is a supported human stage input; names and descriptions
remain compact, system-managed projections.

Identity never comes from a mutable card title.  It is proved by both a
durable database mapping and a machine-readable Opportunity UUID footer.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID

from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from crm.models import (
    Opportunity,
    OpportunityAction,
    OpportunityPipelineEvent,
    OpportunityTrelloState,
)
from linkedin.exceptions import TrelloConflictError, TrelloResponseError


PIPELINE_LISTS: tuple[tuple[str, str], ...] = (
    (Opportunity.PipelineStage.TRIAGE, "Potential / Triage"),
    (Opportunity.PipelineStage.DISCOVERY, "Discovery"),
    (Opportunity.PipelineStage.DEMO_EVALUATION, "Demo / Evaluation"),
    (Opportunity.PipelineStage.PILOT_VALIDATION, "Pilot / Validation"),
    (
        Opportunity.PipelineStage.COMMERCIAL_PROCUREMENT,
        "Commercial / Procurement",
    ),
    (Opportunity.PipelineStage.NURTURE_LATER, "Nurture / Later"),
    (Opportunity.PipelineStage.CLOSED_WON, "Closed Won"),
    (Opportunity.PipelineStage.CLOSED_LOST, "Closed Lost"),
)
STAGE_TO_LIST_NAME = dict(PIPELINE_LISTS)
LIST_NAME_TO_STAGE = {name: stage for stage, name in PIPELINE_LISTS}

_FOOTER_LABEL = "OpenOutreach Opportunity ID:"
_FOOTER_RE = re.compile(
    rf"(?:^|\n)\s*{re.escape(_FOOTER_LABEL)}\s*"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})\s*\Z"
)
_MANAGED_SNAPSHOT_FIELDS = ("name", "desc", "list_id")
_LOCAL_SYNC_LOCK = threading.Lock()


@dataclass(frozen=True)
class TrelloSnapshot:
    board: Mapping[str, Any]
    lists: tuple[Mapping[str, Any], ...]
    cards: tuple[Mapping[str, Any], ...]
    fingerprint: str


@dataclass(frozen=True)
class PlannedCard:
    opportunity_id: UUID
    card_id: str
    list_id: str
    stage: str
    name: str
    description: str
    create: bool
    update_fields: Mapping[str, Any]
    db_stage_before: str
    event_source: str
    baseline_stage: str


@dataclass(frozen=True)
class PipelinePlan:
    cards: tuple[PlannedCard, ...]
    selected_count: int
    board_card_count: int
    database_fingerprint: str


@dataclass(frozen=True)
class PipelineSyncReport:
    mode: str
    selected_opportunities: int
    board_cards: int
    lists_missing: int
    lists_created: int
    cards_to_create: int
    cards_to_update: int
    stages_from_trello: int
    stages_to_trello: int
    mappings_to_create: int
    unchanged_cards: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "selected_opportunities": self.selected_opportunities,
            "board_cards": self.board_cards,
            "lists_missing": self.lists_missing,
            "lists_created": self.lists_created,
            "cards_to_create": self.cards_to_create,
            "cards_to_update": self.cards_to_update,
            "stages_from_trello": self.stages_from_trello,
            "stages_to_trello": self.stages_to_trello,
            "mappings_to_create": self.mappings_to_create,
            "unchanged_cards": self.unchanged_cards,
        }


@transaction.atomic
def sync_trello_pipeline(
    *,
    client: Any,
    board_id: str,
    apply: bool = False,
    bootstrap_lists: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Plan or apply a safe Trello pipeline synchronization.

    Dry-run is the default and performs no Trello or database writes.  Apply
    always reads Trello and the relevant DB records a second time immediately
    before writing.  Any drift, ambiguous identity, or three-way merge conflict
    aborts the whole plan before the first card mutation.
    """

    clean_board_id = str(board_id or "").strip()
    if not clean_board_id:
        raise TrelloResponseError("A Trello pipeline board ID is required.")
    with _trello_pipeline_lock(clean_board_id):
        return _sync_locked(
            client=client,
            board_id=clean_board_id,
            apply=apply,
            bootstrap_lists=bootstrap_lists,
            now=now,
        )


def _sync_locked(
    *,
    client: Any,
    board_id: str,
    apply: bool,
    bootstrap_lists: bool,
    now: datetime | None,
) -> dict[str, Any]:
    sync_time = now or timezone.now()
    if timezone.is_naive(sync_time):
        sync_time = timezone.make_aware(sync_time, timezone.get_current_timezone())

    initial = _read_snapshot(client, board_id)
    list_ids, missing = _resolve_lists(initial)
    lists_created = 0

    if missing:
        if not bootstrap_lists:
            raise TrelloConflictError(
                "The Trello pipeline is missing required lists; rerun with "
                "--bootstrap-lists after reviewing the board."
            )
        _validate_existing_cards_are_keyed(initial, list_ids)
        if not apply:
            return PipelineSyncReport(
                mode="dry-run",
                selected_opportunities=_selected_opportunity_count(),
                board_cards=len(initial.cards),
                lists_missing=len(missing),
                lists_created=0,
                cards_to_create=0,
                cards_to_update=0,
                stages_from_trello=0,
                stages_to_trello=0,
                mappings_to_create=0,
                unchanged_cards=0,
            ).as_dict()

        # Bootstrap is an explicit operation, but still gets a compare-before-
        # write read.  Unknown/duplicate lists were rejected by _resolve_lists.
        compared = _read_snapshot(client, board_id)
        if compared.fingerprint != initial.fingerprint:
            raise TrelloConflictError(
                "The Trello board changed during list bootstrap; no lists were created."
            )
        for _stage, list_name in PIPELINE_LISTS:
            if list_name in missing:
                client.create_list(board_id, name=list_name, position="bottom")
                lists_created += 1
        initial = _read_snapshot(client, board_id)
        list_ids, still_missing = _resolve_lists(initial)
        if still_missing:
            raise TrelloResponseError(
                "Trello did not return all required lists after bootstrap."
            )

    plan = _build_plan(initial, board_id, list_ids)
    report = _report(plan, apply=apply, missing=missing, lists_created=lists_created)
    if not apply:
        return report.as_dict()

    # The second read is deliberately done after all validation and planning.
    # No remote or DB write is allowed if either side drifted since the plan.
    compared = _read_snapshot(client, board_id)
    if compared.fingerprint != initial.fingerprint:
        raise TrelloConflictError(
            "The Trello board changed after planning; no cards were written."
        )
    compared_plan = _build_plan(compared, board_id, list_ids, lock=True)
    if compared_plan.database_fingerprint != plan.database_fingerprint:
        raise TrelloConflictError(
            "The CRM pipeline changed after planning; no cards were written."
        )

    for card in compared_plan.cards:
        if card.create:
            client.create_card(
                list_id=card.list_id,
                name=card.name,
                description=card.description,
            )
        elif card.update_fields:
            client.update_card(card.card_id, **dict(card.update_fields))

    final_snapshot = _read_snapshot(client, board_id)
    final_list_ids, final_missing = _resolve_lists(final_snapshot)
    if final_missing or final_list_ids != list_ids:
        raise TrelloConflictError(
            "Trello list identity changed while cards were being synchronized."
        )
    final_cards = _validate_final_projection(final_snapshot, compared_plan, list_ids)
    _persist_plan(
        compared_plan,
        final_cards=final_cards,
        board_id=board_id,
        synced_at=sync_time,
    )
    return report.as_dict()


@contextmanager
def _trello_pipeline_lock(board_id: str):
    """Serialize a board sync across hosts (and within SQLite test runs)."""

    if connection.vendor != "postgresql":
        if not _LOCAL_SYNC_LOCK.acquire(blocking=False):
            raise TrelloConflictError("Another Trello pipeline sync is running.")
        try:
            yield
        finally:
            _LOCAL_SYNC_LOCK.release()
        return

    digest = hashlib.blake2b(
        f"openoutreach:trello-pipeline:{board_id}".encode("utf-8"),
        digest_size=8,
    ).digest()
    lock_key = int.from_bytes(digest, byteorder="big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [lock_key])
        acquired = bool(cursor.fetchone()[0])
    if not acquired:
        raise TrelloConflictError("Another Trello pipeline sync is running.")
    yield


def _read_snapshot(client: Any, board_id: str) -> TrelloSnapshot:
    board = client.get_board(board_id)
    if bool(board.get("closed")):
        raise TrelloConflictError("The configured Trello board is closed.")
    lists = tuple(client.list_open_lists(board_id))
    cards = tuple(client.list_open_cards(board_id))
    list_ids = [str(item.get("id") or "") for item in lists]
    card_ids = [str(item.get("id") or "") for item in cards]
    if len(list_ids) != len(set(list_ids)) or len(card_ids) != len(set(card_ids)):
        raise TrelloResponseError("Trello returned duplicate stable IDs.")
    payload = {
        "board": _normalized_remote_object(board),
        "lists": sorted(
            (_normalized_remote_object(item) for item in lists),
            key=lambda item: str(item.get("id") or ""),
        ),
        "cards": sorted(
            (_normalized_remote_object(item) for item in cards),
            key=lambda item: str(item.get("id") or ""),
        ),
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return TrelloSnapshot(
        board=board,
        lists=lists,
        cards=cards,
        fingerprint=fingerprint,
    )


def _normalized_remote_object(value: Mapping[str, Any]) -> dict[str, Any]:
    # JSON round-tripping both normalizes tuple/list values and refuses opaque
    # SDK objects that could make compare-before-write silently unreliable.
    try:
        normalized = json.loads(json.dumps(dict(value), sort_keys=True, default=str))
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise TrelloResponseError("Trello returned an unserializable object.") from exc
    if not isinstance(normalized, dict):  # pragma: no cover - dict() above
        raise TrelloResponseError("Trello returned an invalid object.")
    return normalized


def _resolve_lists(snapshot: TrelloSnapshot) -> tuple[dict[str, str], set[str]]:
    ids_by_name: dict[str, str] = {}
    unknown = 0
    duplicate = 0
    for item in snapshot.lists:
        name = str(item.get("name") or "")
        list_id = str(item.get("id") or "")
        if name not in LIST_NAME_TO_STAGE:
            unknown += 1
            continue
        if name in ids_by_name:
            duplicate += 1
            continue
        ids_by_name[name] = list_id
    if unknown:
        raise TrelloConflictError(
            "The Trello pipeline contains unknown open lists; sync is fail-closed."
        )
    if duplicate:
        raise TrelloConflictError(
            "The Trello pipeline contains duplicate stage lists; sync is fail-closed."
        )
    stage_ids = {
        stage: ids_by_name[name]
        for stage, name in PIPELINE_LISTS
        if name in ids_by_name
    }
    missing = {name for _stage, name in PIPELINE_LISTS if name not in ids_by_name}
    return stage_ids, missing


def _validate_existing_cards_are_keyed(
    snapshot: TrelloSnapshot,
    list_ids: Mapping[str, str],
) -> None:
    known_lists = set(list_ids.values())
    unkeyed = 0
    misplaced = 0
    for card in snapshot.cards:
        if str(card.get("idList") or "") not in known_lists:
            misplaced += 1
        try:
            _footer_opportunity_id(str(card.get("desc") or ""))
        except TrelloConflictError:
            unkeyed += 1
    if misplaced or unkeyed:
        raise TrelloConflictError(
            "Existing Trello cards are unkeyed or outside known stage lists; "
            "list bootstrap was aborted."
        )


def _selected_opportunity_count() -> int:
    return Opportunity.objects.exclude(pipeline_stage="").count()


def _build_plan(
    snapshot: TrelloSnapshot,
    board_id: str,
    list_ids: Mapping[str, str],
    *,
    lock: bool = False,
) -> PipelinePlan:
    if set(list_ids) != set(STAGE_TO_LIST_NAME):
        raise TrelloConflictError("The Trello pipeline list map is incomplete.")

    opportunity_query = (
        Opportunity.objects.exclude(pipeline_stage="")
        .select_related("account", "owner")
        .prefetch_related("actions")
        .order_by("id")
    )
    state_query = OpportunityTrelloState.objects.select_related("opportunity").order_by(
        "opportunity_id"
    )
    if lock:
        # ``owner`` is nullable and therefore joined with LEFT OUTER JOIN.
        # PostgreSQL cannot lock the nullable side, so lock only the canonical
        # Opportunity/State rows while still hydrating owner for projection.
        opportunity_query = opportunity_query.select_for_update(of=("self",))
        state_query = state_query.select_for_update(of=("self",))
    opportunities = list(opportunity_query)
    selected = {opportunity.id: opportunity for opportunity in opportunities}
    states = list(state_query)
    states_by_opportunity = {state.opportunity_id: state for state in states}
    states_by_card = {state.card_id: state for state in states if state.board_id == board_id}

    invalid_stages = sum(
        opportunity.pipeline_stage not in STAGE_TO_LIST_NAME
        for opportunity in opportunities
    )
    if invalid_stages:
        raise TrelloConflictError(
            "The CRM contains an unknown nonblank pipeline stage; sync is fail-closed."
        )

    for opportunity in opportunities:
        state = states_by_opportunity.get(opportunity.id)
        if state is not None and state.board_id != board_id:
            raise TrelloConflictError(
                "A selected Opportunity is already mapped to a different Trello board."
            )

    cards_by_opportunity: dict[UUID, Mapping[str, Any]] = {}
    card_ids = set()
    list_to_stage = {list_id: stage for stage, list_id in list_ids.items()}
    for card in snapshot.cards:
        card_id = str(card.get("id") or "")
        card_ids.add(card_id)
        remote_stage = list_to_stage.get(str(card.get("idList") or ""))
        if remote_stage is None:
            raise TrelloConflictError(
                "A Trello card is outside the exact pipeline stage lists."
            )
        opportunity_id = _footer_opportunity_id(str(card.get("desc") or ""))
        if opportunity_id not in selected:
            raise TrelloConflictError(
                "A Trello card references an unknown or unselected Opportunity."
            )
        if opportunity_id in cards_by_opportunity:
            raise TrelloConflictError(
                "Multiple Trello cards reference the same Opportunity."
            )
        mapped = states_by_card.get(card_id)
        if mapped is not None and mapped.opportunity_id != opportunity_id:
            raise TrelloConflictError(
                "A Trello card mapping disagrees with its stable footer ID."
            )
        state = states_by_opportunity.get(opportunity_id)
        if state is not None and state.card_id != card_id:
            raise TrelloConflictError(
                "A Trello Opportunity mapping points to a different card."
            )
        cards_by_opportunity[opportunity_id] = card

    # A mapping that points at a missing/archived card cannot be repaired by
    # creating another card; that could duplicate human pipeline history.
    stale_board_states = [
        state
        for state in states
        if state.board_id == board_id
        and (
            state.card_id not in card_ids
            or state.opportunity_id not in selected
        )
    ]
    if stale_board_states:
        raise TrelloConflictError(
            "The CRM has stale or unselected Trello mappings; sync is fail-closed."
        )

    planned_cards: list[PlannedCard] = []
    db_fingerprint_rows = []
    for opportunity in opportunities:
        state = states_by_opportunity.get(opportunity.id)
        remote = cards_by_opportunity.get(opportunity.id)
        desired_name, desired_description = _card_projection(opportunity)
        db_stage = opportunity.pipeline_stage
        event_source = OpportunityPipelineEvent.Source.SYSTEM
        baseline_stage = state.published_pipeline_stage if state else ""
        update_fields: dict[str, Any] = {}

        db_fingerprint_rows.append(
            {
                "id": str(opportunity.id),
                "pipeline_stage": db_stage,
                "updated_at": opportunity.updated_at.isoformat(),
                "state": _state_fingerprint(state),
                "projection": {
                    "name": desired_name,
                    "desc": desired_description,
                },
            }
        )

        if remote is None:
            if state is not None:
                raise TrelloConflictError(
                    "A mapped Trello card is missing; sync will not create a duplicate."
                )
            final_stage = db_stage
            planned_cards.append(
                PlannedCard(
                    opportunity_id=opportunity.id,
                    card_id="",
                    list_id=list_ids[final_stage],
                    stage=final_stage,
                    name=desired_name,
                    description=desired_description,
                    create=True,
                    update_fields={},
                    db_stage_before=db_stage,
                    event_source=OpportunityPipelineEvent.Source.SYSTEM,
                    baseline_stage="",
                )
            )
            continue

        card_id = str(remote.get("id") or "")
        remote_list_id = str(remote.get("idList") or "")
        remote_stage = list_to_stage[remote_list_id]
        if state is None:
            if remote_stage != db_stage:
                raise TrelloConflictError(
                    "An unmapped keyed card disagrees with the CRM pipeline stage."
                )
            _require_unmapped_content_match(
                remote,
                desired_name=desired_name,
                desired_description=desired_description,
            )
            final_stage = db_stage
        else:
            _validate_state_baseline(state, list_ids)
            final_stage, event_source = _merge_stage(
                baseline=baseline_stage,
                database=db_stage,
                trello=remote_stage,
            )
            _merge_managed_content(
                state=state,
                remote=remote,
                desired_name=desired_name,
                desired_description=desired_description,
                updates=update_fields,
            )

        final_list_id = list_ids[final_stage]
        if remote_list_id != final_list_id:
            update_fields["idList"] = final_list_id
        planned_cards.append(
            PlannedCard(
                opportunity_id=opportunity.id,
                card_id=card_id,
                list_id=final_list_id,
                stage=final_stage,
                name=desired_name,
                description=desired_description,
                create=False,
                update_fields=update_fields,
                db_stage_before=db_stage,
                event_source=event_source,
                baseline_stage=baseline_stage,
            )
        )

    database_fingerprint = hashlib.sha256(
        json.dumps(
            db_fingerprint_rows,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return PipelinePlan(
        cards=tuple(planned_cards),
        selected_count=len(opportunities),
        board_card_count=len(snapshot.cards),
        database_fingerprint=database_fingerprint,
    )


def _footer_opportunity_id(description: str) -> UUID:
    matches = _FOOTER_RE.findall(description or "")
    if len(matches) != 1:
        raise TrelloConflictError(
            "A Trello card is missing one unambiguous OpenOutreach footer ID."
        )
    try:
        return UUID(matches[0])
    except ValueError as exc:  # pragma: no cover - regex already constrains
        raise TrelloConflictError("A Trello footer ID is invalid.") from exc


def _state_fingerprint(state: OpportunityTrelloState | None) -> Any:
    if state is None:
        return None
    return {
        "board_id": state.board_id,
        "card_id": state.card_id,
        "list_id": state.list_id,
        "published_pipeline_stage": state.published_pipeline_stage,
        "published_card_snapshot": state.published_card_snapshot,
        "updated_at": state.updated_at.isoformat(),
    }


def _validate_state_baseline(
    state: OpportunityTrelloState,
    list_ids: Mapping[str, str],
) -> None:
    if state.published_pipeline_stage not in list_ids:
        raise TrelloConflictError("A Trello mapping has no valid stage baseline.")
    if state.list_id != list_ids[state.published_pipeline_stage]:
        raise TrelloConflictError(
            "A Trello mapping's stable list ID disagrees with its stage baseline."
        )
    snapshot = state.published_card_snapshot
    if not isinstance(snapshot, dict) or any(
        key not in snapshot for key in _MANAGED_SNAPSHOT_FIELDS
    ):
        raise TrelloConflictError("A Trello mapping has no complete card baseline.")
    if str(snapshot["list_id"] or "") != state.list_id:
        raise TrelloConflictError(
            "A Trello mapping's card snapshot disagrees with its stable list ID."
        )


def _merge_stage(*, baseline: str, database: str, trello: str) -> tuple[str, str]:
    database_changed = database != baseline
    trello_changed = trello != baseline
    if database_changed and trello_changed and database != trello:
        raise TrelloConflictError(
            "The CRM and Trello pipeline stages changed differently since last sync."
        )
    if trello_changed:
        return trello, OpportunityPipelineEvent.Source.TRELLO
    if database_changed:
        raise TrelloConflictError(
            "The CRM pipeline stage changed outside Trello; move the mapped "
            "card instead of editing pipeline_stage directly."
        )
    return trello, OpportunityPipelineEvent.Source.TRELLO


def _require_unmapped_content_match(
    remote: Mapping[str, Any],
    *,
    desired_name: str,
    desired_description: str,
) -> None:
    if (
        str(remote.get("name") or "") != desired_name
        or str(remote.get("desc") or "") != desired_description
    ):
        raise TrelloConflictError(
            "An unmapped keyed card does not match the compact CRM projection."
        )


def _merge_managed_content(
    *,
    state: OpportunityTrelloState,
    remote: Mapping[str, Any],
    desired_name: str,
    desired_description: str,
    updates: dict[str, Any],
) -> None:
    baseline = state.published_card_snapshot
    for field, desired in (("name", desired_name), ("desc", desired_description)):
        remote_value = str(remote.get(field) or "")
        baseline_value = str(baseline.get(field) or "")
        remote_changed = remote_value != baseline_value
        database_changed = desired != baseline_value
        if remote_changed and remote_value != desired:
            # Card content is managed.  Preserve any human edit by stopping,
            # rather than overwriting it or guessing how it maps back to CRM.
            raise TrelloConflictError(
                "Managed Trello card content changed outside the CRM projection."
            )
        if database_changed and remote_value != desired:
            updates[field] = desired


def _card_projection(opportunity: Opportunity) -> tuple[str, str]:
    owner = "Unassigned"
    if opportunity.owner_id:
        owner = opportunity.owner.display_name or opportunity.owner.handle
    action = next(
        (
            item
            for item in opportunity.actions.all()
            if item.status in {OpportunityAction.Status.OPEN, OpportunityAction.Status.WAITING}
        ),
        None,
    )
    action_line = "None"
    if action is not None:
        timing = action.due_on or action.waiting_until
        action_line = f"{action.get_kind_display()} ({action.get_status_display()})"
        if timing is not None:
            action_line += f" — {timing.isoformat()}"
    activity = (
        opportunity.last_meaningful_activity_at.date().isoformat()
        if opportunity.last_meaningful_activity_at
        else "Unknown"
    )
    step = str(opportunity.sales_motion_step) if opportunity.sales_motion_step else "Unknown"
    name = _single_line(opportunity.account.name, limit=512)
    if not name:
        raise TrelloConflictError(
            "A selected Opportunity has no usable account name for its card."
        )
    description = "\n".join(
        (
            f"Owner: {_single_line(owner, limit=100)}",
            f"Sales motion step: {step}",
            f"Next action: {action_line}",
            f"Last meaningful activity: {activity}",
            "",
            "---",
            f"{_FOOTER_LABEL} {opportunity.id}",
        )
    )
    return name, description


def _single_line(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _validate_final_projection(
    snapshot: TrelloSnapshot,
    plan: PipelinePlan,
    list_ids: Mapping[str, str],
) -> dict[UUID, Mapping[str, Any]]:
    expected = {card.opportunity_id: card for card in plan.cards}
    actual: dict[UUID, Mapping[str, Any]] = {}
    known_list_ids = set(list_ids.values())
    for remote in snapshot.cards:
        if str(remote.get("idList") or "") not in known_list_ids:
            raise TrelloConflictError("A card moved outside the pipeline during sync.")
        opportunity_id = _footer_opportunity_id(str(remote.get("desc") or ""))
        if opportunity_id not in expected or opportunity_id in actual:
            raise TrelloConflictError(
                "The Trello card set changed unexpectedly during synchronization."
            )
        planned = expected[opportunity_id]
        if (
            str(remote.get("idList") or "") != planned.list_id
            or str(remote.get("name") or "") != planned.name
            or str(remote.get("desc") or "") != planned.description
        ):
            raise TrelloConflictError(
                "Trello read-back did not match the planned card projection."
            )
        actual[opportunity_id] = remote
    if set(actual) != set(expected):
        raise TrelloConflictError(
            "Trello read-back is missing one or more planned cards."
        )
    return actual


@transaction.atomic
def _persist_plan(
    plan: PipelinePlan,
    *,
    final_cards: Mapping[UUID, Mapping[str, Any]],
    board_id: str,
    synced_at: datetime,
) -> None:
    opportunities = {
        item.id: item
        for item in Opportunity.objects.select_for_update().filter(
            id__in=[card.opportunity_id for card in plan.cards]
        )
    }
    for planned in plan.cards:
        opportunity = opportunities[planned.opportunity_id]
        remote = final_cards[planned.opportunity_id]
        if opportunity.pipeline_stage != planned.db_stage_before:
            raise TrelloConflictError(
                "The CRM pipeline changed before Trello state could be committed."
            )

        stage_changed = opportunity.pipeline_stage != planned.stage
        baseline_changed = planned.baseline_stage != planned.stage
        if stage_changed:
            opportunity.pipeline_stage = planned.stage
            opportunity.pipeline_stage_entered_at = synced_at
            opportunity.save(
                update_fields={
                    "pipeline_stage",
                    "pipeline_stage_entered_at",
                    "updated_at",
                }
            )
        elif opportunity.pipeline_stage_entered_at is None:
            opportunity.pipeline_stage_entered_at = synced_at
            opportunity.save(
                update_fields={"pipeline_stage_entered_at", "updated_at"}
            )

        if baseline_changed:
            latest_event = opportunity.pipeline_events.order_by(
                "-changed_at", "-created_at", "-id"
            ).first()
            # Pipeline triage policy records the initial blank -> Triage event
            # before the first card exists.  Establishing the Trello baseline
            # must not duplicate that already-audited transition.
            if not (
                latest_event is not None
                and latest_event.from_stage == planned.baseline_stage
                and latest_event.to_stage == planned.stage
            ):
                OpportunityPipelineEvent.objects.create(
                    opportunity=opportunity,
                    from_stage=planned.baseline_stage,
                    to_stage=planned.stage,
                    source=planned.event_source,
                    changed_at=synced_at,
                )

        card_id = str(remote.get("id") or "")
        card_snapshot = {
            "name": planned.name,
            "desc": planned.description,
            "list_id": planned.list_id,
        }
        remote_activity = _parse_trello_datetime(remote.get("dateLastActivity"))
        state, created = OpportunityTrelloState.objects.select_for_update().get_or_create(
            opportunity=opportunity,
            defaults={
                "board_id": board_id,
                "card_id": card_id,
                "list_id": planned.list_id,
            },
        )
        if not created and (state.board_id != board_id or state.card_id != card_id):
            raise TrelloConflictError(
                "The stable Trello card mapping changed before commit."
            )
        state.list_id = planned.list_id
        state.published_pipeline_stage = planned.stage
        state.published_card_snapshot = card_snapshot
        state.trello_date_last_activity = remote_activity
        state.last_synced_at = synced_at
        state.save(
            update_fields={
                "list_id",
                "published_pipeline_stage",
                "published_card_snapshot",
                "trello_date_last_activity",
                "last_synced_at",
                "updated_at",
            }
        )


def _parse_trello_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise TrelloResponseError("Trello returned an invalid activity timestamp.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _report(
    plan: PipelinePlan,
    *,
    apply: bool,
    missing: set[str],
    lists_created: int,
) -> PipelineSyncReport:
    creates = sum(card.create for card in plan.cards)
    updates = sum(bool(card.update_fields) for card in plan.cards)
    mappings = sum(card.create or not card.card_id for card in plan.cards)
    from_trello = sum(
        card.stage != card.db_stage_before
        and card.event_source == OpportunityPipelineEvent.Source.TRELLO
        for card in plan.cards
    )
    to_trello = sum(
        bool(card.update_fields.get("idList"))
        and card.event_source == OpportunityPipelineEvent.Source.SYSTEM
        for card in plan.cards
    )
    unchanged = sum(
        not card.create
        and not card.update_fields
        and card.stage == card.db_stage_before
        and bool(card.baseline_stage)
        for card in plan.cards
    )
    # An existing footer-keyed card without DB state is also a new mapping.
    mappings += sum(
        not card.create and not card.baseline_stage for card in plan.cards
    )
    return PipelineSyncReport(
        mode="apply" if apply else "dry-run",
        selected_opportunities=plan.selected_count,
        board_cards=plan.board_card_count,
        lists_missing=len(missing),
        lists_created=lists_created,
        cards_to_create=creates,
        cards_to_update=updates,
        stages_from_trello=from_trello,
        stages_to_trello=to_trello,
        mappings_to_create=mappings,
        unchanged_cards=unchanged,
    )
