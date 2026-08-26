import copy
import json
from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command

from crm.models import (
    Account,
    Opportunity,
    OpportunityAction,
    OpportunityPipelineEvent,
    OpportunityTrelloState,
    SalesOwner,
)
from linkedin.exceptions import TrelloConflictError, TrelloTransientError
from linkedin.trello_pipeline import PIPELINE_LISTS, sync_trello_pipeline


pytestmark = pytest.mark.django_db


class FakeTrelloClient:
    def __init__(self, *, missing=()):
        missing = set(missing)
        self.board = {
            "id": "board1",
            "name": "Sales Pipeline",
            "closed": False,
        }
        self.lists = [
            {
                "id": f"list{index}",
                "name": name,
                "closed": False,
                "pos": index,
            }
            for index, (_stage, name) in enumerate(PIPELINE_LISTS, start=1)
            if name not in missing
        ]
        self.cards = []
        self.created_lists = []
        self.created_cards = []
        self.updated_cards = []
        self.card_reads = 0
        self.mutate_on_card_read = None
        self.raise_after_create = False

    def get_board(self, board_id):
        assert board_id == "board1"
        return copy.deepcopy(self.board)

    def list_open_lists(self, board_id):
        assert board_id == "board1"
        return copy.deepcopy(self.lists)

    def list_open_cards(self, board_id):
        assert board_id == "board1"
        self.card_reads += 1
        if self.mutate_on_card_read == self.card_reads and self.cards:
            self.cards[0]["name"] = "Concurrent human edit"
        return copy.deepcopy(self.cards)

    def create_list(self, board_id, *, name, position="bottom"):
        assert board_id == "board1"
        item = {
            "id": f"list{len(self.lists) + 20}",
            "name": name,
            "closed": False,
            "pos": len(self.lists) + 1,
        }
        self.lists.append(item)
        self.created_lists.append(name)
        return copy.deepcopy(item)

    def create_card(self, *, list_id, name, description):
        item = {
            "id": f"card{len(self.cards) + 1}",
            "name": name,
            "desc": description,
            "idList": list_id,
            "closed": False,
            "dateLastActivity": "2026-08-26T12:00:00Z",
            "due": None,
            "dueComplete": False,
            "idMembers": [],
            "labels": [],
        }
        self.cards.append(item)
        self.created_cards.append(copy.deepcopy(item))
        if self.raise_after_create:
            self.raise_after_create = False
            raise TrelloTransientError("Ambiguous fake transport failure")
        return copy.deepcopy(item)

    def update_card(self, card_id, **fields):
        card = next(item for item in self.cards if item["id"] == card_id)
        card.update(fields)
        card["dateLastActivity"] = "2026-08-26T12:00:01Z"
        self.updated_cards.append((card_id, copy.deepcopy(fields)))
        return copy.deepcopy(card)


def _opportunity(*, name="Ramp", pipeline_stage=Opportunity.PipelineStage.TRIAGE):
    account = Account.objects.create(name=name)
    return Opportunity.objects.create(
        account=account,
        name=f"{name} primary",
        owner=SalesOwner.objects.get(handle="Arian"),
        pipeline_stage=pipeline_stage,
    )


def _apply_once(client, opportunity):
    report = sync_trello_pipeline(client=client, board_id="board1", apply=True)
    opportunity.refresh_from_db()
    return report


def _list_id(client, stage):
    name = dict(PIPELINE_LISTS)[stage]
    return next(item["id"] for item in client.lists if item["name"] == name)


def test_dry_run_selects_only_nonblank_pipeline_stages_and_writes_nothing():
    _opportunity()
    _opportunity(name="Radar only", pipeline_stage="")
    client = FakeTrelloClient()

    report = sync_trello_pipeline(client=client, board_id="board1")

    assert report == {
        "mode": "dry-run",
        "selected_opportunities": 1,
        "board_cards": 0,
        "lists_missing": 0,
        "lists_created": 0,
        "cards_to_create": 1,
        "cards_to_update": 0,
        "stages_from_trello": 0,
        "stages_to_trello": 0,
        "mappings_to_create": 1,
        "unchanged_cards": 0,
    }
    assert client.created_cards == []
    assert OpportunityTrelloState.objects.count() == 0
    assert OpportunityPipelineEvent.objects.count() == 0


def test_apply_creates_compact_footer_keyed_card_without_transcripts_or_members():
    opportunity = _opportunity()
    action = OpportunityAction.objects.create(
        opportunity=opportunity,
        kind=OpportunityAction.Kind.FOLLOWUP,
        description="SECRET transcript body\nwith several private messages",
        draft="SECRET drafted email",
        due_on=date(2026, 8, 28),
    )
    client = FakeTrelloClient()

    report = _apply_once(client, opportunity)

    assert report["cards_to_create"] == 1
    assert len(client.cards) == 1
    card = client.cards[0]
    assert card["name"] == "Ramp"
    assert f"OpenOutreach Opportunity ID: {opportunity.id}" in card["desc"]
    assert "Follow-up (Open) — 2026-08-28" in card["desc"]
    assert "SECRET" not in card["desc"]
    assert "idMembers" not in client.created_cards[0] or not client.created_cards[0][
        "idMembers"
    ]
    state = OpportunityTrelloState.objects.get(opportunity=opportunity)
    assert state.card_id == card["id"]
    assert state.published_pipeline_stage == Opportunity.PipelineStage.TRIAGE
    event = OpportunityPipelineEvent.objects.get(opportunity=opportunity)
    assert event.source == OpportunityPipelineEvent.Source.SYSTEM
    assert event.from_stage == ""
    assert event.to_stage == Opportunity.PipelineStage.TRIAGE
    assert action.description not in state.published_card_snapshot["desc"]


def test_second_apply_is_idempotent():
    opportunity = _opportunity()
    client = FakeTrelloClient()
    _apply_once(client, opportunity)
    event_count = OpportunityPipelineEvent.objects.count()
    client.created_cards.clear()
    client.updated_cards.clear()

    report = _apply_once(client, opportunity)

    assert report["cards_to_create"] == 0
    assert report["cards_to_update"] == 0
    assert report["unchanged_cards"] == 1
    assert client.created_cards == []
    assert client.updated_cards == []
    assert OpportunityPipelineEvent.objects.count() == event_count


def test_first_card_mapping_does_not_duplicate_existing_triage_event():
    opportunity = _opportunity()
    OpportunityPipelineEvent.objects.create(
        opportunity=opportunity,
        from_stage="",
        to_stage=Opportunity.PipelineStage.TRIAGE,
        source=OpportunityPipelineEvent.Source.SYSTEM,
    )
    client = FakeTrelloClient()

    _apply_once(client, opportunity)

    assert OpportunityPipelineEvent.objects.filter(opportunity=opportunity).count() == 1


def test_trello_list_move_is_imported_as_human_stage_source():
    opportunity = _opportunity()
    client = FakeTrelloClient()
    _apply_once(client, opportunity)
    client.cards[0]["idList"] = _list_id(
        client, Opportunity.PipelineStage.DEMO_EVALUATION
    )
    client.cards[0]["dateLastActivity"] = "2026-08-26T13:00:00Z"

    report = _apply_once(client, opportunity)

    opportunity.refresh_from_db()
    assert report["stages_from_trello"] == 1
    assert opportunity.pipeline_stage == Opportunity.PipelineStage.DEMO_EVALUATION
    event = OpportunityPipelineEvent.objects.order_by("-changed_at", "-created_at").first()
    assert event.source == OpportunityPipelineEvent.Source.TRELLO
    assert event.from_stage == Opportunity.PipelineStage.TRIAGE
    assert event.to_stage == Opportunity.PipelineStage.DEMO_EVALUATION
    assert client.updated_cards == []


def test_database_only_stage_change_is_rejected_after_card_mapping():
    opportunity = _opportunity()
    client = FakeTrelloClient()
    _apply_once(client, opportunity)
    client.updated_cards.clear()
    opportunity.pipeline_stage = Opportunity.PipelineStage.DISCOVERY
    opportunity.save(update_fields={"pipeline_stage", "updated_at"})

    with pytest.raises(TrelloConflictError, match="outside Trello"):
        _apply_once(client, opportunity)

    assert client.cards[0]["idList"] == _list_id(
        client, Opportunity.PipelineStage.TRIAGE
    )
    assert client.updated_cards == []


def test_divergent_three_way_stage_change_fails_before_writes():
    opportunity = _opportunity()
    client = FakeTrelloClient()
    _apply_once(client, opportunity)
    opportunity.pipeline_stage = Opportunity.PipelineStage.DISCOVERY
    opportunity.save(update_fields={"pipeline_stage", "updated_at"})
    client.cards[0]["idList"] = _list_id(
        client, Opportunity.PipelineStage.DEMO_EVALUATION
    )
    client.created_cards.clear()
    client.updated_cards.clear()

    with pytest.raises(TrelloConflictError, match="changed differently"):
        sync_trello_pipeline(client=client, board_id="board1", apply=True)

    assert client.created_cards == []
    assert client.updated_cards == []


@pytest.mark.parametrize(
    "defect",
    [
        "unkeyed",
        "footer_not_last",
        "duplicate",
        "unknown_card",
        "unknown_list",
        "duplicate_list",
    ],
)
def test_ambiguous_board_shapes_fail_closed(defect):
    opportunity = _opportunity()
    client = FakeTrelloClient()
    _apply_once(client, opportunity)
    if defect == "unkeyed":
        client.cards[0]["desc"] = "No stable footer"
    elif defect == "footer_not_last":
        client.cards[0]["desc"] += "\nHuman text after the identity footer"
    elif defect == "duplicate":
        duplicate = copy.deepcopy(client.cards[0])
        duplicate["id"] = "card2"
        client.cards.append(duplicate)
    elif defect == "unknown_card":
        unknown = _opportunity(name="Unselected", pipeline_stage="")
        client.cards[0]["desc"] = client.cards[0]["desc"].replace(
            str(opportunity.id), str(unknown.id)
        )
    elif defect == "unknown_list":
        client.lists.append(
            {"id": "unknown", "name": "Maybe someday", "closed": False, "pos": 99}
        )
    else:
        duplicate = copy.deepcopy(client.lists[0])
        duplicate["id"] = "duplicate-list"
        client.lists.append(duplicate)

    with pytest.raises(TrelloConflictError):
        sync_trello_pipeline(client=client, board_id="board1", apply=True)


def test_managed_card_text_edit_fails_closed_instead_of_overwriting():
    opportunity = _opportunity()
    client = FakeTrelloClient()
    _apply_once(client, opportunity)
    client.cards[0]["name"] = "Human renamed this"

    with pytest.raises(TrelloConflictError, match="content changed"):
        sync_trello_pipeline(client=client, board_id="board1", apply=True)

    assert client.cards[0]["name"] == "Human renamed this"
    assert client.updated_cards == []


def test_footer_recovers_card_created_before_ambiguous_post_failure():
    opportunity = _opportunity()
    client = FakeTrelloClient()
    client.raise_after_create = True

    with pytest.raises(TrelloTransientError):
        sync_trello_pipeline(client=client, board_id="board1", apply=True)

    assert len(client.cards) == 1
    assert OpportunityTrelloState.objects.count() == 0
    client.created_cards.clear()

    report = _apply_once(client, opportunity)

    assert report["cards_to_create"] == 0
    assert report["mappings_to_create"] == 1
    assert report["unchanged_cards"] == 0
    assert client.created_cards == []
    assert OpportunityTrelloState.objects.get(opportunity=opportunity).card_id == "card1"


def test_compare_before_write_catches_remote_drift():
    opportunity = _opportunity()
    client = FakeTrelloClient()
    _apply_once(client, opportunity)
    opportunity.account.name = "Ramp Updated"
    opportunity.account.save(update_fields={"name", "updated_at"})
    client.updated_cards.clear()
    client.mutate_on_card_read = client.card_reads + 2

    with pytest.raises(TrelloConflictError, match="changed after planning"):
        sync_trello_pipeline(client=client, board_id="board1", apply=True)

    assert client.updated_cards == []


def test_missing_lists_require_explicit_bootstrap_and_dry_run_never_creates():
    _opportunity()
    missing_name = "Closed Lost"
    client = FakeTrelloClient(missing={missing_name})

    with pytest.raises(TrelloConflictError, match="--bootstrap-lists"):
        sync_trello_pipeline(client=client, board_id="board1")

    report = sync_trello_pipeline(
        client=client,
        board_id="board1",
        bootstrap_lists=True,
    )
    assert report["lists_missing"] == 1
    assert client.created_lists == []

    report = sync_trello_pipeline(
        client=client,
        board_id="board1",
        bootstrap_lists=True,
        apply=True,
    )
    assert report["lists_created"] == 1
    assert client.created_lists == [missing_name]
    assert [item["name"] for item in client.lists][-1] == missing_name


def test_management_command_defaults_to_dry_run(monkeypatch):
    calls = []
    fake_client = object()
    monkeypatch.setattr("linkedin.conf.TRELLO_API_KEY", "key")
    monkeypatch.setattr("linkedin.conf.TRELLO_API_TOKEN", "token")
    monkeypatch.setattr("linkedin.conf.TRELLO_BOARD_ID", "board1")
    monkeypatch.setattr("linkedin.trello.TrelloClient", lambda **kwargs: fake_client)

    def fake_sync(**kwargs):
        calls.append(kwargs)
        return {"mode": "dry-run", "cards_to_create": 0}

    monkeypatch.setattr(
        "linkedin.trello_pipeline.sync_trello_pipeline",
        fake_sync,
    )
    stdout = StringIO()

    call_command("sync_trello_pipeline", stdout=stdout)

    assert calls == [
        {
            "client": fake_client,
            "board_id": "board1",
            "apply": False,
            "bootstrap_lists": False,
        }
    ]
    assert json.loads(stdout.getvalue()) == {
        "mode": "dry-run",
        "cards_to_create": 0,
    }
