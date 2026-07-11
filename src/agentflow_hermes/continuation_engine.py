"""Board-aware event ingestion and continuation routing.

One event produces one continuation receipt/routing decision. GO routes to
the existing roadmap-next graph creator, code BLOCK routes to the existing
remediation graph creator, and needs_input routes to the owner-input
continuation handler — all three become dependencies of this router rather
than parallel top-level engines. Unknown/malformed outcomes never mutate
anything.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from .board_events import BoardEventSource, BoardRegistryEntry, LiveBoardEventSource
from .board_adapter import RealBoardAdapter, default_board_kanban_db_path
from .continuation import get_handler
from .continuation_config import ContractRegistry, UnknownContractError
from .continuation_store import ContinuationStore
from .graph_creator import propose_next_slice_graph, propose_remediation_graph
from .outcome import ContinuationKind, parse_outcome_envelope

GraphRouter = Callable[..., dict[str, Any]]
SourceFactory = Callable[[str, BoardRegistryEntry], BoardEventSource]
AdapterFactory = Callable[[str, BoardRegistryEntry], Any]


def ingest_board_once(
    *,
    board: str,
    source: BoardEventSource,
    store: ContinuationStore,
    contract_registry: ContractRegistry,
    adapter: Any = None,
    roadmap_router: GraphRouter | None = None,
    code_fix_router: GraphRouter | None = None,
    handle_kinds: Iterable[ContinuationKind] | None = None,
    default_endpoint: str = "",
) -> dict[str, Any]:
    roadmap_router = roadmap_router or propose_next_slice_graph
    code_fix_router = code_fix_router or propose_remediation_graph
    allowed_kinds = set(handle_kinds) if handle_kinds is not None else None

    db_identity = source.db_identity()
    last_seq = store.get_cursor(board, db_identity)
    events = source.fetch_events_since(last_seq)

    results: list[dict[str, Any]] = []
    max_seq = last_seq

    for event in events:
        max_seq = max(max_seq, event.event_seq)
        # Generic return-endpoint resolution: the source task's own typed
        # endpoint (carried on the event) wins; otherwise the board's declared
        # default endpoint. No per-channel branch.
        origin_ref = event.origin_ref or default_endpoint
        return_to_ref = event.return_to_ref or default_endpoint
        envelope = parse_outcome_envelope(
            run_metadata=event.run_metadata,
            summary=event.summary,
            event_id=event.event_id,
            board=board,
            source_task_id=event.source_task_id,
            source_graph_id=event.source_graph_id,
            origin_ref=origin_ref,
            return_to_ref=return_to_ref,
            workspace_ref=event.workspace_ref,
            assignee=event.assignee,
            occurred_at=event.occurred_at,
        )

        if allowed_kinds is not None and envelope.continuation_kind not in allowed_kinds:
            results.append({"event_id": event.event_id, "action": "noop", "reason": "kind_not_handled"})
            continue

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
                origin=origin_ref,
                return_to=return_to_ref,
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
                origin=origin_ref,
                return_to=return_to_ref,
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


def live_source_factory(board: str, entry: BoardRegistryEntry) -> LiveBoardEventSource:
    """Default production source: a read-only live per-board Kanban sqlite DB."""
    db_path = entry.db_path or str(default_board_kanban_db_path(board))
    return LiveBoardEventSource(
        board=board,
        db_path=db_path,
        db_identity=entry.db_identity,
        default_endpoint=entry.default_endpoint,
    )


def real_adapter_factory(board: str, entry: BoardRegistryEntry) -> RealBoardAdapter:
    """Default production adapter: apply owner-continuation ops via real CLI."""
    board_db_path = entry.db_path or None
    return RealBoardAdapter(board=board, board_db_path=board_db_path)


def ingest_all_boards(
    *,
    registry: dict[str, BoardRegistryEntry],
    store: ContinuationStore,
    contract_registry: ContractRegistry,
    source_factory: SourceFactory | None = None,
    adapter_factory: AdapterFactory | None = None,
    handle_kinds: Iterable[ContinuationKind] | None = None,
    migration_cursors: dict[str, int] | None = None,
) -> dict[str, Any]:
    """One board-aware scan loop across every enabled registry board.

    A newly seen board (no cursor row) is seeded to its current max event id so
    history is never replayed, unless an explicit ``migration_cursors[board]``
    override is supplied. Cursor identity is ``(board, db_identity)`` so two
    boards with overlapping event ids stay independent. Enrolling a new board
    is purely additive — no code change, no per-board cron.
    """
    source_factory = source_factory or live_source_factory
    migration_cursors = migration_cursors or {}
    allowed_kinds = tuple(handle_kinds) if handle_kinds is not None else None

    board_results: list[dict[str, Any]] = []
    for board, entry in registry.items():
        if not entry.enabled:
            continue
        source = source_factory(board, entry)
        db_identity = source.db_identity()

        if not store.cursor_exists(board, db_identity):
            seed = migration_cursors.get(board)
            if seed is None:
                seed = source.current_max_seq()
            store.advance_cursor(board, db_identity, int(seed))
            board_results.append({
                "success": True,
                "board": board,
                "processed": 0,
                "seeded_cursor": int(seed),
                "results": [],
            })
            continue

        adapter = adapter_factory(board, entry) if adapter_factory is not None else None
        board_results.append(
            ingest_board_once(
                board=board,
                source=source,
                store=store,
                contract_registry=contract_registry,
                adapter=adapter,
                handle_kinds=allowed_kinds,
                default_endpoint=entry.default_endpoint,
            )
        )

    return {"success": True, "boards": board_results}
