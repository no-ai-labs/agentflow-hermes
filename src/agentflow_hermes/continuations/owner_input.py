"""OwnerInputHandler: needs_input continuation kind.

Creates exactly one durable WAITING_OWNER anchor per source event, refuses
invalid/incomplete owner submissions without advancing state, and lazily
materializes exactly one downstream task only after a validated owner
receipt is recorded. It never fabricates an owner-authority field and never
grants downstream children before the receipt exists.
"""

from __future__ import annotations

from typing import Any

from ..continuation_store import ContinuationState, ContinuationStore
from ..input_contract import InputContract
from ..outcome import ContinuationKind, OutcomeEnvelope
from .base import ContinuationPlan, StepResult


def _owner_anchor_intent(instance: dict[str, Any], contract: InputContract, idempotency_key: str) -> dict[str, Any]:
    owner_fields = [f.name for f in contract.owner_fields()]
    return {
        "kind": "owner_anchor",
        "title": f"[owner-input] {contract.contract_ref} evidence/approval anchor",
        "idempotency_key": idempotency_key,
        "status": "blocked",
        "blocked_reason": "awaiting_owner_input",
        "assignee": contract.owner_role,
        "origin_ref": instance.get("origin_ref", ""),
        "return_to_ref": instance.get("return_to_ref", ""),
        "contract_ref": contract.contract_ref,
        "required_owner_fields": owner_fields,
        "owner_anchor": True,
    }


def _materialization_intent(
    instance: dict[str, Any], contract: InputContract, receipt: dict[str, Any], idempotency_key: str
) -> dict[str, Any]:
    return {
        "kind": "materialization",
        "title": f"{contract.contract_ref} materialize artifacts",
        "idempotency_key": idempotency_key,
        "contract_ref": contract.contract_ref,
        "origin_ref": instance.get("origin_ref", ""),
        "return_to_ref": instance.get("return_to_ref", ""),
        "owner_receipt_id": receipt["id"],
        "owner_receipt_version": receipt["version"],
        "artifact_ids": [a.artifact_id for a in contract.artifacts],
    }


class OwnerInputHandler:
    kind = ContinuationKind.NEEDS_INPUT

    def plan(
        self,
        outcome: OutcomeEnvelope,
        *,
        store: ContinuationStore,
        adapter: Any,
        contract: InputContract,
    ) -> ContinuationPlan:
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

        if creation["created"]:
            store.transition(instance_id, ContinuationState.WAITING_OWNER, reason="needs_input_detected")
            instance = store.get_instance(instance_id)

        anchor_key = f"owner_anchor:{instance_id}"
        intent = _owner_anchor_intent(instance, contract, anchor_key)
        step = store.add_step(instance_id, step_kind="owner_anchor", idempotency_key=anchor_key)

        if step["created"] and adapter is not None:
            result = adapter.create_task(intent)
            if result.get("success"):
                task_id = result.get("task_id", "")
                store.mark_step(step["step"]["id"], state="applied", board_task_id=task_id)
                if instance.get("origin_ref"):
                    adapter.subscribe(task_id, instance["origin_ref"])

        return ContinuationPlan(
            instance_id=instance_id,
            created=creation["created"],
            state=store.get_instance(instance_id)["state"],
            step_intents=(intent,),
        )

    def on_receipt(
        self,
        instance: dict[str, Any],
        submission: dict[str, Any],
        *,
        store: ContinuationStore,
        adapter: Any,
        contract: InputContract,
    ) -> StepResult:
        instance_id = instance["id"]
        if instance["state"] != ContinuationState.WAITING_OWNER.value:
            return StepResult(success=False, reason="not_waiting_owner", state=instance["state"])

        clean, errors = contract.validate_owner_submission(dict(submission.get("fields") or {}))
        if errors:
            return StepResult(
                success=False,
                reason="invalid_owner_submission",
                state=instance["state"],
                metadata={"errors": errors},
            )

        receipt = store.add_owner_receipt(
            instance_id,
            owner_ref=str(submission.get("owner_ref") or ""),
            fields=clean,
            source_ref=str(submission.get("source_ref") or ""),
        )
        store.transition(instance_id, ContinuationState.INPUT_ACCEPTED, reason="owner_receipt_accepted")

        anchor_steps = [s for s in store.list_steps(instance_id) if s["step_kind"] == "owner_anchor"]
        if anchor_steps and anchor_steps[0].get("board_task_id") and adapter is not None:
            adapter.complete_owner_anchor(anchor_steps[0]["board_task_id"], receipt_ref=f"receipt:{receipt['id']}")

        mat_key = f"materialize:{instance_id}"
        step = store.add_step(instance_id, step_kind="materialization", idempotency_key=mat_key)
        materialization_task_id = ""
        if step["created"] and adapter is not None:
            intent = _materialization_intent(instance, contract, receipt, mat_key)
            result = adapter.create_task(intent)
            if result.get("success"):
                materialization_task_id = result.get("task_id", "")
                store.mark_step(step["step"]["id"], state="applied", board_task_id=materialization_task_id)

        store.transition(instance_id, ContinuationState.MATERIALIZING, reason="materialization_task_created")

        return StepResult(
            success=True,
            state=ContinuationState.MATERIALIZING.value,
            created_step_ids=(str(step["step"]["id"]),),
            metadata={"materialization_task_id": materialization_task_id, "receipt_id": receipt["id"]},
        )

    def advance_after_materialization(
        self,
        instance: dict[str, Any],
        *,
        verdict: str,
        store: ContinuationStore,
        adapter: Any,
    ) -> StepResult:
        """Create a review task only after materialization's own semantic GO
        — never from lifecycle ``done`` — and only once."""
        instance_id = instance["id"]
        if instance["state"] != ContinuationState.MATERIALIZING.value:
            return StepResult(success=False, reason="not_materializing", state=instance["state"])

        if verdict != "GO":
            store.transition(instance_id, ContinuationState.FAILED_RETRYABLE, reason="materialization_not_go")
            return StepResult(success=False, reason="materialization_not_go", state=store.get_instance(instance_id)["state"])

        store.transition(instance_id, ContinuationState.WAITING_REVIEW, reason="materialization_go")
        review_key = f"review:{instance_id}"
        step = store.add_step(instance_id, step_kind="review", idempotency_key=review_key)
        review_task_id = ""
        if step["created"] and adapter is not None:
            intent = {
                "kind": "review",
                "title": "Review owner-bound artifact/marker",
                "idempotency_key": review_key,
                "origin_ref": instance.get("origin_ref", ""),
                "return_to_ref": instance.get("return_to_ref", ""),
            }
            result = adapter.create_task(intent)
            if result.get("success"):
                review_task_id = result.get("task_id", "")
                store.mark_step(step["step"]["id"], state="applied", board_task_id=review_task_id)

        return StepResult(
            success=True,
            state=store.get_instance(instance_id)["state"],
            created_step_ids=(str(step["step"]["id"]),),
            metadata={"review_task_id": review_task_id},
        )

    def advance_after_review(
        self,
        instance: dict[str, Any],
        *,
        verdict: str,
        store: ContinuationStore,
        adapter: Any,
    ) -> StepResult:
        """Create the packet-rerun task only after review's own semantic GO,
        and only once."""
        instance_id = instance["id"]
        if instance["state"] != ContinuationState.WAITING_REVIEW.value:
            return StepResult(success=False, reason="not_waiting_review", state=instance["state"])

        if verdict != "GO":
            store.transition(instance_id, ContinuationState.FAILED_RETRYABLE, reason="review_not_go")
            return StepResult(success=False, reason="review_not_go", state=store.get_instance(instance_id)["state"])

        store.transition(instance_id, ContinuationState.RESUMABLE, reason="review_go")
        rerun_key = f"packet_rerun:{instance_id}"
        step = store.add_step(instance_id, step_kind="packet_rerun", idempotency_key=rerun_key)
        rerun_task_id = ""
        if step["created"] and adapter is not None:
            intent = {
                "kind": "packet_rerun",
                "title": f"{instance.get('contract_ref', '')} packet rerun",
                "idempotency_key": rerun_key,
                "origin_ref": instance.get("origin_ref", ""),
                "return_to_ref": instance.get("return_to_ref", ""),
            }
            result = adapter.create_task(intent)
            if result.get("success"):
                rerun_task_id = result.get("task_id", "")
                store.mark_step(step["step"]["id"], state="applied", board_task_id=rerun_task_id)

        store.transition(instance_id, ContinuationState.RESUMED, reason="packet_rerun_created")

        return StepResult(
            success=True,
            state=store.get_instance(instance_id)["state"],
            created_step_ids=(str(step["step"]["id"]),),
            metadata={"packet_rerun_task_id": rerun_task_id},
        )
