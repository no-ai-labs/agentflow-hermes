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
from .base import StepResult


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

        # 1) Targeted trusted-origin wake — never a direct send, and never the
        # generic notify-subscribe path (that fires a passive notify, not a
        # typed origin wake). This must reach a durable *accepted* wake status
        # before we record the refusal ACK/quarantine below: scheduling alone
        # is not semantic completion, and an unaccepted wake leaves the
        # instance retryable without a false success receipt (M30F).
        digest = _stable_digest(instance)
        endpoint = outcome.return_to_ref or outcome.origin_ref
        wake_satisfied = False
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
            if not wake.get("success"):
                return self._fail(store, instance_id, str(wake.get("error") or "refusal_wake_not_yet_accepted"))
            wake_satisfied = True

        # 2) Durable semantic-refusal ACK (idempotent upsert on field_name),
        # recorded only after the trusted-origin wake is known to be durably
        # accepted/started/completed.
        store.record_requirement_satisfaction(
            instance_id,
            field_name="semantic_refusal",
            value={"categories": list(refusal_categories), "blockers": list(blockers)},
            source_kind="semantic_refusal_ack",
            source_ref=",".join(refusal_categories),
        )

        # 3) Durable quarantine — explicitly not a successful CODE_FIX advance.
        if instance["state"] != ContinuationState.BLOCKED_INVALID.value:
            store.transition(instance_id, ContinuationState.BLOCKED_INVALID, reason="semantic_refusal_quarantined")

        return StepResult(
            success=True,
            state=store.get_instance(instance_id)["state"],
            metadata={
                "instance_id": instance_id,
                "created": creation["created"],
                "refusal_ack": True,
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
            return {"success": True, "source": "outbox_applied"}

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
