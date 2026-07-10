"""Warroom G4.21 vertical canary against RealBoardAdapter with a mocked
`hermes` CLI runner.

This proves the real adapter's argv shapes actually drive the vertical loop
end-to-end (create -> notify-subscribe -> receipt -> materialize -> review ->
packet-rerun) using the exact CLI syntax verified in
``board_adapter.py``'s module docstring. It never invokes a real subprocess
or touches a live board: the runner is a fake callable that returns
canned CLI-shaped stdout, so no task is actually created on `warroom-os`.

Running this same code against the real, shared `warroom-os` board (by
passing `RealBoardAdapter(board="warroom-os")` with no injected runner) is a
mutating action on a shared system and was deliberately NOT executed in this
session; see docs/m25-needs-input-continuation-engine-vertical-slice.md for
the readiness note to the supervisor.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentflow_hermes.board_adapter import RealBoardAdapter
from agentflow_hermes.board_events import BoardEvent, FakeBoardEventSource
from agentflow_hermes.continuation import get_handler
from agentflow_hermes.continuation_config import load_contract_registry
from agentflow_hermes.continuation_engine import ingest_board_once
from agentflow_hermes.continuation_store import ContinuationStore
from agentflow_hermes.outcome import ContinuationKind

_G421_YAML = Path(__file__).resolve().parents[1] / "contracts" / "warroom.g421.exposure-resolution.v1.yaml"

_CONTROLLED_EVENT = BoardEvent(
    event_id="g421-real-canary-synthetic-0001",
    event_seq=1,
    source_task_id="t_ab93a206",
    source_graph_id="g_warroom_g421",
    origin_ref="discord:#research",
    return_to_ref="discord:#research",
    run_metadata={
        "agentflow_outcome": {
            "schema_version": 1,
            "verdict": "BLOCK",
            "continuation_kind": "needs_input",
            "contract_ref": "warroom.g421.exposure-resolution.v1",
            "required_inputs": [
                {"name": "resolution_basis", "authority": "owner"},
                {"name": "approval_receipt_id", "authority": "owner"},
                {"name": "owner_confirmation", "authority": "owner"},
            ],
            "resume_transition": "warroom.g421.packet-rerun",
        }
    },
)


class MockedHermesCliRunner:
    """Simulates the real `hermes` CLI's argv contract and output shapes
    (JSON for `create`, plain text for everything else) without spawning a
    subprocess or touching any real board database."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._next_id = 0

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        subcommand = argv[4] if len(argv) > 4 else ""
        if subcommand == "create":
            self._next_id += 1
            task_id = f"t_g421canary{self._next_id:04d}"
            return 0, json.dumps({"id": task_id, "status": "blocked" if "--initial-status" in argv else "todo"}), ""
        if subcommand == "block":
            return 0, f"{argv[5]} blocked", ""
        if subcommand == "notify-subscribe":
            return 0, f"Subscribed to {argv[5]}", ""
        if subcommand == "comment":
            return 0, f"Comment added to {argv[5]}", ""
        if subcommand == "complete":
            return 0, f"Completed {argv[5]}", ""
        return 1, "", f"unknown subcommand: {subcommand}"


def _create_argvs(runner: MockedHermesCliRunner) -> list[list[str]]:
    return [c for c in runner.calls if len(c) > 4 and c[4] == "create"]


def test_real_adapter_g421_needs_input_creates_exactly_one_owner_anchor(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    runner = MockedHermesCliRunner()
    adapter = RealBoardAdapter(runner=runner, board="warroom-os", hermes_bin="hermes")
    contracts = load_contract_registry([_G421_YAML])
    source = FakeBoardEventSource(db_identity="warroom-os-db", events=[_CONTROLLED_EVENT])

    result = ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)

    assert result["processed"] == 1
    instance = store.list_instances()[0]
    assert instance["state"] == "waiting_owner"

    create_calls = _create_argvs(runner)
    assert len(create_calls) == 1
    anchor_argv = create_calls[0]
    assert anchor_argv[:4] == ["hermes", "kanban", "--board", "warroom-os"]
    assert "--initial-status" in anchor_argv and "blocked" in anchor_argv

    subscribe_calls = [c for c in runner.calls if len(c) > 4 and c[4] == "notify-subscribe"]
    assert len(subscribe_calls) == 1
    assert "--platform" in subscribe_calls[0] and "discord" in subscribe_calls[0]
    assert "--chat-id" in subscribe_calls[0] and "research" in subscribe_calls[0]


def test_real_adapter_g421_no_downstream_cards_before_owner_receipt(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    runner = MockedHermesCliRunner()
    adapter = RealBoardAdapter(runner=runner, board="warroom-os")
    contracts = load_contract_registry([_G421_YAML])
    source = FakeBoardEventSource(db_identity="warroom-os-db", events=[_CONTROLLED_EVENT])

    ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)

    assert len(_create_argvs(runner)) == 1  # only the owner anchor


def test_real_adapter_g421_full_vertical_loop_creates_exactly_four_cards(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    runner = MockedHermesCliRunner()
    adapter = RealBoardAdapter(runner=runner, board="warroom-os")
    contracts = load_contract_registry([_G421_YAML])
    source = FakeBoardEventSource(db_identity="warroom-os-db", events=[_CONTROLLED_EVENT])

    ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)
    instance = store.get_instance(store.list_instances()[0]["id"])
    contract = contracts.get("warroom.g421.exposure-resolution.v1")
    handler = get_handler(ContinuationKind.NEEDS_INPUT)

    receipt_result = handler.on_receipt(
        instance,
        {
            "owner_ref": "operator-main",
            "fields": {
                "resolution_basis": "target_never_submitted",
                "approval_receipt_id": "recv_real_canary_0001",
                "owner_confirmation": True,
            },
            "source_ref": "sandbox:owner-input-fixture",
        },
        store=store,
        adapter=adapter,
        contract=contract,
    )
    assert receipt_result.success is True
    assert len(_create_argvs(runner)) == 2  # anchor + materialization

    instance = store.get_instance(instance["id"])
    go_review = handler.advance_after_materialization(instance, verdict="GO", store=store, adapter=adapter)
    assert go_review.success is True
    assert len(_create_argvs(runner)) == 3  # + review

    instance = store.get_instance(instance["id"])
    go_rerun = handler.advance_after_review(instance, verdict="GO", store=store, adapter=adapter)
    assert go_rerun.success is True
    assert len(_create_argvs(runner)) == 4  # + packet rerun
    assert store.get_instance(instance["id"])["state"] == "resumed"


def test_real_adapter_g421_duplicate_ingest_creates_zero_duplicate_cards(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    runner = MockedHermesCliRunner()
    adapter = RealBoardAdapter(runner=runner, board="warroom-os")
    contracts = load_contract_registry([_G421_YAML])
    source = FakeBoardEventSource(db_identity="warroom-os-db", events=[_CONTROLLED_EVENT])

    ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)
    dup = ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)

    assert dup["processed"] == 0
    assert len(_create_argvs(runner)) == 1
