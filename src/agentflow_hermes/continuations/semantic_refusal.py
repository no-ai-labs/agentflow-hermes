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
2. It subscribes the *source* task back to its trusted origin using the board's
   own Kanban notify + active-wake path, so the origin is woken about the
   refusal. It never sends anything to AgentFlow/Discord directly.
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

        # 1) Targeted trusted-origin notify+wake — never a direct send. This
        # must succeed before we record the refusal ACK/quarantine below: the
        # durable notify+wake repair plus semantic-refusal ACK is one semantic
        # operation, so an ACK/active-wake repair failure remains retryable
        # without a false success receipt (M30C).
        digest = _stable_digest(instance)
        endpoint = outcome.return_to_ref or outcome.origin_ref
        subscribed = False
        if endpoint:
            sub = apply_board_operation(
                store,
                instance_id,
                step_id="0",
                operation="subscribe",
                payload={"task_id": outcome.source_task_id, "endpoint": endpoint},
                idempotency_key=f"semantic_refusal_notify:{digest}:{endpoint}",
                adapter=adapter,
            )
            if not sub.get("success"):
                return self._fail(store, instance_id, "refusal_notify_failed")
            subscribed = True

        # 2) Durable semantic-refusal ACK (idempotent upsert on field_name),
        # recorded only after trusted-origin subscribe/active-wake repair is
        # known to have succeeded.
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
                "subscribed": subscribed,
                "endpoint": endpoint,
                "created_tasks": 0,
            },
        )

    def _fail(self, store: ContinuationStore, instance_id: int, reason: str) -> StepResult:
        store.transition(instance_id, ContinuationState.FAILED_RETRYABLE, reason=reason)
        return StepResult(
            success=False,
            reason=reason,
            state=store.get_instance(instance_id)["state"],
            metadata={"instance_id": instance_id},
        )
