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


class ContinuationHandler(Protocol):
    kind: ContinuationKind

    def plan(self, outcome: OutcomeEnvelope, *, store: Any, adapter: Any, contract: Any) -> ContinuationPlan: ...

    def on_receipt(
        self, instance: dict[str, Any], submission: dict[str, Any], *, store: Any, adapter: Any, contract: Any
    ) -> StepResult: ...
