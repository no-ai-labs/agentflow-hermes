"""M30C remediation: apply_board_operation must fail closed when an adapter
returns a structurally-successful top-level result whose nested ACK/active-wake
repair failed (or is malformed) — e.g. RealBoardAdapter.subscribe returning
``{"success": True, "ack": {"success": False, "error": "ack_schema_missing"}}``.
A nested ACK failure is a failed apply: the outbox row must stay pending (not
applied) and the caller must see ``success: False``.
"""

from __future__ import annotations

from typing import Any

from agentflow_hermes.continuation_store import ContinuationStore
from agentflow_hermes.continuations.base import apply_board_operation


def _instance_id(store: ContinuationStore) -> int:
    creation = store.create_instance(
        board="warroom-os",
        source_task_id="t_1",
        source_event_id="e_1",
        contract_ref="c_1",
        verdict="BLOCK",
        continuation_kind="code_fix",
    )
    return creation["instance"]["id"]


class _NestedAckFailureAdapter:
    def __init__(self, ack: Any) -> None:
        self._ack = ack

    def subscribe(self, task_id: str, endpoint: str) -> dict[str, Any]:
        return {"success": True, "ack": self._ack}


def test_subscribe_with_nested_ack_failure_is_a_failed_apply(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    instance_id = _instance_id(store)
    adapter = _NestedAckFailureAdapter({"success": False, "error": "ack_schema_missing"})

    result = apply_board_operation(
        store, instance_id, step_id="0", operation="subscribe",
        payload={"task_id": "t_1", "endpoint": "discord:#research"},
        idempotency_key="subscribe:t_1", adapter=adapter,
    )

    assert result["success"] is False
    rows = store.list_outbox()
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"


def test_subscribe_with_malformed_nested_ack_is_a_failed_apply(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    instance_id = _instance_id(store)
    # A non-dict ack (or one missing "success") is malformed, not proof of
    # success -- must not be trusted as an applied ACK repair.
    adapter = _NestedAckFailureAdapter("not-a-dict")

    result = apply_board_operation(
        store, instance_id, step_id="0", operation="subscribe",
        payload={"task_id": "t_1", "endpoint": "discord:#research"},
        idempotency_key="subscribe:t_1", adapter=adapter,
    )

    assert result["success"] is False
    rows = store.list_outbox()
    assert rows[0]["state"] == "pending"


def test_subscribe_with_successful_nested_ack_still_applies(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    instance_id = _instance_id(store)
    adapter = _NestedAckFailureAdapter({"success": True})

    result = apply_board_operation(
        store, instance_id, step_id="0", operation="subscribe",
        payload={"task_id": "t_1", "endpoint": "discord:#research"},
        idempotency_key="subscribe:t_1", adapter=adapter,
    )

    assert result["success"] is True
    rows = store.list_outbox()
    assert rows[0]["state"] == "applied"


def test_subscribe_with_no_ack_key_still_applies(tmp_path):
    """Non-subscribe operations, or a Fake adapter that never nests an "ack"
    key at all, must not be penalized -- only a *present* nested ack decides
    anything."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    instance_id = _instance_id(store)

    class _PlainSubscribeAdapter:
        def subscribe(self, task_id: str, endpoint: str) -> dict[str, Any]:
            return {"success": True}

    result = apply_board_operation(
        store, instance_id, step_id="0", operation="subscribe",
        payload={"task_id": "t_1", "endpoint": "discord:#research"},
        idempotency_key="subscribe:t_1", adapter=_PlainSubscribeAdapter(),
    )

    assert result["success"] is True
    rows = store.list_outbox()
    assert rows[0]["state"] == "applied"
