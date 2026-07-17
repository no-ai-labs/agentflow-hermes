"""SemanticRefusalHandler: the semantic_refusal continuation kind.

An unsafe reviewer BLOCK/NEED_MORE — one whose blocker names a category that
must never be auto-remediated (credentials/secrets, destructive/data-loss,
owner-only proof or user input, live-money/financial approval) — is compiled to
``ContinuationKind.SEMANTIC_REFUSAL`` by ``outcome_compiler.py`` rather than to a
CODE_FIX. This handler materializes that refusal WITHOUT creating any
fix/review/activation board task:

1. It records a durable, idempotent semantic-refusal ACK (a requirement
   satisfaction carrying the unsafe categories + offending blockers) — an
   explicit acknowledgement distinct from any passive board delivery.
2. It checks for a durable prior wake receipt for the *source* task's trusted
   origin, and otherwise schedules a typed origin wake via the board's own
   Kanban ``wake-origin`` path, waiting for a durable accepted/started/
   completed wake status before proceeding. It never sends anything to
   AgentFlow/Discord directly, and never uses the generic notify-subscribe
   path (that is a passive notify, not a typed origin wake).
3. It parks the instance in the durable ``BLOCKED_INVALID`` quarantine state —
   explicitly NOT a successful CODE_FIX advance.

Every board mutation goes through the shared durable outbox cycle
(``apply_board_operation``); a failure parks the instance in
``FAILED_RETRYABLE`` and returns ``success=False`` so the caller fails the
cursor closed and replays the whole event later. Replay/restart re-derives the
same source-scoped idempotency keys and upserts the same ACK, so zero duplicate
ACKs/wakes are ever created (plan/M30A remediation items 2/3).
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from ..continuation_store import ContinuationState, ContinuationStore
from ..outcome import ContinuationKind, OutcomeEnvelope
from .base import StepResult, apply_board_operation


def _stable_digest(instance: dict[str, Any]) -> str:
    base = str(instance.get("idempotency_key") or "")
    if not base:
        base = f"instance:{instance.get('board', '')}:{instance.get('id', '')}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


class SemanticRefusalHandler:
    kind = ContinuationKind.SEMANTIC_REFUSAL

    def materialize(
        self,
        outcome: OutcomeEnvelope,
        *,
        store: ContinuationStore,
        adapter: Any,
        blockers: tuple[str, ...] = (),
        refusal_categories: tuple[str, ...] = (),
    ) -> StepResult:
        """Durably quarantine an unsafe BLOCK and wake the trusted origin.
        Idempotent and fail-closed; creates zero fix/review/activation tasks."""
        creation = store.create_instance(
            board=outcome.board,
            source_task_id=outcome.source_task_id,
            source_event_id=outcome.event_id,
            source_graph_id=outcome.source_graph_id,
            contract_ref=outcome.contract_ref,
            verdict=outcome.verdict.value,
            continuation_kind=outcome.continuation_kind.value,
            origin_ref=outcome.origin_ref,
            return_to_ref=outcome.return_to_ref,
            workspace_ref=outcome.workspace_ref,
        )
        instance = creation["instance"]
        instance_id = instance["id"]

        if adapter is None:
            return StepResult(
                success=False,
                reason="no_adapter",
                state=instance["state"],
                metadata={"instance_id": instance_id, "created": creation["created"]},
            )

        # Normalize into MATERIALIZING from a fresh detection or a prior failed
        # attempt so a retry re-derives the same terminal quarantine.
        if instance["state"] in (
            ContinuationState.DETECTED.value,
            ContinuationState.FAILED_RETRYABLE.value,
        ):
            store.transition(instance_id, ContinuationState.MATERIALIZING, reason="semantic_refusal_detected")
            instance = store.get_instance(instance_id)
        assert instance is not None

        # 1) Internal semantic safety ACK + quarantine. This is the durable
        # semantic state that protects downstream dependencies/cursor progress;
        # callback delivery is recorded separately below and must never roll it
        # back when the source has no typed origin (M31B cursor poison fix).
        digest = _stable_digest(instance)
        endpoint = outcome.return_to_ref or outcome.origin_ref
        store.record_requirement_satisfaction(
            instance_id,
            field_name="semantic_refusal",
            value={"categories": list(refusal_categories), "blockers": list(blockers)},
            source_kind="semantic_refusal_ack",
            source_ref=",".join(refusal_categories),
        )
        if instance["state"] != ContinuationState.BLOCKED_INVALID.value:
            store.transition(instance_id, ContinuationState.BLOCKED_INVALID, reason="semantic_refusal_quarantined")

        # 2) Best-effort callback routing ledger. A missing/no-origin callback
        # edge is a callback limitation, not a semantic materialization failure.
        # The outbox rows below remain durable for reconcile/doctor, have a
        # bounded retry/deadletter policy in continuations.base/reconcile_outbox,
        # and do not hold the board cursor after the quarantine is durable.
        callback_status = "not_requested"
        callback_errors: list[str] = []
        wake_satisfied = False
        consumer_ack_status = "not_requested"
        if endpoint:
            wake = self._ensure_origin_wake(
                store,
                instance_id,
                task_id=outcome.source_task_id,
                endpoint=endpoint,
                idempotency_key=f"semantic_refusal_wake:{digest}:{endpoint}",
                legacy_key_prefix=f"semantic_refusal_notify:{digest}:",
                adapter=adapter,
            )
            if wake.get("success"):
                wake_satisfied = True
                callback_status = "wake_satisfied"
            else:
                error = str(wake.get("error") or "refusal_wake_not_yet_accepted")
                callback_errors.append(error)
                callback_status = "callback_deferred"

            consumer_ack = apply_board_operation(
                store,
                instance_id,
                step_id="0",
                operation="record_consumer_ack",
                payload={
                    "task_id": outcome.source_task_id,
                    "endpoint": endpoint,
                    "status": "semantic_refusal_ack",
                },
                idempotency_key=f"semantic_refusal_consumer_ack:{digest}:{endpoint}",
                adapter=adapter,
            )
            if consumer_ack.get("success"):
                consumer_ack_status = "semantic_refusal_ack"
                callback_status = "delivered" if wake_satisfied else callback_status
            else:
                error = str(consumer_ack.get("error") or "consumer_ack_failed")
                callback_errors.append(error)
                consumer_ack_status = "callback_deferred"
                if error in {"typed_origin_missing", "consumer_ack_missing", "unparseable_origin_endpoint"}:
                    callback_status = "callback_unroutable"
                elif callback_status != "callback_unroutable":
                    callback_status = "callback_deferred"
        else:
            callback_status = "callback_unroutable"
            callback_errors.append("semantic_refusal_origin_missing")

        return StepResult(
            success=True,
            state=store.get_instance(instance_id)["state"],
            metadata={
                "instance_id": instance_id,
                "created": creation["created"],
                "refusal_ack": True,
                "consumer_ack_status": consumer_ack_status,
                "callback_status": callback_status,
                "callback_errors": callback_errors,
                "refusal_categories": list(refusal_categories),
                "subscribed": wake_satisfied,
                "endpoint": endpoint,
                "created_tasks": 0,
            },
        )

    @staticmethod
    def _origin_wake_satisfied(adapter: Any, task_id: str, endpoint: str) -> bool:
        check = getattr(adapter, "origin_wake_satisfied", None)
        if check is None:
            return False
        result = check(task_id, endpoint)
        return bool(result and result.get("success"))

    def _ensure_origin_wake(
        self,
        store: ContinuationStore,
        instance_id: int,
        *,
        task_id: str,
        endpoint: str,
        idempotency_key: str,
        legacy_key_prefix: str,
        adapter: Any,
    ) -> dict[str, Any]:
        """Schedule one typed origin wake, then wait for durable acceptance.

        ``wake-origin`` first records ``scheduled``. Scheduled is not semantic
        completion; the outbox row stays pending until a later durable receipt
        check sees accepted/started/completed. Once accepted, the same row is
        marked applied without issuing another wake-origin call. Legacy pending
        ``subscribe`` rows are reinterpreted in place for safe live replay.
        """
        store.outbox_reinterpret_pending(
            instance_id,
            from_operation="subscribe",
            key_prefix=legacy_key_prefix,
            to_operation="schedule_origin_wake",
            new_idempotency_key=idempotency_key,
        )
        enqueued = store.outbox_enqueue(
            instance_id,
            step_id="0",
            operation="schedule_origin_wake",
            payload={"task_id": task_id, "endpoint": endpoint},
            idempotency_key=idempotency_key,
        )
        row = enqueued["outbox"]
        if row["state"] == "applied":
            # Defense in depth for stale poisoned rows written by older
            # reconcile code: an applied outbox state is only trusted when the
            # authoritative durable wake receipt is now satisfied.
            if self._origin_wake_satisfied(adapter, task_id, endpoint):
                return {"success": True, "source": "outbox_applied_receipt_verified"}
            self._mark_wake_pending(store, row)
            return {"success": False, "error": "origin_wake_not_yet_accepted"}

        if self._origin_wake_satisfied(adapter, task_id, endpoint):
            store.outbox_mark(row["id"], state="applied")
            return {"success": True, "source": "origin_wake_receipt"}

        now = time.time()
        if float(row.get("next_attempt_at") or 0) > now:
            return {"success": False, "error": "outbox_retry_not_due"}

        # After a wake-origin call has been durably scheduled once, do not
        # issue duplicates while waiting for the gateway receipt transition.
        if str(row.get("last_error") or "") == "origin_wake_not_yet_accepted":
            self._mark_wake_pending(store, row)
            return {"success": False, "error": "origin_wake_not_yet_accepted"}

        schedule = getattr(adapter, "schedule_origin_wake", None)
        if schedule is None:
            return {"success": False, "error": "adapter_missing_schedule_origin_wake"}
        scheduled = schedule(task_id, endpoint)
        if not scheduled.get("success"):
            error = str(scheduled.get("error") or "origin_wake_schedule_failed")[:200]
            self._mark_wake_pending(store, row, error=error)
            return {"success": False, "error": error}
        if self._origin_wake_satisfied(adapter, task_id, endpoint):
            store.outbox_mark(row["id"], state="applied")
            return {"success": True, "source": "scheduled_and_accepted"}
        self._mark_wake_pending(store, row)
        return {"success": False, "error": "origin_wake_not_yet_accepted"}

    @staticmethod
    def _mark_wake_pending(
        store: ContinuationStore, row: dict[str, Any], *, error: str = "origin_wake_not_yet_accepted"
    ) -> None:
        attempts_after = int(row.get("attempts") or 0) + 1
        delay = min(300.0, max(5.0, 5.0 * (2 ** min(attempts_after - 1, 6))))
        store.outbox_mark(
            row["id"],
            state="pending",
            next_attempt_at=time.time() + delay,
            last_error=error,
        )

    def _fail(self, store: ContinuationStore, instance_id: int, reason: str) -> StepResult:
        store.transition(instance_id, ContinuationState.FAILED_RETRYABLE, reason=reason)
        return StepResult(
            success=False,
            reason=reason,
            state=store.get_instance(instance_id)["state"],
            metadata={"instance_id": instance_id},
        )
