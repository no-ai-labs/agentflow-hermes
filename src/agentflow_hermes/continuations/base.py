"""Router contract shared by all continuation handlers (GO/code-fix/needs-input/...).

Handlers depend on the durable ``ContinuationStore`` and an injectable board
adapter; they never talk to a real board directly, mirroring the existing
fake/real adapter split in ``graph_creator.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..outcome import ContinuationKind, OutcomeEnvelope


@dataclass(frozen=True)
class ContinuationPlan:
    instance_id: int
    created: bool
    state: str
    step_intents: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class StepResult:
    success: bool
    reason: str = ""
    state: str = ""
    created_step_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class BoardAdapterLike(Protocol):
    def create_task(self, intent: dict[str, Any]) -> dict[str, Any]: ...
    def subscribe(self, task_id: str, endpoint: str) -> dict[str, Any]: ...
    def complete_owner_anchor(self, task_id: str, *, receipt_ref: str) -> dict[str, Any]: ...


def apply_board_operation(
    store: Any,
    instance_id: int,
    *,
    step_id: int | str,
    operation: str,
    payload: dict[str, Any],
    idempotency_key: str,
    adapter: Any,
) -> dict[str, Any]:
    """Durable outbox intent/attempt/applied cycle for a single board mutation.

    Enqueues (idempotent by key) before ever calling the adapter, so a crash or
    adapter failure between enqueue and apply leaves a durable ``pending`` row
    that ``continuation retry``/``reconcile_outbox`` can see and replay instead
    of an external mutation that only ever existed as a direct, unrecorded
    adapter call. Shared by every continuation handler (owner-input, code-fix)
    so the durability contract is written once."""
    enqueued = store.outbox_enqueue(
        instance_id, step_id=str(step_id), operation=operation, payload=payload, idempotency_key=idempotency_key
    )
    row = enqueued["outbox"]
    if row["state"] == "applied" and row.get("board_task_id"):
        return {"success": True, "task_id": row["board_task_id"]}
    if adapter is None:
        return {"success": False, "error": "no_adapter"}
    if operation == "create_task":
        result = adapter.create_task(payload)
    elif operation == "subscribe":
        result = adapter.subscribe(str(payload.get("task_id") or ""), str(payload.get("endpoint") or ""))
    elif operation == "complete_owner_anchor":
        result = adapter.complete_owner_anchor(
            str(payload.get("task_id") or ""), receipt_ref=str(payload.get("receipt_ref") or "")
        )
    else:
        result = {"success": False, "error": "unknown_outbox_operation"}
    if result.get("success"):
        task_id = result.get("task_id", "")
        store.outbox_mark(row["id"], state="applied", board_task_id=task_id)
        return {"success": True, "task_id": task_id}
    store.outbox_mark(row["id"], state="pending")
    return {"success": False, "error": result.get("error", "adapter_error")}


class ContinuationHandler(Protocol):
    kind: ContinuationKind

    def plan(self, outcome: OutcomeEnvelope, *, store: Any, adapter: Any, contract: Any) -> ContinuationPlan: ...

    def on_receipt(
        self, instance: dict[str, Any], submission: dict[str, Any], *, store: Any, adapter: Any, contract: Any
    ) -> StepResult: ...
