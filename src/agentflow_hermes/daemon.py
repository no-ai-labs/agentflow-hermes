"""agentflowd: the unified event-driven continuation runtime (plan section 9).

This module is the "one runtime, many handlers" implementation: board
discovery, a fast wake source (short polling loop; real inotify hardware is
not required by the plan — see module docstring in ``scripts/agentflowd.py``),
a single unified handler router, and a durable outbox/cursor reconciliation
pass that also runs on every wake. ``AgentflowDaemon.tick()`` is the
synchronous core — the async loop in ``run()`` is a thin wrapper around it so
tests can exercise routing/latency without any real asyncio sleeping.

Commit 6 intentionally keeps the full per-kind router (``route_board_events``)
local to this module rather than reusing ``continuation_engine.ingest_board_once``,
because that function does not yet cover APPROVAL_REQUIRED/EXTERNAL_WAIT and
splitting the pass in two would let the cursor advance past events the second
pass never got to handle. Commit 7 folds this router into
``continuation_engine.py`` itself and turns this module into a thin caller,
removing the duplication (plan 14, commit 7 item 1).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .board_adapter import FakeBoardAdapter, RealBoardAdapter
from .board_events import BoardRegistryEntry, load_board_registry
from .continuation import get_handler
from .continuation_config import ContractRegistry, load_contract_registry
from .continuation_engine import live_source_factory, real_adapter_factory
from .continuation_store import ContinuationState, ContinuationStore
from .graph_creator import propose_next_slice_graph, propose_remediation_graph
from .input_contract import InputContract
from .interaction import InteractionInbox
from .outcome import GENERIC_OWNER_INPUT_CONTRACT, ContinuationKind
from .outcome_compiler import compile_outcome
from .requirement_resolver import HumanEffortResolver
from .requirements import Requirement

DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_RECONCILE_INTERVAL_SECONDS = 300.0

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

ExternalWaitChecker = Callable[[dict[str, Any]], str | None]


def default_boards_root() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "kanban" / "boards"


def default_runtime_dir() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "agentflow" / "run"


def ensure_external_wait_schema(store: ContinuationStore) -> None:
    store.init()
    with store.connect() as con:
        con.executescript(_EXTERNAL_WAIT_DDL)


# -- board discovery (plan 9.1) --------------------------------------------


def discover_boards(
    *,
    boards_root: Path | None = None,
    overrides_path: Path | str | None = None,
) -> dict[str, BoardRegistryEntry]:
    """Scan ``boards_root``/*/kanban.db for every enrolled board. Every
    discovered board is auto-enrolled unless an override in
    ``config/boards.yaml`` (or an equivalent path) explicitly disables it.
    ``config/boards.yaml`` is consulted per-board as an override catalog
    (disable/endpoint/contract override) — it is no longer a gate on
    discovery itself."""
    root = boards_root if boards_root is not None else default_boards_root()
    overrides: dict[str, BoardRegistryEntry] = {}
    if overrides_path is not None and Path(overrides_path).exists():
        overrides = load_board_registry(overrides_path)

    registry: dict[str, BoardRegistryEntry] = {}
    if root.exists() and root.is_dir():
        for board_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            db_path = board_dir / "kanban.db"
            if not db_path.exists():
                continue
            board = board_dir.name
            override = overrides.get(board)
            if override is not None and not override.enabled:
                continue
            registry[board] = BoardRegistryEntry(
                board=board,
                db_identity=(override.db_identity if override else "") or board,
                outcome_handlers=override.outcome_handlers if override else (),
                enabled=True,
                default_endpoint=(override.default_endpoint if override else ""),
                db_path=(override.db_path if override and override.db_path else "") or str(db_path),
            )
    return registry


# -- unified handler router (plan 9.4) --------------------------------------


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
    except Exception:
        return {"action": "noop", "reason": "unknown_contract_ref"}
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


def route_board_events(
    *,
    board: str,
    entry: BoardRegistryEntry,
    store: ContinuationStore,
    contract_registry: ContractRegistry,
    adapter: Any,
    roadmap_router: Callable[..., dict[str, Any]] = propose_next_slice_graph,
    code_fix_router: Callable[..., dict[str, Any]] = propose_remediation_graph,
    interaction_inbox: InteractionInbox | None = None,
    source_factory: Callable[[str, BoardRegistryEntry], Any] = live_source_factory,
) -> dict[str, Any]:
    """One board's worth of the unified handler router (plan 9.4). Every
    terminal event since the durable cursor is classified via the three-stage
    outcome compiler and dispatched to exactly one handler; the cursor only
    advances after every event in the batch has been dispatched (or
    deliberately no-opped), so a crash mid-batch always leaves the cursor
    behind the last *fully processed* event."""
    source = source_factory(board, entry)
    db_identity = source.db_identity()

    if not store.cursor_exists(board, db_identity):
        # A newly discovered board is seeded to its current max event id so
        # history is never replayed (plan 9.1/2.6) — mirrors
        # continuation_engine.ingest_all_boards's per-board seeding.
        seed = source.current_max_seq()
        store.advance_cursor(board, db_identity, seed)
        return {"success": True, "board": board, "processed": 0, "seeded_cursor": seed, "results": [], "cursor": seed}

    last_seq = store.get_cursor(board, db_identity)
    events = source.fetch_events_since(last_seq)

    results: list[dict[str, Any]] = []
    max_seq = last_seq

    for event in events:
        max_seq = max(max_seq, event.event_seq)
        origin_ref = event.origin_ref or entry.default_endpoint
        return_to_ref = event.return_to_ref or entry.default_endpoint
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
        kind = envelope.continuation_kind

        try:
            if kind in (ContinuationKind.ROADMAP_NEXT, ContinuationKind.COMPLETE):
                result = roadmap_router(
                    event.summary,
                    event_id=event.event_id,
                    source_final_ref=event.source_task_id,
                    origin=origin_ref,
                    return_to=return_to_ref,
                    occurred_at=event.occurred_at,
                    adapter=None,
                )
                outcome = {"action": "roadmap_routed", "router_success": bool(result.get("success"))}
            elif kind == ContinuationKind.CODE_FIX:
                result = code_fix_router(
                    event.summary, source_ref=event.source_task_id, origin=origin_ref, return_to=return_to_ref, adapter=None
                )
                outcome = {"action": "code_fix_routed", "router_success": bool(result.get("success"))}
            elif kind in (ContinuationKind.NEEDS_INPUT, ContinuationKind.APPROVAL_REQUIRED):
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
            elif kind == ContinuationKind.EXTERNAL_WAIT:
                outcome = _handle_external_wait(envelope=envelope, event_run_metadata=event.run_metadata, store=store)
            else:
                outcome = {"action": "noop", "reason": "unknown_outcome", "confidence": compiled.confidence}
        except Exception as exc:  # never let one bad event stall the whole board's cursor forever
            outcome = {"action": "noop", "reason": "handler_error", "error": str(exc)}

        results.append({"event_id": event.event_id, "kind": kind.value, **outcome})

    store.advance_cursor(board, db_identity, max_seq)
    return {"success": True, "board": board, "processed": len(events), "results": results, "cursor": max_seq}


# -- external wait polling (plan 11.1) --------------------------------------


def _default_external_wait_checker(condition: dict[str, Any]) -> str | None:
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


# -- durable outbox reconciliation ------------------------------------------


def reconcile_outbox(store: ContinuationStore, *, adapter_by_board: dict[str, Any]) -> dict[str, Any]:
    """Retry every pending outbox row (plan 13.7 restart-recovery). Idempotent:
    each row's ``idempotency_key`` guarantees a duplicate apply never creates
    a second board task."""
    retried: list[str] = []
    with store.connect() as con:
        rows = [dict(r) for r in con.execute(
            "select o.*, c.board as board from board_outbox o join continuation_instances c on c.id=o.continuation_id where o.state='pending'"
        ).fetchall()]
    for row in rows:
        adapter = adapter_by_board.get(row["board"])
        if adapter is None:
            continue
        payload = json.loads(row["payload_json"])
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


# -- single-instance lock ----------------------------------------------------


class SingleInstanceLock:
    """A pidfile+flock lock in the runtime dir. Best-effort on non-POSIX
    platforms (``fcntl`` import failure never crashes the daemon; it just
    can't guarantee exclusivity there)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:
            return True
        fh = open(self.path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            import fcntl
            fcntl.flock(self._fh, fcntl.LOCK_UN)
        except ImportError:
            pass
        finally:
            self._fh.close()
            self._fh = None


# -- daemon -------------------------------------------------------------------


@dataclass
class DaemonConfig:
    store: ContinuationStore
    boards_root: Path
    overrides_path: Path | None = None
    contracts_dir: Path | None = None
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    reconcile_interval_seconds: float = DEFAULT_RECONCILE_INTERVAL_SECONDS
    apply: bool = False
    external_wait_checker: ExternalWaitChecker | None = None
    source_factory: Callable[[str, BoardRegistryEntry], Any] = live_source_factory
    adapter_factory: Callable[[str, BoardRegistryEntry], Any] | None = None


def _fake_adapter_factory(board: str, entry: BoardRegistryEntry) -> FakeBoardAdapter:
    return FakeBoardAdapter()


@dataclass
class AgentflowDaemon:
    config: DaemonConfig
    _contracts: ContractRegistry | None = field(default=None, init=False, repr=False)

    def _contract_registry(self) -> ContractRegistry:
        if self._contracts is None:
            contracts_dir = self.config.contracts_dir
            paths = sorted(contracts_dir.glob("*.yaml")) if contracts_dir else []
            self._contracts = load_contract_registry(paths)
        return self._contracts

    def _adapter_factory(self) -> Callable[[str, BoardRegistryEntry], Any]:
        if self.config.adapter_factory is not None:
            return self.config.adapter_factory
        return real_adapter_factory if self.config.apply else _fake_adapter_factory

    def tick(self) -> dict[str, Any]:
        """One synchronous wake cycle: discover boards, route every new
        event, poll due external-wait conditions. This is the function every
        test exercises directly — the async loop below only decides *when*
        to call it."""
        registry = discover_boards(boards_root=self.config.boards_root, overrides_path=self.config.overrides_path)
        contracts = self._contract_registry()
        adapter_factory = self._adapter_factory()
        interaction_inbox = InteractionInbox(store=self.config.store)

        board_reports = []
        for board, entry in registry.items():
            adapter = adapter_factory(board, entry)
            board_reports.append(
                route_board_events(
                    board=board,
                    entry=entry,
                    store=self.config.store,
                    contract_registry=contracts,
                    adapter=adapter,
                    interaction_inbox=interaction_inbox,
                    source_factory=self.config.source_factory,
                )
            )
        wait_report = poll_external_wait_conditions(self.config.store, checker=self.config.external_wait_checker)
        return {"success": True, "boards": board_reports, "external_wait": wait_report, "ts": time.time()}

    def reconcile(self) -> dict[str, Any]:
        """Reconciliation pass (plan 2.6/9.5): identical event routing plus
        durable outbox replay. This is the quiet recovery path, never the
        primary path."""
        report = self.tick()
        registry = discover_boards(boards_root=self.config.boards_root, overrides_path=self.config.overrides_path)
        adapter_factory = self._adapter_factory()
        adapter_by_board = {board: adapter_factory(board, entry) for board, entry in registry.items()}
        report["outbox"] = reconcile_outbox(self.config.store, adapter_by_board=adapter_by_board)
        return report

    async def run(self, *, stop_event: asyncio.Event | None = None, max_ticks: int | None = None) -> None:
        """Async wake loop: a short coalescing poll (fallback per plan 9.3 —
        no inotify hardware dependency required) plus a periodic
        reconciliation pass. Stops on ``stop_event``, SIGTERM/SIGINT, or
        after ``max_ticks`` (test-only escape hatch)."""
        stop_event = stop_event or asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, stop_event.set)

        last_reconcile = 0.0
        ticks = 0
        while not stop_event.is_set():
            self.tick()
            ticks += 1
            now = time.time()
            if now - last_reconcile >= self.config.reconcile_interval_seconds:
                self.reconcile()
                last_reconcile = now
            if max_ticks is not None and ticks >= max_ticks:
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self.config.poll_interval_seconds)
