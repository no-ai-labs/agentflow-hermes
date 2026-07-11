"""M26 global needs_input rollout: one board-aware scan loop drives every
enabled board from the canonical registry, seeds newly seen boards without
replay, honors migration cursors, resolves endpoints generically, and applies
the versioned generic owner-input contract when no domain contract is named.
Enrolling a future board is purely additive."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentflow_hermes.board_adapter import FakeBoardAdapter
from agentflow_hermes.board_events import (
    BoardEvent,
    BoardRegistryEntry,
    FakeBoardEventSource,
    load_board_registry,
)
from agentflow_hermes.continuation_config import load_contract_registry
from agentflow_hermes.continuation_engine import ingest_all_boards, ingest_board_once
from agentflow_hermes.continuation_store import ContinuationStore
from agentflow_hermes.outcome import ContinuationKind

_REPO = Path(__file__).resolve().parents[1]
_REGISTRY = _REPO / "config" / "boards.yaml"
_CONTRACTS = sorted((_REPO / "contracts").glob("*.yaml"))


def _contracts():
    return load_contract_registry(_CONTRACTS)


def _needs_input_event(seq, task_id, contract_ref="generic.owner-input.v1", origin=""):
    block = {
        "schema_version": 1,
        "verdict": "BLOCK",
        "continuation_kind": "needs_input",
    }
    if contract_ref:
        block["contract_ref"] = contract_ref
    return BoardEvent(
        event_id=f"kanban-event-{seq}",
        event_seq=seq,
        source_task_id=task_id,
        source_graph_id=f"g_{task_id}",
        run_metadata={"agentflow_outcome": block},
        origin_ref=origin,
        return_to_ref=origin,
    )


def _stateful_factory(sources):
    def factory(board, entry):
        return sources[board]
    return factory


def test_registry_loads_all_three_boards_with_route_data():
    registry = load_board_registry(_REGISTRY)
    assert set(registry) == {"agentflow-hermes", "warroom-os", "oracle-lab"}
    for board, entry in registry.items():
        assert entry.enabled is True
        assert entry.default_endpoint.startswith("discord:")


def test_first_sight_seeds_all_boards_without_replay(tmp_path):
    registry = load_board_registry(_REGISTRY)
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    sources = {
        b: FakeBoardEventSource(db_identity=b, events=[_needs_input_event(100, f"t_{b}")])
        for b in registry
    }

    result = ingest_all_boards(
        registry=registry, store=store, contract_registry=_contracts(),
        source_factory=_stateful_factory(sources),
        adapter_factory=lambda b, e: FakeBoardAdapter(),
        handle_kinds=(ContinuationKind.NEEDS_INPUT,),
    )

    # Every board seeded to its current max, nothing replayed.
    for br in result["boards"]:
        assert br["processed"] == 0
        assert br["seeded_cursor"] == 100
    assert store.list_instances() == []
    for b in registry:
        assert store.get_cursor(b, b) == 100


def test_new_events_after_seed_processed_on_every_board(tmp_path):
    registry = load_board_registry(_REGISTRY)
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapters = {b: FakeBoardAdapter() for b in registry}
    sources = {
        b: FakeBoardEventSource(db_identity=b, events=[_needs_input_event(100, f"t_{b}")])
        for b in registry
    }
    factory = _stateful_factory(sources)

    ingest_all_boards(
        registry=registry, store=store, contract_registry=_contracts(),
        source_factory=factory, adapter_factory=lambda b, e: adapters[b],
        handle_kinds=(ContinuationKind.NEEDS_INPUT,),
    )
    # A genuinely new event arrives after the board was first seen.
    for b in registry:
        sources[b].events.append(_needs_input_event(101, f"t_{b}_new"))

    result = ingest_all_boards(
        registry=registry, store=store, contract_registry=_contracts(),
        source_factory=factory, adapter_factory=lambda b, e: adapters[b],
        handle_kinds=(ContinuationKind.NEEDS_INPUT,),
    )

    processed = {br["board"]: br["processed"] for br in result["boards"]}
    assert processed == {b: 1 for b in registry}
    instances = store.list_instances()
    assert len(instances) == len(registry)
    assert all(i["continuation_kind"] == "needs_input" for i in instances)
    # exactly one owner anchor per board
    for b in registry:
        assert len(adapters[b].tasks) == 1


def test_disabled_board_is_skipped(tmp_path):
    registry = {
        "on": BoardRegistryEntry(board="on", db_identity="on", enabled=True),
        "off": BoardRegistryEntry(board="off", db_identity="off", enabled=False),
    }
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    sources = {
        "on": FakeBoardEventSource(db_identity="on", events=[_needs_input_event(5, "t_on")]),
        "off": FakeBoardEventSource(db_identity="off", events=[_needs_input_event(5, "t_off")]),
    }
    ingest_all_boards(
        registry=registry, store=store, contract_registry=_contracts(),
        source_factory=_stateful_factory(sources), handle_kinds=(ContinuationKind.NEEDS_INPUT,),
    )
    assert store.cursor_exists("on", "on")
    assert not store.cursor_exists("off", "off")


def test_migration_cursor_override_prevents_seeding_to_max(tmp_path):
    registry = {"b": BoardRegistryEntry(board="b", db_identity="b", enabled=True)}
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    sources = {"b": FakeBoardEventSource(db_identity="b", events=[_needs_input_event(500, "t_b")])}

    ingest_all_boards(
        registry=registry, store=store, contract_registry=_contracts(),
        source_factory=_stateful_factory(sources), migration_cursors={"b": 499},
        handle_kinds=(ContinuationKind.NEEDS_INPUT,),
    )
    # Seeded to the explicit migration cursor, so event 500 is still pending.
    assert store.get_cursor("b", "b") == 499
    sources["b"].events.append(_needs_input_event(501, "t_b2"))
    result = ingest_all_boards(
        registry=registry, store=store, contract_registry=_contracts(),
        source_factory=_stateful_factory(sources), adapter_factory=lambda b, e: FakeBoardAdapter(),
        handle_kinds=(ContinuationKind.NEEDS_INPUT,),
    )
    # Both event 500 and 501 are now processed.
    assert result["boards"][0]["processed"] == 2


def test_future_board_enrollment_is_additive(tmp_path):
    registry = load_board_registry(_REGISTRY)
    registry["future-board"] = BoardRegistryEntry(
        board="future-board", db_identity="future-board", enabled=True,
        default_endpoint="discord:#future:42",
    )
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    sources = {b: FakeBoardEventSource(db_identity=b, events=[]) for b in registry}

    ingest_all_boards(
        registry=registry, store=store, contract_registry=_contracts(),
        source_factory=_stateful_factory(sources), handle_kinds=(ContinuationKind.NEEDS_INPUT,),
    )
    assert store.cursor_exists("future-board", "future-board")


def test_generic_contract_used_when_no_domain_contract(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    # Pre-seed cursor so ingest_board_once processes.
    store.advance_cursor("agentflow-hermes", "agentflow-hermes", 0)
    source = FakeBoardEventSource(
        db_identity="agentflow-hermes",
        events=[_needs_input_event(1, "t_generic", contract_ref="")],
    )
    result = ingest_board_once(
        board="agentflow-hermes", source=source, store=store, contract_registry=_contracts(),
        adapter=adapter, handle_kinds=(ContinuationKind.NEEDS_INPUT,),
    )
    assert result["processed"] == 1
    instance = store.list_instances()[0]
    assert instance["contract_ref"] == "generic.owner-input.v1"
    assert len(adapter.tasks) == 1


def test_default_endpoint_fallback_used_when_event_has_no_endpoint(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    source = FakeBoardEventSource(
        db_identity="oracle-lab",
        events=[_needs_input_event(1, "t_ep", origin="")],
    )
    ingest_board_once(
        board="oracle-lab", source=source, store=store, contract_registry=_contracts(),
        adapter=adapter, handle_kinds=(ContinuationKind.NEEDS_INPUT,),
        default_endpoint="discord:#shaman:1500539609413849200",
    )
    instance = store.list_instances()[0]
    assert instance["origin_ref"] == "discord:#shaman:1500539609413849200"
    # subscribe (active-wake) used the resolved default endpoint
    assert any("1500539609413849200" in ep for _t, ep in adapter.subscriptions)


def test_handle_kinds_restricts_to_needs_input(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    routed = []
    go_event = BoardEvent(event_id="ev_go", event_seq=1, source_task_id="t_go", source_graph_id="g", summary="Verdict: GO")
    source = FakeBoardEventSource(db_identity="agentflow-hermes", events=[go_event])
    result = ingest_board_once(
        board="agentflow-hermes", source=source, store=store, contract_registry=_contracts(),
        adapter=adapter, handle_kinds=(ContinuationKind.NEEDS_INPUT,),
        roadmap_router=lambda *a, **k: routed.append(1) or {"success": True},
    )
    assert result["results"][0]["action"] == "noop"
    assert result["results"][0]["reason"] == "kind_not_handled"
    assert routed == []  # GO never routed when handle_kinds is needs_input only
