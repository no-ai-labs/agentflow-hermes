"""CodeFixHandler: the code_fix continuation kind.

A reviewer BLOCK/NEED_MORE with a concrete blocker (whether that blocker came
from authoritative flat reviewer metadata or from a named blocker in the
summary) materializes exactly one idempotent runnable-fix -> independent-review
graph on the real board, then subscribes the review task back to the source's
trusted origin using the board's own Kanban notify + active-wake path. It never
sends anything to AgentFlow/Discord directly — the return trip is a durable
board subscription only.

Every board mutation goes through the shared durable outbox cycle
(``apply_board_operation``), so a crash or adapter failure leaves a replayable
``pending`` outbox row rather than a silent, unrecorded external write. If any
mutation fails, the instance is parked in ``FAILED_RETRYABLE`` and the handler
returns ``success=False`` so the caller can fail the cursor closed and replay
the whole event later. A replay/restart re-derives the same source-scoped
idempotency keys, so the board adapter dedupes to the same task ids and zero
duplicate tasks/wakes are ever created (plan/M30A items 2/3).
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..continuation_store import ContinuationState, ContinuationStore
from ..outcome import ContinuationKind, OutcomeEnvelope
from .base import StepResult, apply_board_operation


def _stable_digest(instance: dict[str, Any]) -> str:
    """Digest of the instance's durable, source-scoped idempotency key — never
    the local SQLite row id, so two fresh stores that each assign ``id=1`` to a
    *different* source event still produce different board mutation keys."""
    base = str(instance.get("idempotency_key") or "")
    if not base:
        base = f"instance:{instance.get('board', '')}:{instance.get('id', '')}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def _blocker_lines(blockers: tuple[str, ...]) -> str:
    if not blockers:
        return ""
    return "\n".join(["Blocking findings:", *(f"- {b}" for b in blockers)])


class CodeFixHandler:
    kind = ContinuationKind.CODE_FIX

    def materialize(
        self,
        outcome: OutcomeEnvelope,
        *,
        store: ContinuationStore,
        adapter: Any,
        blockers: tuple[str, ...] = (),
    ) -> StepResult:
        """Create the fix -> review graph and subscribe review to the trusted
        origin. Idempotent and fail-closed."""
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

        if instance["state"] == ContinuationState.DETECTED.value:
            store.transition(instance_id, ContinuationState.MATERIALIZING, reason="code_fix_detected")
            instance = store.get_instance(instance_id)
        assert instance is not None

        digest = _stable_digest(instance)
        evidence = _blocker_lines(blockers)

        # 1) Runnable fix task.
        fix_key = f"code_fix_fix:{digest}"
        fix_body = "\n\n".join(
            filter(
                None,
                [
                    f"Runnable fix for BLOCK on source {outcome.source_task_id}.",
                    evidence,
                    "Independent review follows; do not self-approve.",
                ],
            )
        )
        fix_task_id = self._create(
            store,
            instance_id,
            adapter,
            step_kind="code_fix",
            key=fix_key,
            payload={
                "kind": "code_fix",
                "title": f"[code-fix] remediate BLOCK on {outcome.source_task_id}",
                "body": fix_body,
                "idempotency_key": fix_key,
                "origin_ref": outcome.origin_ref,
                "return_to_ref": outcome.return_to_ref,
                "source_task_id": outcome.source_task_id,
            },
        )
        if fix_task_id is None:
            return self._fail(store, instance_id, "fix_create_failed", instance_id_meta=instance_id)

        # 2) Independent review task, linked to the fix task.
        review_key = f"code_fix_review:{digest}"
        review_task_id = self._create(
            store,
            instance_id,
            adapter,
            step_kind="review",
            key=review_key,
            payload={
                "kind": "review",
                "title": f"[code-fix review] verify remediation of {outcome.source_task_id}",
                "body": f"Independently review the runnable fix task {fix_task_id}. Report Verdict: GO/BLOCK.",
                "idempotency_key": review_key,
                "origin_ref": outcome.origin_ref,
                "return_to_ref": outcome.return_to_ref,
                "parent_task_id": fix_task_id,
                "source_task_id": outcome.source_task_id,
            },
            parent_step_key=fix_key,
        )
        if review_task_id is None:
            return self._fail(store, instance_id, "review_create_failed", instance_id_meta=instance_id)

        # 3) Subscribe the review task back to the source's trusted origin via
        #    the board's own notify + active-wake path (never a direct send).
        endpoint = outcome.return_to_ref or outcome.origin_ref
        subscribed = False
        if endpoint:
            sub = apply_board_operation(
                store,
                instance_id,
                step_id="0",
                operation="subscribe",
                payload={"task_id": review_task_id, "endpoint": endpoint},
                idempotency_key=f"code_fix_subscribe:{review_key}:{endpoint}",
                adapter=adapter,
            )
            if not sub.get("success"):
                return self._fail(store, instance_id, "review_subscribe_failed", instance_id_meta=instance_id)
            subscribed = True

        if instance["state"] == ContinuationState.MATERIALIZING.value:
            store.transition(instance_id, ContinuationState.WAITING_REVIEW, reason="code_fix_graph_materialized")

        return StepResult(
            success=True,
            state=store.get_instance(instance_id)["state"],
            metadata={
                "instance_id": instance_id,
                "created": creation["created"],
                "fix_task_id": fix_task_id,
                "review_task_id": review_task_id,
                "subscribed": subscribed,
                "endpoint": endpoint,
            },
        )

    def _create(
        self,
        store: ContinuationStore,
        instance_id: int,
        adapter: Any,
        *,
        step_kind: str,
        key: str,
        payload: dict[str, Any],
        parent_step_key: str = "",
    ) -> str | None:
        parent_step_id: int | None = None
        if parent_step_key:
            for s in store.list_steps(instance_id):
                if s["idempotency_key"] == parent_step_key:
                    parent_step_id = s["id"]
                    break
        step = store.add_step(
            instance_id, step_kind=step_kind, idempotency_key=key, parent_step_id=parent_step_id
        )
        # On replay the step already exists with an applied board_task_id.
        if not step["created"] and step["step"].get("board_task_id"):
            return str(step["step"]["board_task_id"])
        result = apply_board_operation(
            store,
            instance_id,
            step_id=step["step"]["id"],
            operation="create_task",
            payload=payload,
            idempotency_key=key,
            adapter=adapter,
        )
        if not result.get("success"):
            return None
        task_id = str(result.get("task_id") or "")
        if task_id:
            store.mark_step(step["step"]["id"], state="applied", board_task_id=task_id)
        return task_id

    def _fail(self, store: ContinuationStore, instance_id: int, reason: str, *, instance_id_meta: int) -> StepResult:
        store.transition(instance_id, ContinuationState.FAILED_RETRYABLE, reason=reason)
        return StepResult(
            success=False,
            reason=reason,
            state=store.get_instance(instance_id)["state"],
            metadata={"instance_id": instance_id_meta},
        )
