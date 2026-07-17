"""Operator CLI surface for the needs-input continuation engine.

``ingest`` reads a controlled fixture file of board events (there is no
importable real-time Hermes Kanban event stream client available at runtime,
matching the same constraint documented on ``RealKanbanGraphAdapter``).
Real-time board polling against a live per-board Kanban DB is an explicit,
documented follow-up (see docs/plans design section 3.7), not hidden here.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from .board_adapter import FakeBoardAdapter, RealBoardAdapter
from .board_events import BoardEvent, FakeBoardEventSource
from .continuation_config import ContractRegistry, UnknownContractError, load_contract_registry
from .continuation_engine import ingest_board_once
from .continuation_store import (
    ContinuationState,
    ContinuationStore,
    default_legacy_continuation_db_paths,
    doctor_store_selection,
    legacy_residue_report,
    migrate_all_legacy_stores,
    migrate_legacy_store,
)
from .live.sanitize import safe_event_payload

_DEFAULT_CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"


def _default_contract_paths() -> list[Path]:
    if not _DEFAULT_CONTRACTS_DIR.exists():
        return []
    return sorted(_DEFAULT_CONTRACTS_DIR.glob("*.yaml"))


def _load_registry(contract_files: list[str] | None) -> ContractRegistry:
    paths: list[Path] = [Path(p) for p in contract_files] if contract_files else _default_contract_paths()
    return load_contract_registry(paths)


def _store(args: argparse.Namespace) -> ContinuationStore:
    db = str(getattr(args, "db", "") or "")
    return ContinuationStore(Path(db)) if db else ContinuationStore.canonical()


def _adapter(args: argparse.Namespace):
    mode = getattr(args, "adapter_mode", "fake")
    if mode == "real":
        board = getattr(args, "board", "") or ""
        if not board:
            raise ValueError("adapter_mode_real_requires_explicit_board")
        return RealBoardAdapter(board=board, hermes_bin=getattr(args, "hermes_bin", "hermes"))
    return FakeBoardAdapter()


def add_continuation_cli_args(sub: argparse._SubParsersAction) -> None:
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--board", required=True)
    ingest.add_argument("--board-db-identity", default="")
    ingest.add_argument("--events-file", required=True)
    ingest.add_argument("--contract-file", action="append", default=None)
    ingest.add_argument("--adapter-mode", choices=["fake", "real"], default="fake")
    ingest.add_argument("--hermes-bin", default="hermes")
    ingest.add_argument("--db", default="")

    listp = sub.add_parser("list")
    listp.add_argument("--state", default="")
    listp.add_argument("--db", default="")

    show = sub.add_parser("show")
    show.add_argument("instance_id", type=int)
    show.add_argument("--db", default="")

    submit = sub.add_parser("submit")
    submit.add_argument("instance_id", type=int)
    submit.add_argument("--input-file", required=True)
    submit.add_argument("--contract-file", action="append", default=None)
    submit.add_argument("--adapter-mode", choices=["fake", "real"], default="fake")
    submit.add_argument("--hermes-bin", default="hermes")
    submit.add_argument("--board", default="")
    submit.add_argument("--db", default="")

    retry = sub.add_parser("retry")
    retry.add_argument("instance_id", type=int)
    retry.add_argument("--adapter-mode", choices=["fake", "real"], default="fake")
    retry.add_argument("--hermes-bin", default="hermes")
    retry.add_argument("--board", default="")
    retry.add_argument("--db", default="")

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--db", default="")
    doctor.add_argument("--fallback-db", default="")

    migrate_store = sub.add_parser("migrate-store")
    migrate_store.add_argument("--db", default="")
    migrate_store.add_argument("--legacy-db", action="append", default=None)


def run_continuation_ingest(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    store = _store(args)
    contracts = _load_registry(args.contract_file)
    try:
        adapter = _adapter(args)
    except ValueError as exc:
        return 2, {"success": False, "error": str(exc)}

    raw_events = json.loads(Path(args.events_file).read_text(encoding="utf-8"))
    events = [
        BoardEvent(
            event_id=str(e.get("event_id") or ""),
            event_seq=int(e.get("event_seq") or 0),
            source_task_id=str(e.get("source_task_id") or ""),
            source_graph_id=str(e.get("source_graph_id") or ""),
            summary=str(e.get("summary") or ""),
            run_metadata=e.get("run_metadata"),
            origin_ref=str(e.get("origin_ref") or ""),
            return_to_ref=str(e.get("return_to_ref") or ""),
            workspace_ref=str(e.get("workspace_ref") or ""),
            assignee=str(e.get("assignee") or ""),
            occurred_at=float(e.get("occurred_at") or 0.0),
        )
        for e in raw_events
    ]
    source = FakeBoardEventSource(db_identity=args.board_db_identity or args.board, events=events)
    result = ingest_board_once(board=args.board, source=source, store=store, contract_registry=contracts, adapter=adapter)
    return 0, safe_event_payload(result)


def run_continuation_list(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    store = _store(args)
    instances = store.list_instances(state=args.state or None)
    return 0, safe_event_payload({"success": True, "instances": instances})


def run_continuation_show(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    store = _store(args)
    instance = store.get_instance(args.instance_id)
    if instance is None:
        return 2, {"success": False, "error": "unknown_instance"}

    contracts = _load_registry(None)
    required_owner_fields: list[str] = []
    system_fields: list[str] = []
    try:
        contract = contracts.get(instance.get("contract_ref") or "")
        required_owner_fields = [f.name for f in contract.owner_fields()]
        system_fields = [f.name for f in contract.fields if f.authority.value == "system"]
    except UnknownContractError:
        pass

    steps = store.list_steps(args.instance_id)
    receipts = store.list_owner_receipts(args.instance_id)

    report = {
        "success": True,
        "instance": instance,
        "why_paused": instance.get("continuation_kind") or "",
        "required_owner_fields": required_owner_fields,
        "system_derived_fields_available": system_fields,
        "steps": steps,
        "owner_receipts": [{"version": r["version"], "owner_ref": r["owner_ref"]} for r in receipts],
        "after_submit_will": "materialize exactly one downstream artifact task",
        "downstream_will_not": [
            "fabricate an owner-authority field",
            "assert owner approval without a real receipt",
            "create review or packet-rerun tasks before prior semantic GO",
        ],
    }
    return 0, safe_event_payload(report)


def run_continuation_submit(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    store = _store(args)
    instance = store.get_instance(args.instance_id)
    if instance is None:
        return 2, {"success": False, "error": "unknown_instance"}

    contracts = _load_registry(args.contract_file)
    try:
        contract = contracts.get(instance.get("contract_ref") or "")
    except UnknownContractError:
        return 2, {"success": False, "error": "unknown_contract_ref"}

    if instance["state"] != ContinuationState.WAITING_OWNER.value:
        return 2, {"success": False, "error": "not_waiting_owner", "state": instance["state"]}

    submission = json.loads(Path(args.input_file).read_text(encoding="utf-8"))
    clean, errors = contract.validate_owner_submission(dict(submission.get("fields") or {}))
    if errors:
        return 2, safe_event_payload({"success": False, "errors": errors})

    from .continuation import get_handler
    from .outcome import ContinuationKind

    handler = get_handler(ContinuationKind.NEEDS_INPUT)
    try:
        adapter = _adapter(args)
    except ValueError as exc:
        return 2, {"success": False, "error": str(exc)}
    result = handler.on_receipt(instance, submission, store=store, adapter=adapter, contract=contract)
    payload = {"success": result.success, "state": result.state, "reason": result.reason, "metadata": result.metadata}
    return (0 if result.success else 2), safe_event_payload(payload)


def run_continuation_retry(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    store = _store(args)
    instance = store.get_instance(args.instance_id)
    if instance is None:
        return 2, {"success": False, "error": "unknown_instance"}

    try:
        adapter = _adapter(args)
    except ValueError as exc:
        return 2, {"success": False, "error": str(exc)}
    pending = [row for row in store.list_outbox() if row["continuation_id"] == args.instance_id and row["state"] == "pending"]
    retried = []
    for row in pending:
        payload = json.loads(row["payload_json"])
        operation = row.get("operation") or "create_task"
        if operation == "create_task":
            result = adapter.create_task(payload)
        elif operation == "subscribe":
            result = adapter.subscribe(str(payload.get("task_id") or ""), str(payload.get("endpoint") or ""))
        elif operation == "complete_owner_anchor":
            result = adapter.complete_owner_anchor(
                str(payload.get("task_id") or ""), receipt_ref=str(payload.get("receipt_ref") or "")
            )
        else:
            result = {"success": False, "error": "unknown_outbox_operation"}
        if result.get("success"):
            task_id = result.get("task_id", "")
            store.outbox_mark(row["id"], state="applied", board_task_id=task_id)
            step_id = row.get("step_id") or ""
            if step_id:
                store.mark_step(int(step_id), state="applied", board_task_id=task_id)
            retried.append(row["idempotency_key"])
    return 0, safe_event_payload({"success": True, "retried": retried, "pending_before": len(pending)})


def run_continuation_doctor(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    result = doctor_store_selection(
        canonical_path=Path(args.db) if args.db else None,
        fallback_path=Path(args.fallback_db) if args.fallback_db else None,
    )
    # Report stale legacy state without requiring manual cleanup (plan
    # section 10 item 6): every known legacy path's residue, even ones
    # doctor_store_selection itself never inspects (e.g. the M26 needs-input
    # watchdog DB).
    result["legacy_residue"] = legacy_residue_report()
    selected = result.get("selected")
    if selected:
        store = ContinuationStore(Path(str(selected)))
        store.init()
        now = time.time()
        with store.connect() as con:
            cursors = [dict(r) for r in con.execute("select board, db_identity, last_event_id, updated_at from board_cursors order by board, db_identity").fetchall()]
            outbox_rows = [dict(r) for r in con.execute(
                """
                select o.id, o.continuation_id, o.operation, o.state, o.attempts, o.last_error, o.next_attempt_at,
                       c.board, c.source_task_id, c.source_event_id, c.continuation_kind, c.state as continuation_state, c.updated_at as continuation_updated_at
                  from board_outbox o join continuation_instances c on c.id=o.continuation_id
                 where o.operation in ('schedule_origin_wake', 'record_consumer_ack')
                   and o.state in ('pending', 'callback_deadletter', 'callback_unroutable', 'callback_deferred')
                 order by o.updated_at, o.id
                """
            ).fetchall()]
            oldest = con.execute(
                """
                select min(updated_at) as oldest
                  from continuation_instances
                 where state in ('failed_retryable', 'materializing', 'waiting_review')
                """
            ).fetchone()
        pending = [r for r in outbox_rows if r["state"] == "pending"]
        dead = [r for r in outbox_rows if r["state"] != "pending"]
        typed_origin_missing = [r for r in outbox_rows if r.get("last_error") == "typed_origin_missing"]
        result["cursor_health"] = {
            "cursors": cursors,
            "cursor_lag_available": False,
            "poison_candidates": [
                {
                    "board": r["board"],
                    "source_event_id": r["source_event_id"],
                    "continuation_id": r["continuation_id"],
                    "operation": r["operation"],
                    "state": r["state"],
                    "attempts": r["attempts"],
                    "last_error": r["last_error"],
                }
                for r in outbox_rows if r["continuation_state"] == ContinuationState.FAILED_RETRYABLE.value
            ],
            "oldest_blocked_age_seconds": (now - float(oldest["oldest"])) if oldest and oldest["oldest"] is not None else None,
        }
        result["callback_routing"] = {
            "pending": len(pending),
            "deadletter": len(dead),
            "typed_origin_missing": len(typed_origin_missing),
            "unhealthy": len(outbox_rows),
            "typed_origin_missing_is_semantic_protection_absent": False,
            "rows": outbox_rows[:20],
        }
    return (0 if result.get("success") else 2), result


def run_continuation_migrate_store(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    canonical = ContinuationStore(Path(args.db)) if args.db else ContinuationStore.canonical()
    legacy_paths = tuple(Path(p) for p in args.legacy_db) if args.legacy_db else default_legacy_continuation_db_paths()
    result = migrate_all_legacy_stores(canonical=canonical, legacy_paths=legacy_paths)
    return (0 if result.get("success") else 2), result
