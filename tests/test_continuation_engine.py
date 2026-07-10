from __future__ import annotations

from agentflow_hermes.board_adapter import FakeBoardAdapter
from agentflow_hermes.board_events import BoardEvent, FakeBoardEventSource
from agentflow_hermes.continuation_config import load_contract_registry
from agentflow_hermes.continuation_engine import ingest_board_once
from agentflow_hermes.continuation_store import ContinuationStore
from pathlib import Path

_G421_YAML = Path(__file__).resolve().parents[1] / "contracts" / "warroom.g421.exposure-resolution.v1.yaml"


def _needs_input_event(event_id="ev1", event_seq=1, source_task_id="t_ab93a206") -> BoardEvent:
    return BoardEvent(
        event_id=event_id,
        event_seq=event_seq,
        source_task_id=source_task_id,
        source_graph_id="g_1",
        run_metadata={
            "agentflow_outcome": {
                "schema_version": 1,
                "verdict": "BLOCK",
                "continuation_kind": "needs_input",
                "contract_ref": "warroom.g421.exposure-resolution.v1",
                "required_inputs": [{"name": "approval_receipt_id", "authority": "owner"}],
                "resume_transition": "warroom.g421.packet-rerun",
            }
        },
        origin_ref="discord:#research",
        return_to_ref="discord:#research",
    )


def test_structured_metadata_needs_input_creates_owner_anchor(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    contracts = load_contract_registry([_G421_YAML])
    source = FakeBoardEventSource(db_identity="warroom-os-db", events=[_needs_input_event()])

    result = ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)

    assert result["processed"] == 1
    instances = store.list_instances()
    assert len(instances) == 1
    assert instances[0]["continuation_kind"] == "needs_input"
    assert len(adapter.tasks) == 1


def test_unknown_contract_ref_is_non_mutating(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    contracts = load_contract_registry([_G421_YAML])
    bad_event = BoardEvent(
        event_id="ev_bad",
        event_seq=1,
        source_task_id="t_1",
        source_graph_id="g_1",
        run_metadata={
            "agentflow_outcome": {
                "schema_version": 1,
                "verdict": "BLOCK",
                "continuation_kind": "needs_input",
                "contract_ref": "no.such.contract.v1",
            }
        },
    )
    source = FakeBoardEventSource(db_identity="warroom-os-db", events=[bad_event])

    result = ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)

    assert result["results"][0]["action"] == "noop"
    assert store.list_instances() == []
    assert adapter.tasks == {}


def test_vague_unknown_outcome_is_non_mutating(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    contracts = load_contract_registry([_G421_YAML])
    event = BoardEvent(event_id="ev_vague", event_seq=1, source_task_id="t_1", source_graph_id="g_1", summary="something might be wrong")
    source = FakeBoardEventSource(db_identity="warroom-os-db", events=[event])

    result = ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)

    assert result["results"][0]["action"] == "noop"
    assert store.list_instances() == []


def test_go_routes_to_roadmap_router():
    calls = []

    def fake_roadmap_router(summary, **kwargs):
        calls.append((summary, kwargs))
        return {"success": True, "action": "propose"}

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        store = ContinuationStore(Path(tmp) / "agentflow.sqlite")
        adapter = FakeBoardAdapter()
        contracts = load_contract_registry([_G421_YAML])
        event = BoardEvent(event_id="ev_go", event_seq=1, source_task_id="t_1", source_graph_id="g_1", summary="Verdict: GO")
        source = FakeBoardEventSource(db_identity="warroom-os-db", events=[event])
        result = ingest_board_once(
            board="warroom-os", source=source, store=store, contract_registry=contracts,
            adapter=adapter, roadmap_router=fake_roadmap_router,
        )
    assert len(calls) == 1
    assert result["results"][0]["action"] == "roadmap_routed"


def test_block_code_fix_routes_to_code_fix_router():
    calls = []

    def fake_code_fix_router(summary, **kwargs):
        calls.append((summary, kwargs))
        return {"success": True, "candidates": []}

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        store = ContinuationStore(Path(tmp) / "agentflow.sqlite")
        adapter = FakeBoardAdapter()
        contracts = load_contract_registry([_G421_YAML])
        event = BoardEvent(
            event_id="ev_block", event_seq=1, source_task_id="t_1", source_graph_id="g_1",
            summary="Verdict: BLOCK — stale_inline_route detected",
        )
        source = FakeBoardEventSource(db_identity="warroom-os-db", events=[event])
        result = ingest_board_once(
            board="warroom-os", source=source, store=store, contract_registry=contracts,
            adapter=adapter, code_fix_router=fake_code_fix_router,
        )
    assert len(calls) == 1
    assert result["results"][0]["action"] == "code_fix_routed"


def test_cursor_scoped_per_board_overlapping_event_seq_valid(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    contracts = load_contract_registry([_G421_YAML])
    adapter = FakeBoardAdapter()

    warroom_source = FakeBoardEventSource(db_identity="warroom-db", events=[_needs_input_event(event_id="ev1", event_seq=1)])
    oracle_source = FakeBoardEventSource(
        db_identity="oracle-db",
        events=[BoardEvent(event_id="ev1", event_seq=1, source_task_id="t_oracle", source_graph_id="g_o", summary="Verdict: GO")],
    )

    ingest_board_once(board="warroom-os", source=warroom_source, store=store, contract_registry=contracts, adapter=adapter, roadmap_router=lambda *a, **k: {"success": True})
    ingest_board_once(board="oracle-lab", source=oracle_source, store=store, contract_registry=contracts, adapter=adapter, roadmap_router=lambda *a, **k: {"success": True})

    assert store.get_cursor("warroom-os", "warroom-db") == 1
    assert store.get_cursor("oracle-lab", "oracle-db") == 1
    assert len(store.list_instances()) == 1  # only the warroom needs_input event created an instance


def test_duplicate_ingest_processes_zero_new_events(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    contracts = load_contract_registry([_G421_YAML])
    source = FakeBoardEventSource(db_identity="warroom-os-db", events=[_needs_input_event()])

    first = ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)
    second = ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)

    assert first["processed"] == 1
    assert second["processed"] == 0
    assert len(store.list_instances()) == 1
    assert len(adapter.tasks) == 1
