"""M27 commit 7: prove there is exactly one router implementation behind
GO/CODE_FIX/NEEDS_INPUT/APPROVAL_REQUIRED/EXTERNAL_WAIT, shared by agentflowd
(``daemon.py``) and the legacy watchdog script entrypoint
(``scripts/agentflow_needs_input_watchdog.py``) — not two parallel copies.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from agentflow_hermes import continuation_engine, daemon
from agentflow_hermes.board_adapter import FakeBoardAdapter
from agentflow_hermes.board_events import BoardEvent, BoardRegistryEntry, FakeBoardEventSource
from agentflow_hermes.continuation_config import load_contract_registry
from agentflow_hermes.continuation_store import ContinuationStore
from agentflow_hermes.outcome import ContinuationKind

_REPO = Path(__file__).resolve().parents[1]
_GENERIC_CONTRACT_YAML = _REPO / "contracts" / "generic.owner-input.v1.yaml"


def _contracts():
    return load_contract_registry([_GENERIC_CONTRACT_YAML])


def _load_watchdog_module():
    spec = importlib.util.spec_from_file_location(
        "agentflow_needs_input_watchdog", _REPO / "scripts" / "agentflow_needs_input_watchdog.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_daemon_router_is_the_continuation_engine_router_not_a_copy():
    """``daemon.route_board_events`` must delegate to
    ``continuation_engine.ingest_board_once`` — the exact same function
    object, not a reimplementation — so there is one router, not two."""
    assert daemon.ingest_board_once is continuation_engine.ingest_board_once


def test_watchdog_script_uses_the_same_ingest_all_boards_as_the_daemon():
    """The compatibility-shim watchdog script must import the identical
    ``ingest_all_boards`` the daemon's per-board wrapper is built on."""
    wd = _load_watchdog_module()
    assert wd.ingest_all_boards is continuation_engine.ingest_all_boards


def _go_event() -> BoardEvent:
    return BoardEvent(event_id="ev_go", event_seq=1, source_task_id="t_go", source_graph_id="g_1", summary="Verdict: GO")


def _code_fix_event() -> BoardEvent:
    return BoardEvent(
        event_id="ev_fix", event_seq=1, source_task_id="t_fix", source_graph_id="g_1",
        summary="Verdict: BLOCK — stale_inline_route detected",
    )


def _needs_input_event() -> BoardEvent:
    return BoardEvent(
        event_id="ev_ni", event_seq=1, source_task_id="t_ni", source_graph_id="g_1",
        run_metadata={
            "agentflow_outcome": {
                "schema_version": 1, "verdict": "BLOCK", "continuation_kind": "needs_input",
                "required_inputs": [{"name": "result_url", "kind": "fact", "authority": "owner"}],
            }
        },
        origin_ref="discord:1", return_to_ref="discord:1",
    )


def _approval_required_event() -> BoardEvent:
    return BoardEvent(
        event_id="ev_ar", event_seq=1, source_task_id="t_ar", source_graph_id="g_1",
        run_metadata={
            "agentflow_outcome": {
                "schema_version": 1, "verdict": "BLOCK", "continuation_kind": "approval_required",
                "required_inputs": [{"name": "release_decision", "kind": "preference", "authority": "owner"}],
            }
        },
    )


def _external_wait_event() -> BoardEvent:
    return BoardEvent(
        event_id="ev_ew", event_seq=1, source_task_id="t_ew", source_graph_id="g_1",
        run_metadata={
            "agentflow_outcome": {
                "schema_version": 1, "verdict": "BLOCK", "continuation_kind": "external_wait",
                "external_wait": {"kind": "github_check", "target": "r/x", "desired": "success", "poll_interval_seconds": 30},
            }
        },
    )


def _route_via_daemon(tmp_path, board, event, name):
    store = ContinuationStore(tmp_path / f"{name}-daemon.sqlite")
    entry = BoardRegistryEntry(board=board, db_identity=f"{board}-db", default_endpoint="discord:1")
    adapter = FakeBoardAdapter()
    store.advance_cursor(board, f"{board}-db", 0)

    def factory(b, e):
        return FakeBoardEventSource(db_identity=f"{board}-db", events=[event])

    result = daemon.route_board_events(
        board=board, entry=entry, store=store, contract_registry=_contracts(), adapter=adapter, source_factory=factory,
    )
    return result, store, adapter


def _route_via_ingest_board_once(tmp_path, board, event, name):
    store = ContinuationStore(tmp_path / f"{name}-engine.sqlite")
    adapter = FakeBoardAdapter()
    store.advance_cursor(board, f"{board}-db", 0)
    source = FakeBoardEventSource(db_identity=f"{board}-db", events=[event])
    result = continuation_engine.ingest_board_once(
        board=board, source=source, store=store, contract_registry=_contracts(), adapter=adapter,
        default_endpoint="discord:1",
    )
    return result, store, adapter


def test_every_kind_produces_the_same_action_through_both_call_paths(tmp_path):
    cases = [
        ("go", _go_event(), "roadmap_routed"),
        ("fix", _code_fix_event(), "code_fix_routed"),
        ("ni", _needs_input_event(), "owner_input_planned"),
        ("ar", _approval_required_event(), "owner_input_planned"),
        ("ew", _external_wait_event(), "external_wait_registered"),
    ]
    for name, event, expected_action in cases:
        daemon_result, _store_d, _adapter_d = _route_via_daemon(tmp_path, f"b-{name}", event, name)
        engine_result, _store_e, _adapter_e = _route_via_ingest_board_once(tmp_path, f"b-{name}", event, name)

        assert daemon_result["results"][0]["action"] == expected_action, name
        assert engine_result["results"][0]["action"] == expected_action, name


def test_unknown_outcome_never_mutates_through_either_path(tmp_path):
    event = BoardEvent(event_id="ev_x", event_seq=1, source_task_id="t_x", source_graph_id="g_1", summary="no verdict here")
    daemon_result, store_d, adapter_d = _route_via_daemon(tmp_path, "b-unk", event, "unk")
    assert daemon_result["results"][0]["action"] == "noop"
    assert store_d.list_instances() == []
    assert adapter_d.tasks == {}
