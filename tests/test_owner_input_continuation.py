from __future__ import annotations

from typing import Any

from agentflow_hermes.continuation_store import ContinuationState, ContinuationStore
from agentflow_hermes.continuations.owner_input import OwnerInputHandler
from agentflow_hermes.input_contract import ArtifactSpec, FieldAuthority, InputContract, InputField
from agentflow_hermes.outcome import ContinuationKind, OutcomeEnvelope, RequirementRef, Verdict


class FakeAdapter:
    def __init__(self) -> None:
        self.created_tasks: list[dict[str, Any]] = []
        self.subscriptions: list[tuple[str, str]] = []
        self.completed_anchors: list[tuple[str, str]] = []
        self._next_id = 0

    def create_task(self, intent: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        task_id = f"task:{self._next_id}"
        self.created_tasks.append({**intent, "task_id": task_id})
        return {"success": True, "task_id": task_id}

    def subscribe(self, task_id: str, endpoint: str) -> dict[str, Any]:
        self.subscriptions.append((task_id, endpoint))
        return {"success": True}

    def complete_owner_anchor(self, task_id: str, *, receipt_ref: str) -> dict[str, Any]:
        self.completed_anchors.append((task_id, receipt_ref))
        return {"success": True}


def _contract() -> InputContract:
    return InputContract(
        contract_ref="warroom.g421.exposure-resolution.v1",
        version=1,
        owner_role="warroom-owner",
        fields=(
            InputField(name="resolution_basis", value_type="enum", authority=FieldAuthority.OWNER, allowed_values=("target_never_submitted",)),
            InputField(name="approval_receipt_id", value_type="opaque_id", authority=FieldAuthority.OWNER),
            InputField(name="owner_confirmation", value_type="boolean", authority=FieldAuthority.OWNER),
        ),
        artifacts=(
            ArtifactSpec(artifact_id="evidence", template_path="t.json", final_path="f.json", write_mode="materialize"),
        ),
        resume_transition="warroom.g421.packet-rerun",
    )


def _outcome(**overrides) -> OutcomeEnvelope:
    kwargs = dict(
        schema_version=1,
        event_id="ev_1",
        board="warroom-os",
        source_task_id="t_ab93a206",
        source_graph_id="g_1",
        verdict=Verdict.BLOCK,
        continuation_kind=ContinuationKind.NEEDS_INPUT,
        contract_ref="warroom.g421.exposure-resolution.v1",
        origin_ref="discord:#research",
        return_to_ref="discord:#research",
        requirements=(RequirementRef(name="approval_receipt_id", authority="owner"),),
    )
    kwargs.update(overrides)
    return OutcomeEnvelope(**kwargs)


def test_needs_input_plan_creates_one_waiting_owner_instance(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeAdapter()
    handler = OwnerInputHandler()

    plan = handler.plan(_outcome(), store=store, adapter=adapter, contract=_contract())

    instance = store.get_instance(plan.instance_id)
    assert instance["state"] == ContinuationState.WAITING_OWNER.value
    assert len(store.list_instances()) == 1
    assert len(adapter.created_tasks) == 1
    assert adapter.subscriptions == [(adapter.created_tasks[0]["task_id"], "discord:#research")]


def test_board_idempotency_keys_are_stable_and_differ_across_fresh_stores_sharing_row_id(tmp_path):
    """Two independent fresh stores (e.g. two canary runs, or a temp store vs
    the canonical store) commonly both assign row id 1 to their first
    instance. Board mutation idempotency keys must be derived from the
    instance's durable source-scoped key, not the row id, so they never
    collide across unrelated continuations/runs/boards."""
    store_a = ContinuationStore(tmp_path / "a" / "agentflow.sqlite")
    store_b = ContinuationStore(tmp_path / "b" / "agentflow.sqlite")
    adapter_a = FakeAdapter()
    adapter_b = FakeAdapter()
    handler = OwnerInputHandler()

    plan_a = handler.plan(
        _outcome(event_id="ev_a", source_task_id="t_a"), store=store_a, adapter=adapter_a, contract=_contract()
    )
    plan_b = handler.plan(
        _outcome(event_id="ev_b", source_task_id="t_b"), store=store_b, adapter=adapter_b, contract=_contract()
    )

    assert plan_a.instance_id == 1
    assert plan_b.instance_id == 1  # same local row id in each fresh store

    key_a = store_a.list_steps(plan_a.instance_id)[0]["idempotency_key"]
    key_b = store_b.list_steps(plan_b.instance_id)[0]["idempotency_key"]
    assert key_a != key_b
    assert not key_a.endswith(":1")
    assert not key_b.endswith(":1")
    assert key_a.startswith("owner_anchor:")
    assert key_b.startswith("owner_anchor:")


def test_board_idempotency_key_is_stable_across_repeat_calls_for_same_instance(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeAdapter()
    handler = OwnerInputHandler()

    plan = handler.plan(_outcome(), store=store, adapter=adapter, contract=_contract())
    key_first = store.list_steps(plan.instance_id)[0]["idempotency_key"]

    # Re-plan the same source event: duplicate ingest must resolve to the same
    # instance and the same board idempotency key (no duplicate anchor card).
    plan_again = handler.plan(_outcome(), store=store, adapter=adapter, contract=_contract())
    key_again = store.list_steps(plan_again.instance_id)[0]["idempotency_key"]

    assert key_first == key_again
    assert len(adapter.created_tasks) == 1


def test_repeated_event_returns_existing_anchor_without_duplicate(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeAdapter()
    handler = OwnerInputHandler()

    first = handler.plan(_outcome(), store=store, adapter=adapter, contract=_contract())
    second = handler.plan(_outcome(), store=store, adapter=adapter, contract=_contract())

    assert first.instance_id == second.instance_id
    assert len(store.list_instances()) == 1
    assert len(adapter.created_tasks) == 1  # no duplicate owner anchor card


def test_no_downstream_children_before_owner_receipt(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeAdapter()
    handler = OwnerInputHandler()

    plan = handler.plan(_outcome(), store=store, adapter=adapter, contract=_contract())

    assert store.count_steps(plan.instance_id, step_kind="materialization") == 0
    assert store.count_steps(plan.instance_id, step_kind="review") == 0
    assert store.count_steps(plan.instance_id, step_kind="packet_rerun") == 0


def test_missing_owner_field_is_refused_without_state_advance(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeAdapter()
    handler = OwnerInputHandler()
    plan = handler.plan(_outcome(), store=store, adapter=adapter, contract=_contract())

    result = handler.on_receipt(
        store.get_instance(plan.instance_id),
        {"owner_ref": "operator-main", "fields": {"resolution_basis": "target_never_submitted"}},
        store=store,
        adapter=adapter,
        contract=_contract(),
    )

    assert result.success is False
    instance = store.get_instance(plan.instance_id)
    assert instance["state"] == ContinuationState.WAITING_OWNER.value
    assert store.list_owner_receipts(plan.instance_id) == []
    assert len(adapter.created_tasks) == 1  # only the owner anchor; no materialization


def test_valid_receipt_advances_state_and_creates_one_materialization_task(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeAdapter()
    handler = OwnerInputHandler()
    plan = handler.plan(_outcome(), store=store, adapter=adapter, contract=_contract())

    result = handler.on_receipt(
        store.get_instance(plan.instance_id),
        {
            "owner_ref": "operator-main",
            "fields": {
                "resolution_basis": "target_never_submitted",
                "approval_receipt_id": "recv_1",
                "owner_confirmation": True,
            },
        },
        store=store,
        adapter=adapter,
        contract=_contract(),
    )

    assert result.success is True
    instance = store.get_instance(plan.instance_id)
    assert instance["state"] == ContinuationState.MATERIALIZING.value
    assert len(store.list_owner_receipts(plan.instance_id)) == 1
    assert store.count_steps(plan.instance_id, step_kind="materialization") == 1
    assert len(adapter.created_tasks) == 2  # owner anchor + exactly one materialization task
    assert adapter.completed_anchors  # owner anchor completed with receipt ref


def _accepted_instance(store, adapter):
    handler = OwnerInputHandler()
    plan = handler.plan(_outcome(), store=store, adapter=adapter, contract=_contract())
    handler.on_receipt(
        store.get_instance(plan.instance_id),
        {
            "owner_ref": "operator-main",
            "fields": {
                "resolution_basis": "target_never_submitted",
                "approval_receipt_id": "recv_1",
                "owner_confirmation": True,
            },
        },
        store=store,
        adapter=adapter,
        contract=_contract(),
    )
    return handler, store.get_instance(plan.instance_id)


def test_materialization_block_does_not_create_review(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeAdapter()
    handler, instance = _accepted_instance(store, adapter)

    result = handler.advance_after_materialization(instance, verdict="BLOCK", store=store, adapter=adapter)

    assert result.success is False
    assert store.count_steps(instance["id"], step_kind="review") == 0


def test_materialization_go_creates_exactly_one_review_task(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeAdapter()
    handler, instance = _accepted_instance(store, adapter)

    result = handler.advance_after_materialization(instance, verdict="GO", store=store, adapter=adapter)
    duplicate = handler.advance_after_materialization(store.get_instance(instance["id"]), verdict="GO", store=store, adapter=adapter)

    assert result.success is True
    assert store.get_instance(instance["id"])["state"] == "waiting_review"
    assert store.count_steps(instance["id"], step_kind="review") == 1
    assert duplicate.success is False  # not_materializing any more; no duplicate review


def test_review_block_does_not_create_packet_rerun(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeAdapter()
    handler, instance = _accepted_instance(store, adapter)
    handler.advance_after_materialization(instance, verdict="GO", store=store, adapter=adapter)
    instance = store.get_instance(instance["id"])

    result = handler.advance_after_review(instance, verdict="BLOCK", store=store, adapter=adapter)

    assert result.success is False
    assert store.count_steps(instance["id"], step_kind="packet_rerun") == 0


def test_review_go_creates_exactly_one_packet_rerun_and_resumes(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeAdapter()
    handler, instance = _accepted_instance(store, adapter)
    handler.advance_after_materialization(instance, verdict="GO", store=store, adapter=adapter)
    instance = store.get_instance(instance["id"])

    result = handler.advance_after_review(instance, verdict="GO", store=store, adapter=adapter)

    assert result.success is True
    assert store.get_instance(instance["id"])["state"] == "resumed"
    assert store.count_steps(instance["id"], step_kind="packet_rerun") == 1


def test_continuation_handler_registry_resolves_needs_input_handler():
    from agentflow_hermes.continuation import get_handler

    handler = get_handler(ContinuationKind.NEEDS_INPUT)
    assert isinstance(handler, OwnerInputHandler)
    assert get_handler(ContinuationKind.UNKNOWN) is None


def test_duplicate_receipt_submission_does_not_duplicate_materialization(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeAdapter()
    handler = OwnerInputHandler()
    plan = handler.plan(_outcome(), store=store, adapter=adapter, contract=_contract())
    submission = {
        "owner_ref": "operator-main",
        "fields": {
            "resolution_basis": "target_never_submitted",
            "approval_receipt_id": "recv_1",
            "owner_confirmation": True,
        },
    }
    handler.on_receipt(store.get_instance(plan.instance_id), submission, store=store, adapter=adapter, contract=_contract())
    second = handler.on_receipt(store.get_instance(plan.instance_id), submission, store=store, adapter=adapter, contract=_contract())

    assert second.success is False
    assert second.reason == "not_waiting_owner"
    assert len(adapter.created_tasks) == 2
