"""Warroom G4.21 controlled vertical canary.

Uses a fresh controlled/synthetic needs_input source event (not a replay of
historical event ids) and the FakeBoardAdapter only. No exchange/private/
signed/live order API call and no Discord live send are reachable from this
path: the adapter is in-memory and the engine never imports a live gateway.
"""

from __future__ import annotations

from pathlib import Path

from agentflow_hermes.board_adapter import FakeBoardAdapter
from agentflow_hermes.board_events import BoardEvent, FakeBoardEventSource
from agentflow_hermes.continuation import get_handler
from agentflow_hermes.continuation_config import load_contract_registry
from agentflow_hermes.continuation_engine import ingest_board_once
from agentflow_hermes.continuation_store import ContinuationStore
from agentflow_hermes.outcome import ContinuationKind

_G421_YAML = Path(__file__).resolve().parents[1] / "contracts" / "warroom.g421.exposure-resolution.v1.yaml"

_CONTROLLED_EVENT = BoardEvent(
    event_id="g421-canary-synthetic-0001",
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

_OWNER_SUBMISSION = {
    "owner_ref": "operator-main",
    "fields": {
        "resolution_basis": "target_never_submitted",
        "approval_receipt_id": "recv_canary_0001",
        "owner_confirmation": True,
    },
    "source_ref": "sandbox:owner-input-fixture",
}


def _setup(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    contracts = load_contract_registry([_G421_YAML])
    source = FakeBoardEventSource(db_identity="warroom-os-db", events=[_CONTROLLED_EVENT])
    return store, adapter, contracts, source


def test_g421_needs_input_creates_exactly_one_owner_anchor_with_research_subscription(tmp_path):
    store, adapter, contracts, source = _setup(tmp_path)

    result = ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)

    assert result["processed"] == 1
    instances = store.list_instances()
    assert len(instances) == 1
    instance = instances[0]
    assert instance["state"] == "waiting_owner"
    assert instance["board"] == "warroom-os"

    anchor_tasks = [t for t in adapter.tasks.values() if t.get("kind") == "owner_anchor"]
    assert len(anchor_tasks) == 1
    anchor_task_id = anchor_tasks[0]["task_id"]
    assert adapter.subscriptions == [(anchor_task_id, "discord:#research")]


def test_g421_no_children_exist_before_owner_receipt(tmp_path):
    store, adapter, contracts, source = _setup(tmp_path)
    ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)
    instance_id = store.list_instances()[0]["id"]

    assert store.count_steps(instance_id, step_kind="materialization") == 0
    assert store.count_steps(instance_id, step_kind="review") == 0
    assert store.count_steps(instance_id, step_kind="packet_rerun") == 0
    assert len(adapter.tasks) == 1  # only the owner anchor


def test_g421_full_vertical_loop_owner_receipt_to_packet_rerun(tmp_path):
    store, adapter, contracts, source = _setup(tmp_path)
    ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)
    instance = store.get_instance(store.list_instances()[0]["id"])
    contract = contracts.get("warroom.g421.exposure-resolution.v1")
    handler = get_handler(ContinuationKind.NEEDS_INPUT)

    # Submit controlled, secret-free owner input bound to this continuation instance.
    receipt_result = handler.on_receipt(instance, _OWNER_SUBMISSION, store=store, adapter=adapter, contract=contract)
    assert receipt_result.success is True
    instance = store.get_instance(instance["id"])
    assert instance["state"] == "materializing"
    assert store.count_steps(instance["id"], step_kind="materialization") == 1
    assert len(store.list_owner_receipts(instance["id"])) == 1

    # Review must not be runnable before materialization's own semantic GO —
    # lifecycle `done` alone must never grant it (there is no `done` concept
    # modeled here at all; only an explicit verdict advances state).
    blocked_review = handler.advance_after_materialization(instance, verdict="BLOCK", store=store, adapter=adapter)
    assert blocked_review.success is False
    assert store.count_steps(instance["id"], step_kind="review") == 0

    # Retry: a failed materialization attempt returns to MATERIALIZING before a
    # later real attempt can report its own GO.
    from agentflow_hermes.continuation_store import ContinuationState

    store.transition(instance["id"], ContinuationState.MATERIALIZING, reason="retry")
    go_review = handler.advance_after_materialization(store.get_instance(instance["id"]), verdict="GO", store=store, adapter=adapter)
    assert go_review.success is True
    instance = store.get_instance(instance["id"])
    assert instance["state"] == "waiting_review"
    assert store.count_steps(instance["id"], step_kind="review") == 1

    # Packet-rerun must not be runnable before review's own semantic GO.
    blocked_rerun = handler.advance_after_review(instance, verdict="BLOCK", store=store, adapter=adapter)
    assert blocked_rerun.success is False
    assert store.count_steps(instance["id"], step_kind="packet_rerun") == 0

    store.transition(instance["id"], ContinuationState.WAITING_REVIEW, reason="retry")
    go_rerun = handler.advance_after_review(store.get_instance(instance["id"]), verdict="GO", store=store, adapter=adapter)
    assert go_rerun.success is True
    instance = store.get_instance(instance["id"])
    assert instance["state"] == "resumed"
    assert store.count_steps(instance["id"], step_kind="packet_rerun") == 1

    # Exactly: 1 owner anchor + 1 materialization + 1 review + 1 packet rerun.
    assert len(adapter.tasks) == 4


def test_g421_owner_receipt_never_invents_omitted_fields(tmp_path):
    store, adapter, contracts, source = _setup(tmp_path)
    ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)
    instance = store.get_instance(store.list_instances()[0]["id"])
    contract = contracts.get("warroom.g421.exposure-resolution.v1")
    handler = get_handler(ContinuationKind.NEEDS_INPUT)

    incomplete_submission = {
        "owner_ref": "operator-main",
        "fields": {"resolution_basis": "target_never_submitted"},  # missing approval_receipt_id/owner_confirmation
    }
    result = handler.on_receipt(instance, incomplete_submission, store=store, adapter=adapter, contract=contract)

    assert result.success is False
    assert any("approval_receipt_id" in e for e in result.metadata["errors"])
    assert any("owner_confirmation" in e for e in result.metadata["errors"])
    assert store.get_instance(instance["id"])["state"] == "waiting_owner"
    assert store.list_owner_receipts(instance["id"]) == []


def test_g421_duplicate_ingest_and_submit_create_zero_duplicate_cards(tmp_path):
    store, adapter, contracts, source = _setup(tmp_path)
    ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)
    # Duplicate ingest of the exact same fixture (cursor already advanced).
    dup = ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)
    assert dup["processed"] == 0
    assert len(store.list_instances()) == 1
    assert len(adapter.tasks) == 1

    instance = store.get_instance(store.list_instances()[0]["id"])
    contract = contracts.get("warroom.g421.exposure-resolution.v1")
    handler = get_handler(ContinuationKind.NEEDS_INPUT)
    handler.on_receipt(instance, _OWNER_SUBMISSION, store=store, adapter=adapter, contract=contract)
    tasks_after_first_submit = len(adapter.tasks)

    # Duplicate submit is refused because the instance already left WAITING_OWNER.
    dup_submit = handler.on_receipt(store.get_instance(instance["id"]), _OWNER_SUBMISSION, store=store, adapter=adapter, contract=contract)
    assert dup_submit.success is False
    assert len(adapter.tasks) == tasks_after_first_submit  # no duplicate materialization card

    # Retry (re-enqueue the same stable, source-scoped idempotency key that was
    # actually used to create the materialization step) creates zero duplicate
    # cards. The key is derived from the instance's durable source-scoped
    # idempotency_key, not the local row id, so this must be looked up from
    # the store rather than reconstructed from instance["id"].
    materialization_step = next(s for s in store.list_steps(instance["id"]) if s["step_kind"] == "materialization")
    retry_step = store.add_step(
        instance["id"], step_kind="materialization", idempotency_key=materialization_step["idempotency_key"]
    )
    assert retry_step["created"] is False
    assert len(adapter.tasks) == tasks_after_first_submit


def test_g421_canary_never_touches_a_live_gateway_or_subprocess(tmp_path):
    """FakeBoardAdapter is pure in-memory: no subprocess/network call is
    reachable, so no exchange/private/signed/live order API and no Discord
    live send can occur from this canary path."""
    store, adapter, contracts, source = _setup(tmp_path)
    # FakeBoardAdapter has no CLI/subprocess runner and no gateway dependency,
    # so nothing in this canary path can reach a live exchange or Discord send.
    assert not hasattr(adapter, "runner")
    assert not hasattr(adapter, "hermes_bin")
    import inspect

    from agentflow_hermes import board_adapter as board_adapter_module

    source_text = inspect.getsource(board_adapter_module.FakeBoardAdapter)
    assert "subprocess" not in source_text
    assert "gateway" not in source_text.lower()

    ingest_board_once(board="warroom-os", source=source, store=store, contract_registry=contracts, adapter=adapter)
