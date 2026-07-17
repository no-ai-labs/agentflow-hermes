"""Router contract shared by all continuation handlers (GO/code-fix/needs-input/...).

Handlers depend on the durable ``ContinuationStore`` and an injectable board
adapter; they never talk to a real board directly, mirroring the existing
fake/real adapter split in ``graph_creator.py``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..outcome import ContinuationKind, OutcomeEnvelope

CALLBACK_ONLY_OPERATIONS = {"schedule_origin_wake", "record_consumer_ack"}
CALLBACK_RETRY_CAP = 5


def pending_outbox_retry_not_due(store: Any, instance_id: int, *, now: float | None = None) -> bool:
    """Return True when an existing pending outbox row is still in backoff.

    Handlers call this before re-entering MATERIALIZING from FAILED_RETRYABLE.
    A non-due outbox is already the durable retry state; toggling the instance
    FAILED_RETRYABLE -> MATERIALIZING -> FAILED_RETRYABLE on every daemon tick
    creates an event storm without making any external mutation eligible.
    """
    # Prefer the store helper when present: it implements the precise contract
    # that all pending rows are in the future. If one pending row is due, the
    # handler must re-enter materialization so that due work can converge.
    helper = getattr(store, "outbox_pending_retry_at", None)
    if helper is not None:
        return helper(instance_id, now=now) is not None

    due_at = time.time() if now is None else now
    with store.connect() as con:
        row = con.execute(
            """
            select 1 from board_outbox
             where continuation_id=?
               and state='pending'
               and coalesce(next_attempt_at, 0) > ?
               and not exists (
                   select 1 from board_outbox due
                    where due.continuation_id=board_outbox.continuation_id
                      and due.state='pending'
                      and coalesce(due.next_attempt_at, 0) <= ?
               )
             limit 1
            """,
            (instance_id, due_at, due_at),
        ).fetchone()
    return row is not None


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
    if operation == "schedule_origin_wake":
        return _apply_schedule_origin_wake(store, row, payload, adapter)
    if operation == "record_consumer_ack":
        return _apply_record_consumer_ack(store, row, payload, adapter)
    if row["state"] == "applied":
        return {"success": True, "task_id": row.get("board_task_id", "")}
    if row["state"] == "pending" and float(row.get("next_attempt_at") or 0) > time.time():
        return {"success": False, "error": "outbox_retry_not_due"}
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
    if result.get("success") and not _has_failed_nested_ack(result):
        task_id = result.get("task_id", "")
        store.outbox_mark(row["id"], state="applied", board_task_id=task_id)
        return {"success": True, "task_id": task_id}
    error = result.get("error")
    if not error and _has_failed_nested_ack(result):
        ack = result.get("ack")
        error = ack.get("error", "ack_ensure_failed") if isinstance(ack, dict) else "ack_malformed"
    safe_error = str(error or "adapter_error")[:200]
    attempts_after = int(row.get("attempts") or 0) + 1
    # Durable exponential-ish backoff with a bounded floor/ceiling: enough to
    # prevent the 100+ attempts/seconds hot loop while still allowing a daemon
    # restart or reconcile pass to make a single due retry after recovery.
    delay = min(300.0, max(5.0, 5.0 * (2 ** min(attempts_after - 1, 6))))
    store.outbox_mark(row["id"], state="pending", next_attempt_at=time.time() + delay, last_error=safe_error)
    return {"success": False, "error": safe_error}


def _apply_schedule_origin_wake(store: Any, row: dict[str, Any], payload: dict[str, Any], adapter: Any) -> dict[str, Any]:
    task_id = str(payload.get("task_id") or "")
    endpoint = str(payload.get("endpoint") or "")
    if row["state"] == "applied":
        if _origin_wake_satisfied(adapter, task_id, endpoint):
            return {"success": True, "task_id": row.get("board_task_id", "")}
        _mark_outbox_pending(store, row, "origin_wake_not_yet_accepted")
        return {"success": False, "error": "origin_wake_not_yet_accepted"}
    if row["state"] == "pending" and float(row.get("next_attempt_at") or 0) > time.time():
        return {"success": False, "error": "outbox_retry_not_due"}
    if adapter is None:
        return {"success": False, "error": "no_adapter"}
    if _origin_wake_satisfied(adapter, task_id, endpoint):
        store.outbox_mark(row["id"], state="applied")
        return {"success": True, "task_id": row.get("board_task_id", "")}
    if str(row.get("last_error") or "") == "origin_wake_not_yet_accepted":
        _mark_outbox_pending(store, row, "origin_wake_not_yet_accepted")
        return {"success": False, "error": "origin_wake_not_yet_accepted"}
    schedule = getattr(adapter, "schedule_origin_wake", None)
    if schedule is None:
        return {"success": False, "error": "adapter_missing_schedule_origin_wake"}
    result = schedule(task_id, endpoint)
    if result.get("success") and _origin_wake_satisfied(adapter, task_id, endpoint):
        store.outbox_mark(row["id"], state="applied")
        return {"success": True, "task_id": row.get("board_task_id", "")}
    error = str(result.get("error") or "origin_wake_not_yet_accepted")[:200]
    _mark_outbox_pending(store, row, error)
    return {"success": False, "error": error}


def _origin_wake_satisfied(adapter: Any, task_id: str, endpoint: str) -> bool:
    check = getattr(adapter, "origin_wake_satisfied", None)
    if check is None:
        return False
    result = check(task_id, endpoint)
    return bool(result and result.get("success"))


def _apply_record_consumer_ack(store: Any, row: dict[str, Any], payload: dict[str, Any], adapter: Any) -> dict[str, Any]:
    task_id = str(payload.get("task_id") or "")
    endpoint = str(payload.get("endpoint") or "")
    status = str(payload.get("status") or "")
    if adapter is None:
        return {"success": False, "error": "no_adapter"}
    check = getattr(adapter, "consumer_ack_satisfied", None)
    if row["state"] == "applied":
        if check is not None:
            verified = check(task_id, endpoint, status)
            if verified and verified.get("success"):
                return {"success": True, "task_id": row.get("board_task_id", "")}
        _mark_outbox_pending(store, row, "consumer_ack_not_verified")
        return {"success": False, "error": "consumer_ack_not_verified"}
    if row["state"] == "pending" and float(row.get("next_attempt_at") or 0) > time.time():
        return {"success": False, "error": "outbox_retry_not_due"}
    record = getattr(adapter, "record_consumer_ack", None)
    if record is None:
        return {"success": False, "error": "adapter_missing_record_consumer_ack"}
    result = record(task_id, endpoint, status)
    if result.get("success"):
        verified = check(task_id, endpoint, status) if check is not None else result
        if verified and verified.get("success"):
            store.outbox_mark(row["id"], state="applied")
            return {"success": True, "task_id": row.get("board_task_id", "")}
        result = {"success": False, "error": "consumer_ack_not_verified", "ack": verified}
    error = str(result.get("error") or "consumer_ack_failed")[:200]
    _mark_outbox_pending(store, row, error)
    return {"success": False, "error": error}


def _mark_outbox_pending(store: Any, row: dict[str, Any], error: str) -> None:
    attempts_after = int(row.get("attempts") or 0) + 1
    if str(row.get("operation") or "") in CALLBACK_ONLY_OPERATIONS and attempts_after >= CALLBACK_RETRY_CAP:
        store.outbox_mark(row["id"], state="callback_deadletter", next_attempt_at=0, last_error=error)
        return
    delay = min(300.0, max(5.0, 5.0 * (2 ** min(attempts_after - 1, 6))))
    store.outbox_mark(row["id"], state="pending", next_attempt_at=time.time() + delay, last_error=error)


def _has_failed_nested_ack(result: dict[str, Any]) -> bool:
    """Structurally fail closed on a required durable ACK/active-wake repair
    that a subscribe-shaped adapter result nests under ``ack``: notify+wake
    plus ACK repair is one semantic operation, so a top-level ``success: True``
    alongside a missing/malformed/failed nested ``ack`` must never be treated
    as an applied board mutation."""
    if "ack" not in result:
        return False
    ack = result.get("ack")
    if not isinstance(ack, dict):
        return True
    return not ack.get("success")


class ContinuationHandler(Protocol):
    kind: ContinuationKind

    def plan(self, outcome: OutcomeEnvelope, *, store: Any, adapter: Any, contract: Any) -> ContinuationPlan: ...

    def on_receipt(
        self, instance: dict[str, Any], submission: dict[str, Any], *, store: Any, adapter: Any, contract: Any
    ) -> StepResult: ...
