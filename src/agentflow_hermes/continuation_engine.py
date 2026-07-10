"""Board-aware event ingestion and continuation routing.

One event produces one continuation receipt/routing decision. GO routes to
the existing roadmap-next graph creator, code BLOCK routes to the existing
remediation graph creator, and needs_input routes to the owner-input
continuation handler — all three become dependencies of this router rather
than parallel top-level engines. Unknown/malformed outcomes never mutate
anything.
"""

from __future__ import annotations

from typing import Any, Callable

from .board_events import BoardEventSource
from .continuation import get_handler
from .continuation_config import ContractRegistry, UnknownContractError
from .continuation_store import ContinuationStore
from .graph_creator import propose_next_slice_graph, propose_remediation_graph
from .outcome import ContinuationKind, parse_outcome_envelope

GraphRouter = Callable[..., dict[str, Any]]


def ingest_board_once(
    *,
    board: str,
    source: BoardEventSource,
    store: ContinuationStore,
    contract_registry: ContractRegistry,
    adapter: Any = None,
    roadmap_router: GraphRouter | None = None,
    code_fix_router: GraphRouter | None = None,
) -> dict[str, Any]:
    roadmap_router = roadmap_router or propose_next_slice_graph
    code_fix_router = code_fix_router or propose_remediation_graph

    db_identity = source.db_identity()
    last_seq = store.get_cursor(board, db_identity)
    events = source.fetch_events_since(last_seq)

    results: list[dict[str, Any]] = []
    max_seq = last_seq

    for event in events:
        max_seq = max(max_seq, event.event_seq)
        envelope = parse_outcome_envelope(
            run_metadata=event.run_metadata,
            summary=event.summary,
            event_id=event.event_id,
            board=board,
            source_task_id=event.source_task_id,
            source_graph_id=event.source_graph_id,
            origin_ref=event.origin_ref,
            return_to_ref=event.return_to_ref,
            workspace_ref=event.workspace_ref,
            assignee=event.assignee,
            occurred_at=event.occurred_at,
        )

        if envelope.continuation_kind == ContinuationKind.NEEDS_INPUT:
            try:
                contract = contract_registry.get(envelope.contract_ref)
            except UnknownContractError:
                results.append({"event_id": event.event_id, "action": "noop", "reason": "unknown_contract_ref"})
                continue
            handler = get_handler(ContinuationKind.NEEDS_INPUT)
            plan = handler.plan(envelope, store=store, adapter=adapter, contract=contract)
            results.append({
                "event_id": event.event_id,
                "action": "owner_input_planned",
                "instance_id": plan.instance_id,
                "created": plan.created,
                "state": plan.state,
            })
        elif envelope.continuation_kind in (ContinuationKind.ROADMAP_NEXT, ContinuationKind.COMPLETE):
            result = roadmap_router(
                event.summary,
                event_id=event.event_id,
                source_final_ref=event.source_task_id,
                origin=event.origin_ref,
                return_to=event.return_to_ref,
                occurred_at=event.occurred_at,
                adapter=None,
            )
            results.append({
                "event_id": event.event_id,
                "action": "roadmap_routed",
                "router_success": bool(result.get("success")),
            })
        elif envelope.continuation_kind == ContinuationKind.CODE_FIX:
            result = code_fix_router(
                event.summary,
                source_ref=event.source_task_id,
                origin=event.origin_ref,
                return_to=event.return_to_ref,
                adapter=None,
            )
            results.append({
                "event_id": event.event_id,
                "action": "code_fix_routed",
                "router_success": bool(result.get("success")),
            })
        else:
            results.append({
                "event_id": event.event_id,
                "action": "noop",
                "reason": "unknown_outcome",
                "confidence": envelope.confidence,
            })

    store.advance_cursor(board, db_identity, max_seq)
    return {
        "success": True,
        "board": board,
        "processed": len(events),
        "results": results,
        "cursor": max_seq,
    }
