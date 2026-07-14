"""Board-aware event ingestion and continuation routing.

One event produces one continuation receipt/routing decision. GO routes to
the existing roadmap-next graph creator, code BLOCK routes to the existing
remediation graph creator, needs_input/approval_required route to the
requirement-resolver-backed owner-input continuation handler, and
external_wait registers a durable condition — all become dependencies of
this one router (plan section 9.4) rather than parallel top-level engines.
Unknown/malformed outcomes never mutate anything.

``agentflowd`` (``daemon.py``) is a thin caller of this module: it adds board
discovery, the wake loop, and external-wait polling/reconciliation scheduling
on top of the exact same per-board router implemented here, instead of
maintaining its own parallel copy (plan 14, commit 7 item 1).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from .board_events import BoardEventSource, BoardRegistryEntry, LiveBoardEventSource
from .board_adapter import RealBoardAdapter, default_board_kanban_db_path
from .continuation import get_handler
from .continuation_config import ContractRegistry, UnknownContractError
from .continuation_store import ContinuationState, ContinuationStore
from .graph_creator import propose_next_slice_graph, propose_remediation_graph
from .input_contract import InputContract
from .interaction import InteractionInbox
from .outcome import GENERIC_OWNER_INPUT_CONTRACT, ContinuationKind
from .outcome_compiler import compile_outcome
from .roadmap import InMemoryRoadmapApplyLedger, InMemoryRoadmapPromotionLedger, RoadmapPromotionPolicy, apply_roadmap_promotion
from .roadmap_config import RepoRoadmapConfig, build_registry
from .requirement_resolver import HumanEffortResolver
from .requirements import Requirement

GraphRouter = Callable[..., dict[str, Any]]
SourceFactory = Callable[[str, BoardRegistryEntry], BoardEventSource]
AdapterFactory = Callable[[str, BoardRegistryEntry], Any]

# External-wait condition store (plan 11.1). Scoped to the continuation store
# via idempotent DDL executed directly by this module, the same pattern
# ``continuation_store.py`` already uses for its own control-plane tables.
_EXTERNAL_WAIT_DDL = """
create table if not exists external_wait_conditions (
    id integer primary key autoincrement,
    continuation_id integer not null,
    kind text not null default '',
    target text not null default '',
    desired text not null default '',
    poll_interval_seconds real not null default 60,
    resume_transition text not null default '',
    state text not null default 'pending',
    last_checked_at real not null default 0,
    created_at real not null,
    updated_at real not null
);
create index if not exists idx_external_wait_state on external_wait_conditions(state);
"""

ExternalWaitChecker = Callable[[dict[str, Any]], "str | None"]


def ensure_external_wait_schema(store: ContinuationStore) -> None:
    store.init()
    with store.connect() as con:
        con.executescript(_EXTERNAL_WAIT_DDL)


def _context_for(event_run_metadata: dict[str, Any] | None) -> dict[str, Any]:
    metadata = event_run_metadata if isinstance(event_run_metadata, dict) else {}
    return {
        "system_derived": metadata.get("system_derived") or {},
        "verified_artifacts": metadata.get("verified_artifacts") or {},
        "context_candidates": metadata.get("context_candidates") or {},
        "policy_conditions": metadata.get("policy_conditions") or {},
    }


def _owner_input_like(
    *,
    envelope: Any,
    requirements: tuple[Requirement, ...],
    event_run_metadata: dict[str, Any] | None,
    board: str,
    store: ContinuationStore,
    contract_registry: ContractRegistry,
    adapter: Any,
    interaction_inbox: InteractionInbox | None,
) -> dict[str, Any]:
    """Shared resolution+handler path for NEEDS_INPUT and APPROVAL_REQUIRED
    (plan 11.2 reuses exactly the needs_input resolution ladder for
    approvals). Resolves every requirement first; if every field resolves
    before the ask step the continuation becomes H0 and an owner receipt is
    recorded automatically with no owner anchor (plan 4.4/13.2)."""
    contract_ref = envelope.contract_ref or GENERIC_OWNER_INPUT_CONTRACT
    try:
        contract = contract_registry.get(contract_ref)
    except UnknownContractError:
        return {"action": "noop", "reason": "unknown_contract_ref"}
    if not requirements:
        # No explicit ``required_inputs`` on the compiled outcome: fall back
        # to the static contract's own owner-authority fields (e.g. generic
        # approve/reject/confirm) so a bare structured needs_input outcome
        # still resolves/asks against real owner-authority fields instead of
        # silently becoming a zero-field H0 (the static contract's fields are
        # exactly what the pre-resolver-ladder handler always asked for).
        requirements = tuple(
            Requirement(name=f.name, kind=f.kind, authority=f.authority.value, question=f.question)
            for f in contract.owner_fields()
        )
    if contract_ref == GENERIC_OWNER_INPUT_CONTRACT and requirements:
        # The static generic.owner-input.v1 contract only declares the fixed
        # approve/reject/confirm pair; a compiled outcome that names actual
        # typed requirements (e.g. from the deterministic natural-prose
        # grammar in outcome_compiler.py) needs the dynamic per-outcome field
        # list instead (plan 1.4/1.5 — "provide the URL"/"choose A/B"/etc.).
        contract = InputContract.dynamic_owner_input(
            contract_ref=contract.contract_ref,
            owner_role=contract.owner_role,
            resume_transition=contract.resume_transition,
            requirements=requirements,
        )

    resolver = HumanEffortResolver(store=store)
    context = _context_for(event_run_metadata)
    result = resolver.resolve(
        requirements,
        context=context,
        owner_ref="",
        project_scope=board,
        action_scope=contract_ref,
    )

    handler = get_handler(ContinuationKind.NEEDS_INPUT)

    if not result.interaction_needed:
        # H0: every requirement resolved from evidence/policy before the ask
        # step. Skip OwnerInputHandler.plan() entirely — it always creates a
        # blocked owner-anchor task — and instead create the instance,
        # record the satisfactions, and submit them straight to on_receipt()
        # as an automatic decision receipt (plan 4.4/13.2: zero owner
        # questions, zero owner anchor for a fully H0 continuation).
        creation = store.create_instance(
            board=envelope.board,
            source_task_id=envelope.source_task_id,
            source_event_id=envelope.event_id,
            source_graph_id=envelope.source_graph_id,
            contract_ref=contract_ref,
            verdict=envelope.verdict.value,
            continuation_kind=envelope.continuation_kind.value,
            origin_ref=envelope.origin_ref,
            return_to_ref=envelope.return_to_ref,
            workspace_ref=envelope.workspace_ref,
        )
        instance = creation["instance"]
        instance_id = instance["id"]
        if creation["created"]:
            store.transition(instance_id, ContinuationState.WAITING_OWNER, reason="needs_input_detected_h0")
            instance = store.get_instance(instance_id)

        for satisfied in result.satisfied:
            store.record_requirement_satisfaction(
                instance_id,
                field_name=satisfied.requirement.name,
                value=satisfied.value,
                source_kind=satisfied.source.value,
                source_ref=satisfied.source_ref,
                policy_id=satisfied.policy_id,
            )
        fields = {s.requirement.name: s.value for s in result.satisfied}
        receipt_result = handler.on_receipt(
            instance,
            {"owner_ref": "system:auto-resolved", "fields": fields, "source_ref": "requirement_resolver"},
            store=store,
            adapter=adapter,
            contract=contract,
        )
        return {
            "action": "auto_resolved",
            "instance_id": instance_id,
            "created": creation["created"],
            "state": receipt_result.state or instance["state"],
            "h0": True,
        }

    plan = handler.plan(
        envelope,
        store=store,
        adapter=adapter,
        contract=contract,
        interaction_inbox=interaction_inbox,
        unresolved_requirements=result.unresolved,
    )
    return {
        "action": "owner_input_planned",
        "instance_id": plan.instance_id,
        "created": plan.created,
        "state": plan.state,
        "h0": False,
    }


def _external_wait_intent(event_run_metadata: dict[str, Any] | None, envelope: Any) -> dict[str, Any]:
    metadata = event_run_metadata if isinstance(event_run_metadata, dict) else {}
    block = metadata.get("agentflow_outcome") if isinstance(metadata.get("agentflow_outcome"), dict) else {}
    wait = block.get("external_wait") if isinstance(block.get("external_wait"), dict) else {}
    return {
        "kind": str(wait.get("kind") or ""),
        "target": str(wait.get("target") or ""),
        "desired": str(wait.get("desired") or ""),
        "poll_interval_seconds": float(wait.get("poll_interval_seconds") or 60),
        "resume_transition": str(wait.get("resume_transition") or envelope.next_transition or ""),
    }


def _handle_external_wait(
    *, envelope: Any, event_run_metadata: dict[str, Any] | None, store: ContinuationStore
) -> dict[str, Any]:
    ensure_external_wait_schema(store)
    creation = store.create_instance(
        board=envelope.board,
        source_task_id=envelope.source_task_id,
        source_event_id=envelope.event_id,
        source_graph_id=envelope.source_graph_id,
        contract_ref=envelope.contract_ref,
        verdict=envelope.verdict.value,
        continuation_kind=envelope.continuation_kind.value,
        origin_ref=envelope.origin_ref,
        return_to_ref=envelope.return_to_ref,
        workspace_ref=envelope.workspace_ref,
    )
    instance = creation["instance"]
    if creation["created"]:
        # No owner-facing WAITING_OWNER state exists for a condition nobody
        # needs to answer (plan 11.1) — MATERIALIZING is the legal state that
        # both directly permits RESUMABLE (on satisfied) and FAILED_RETRYABLE
        # (on permanent failure) without ever touching an owner anchor.
        store.transition(instance["id"], ContinuationState.MATERIALIZING, reason="external_wait_registered")
        intent = _external_wait_intent(event_run_metadata, envelope)
        now = time.time()
        with store.connect() as con:
            existing = con.execute(
                "select id from external_wait_conditions where continuation_id=?", (instance["id"],)
            ).fetchone()
            if existing is None:
                con.execute(
                    """
                    insert into external_wait_conditions(
                        continuation_id, kind, target, desired, poll_interval_seconds,
                        resume_transition, state, last_checked_at, created_at, updated_at
                    ) values(?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                    """,
                    (
                        instance["id"], intent["kind"], intent["target"], intent["desired"],
                        intent["poll_interval_seconds"], intent["resume_transition"], now, now,
                    ),
                )
    return {"action": "external_wait_registered", "instance_id": instance["id"], "created": creation["created"]}


def _default_external_wait_checker(condition: dict[str, Any]) -> "str | None":
    """No real network/GitHub wiring exists (stdlib-only, zero deps). Always
    declines to determine the condition, leaving it pending until a caller
    injects a real checker or the condition is resolved another way."""
    return None


def poll_external_wait_conditions(
    store: ContinuationStore,
    *,
    checker: ExternalWaitChecker | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Poll every due, still-pending external-wait condition. ``checker``
    returns ``"satisfied"``, ``"failed"``, or ``None`` (still pending). On
    satisfaction the continuation resumes automatically with zero owner
    questions (plan 13.6); on permanent failure the condition is marked
    failed for the operator surfaces to report — it does not raise an owner
    question by itself here (plan 11.1)."""
    ensure_external_wait_schema(store)
    checker = checker or _default_external_wait_checker
    now = now if now is not None else time.time()
    checked: list[dict[str, Any]] = []
    with store.connect() as con:
        rows = [dict(r) for r in con.execute("select * from external_wait_conditions where state='pending'").fetchall()]
    for row in rows:
        if now - float(row["last_checked_at"] or 0) < float(row["poll_interval_seconds"] or 60):
            continue
        outcome = checker(row)
        # Close/commit the sqlite write before calling store.transition(),
        # which opens its own connection — sqlite3's default isolation level
        # holds the write lock for the whole ``with`` block otherwise, and a
        # second writer connection on the same file would deadlock.
        with store.connect() as con:
            con.execute(
                "update external_wait_conditions set last_checked_at=?, updated_at=? where id=?", (now, now, row["id"])
            )
            if outcome == "satisfied":
                con.execute("update external_wait_conditions set state='satisfied', updated_at=? where id=?", (now, row["id"]))
            elif outcome == "failed":
                con.execute("update external_wait_conditions set state='failed_permanent', updated_at=? where id=?", (now, row["id"]))
        if outcome == "satisfied":
            store.transition(row["continuation_id"], ContinuationState.RESUMABLE, reason="external_wait_satisfied")
            store.transition(row["continuation_id"], ContinuationState.RESUMED, reason="external_wait_resumed")
        elif outcome == "failed":
            store.transition(row["continuation_id"], ContinuationState.FAILED_RETRYABLE, reason="external_wait_failed_permanent")
        checked.append({"condition_id": row["id"], "continuation_id": row["continuation_id"], "outcome": outcome or "pending"})
    return {"checked": checked}


def reconcile_outbox(store: ContinuationStore, *, adapter_by_board: dict[str, Any]) -> dict[str, Any]:
    """Retry every pending outbox row (plan 13.7 restart-recovery). Idempotent:
    each row's ``idempotency_key`` guarantees a duplicate apply never creates
    a second board task."""
    import contextlib
    import json as _json

    retried: list[str] = []
    with store.connect() as con:
        rows = [dict(r) for r in con.execute(
            "select o.*, c.board as board from board_outbox o join continuation_instances c on c.id=o.continuation_id where o.state='pending'"
        ).fetchall()]
    for row in rows:
        adapter = adapter_by_board.get(row["board"])
        if adapter is None:
            continue
        payload = _json.loads(row["payload_json"])
        operation = row.get("operation") or "create_task"
        if operation == "create_task":
            result = adapter.create_task(payload)
        elif operation == "subscribe":
            result = adapter.subscribe(str(payload.get("task_id") or ""), str(payload.get("endpoint") or ""))
        elif operation == "complete_owner_anchor":
            result = adapter.complete_owner_anchor(str(payload.get("task_id") or ""), receipt_ref=str(payload.get("receipt_ref") or ""))
        else:
            result = {"success": False, "error": "unknown_outbox_operation"}
        if result.get("success"):
            task_id = result.get("task_id", "")
            store.outbox_mark(row["id"], state="applied", board_task_id=task_id)
            step_id = row.get("step_id") or ""
            if step_id:
                with contextlib.suppress(Exception):
                    store.mark_step(int(step_id), state="applied", board_task_id=task_id)
            retried.append(row["idempotency_key"])
    return {"retried": retried, "pending_before": len(rows)}


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
    interaction_inbox: InteractionInbox | None = None,
    roadmap_config: RepoRoadmapConfig | None = None,
    roadmap_apply_ledger: InMemoryRoadmapApplyLedger | None = None,
    roadmap_receipts_file: str = "",
    roadmap_graph_adapter: Any = None,
    apply: bool = False,
) -> dict[str, Any]:
    explicit_roadmap_router = roadmap_router is not None
    roadmap_router = roadmap_router or propose_next_slice_graph
    code_fix_router = code_fix_router or propose_remediation_graph
    allowed_kinds = set(handle_kinds) if handle_kinds is not None else None

    db_identity = source.db_identity()
    last_seq = store.get_cursor(board, db_identity)
    events = source.fetch_events_since(last_seq)

    results: list[dict[str, Any]] = []
    max_seq = last_seq

    for event in events:
        # Generic return-endpoint resolution: the source task's own typed
        # endpoint (carried on the event) wins; otherwise the board's declared
        # default endpoint. No per-channel branch.
        origin_ref = event.origin_ref or default_endpoint
        return_to_ref = event.return_to_ref or default_endpoint
        compiled = compile_outcome(
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
            title=event.title,
            event_kind=event.event_kind,
        )
        envelope = compiled.envelope

        if allowed_kinds is not None and envelope.continuation_kind not in allowed_kinds:
            results.append({"event_id": event.event_id, "action": "noop", "reason": "kind_not_handled"})
            max_seq = max(max_seq, event.event_seq)
            continue

        if envelope.continuation_kind in (ContinuationKind.NEEDS_INPUT, ContinuationKind.APPROVAL_REQUIRED):
            outcome = _owner_input_like(
                envelope=envelope,
                requirements=compiled.requirements,
                event_run_metadata=event.run_metadata,
                board=board,
                store=store,
                contract_registry=contract_registry,
                adapter=adapter,
                interaction_inbox=interaction_inbox,
            )
            results.append({"event_id": event.event_id, **outcome})
            max_seq = max(max_seq, event.event_seq)
        elif envelope.continuation_kind in (ContinuationKind.ROADMAP_NEXT, ContinuationKind.COMPLETE):
            if roadmap_config is None and explicit_roadmap_router:
                result = roadmap_router(
                    event.summary,
                    event_id=event.event_id,
                    source_final_ref=event.source_task_id,
                    origin=origin_ref,
                    return_to=return_to_ref,
                    occurred_at=event.occurred_at,
                    adapter=None,
                )
            elif roadmap_config is None:
                result = {"success": True, "action": "noop", "reason": "no_transition_config", "created_task_ids": []}
            elif not roadmap_config.enabled or roadmap_config.kill_switch:
                result = {"success": True, "action": "noop", "reason": "transition_config_disabled", "created_task_ids": []}
            else:
                ledger = roadmap_apply_ledger or load_roadmap_apply_ledger(roadmap_receipts_file)
                result = apply_roadmap_promotion(
                    event.summary,
                    event_id=event.event_id,
                    source_final_ref=event.source_task_id,
                    source_assignee=event.assignee,
                    origin=roadmap_config.expected_origin or origin_ref,
                    return_to=roadmap_config.expected_return_to or return_to_ref,
                    subscription_status=_summary_marker(event.summary, "Subscription-Status") or "unverified",
                    policy_resolution_ref=_summary_marker(event.summary, "Policy-Resolution-Ref"),
                    occurred_at=event.occurred_at,
                    registry=build_registry(roadmap_config),
                    ledger=InMemoryRoadmapPromotionLedger(),
                    apply_ledger=ledger,
                    policy=_roadmap_policy_from_config(roadmap_config, apply=apply),
                    adapter=roadmap_graph_adapter if apply else None,
                )
                if roadmap_receipts_file:
                    _persist_roadmap_apply_ledger(ledger, roadmap_receipts_file)
            router_success = bool(result.get("success"))
            results.append({
                "event_id": event.event_id,
                "action": "roadmap_routed",
                "router_success": router_success,
                "roadmap": result,
            })
            if not router_success:
                # Fail-closed (reviewer BLOCK t_2463a93c): an adapter
                # create/subscribe failure must not advance the cursor past
                # this event — it stays retryable and every later event in
                # this batch is left unprocessed too, so ordering is
                # preserved on the next tick's replay. The apply ledger
                # itself already refuses to record anything for a failed
                # apply (roadmap.py's fail-closed result), so a later retry
                # with a working adapter is not deduped against a phantom
                # receipt.
                break
            max_seq = max(max_seq, event.event_seq)
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
            max_seq = max(max_seq, event.event_seq)
        elif envelope.continuation_kind == ContinuationKind.EXTERNAL_WAIT:
            outcome = _handle_external_wait(envelope=envelope, event_run_metadata=event.run_metadata, store=store)
            results.append({"event_id": event.event_id, **outcome})
            max_seq = max(max_seq, event.event_seq)
        else:
            results.append({
                "event_id": event.event_id,
                "action": "noop",
                "reason": "unknown_outcome",
                "confidence": envelope.confidence,
            })
            max_seq = max(max_seq, event.event_seq)

    store.advance_cursor(board, db_identity, max_seq)
    return {
        "success": True,
        "board": board,
        "processed": len(results),
        "results": results,
        "cursor": max_seq,
    }


def _summary_marker(text: str, marker: str) -> str:
    wanted = marker.strip().lower()
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == wanted:
            return value.strip()
    return ""


def _roadmap_policy_from_config(config: RepoRoadmapConfig, *, apply: bool) -> RoadmapPromotionPolicy:
    return RoadmapPromotionPolicy(
        auto_continue=True,
        allowlisted_transitions=config.allowed_transitions,
        max_chain_depth=config.max_chain_depth,
        max_promotions_per_roadmap=config.max_promotions_per_roadmap,
        promote_cooldown_seconds=config.promote_cooldown_seconds,
        require_review_edge=config.require_review_edge,
        require_ack_edge=config.require_ack_edge,
        require_trusted_assignee=config.require_trusted_assignee,
        trusted_assignees=config.trusted_assignees,
        require_origin_match=config.require_origin_match,
        expected_origin=config.expected_origin,
        expected_return_to=config.expected_return_to,
        require_policy_resolution=config.require_policy_resolution,
        apply_enabled=bool(apply and config.apply_mode),
        impl_assignee=config.impl_assignee,
        review_assignee=config.review_assignee,
        ack_trigger_agent=config.ack_trigger_agent,
    )


def load_roadmap_apply_ledger(path: str) -> InMemoryRoadmapApplyLedger:
    ledger = InMemoryRoadmapApplyLedger()
    if not path:
        return ledger
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ledger
    if isinstance(data, dict):
        for key, receipt in data.items():
            if isinstance(receipt, dict):
                ledger.applied[str(key)] = receipt
    return ledger


def _persist_roadmap_apply_ledger(ledger: InMemoryRoadmapApplyLedger, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ledger.applied, ensure_ascii=False), encoding="utf-8")


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
    interaction_inbox: InteractionInbox | None = None,
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
                interaction_inbox=interaction_inbox,
            )
        )

    return {"success": True, "boards": board_results}
