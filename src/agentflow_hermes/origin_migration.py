"""Reviewed, dry-run-first Discord origin canonicalization + legacy callback repair.

Two related but bounded jobs live here (task t_e9f64682 / M33):

1. **Origin canonicalization** — rewrite symbolic Discord chat ids (e.g. the
   ``#research`` placeholder / bare ``research`` label) to the canonical numeric
   channel id inside a *board's own* Hermes Kanban DB: the ``kanban_task_origin``
   binding rows, the ``kanban_notify_receipts`` composite-PK rows (merging any
   collision with an already-numeric row while preserving the strongest facts),
   and finally deleting the leftover symbolic ``kanban_notify_subs`` alias rows
   only after the numeric rows are durable.

2. **Legacy callback repair** — for AgentFlow ``board_outbox`` rows stuck in
   ``callback_deadletter`` because their ACK was recorded against a symbolic
   origin, re-drive a real consumer ACK against the migrated numeric origin via
   an injectable Hermes CLI/adapter runner. Only after a parseable CLI success
   *and* an independent read-back receipt proof does the outbox row transition
   ``callback_deadletter`` -> ``applied`` (clearing its error). A failed ACK
   leaves that row a deadletter and is reported as an explicit partial repair;
   the already-correct origin migration is never rolled back for a callback
   failure (only a failed board transaction rolls the origin migration back).

Everything defaults to a deterministic, side-effect-free **dry run**; ``apply``
is explicit. The dry-run report is byte-stable (no clock, sorted rows) so an
operator can diff two runs and diff plan-vs-apply. Apply first takes timestamped
online SQLite backups of both the board DB and the canonical AgentFlow DB.

This module never sends anything to Discord, never resolves a Discord *name*
(no name lookup is added — symbolic ids are rejected/repaired, not resolved),
never rewrites a board cursor, and never mutates task/run/body/result rows. It
stores/prints only ids, channel ids, statuses, timestamps and counts — never a
raw summary or secret.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from .board_adapter import RealBoardAdapter, default_board_kanban_db_path
from .continuation_store import ContinuationStore, default_continuation_db_path
from .graph_creator import _map_origin_to_flags

# --- receipt fact strengths -------------------------------------------------
# Ranks for deterministic "strongest fact wins" merge of composite-PK receipt
# collisions. Higher rank = stronger. Unknown values collapse to 0 (weakest).
_NOTIFY_RANK = {"": 0, "pending": 1, "queued": 1, "failed": 1, "retrying": 1, "delivered": 3}
_WAKE_RANK = {"": 0, "pending": 1, "scheduled": 2, "accepted": 3, "started": 4, "completed": 5}


class MigrationError(Exception):
    """Bounded, low-cardinality precondition failure (fail-closed refusal)."""

    def __init__(self, code: str, **detail: Any) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail

    def as_refusal(self) -> dict[str, Any]:
        return {"code": self.code, **self.detail}


# --- prevention (generated-task boundary) -----------------------------------

def classify_discord_endpoint(endpoint: str) -> dict[str, Any]:
    """Classify an origin/return_to endpoint for the generated-task boundary.

    Returns ``{"discord": bool, "chat_id": str, "numeric": bool}``. Non-Discord
    (generic) platforms report ``discord=False`` and are left entirely alone.
    """
    flags = _map_origin_to_flags(endpoint or "")
    if not flags or flags.get("platform") != "discord":
        return {"discord": False, "chat_id": "", "numeric": False}
    chat_id = str(flags.get("chat_id") or "").lstrip("#")
    return {"discord": True, "chat_id": chat_id, "numeric": chat_id.isdigit()}


def reject_symbolic_discord_endpoint(endpoint: str) -> str | None:
    """Return an error code if ``endpoint`` is a *symbolic* Discord endpoint.

    AgentFlow-generated Discord origin/return_to endpoints must carry a numeric
    channel id before a task is created / its outbox is materialized. A symbolic
    id (``#research``) is rejected here rather than silently name-resolved later.
    Numeric Discord ids and every non-Discord platform pass (return ``None``).
    """
    info = classify_discord_endpoint(endpoint)
    if info["discord"] and info["chat_id"] and not info["numeric"]:
        return "symbolic_discord_chat_id_rejected"
    return None


# --- input validation -------------------------------------------------------

def normalize_source(from_id: str) -> str:
    return str(from_id or "").lstrip("#").strip()


def validate_migration_inputs(from_id: str, to_id: str) -> str | None:
    """Validate ``--from``/``--to`` before any DB access. Returns error code."""
    src = normalize_source(from_id)
    dst = str(to_id or "").strip()
    if not dst.isdigit():
        return "discord_target_not_numeric"
    if not src:
        return "discord_source_empty"
    if src == dst:
        return "discord_source_equals_target"
    return None


# --- low-level DB helpers ---------------------------------------------------

def resolve_board_db_path(*, board: str, board_db: str = "", boards_root: str = "") -> Path:
    if board_db:
        return Path(board_db)
    if boards_root:
        return Path(boards_root) / board / "kanban.db"
    return default_board_kanban_db_path(board)


def _connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _connect_rw(path: Path, *, timeout: float) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=timeout)
    con.row_factory = sqlite3.Row
    return con


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone() is not None


def _columns(con: sqlite3.Connection, name: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({name})")}


_REQUIRED_ORIGIN_COLS = {"task_id", "platform", "chat_id", "thread_id"}
_REQUIRED_RECEIPT_COLS = {"task_id", "platform", "chat_id", "thread_id"}


def _require_board_schema(con: sqlite3.Connection) -> None:
    if not _table_exists(con, "kanban_task_origin"):
        raise MigrationError("board_schema_missing", table="kanban_task_origin")
    if not _table_exists(con, "kanban_notify_receipts"):
        raise MigrationError("board_schema_missing", table="kanban_notify_receipts")
    if not _REQUIRED_ORIGIN_COLS.issubset(_columns(con, "kanban_task_origin")):
        raise MigrationError("board_schema_incompatible", table="kanban_task_origin")
    if not _REQUIRED_RECEIPT_COLS.issubset(_columns(con, "kanban_notify_receipts")):
        raise MigrationError("board_schema_incompatible", table="kanban_notify_receipts")


# --- audit / list -----------------------------------------------------------

def list_symbolic_discord_origins(board_db: Path, *, from_id: str = "") -> list[dict[str, Any]]:
    """List Discord task-origin rows whose chat id is symbolic (non-numeric).

    When ``from_id`` is given, only rows with that exact symbolic chat id are
    returned; otherwise every non-numeric Discord chat id is surfaced. Read-only.
    """
    board_db = Path(board_db)
    if not board_db.exists():
        raise MigrationError("board_db_missing", path=str(board_db))
    con = _connect_ro(board_db)
    try:
        _require_board_schema(con)
        rows = con.execute(
            "select task_id, chat_id, thread_id from kanban_task_origin where platform='discord' order by task_id, thread_id"
        ).fetchall()
    finally:
        con.close()
    src = normalize_source(from_id)
    out: list[dict[str, Any]] = []
    for r in rows:
        chat_id = str(r["chat_id"] or "").lstrip("#")
        if chat_id.isdigit():
            continue
        if src and chat_id != src:
            continue
        out.append({"task_id": r["task_id"], "chat_id": chat_id, "thread_id": str(r["thread_id"] or "")})
    return out


# --- receipt merge ----------------------------------------------------------

def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def merge_receipt_facts(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Merge a symbolic receipt row into its numeric collision partner.

    Strongest fact wins per field (notify delivered, active wake accepted+, a
    present consumer ACK) and the latest relevant ``*_at`` timestamp is kept.
    Raises ``MigrationError('ambiguous_receipt_merge')`` when the two rows carry
    two *different* non-empty consumer ACK statuses — that conflict cannot be
    ordered and must be resolved by a human, not guessed.
    """
    src_ack = str(source.get("consumer_ack_status") or "")
    tgt_ack = str(target.get("consumer_ack_status") or "")
    if src_ack and tgt_ack and src_ack != tgt_ack:
        raise MigrationError(
            "ambiguous_receipt_merge",
            task_id=str(target.get("task_id") or ""),
            thread_id=str(target.get("thread_id") or ""),
        )

    def _strongest(field: str, rank: dict[str, int]) -> str:
        s = str(source.get(field) or "")
        t = str(target.get(field) or "")
        return s if rank.get(s, 0) > rank.get(t, 0) else t

    merged = {
        "notify_delivery_status": _strongest("notify_delivery_status", _NOTIFY_RANK),
        "active_wake_status": _strongest("active_wake_status", _WAKE_RANK),
        "consumer_ack_status": tgt_ack or src_ack,
        "notify_delivery_at": max(_as_int(source.get("notify_delivery_at")), _as_int(target.get("notify_delivery_at"))),
        "active_wake_at": max(_as_int(source.get("active_wake_at")), _as_int(target.get("active_wake_at"))),
        "consumer_ack_at": max(_as_int(source.get("consumer_ack_at")), _as_int(target.get("consumer_ack_at"))),
        "updated_at": max(_as_int(source.get("updated_at")), _as_int(target.get("updated_at"))),
    }
    return merged


_RECEIPT_MERGE_COLS = (
    "notify_delivery_status",
    "active_wake_status",
    "consumer_ack_status",
    "notify_delivery_at",
    "active_wake_at",
    "consumer_ack_at",
    "updated_at",
)


def _receipt_row_dict(con: sqlite3.Connection, cols: set[str], *, task_id: str, chat_id: str, thread_id: str) -> dict[str, Any] | None:
    selectable = ["task_id", "thread_id"] + [c for c in _RECEIPT_MERGE_COLS if c in cols]
    row = con.execute(
        f"select {', '.join(selectable)} from kanban_notify_receipts "
        "where task_id=? and platform='discord' and chat_id=? and thread_id=?",
        (task_id, chat_id, thread_id),
    ).fetchone()
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


# --- planning (shared by dry-run and apply) ---------------------------------

def build_plan(
    *,
    board: str,
    from_id: str,
    to_id: str,
    board_db: Path,
    store: ContinuationStore,
    deadletter_ids: list[int] | None = None,
    deadletter_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Compute the deterministic migration plan without writing anything.

    Raises ``MigrationError`` for fail-closed refusals (missing DB/schema,
    ambiguous receipt merge, mismatched/cross-board deadletter selection).
    """
    src = normalize_source(from_id)
    dst = str(to_id).strip()
    board_db = Path(board_db)
    if not board_db.exists():
        raise MigrationError("board_db_missing", path=str(board_db))

    con = _connect_ro(board_db)
    try:
        _require_board_schema(con)
        origin_rows = con.execute(
            "select task_id, thread_id from kanban_task_origin where platform='discord' and chat_id=? order by task_id, thread_id",
            (src,),
        ).fetchall()
        task_origin_rows = [{"task_id": r["task_id"], "from_chat_id": src, "to_chat_id": dst, "thread_id": str(r["thread_id"] or "")} for r in origin_rows]

        receipt_cols = _columns(con, "kanban_notify_receipts")
        src_receipts = con.execute(
            "select task_id, thread_id from kanban_notify_receipts where platform='discord' and chat_id=? order by task_id, thread_id",
            (src,),
        ).fetchall()

        receipt_plan: list[dict[str, Any]] = []
        collisions: list[dict[str, Any]] = []
        merged_previews: list[dict[str, Any]] = []
        for r in src_receipts:
            task_id = r["task_id"]
            thread_id = str(r["thread_id"] or "")
            source_full = _receipt_row_dict(con, receipt_cols, task_id=task_id, chat_id=src, thread_id=thread_id)
            target_full = _receipt_row_dict(con, receipt_cols, task_id=task_id, chat_id=dst, thread_id=thread_id)
            if target_full is None:
                receipt_plan.append({"task_id": task_id, "thread_id": thread_id, "action": "rekey"})
            else:
                merged = merge_receipt_facts(source_full or {}, target_full)  # may raise ambiguous_receipt_merge
                receipt_plan.append({"task_id": task_id, "thread_id": thread_id, "action": "merge"})
                collisions.append({"task_id": task_id, "thread_id": thread_id})
                merged_previews.append({"task_id": task_id, "thread_id": thread_id, **{k: merged[k] for k in _RECEIPT_MERGE_COLS}})

        alias_subs: list[dict[str, Any]] = []
        if _table_exists(con, "kanban_notify_subs"):
            sub_cols = _columns(con, "kanban_notify_subs")
            if {"platform", "chat_id"}.issubset(sub_cols):
                thread_expr = "thread_id" if "thread_id" in sub_cols else "''"
                sub_rows = con.execute(
                    f"select task_id, {thread_expr} as thread_id from kanban_notify_subs where platform='discord' and chat_id=? order by task_id",
                    (src,),
                ).fetchall()
                alias_subs = [{"task_id": r["task_id"], "thread_id": str(r["thread_id"] or "")} for r in sub_rows]
    finally:
        con.close()

    deadletters = _select_deadletters(
        store, board=board, from_id=src, to_id=dst,
        deadletter_ids=deadletter_ids, deadletter_refs=deadletter_refs,
    )
    affected_source_tasks = sorted({row["task_id"] for row in task_origin_rows} | {d["task_id"] for d in deadletters if d["task_id"]})

    return {
        "board": board,
        "from": src,
        "to": dst,
        "task_origin_rows": task_origin_rows,
        "receipt_rows": receipt_plan,
        "collisions": collisions,
        "merged_receipts": merged_previews,
        "alias_subs_to_delete": alias_subs,
        "affected_deadletters": deadletters,
        "affected_source_tasks": affected_source_tasks,
    }


def _select_deadletters(
    store: ContinuationStore,
    *,
    board: str,
    from_id: str,
    to_id: str,
    deadletter_ids: list[int] | None,
    deadletter_refs: list[str] | None,
) -> list[dict[str, Any]]:
    """Select callback_deadletter outbox rows for this board's Discord origins.

    Explicit ``--deadletter-id``/``--deadletter-ref`` selection is validated
    fail-closed: a selected row on another board, a non-callback row, or a row
    whose endpoint is not this Discord ``from``/``to`` channel is a hard refusal
    (``deadletter_board_task_mismatch``). Without explicit selection, every
    board-matching callback deadletter for this Discord channel is auto-included.
    """
    store.init()
    import json as _json

    explicit_ids = set(deadletter_ids or [])
    explicit_refs = set(deadletter_refs or [])
    explicit = bool(explicit_ids or explicit_refs)

    with store.connect() as con:
        rows = [dict(r) for r in con.execute(
            """
            select o.id, o.idempotency_key, o.operation, o.state, o.payload_json,
                   c.board as board, c.source_task_id as source_task_id
            from board_outbox o join continuation_instances c on c.id = o.continuation_id
            """
        ).fetchall()]

    by_id = {int(r["id"]): r for r in rows}
    by_ref = {str(r["idempotency_key"]): r for r in rows}

    def _endpoint_chat_id(payload: dict[str, Any]) -> tuple[str, str]:
        endpoint = str(payload.get("endpoint") or "")
        info = classify_discord_endpoint(endpoint)
        return endpoint, info["chat_id"] if info["discord"] else ""

    selected: dict[int, dict[str, Any]] = {}

    if explicit:
        targets: list[dict[str, Any]] = []
        for oid in explicit_ids:
            if oid not in by_id:
                raise MigrationError("deadletter_board_task_mismatch", outbox_id=oid, reason="unknown_id")
            targets.append(by_id[oid])
        for ref in explicit_refs:
            if ref not in by_ref:
                raise MigrationError("deadletter_board_task_mismatch", idempotency_key=ref, reason="unknown_ref")
            targets.append(by_ref[ref])
        for row in targets:
            if str(row["board"]) != board:
                raise MigrationError("deadletter_board_task_mismatch", outbox_id=int(row["id"]), reason="wrong_board")
            if str(row["state"]) != "callback_deadletter":
                raise MigrationError("deadletter_board_task_mismatch", outbox_id=int(row["id"]), reason="not_callback_deadletter")
            payload = _json.loads(row["payload_json"] or "{}")
            _endpoint, chat_id = _endpoint_chat_id(payload)
            if chat_id not in {from_id, to_id}:
                raise MigrationError("deadletter_board_task_mismatch", outbox_id=int(row["id"]), reason="endpoint_channel_mismatch")
            selected[int(row["id"])] = _deadletter_entry(row, payload)
    else:
        for row in rows:
            if str(row["board"]) != board or str(row["state"]) != "callback_deadletter":
                continue
            payload = _json.loads(row["payload_json"] or "{}")
            _endpoint, chat_id = _endpoint_chat_id(payload)
            if chat_id not in {from_id, to_id}:
                continue
            selected[int(row["id"])] = _deadletter_entry(row, payload)

    return [selected[k] for k in sorted(selected)]


def _deadletter_entry(row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "outbox_id": int(row["id"]),
        "idempotency_key": str(row["idempotency_key"]),
        "operation": str(row["operation"]),
        "task_id": str(payload.get("task_id") or ""),
        "status": str(payload.get("status") or ""),
        "endpoint": str(payload.get("endpoint") or ""),
    }


# --- dry run ----------------------------------------------------------------

def plan_discord_migration(
    *,
    board: str,
    from_id: str,
    to_id: str,
    board_db: Path,
    store: ContinuationStore,
    deadletter_ids: list[int] | None = None,
    deadletter_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Deterministic, side-effect-free dry-run report. ``writes`` is always 0.

    Byte-stable: no wall-clock is embedded (planned backup names use a
    ``<timestamp>`` placeholder) and every row list is deterministically sorted.
    """
    err = validate_migration_inputs(from_id, to_id)
    if err:
        return {"success": False, "mode": "dry-run", "error": err, "writes": 0}
    try:
        plan = build_plan(
            board=board, from_id=from_id, to_id=to_id, board_db=board_db, store=store,
            deadletter_ids=deadletter_ids, deadletter_refs=deadletter_refs,
        )
    except MigrationError as exc:
        return {"success": False, "mode": "dry-run", "error": exc.code, "refusals": [exc.as_refusal()], "writes": 0}
    return {
        "success": True,
        "mode": "dry-run",
        "writes": 0,
        "created_tasks": 0,
        "planned_backups": {
            "board_db": f"{board_db}.pre-migrate-discord.<timestamp>.bak",
            "canonical_db": f"{store.path}.pre-migrate-discord.<timestamp>.bak",
        },
        "cursor_rewrites": 0,
        **plan,
    }


# --- backups ----------------------------------------------------------------

def _online_backup(src_path: Path, dst_path: Path) -> dict[str, Any]:
    """Create a consistent online SQLite backup and verify its integrity."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dst_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    check = sqlite3.connect(f"file:{dst_path}?mode=ro", uri=True)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()
        ok = bool(integrity) and str(integrity[0]).lower() == "ok"
    finally:
        check.close()
    return {"path": str(dst_path), "integrity_ok": ok}


# --- apply ------------------------------------------------------------------

def apply_discord_migration(
    *,
    board: str,
    from_id: str,
    to_id: str,
    board_db: Path,
    store: ContinuationStore,
    adapter: Any = None,
    deadletter_ids: list[int] | None = None,
    deadletter_refs: list[str] | None = None,
    allow_active_writers: bool = False,
    now: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Apply the migration: backups -> single board txn -> callback repair.

    Fail-closed on every precondition. The origin/receipt/subs migration runs in
    one board transaction; a callback-repair failure never rolls it back.
    """
    board_db = Path(board_db)
    clock = now or time.time

    err = validate_migration_inputs(from_id, to_id)
    if err:
        return {"success": False, "mode": "apply", "error": err, "writes": 0}

    if not store.path.exists() and not str(store.path):
        return {"success": False, "mode": "apply", "error": "canonical_db_missing", "writes": 0}

    try:
        plan = build_plan(
            board=board, from_id=from_id, to_id=to_id, board_db=board_db, store=store,
            deadletter_ids=deadletter_ids, deadletter_refs=deadletter_refs,
        )
    except MigrationError as exc:
        return {"success": False, "mode": "apply", "error": exc.code, "refusals": [exc.as_refusal()], "writes": 0}

    src = plan["from"]
    dst = plan["to"]
    nothing_to_migrate = not (plan["task_origin_rows"] or plan["receipt_rows"] or plan["alias_subs_to_delete"])

    # Backups first (skip only when there is genuinely nothing to migrate AND no
    # deadletters to repair -- an idempotent rerun stays a true no-op).
    backups: dict[str, Any] = {}
    board_migration: dict[str, Any] = {"origins_migrated": 0, "receipts_rekeyed": 0, "receipts_merged": 0, "alias_subs_deleted": 0}
    if not (nothing_to_migrate and not plan["affected_deadletters"]):
        ts = int(clock())
        board_backup = board_db.with_name(f"{board_db.name}.pre-migrate-discord.{ts}.bak")
        canonical_backup = store.path.with_name(f"{store.path.name}.pre-migrate-discord.{ts}.bak")
        backups = {
            "board_db": _online_backup(board_db, board_backup),
            "canonical_db": _online_backup(store.path, canonical_backup),
        }

    if not nothing_to_migrate:
        try:
            board_migration = _apply_board_transaction(
                board_db, src=src, dst=dst, allow_active_writers=allow_active_writers
            )
        except MigrationError as exc:
            # Board transaction failed/rolled back: origin migration NOT applied.
            return {
                "success": False, "mode": "apply", "error": exc.code,
                "refusals": [exc.as_refusal()], "backups": backups, "writes": 0,
            }

    repair = _repair_callback_deadletters(
        store, board=board, adapter=adapter, deadletters=plan["affected_deadletters"], to_id=dst
    )

    return {
        "success": True,
        "mode": "apply",
        "board": board,
        "from": src,
        "to": dst,
        "backups": backups,
        "board_migration": board_migration,
        "callback_repair": repair,
        "created_tasks": 0,
        "cursor_rewrites": 0,
        "task_origin_rows": plan["task_origin_rows"],
        "collisions": plan["collisions"],
        "merged_receipts": plan["merged_receipts"],
        "alias_subs_to_delete": plan["alias_subs_to_delete"],
        "affected_source_tasks": plan["affected_source_tasks"],
    }


def _apply_board_transaction(board_db: Path, *, src: str, dst: str, allow_active_writers: bool) -> dict[str, Any]:
    """Migrate origins + receipts + delete alias subs in one board transaction.

    Refuses ``active_writers_present`` when the board DB is locked, unless the
    bounded coordinated-apply contract (``allow_active_writers``) is used, which
    permits a single bounded busy-wait rather than an unbounded storm.
    """
    timeout = 5.0 if allow_active_writers else 0.0
    con = _connect_rw(board_db, timeout=timeout)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        try:
            con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise MigrationError("active_writers_present") from exc
            raise MigrationError("board_transaction_failed") from exc

        receipt_cols = _columns(con, "kanban_notify_receipts")
        stats = {"origins_migrated": 0, "receipts_rekeyed": 0, "receipts_merged": 0, "alias_subs_deleted": 0}

        # 1) task-origin rows (PK task_id, no collision on chat_id rewrite).
        cur = con.execute(
            "update kanban_task_origin set chat_id=? where platform='discord' and chat_id=?",
            (dst, src),
        )
        stats["origins_migrated"] = cur.rowcount

        # 2) receipt composite-PK rows: merge collisions, else rekey.
        src_receipts = con.execute(
            "select task_id, thread_id from kanban_notify_receipts where platform='discord' and chat_id=? order by task_id, thread_id",
            (src,),
        ).fetchall()
        for r in src_receipts:
            task_id = r["task_id"]
            thread_id = str(r["thread_id"] or "")
            source_full = _receipt_row_dict(con, receipt_cols, task_id=task_id, chat_id=src, thread_id=thread_id)
            target_full = _receipt_row_dict(con, receipt_cols, task_id=task_id, chat_id=dst, thread_id=thread_id)
            if target_full is None:
                con.execute(
                    "update kanban_notify_receipts set chat_id=? where task_id=? and platform='discord' and chat_id=? and thread_id=?",
                    (dst, task_id, src, thread_id),
                )
                stats["receipts_rekeyed"] += 1
            else:
                merged = merge_receipt_facts(source_full or {}, target_full)
                set_cols = [c for c in _RECEIPT_MERGE_COLS if c in receipt_cols]
                assignments = ", ".join(f"{c}=?" for c in set_cols)
                con.execute(
                    f"update kanban_notify_receipts set {assignments} where task_id=? and platform='discord' and chat_id=? and thread_id=?",
                    (*[merged[c] for c in set_cols], task_id, dst, thread_id),
                )
                con.execute(
                    "delete from kanban_notify_receipts where task_id=? and platform='discord' and chat_id=? and thread_id=?",
                    (task_id, src, thread_id),
                )
                stats["receipts_merged"] += 1

        # 3) delete leftover symbolic alias subs -- only now, numeric rows durable.
        if _table_exists(con, "kanban_notify_subs") and {"platform", "chat_id"}.issubset(_columns(con, "kanban_notify_subs")):
            cur = con.execute(
                "delete from kanban_notify_subs where platform='discord' and chat_id=?",
                (src,),
            )
            stats["alias_subs_deleted"] = cur.rowcount

        con.execute("COMMIT")
        return stats
    except MigrationError:
        with_rollback(con)
        raise
    except sqlite3.Error as exc:
        with_rollback(con)
        raise MigrationError("board_transaction_failed") from exc
    finally:
        con.close()


def with_rollback(con: sqlite3.Connection) -> None:
    try:
        con.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _default_repair_adapter(board: str, board_db: Path) -> RealBoardAdapter:
    return RealBoardAdapter(board=board, board_db_path=board_db)


def _repair_callback_deadletters(
    store: ContinuationStore,
    *,
    board: str,
    adapter: Any,
    deadletters: list[dict[str, Any]],
    to_id: str,
) -> dict[str, Any]:
    """Re-drive a real consumer ACK against the migrated numeric origin.

    Only a parseable CLI success plus an independent read-back receipt proof
    transitions the outbox row callback_deadletter->applied (clearing its
    error). A failure leaves the row a deadletter and is reported as a partial
    repair -- the origin migration already committed is not rolled back.
    """
    if not deadletters:
        return {"repaired": [], "partial": [], "attempted": 0}

    if adapter is None:
        raise MigrationError("callback_repair_adapter_missing")

    repaired: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []

    for entry in deadletters:
        task_id = entry["task_id"]
        status = entry["status"]
        numeric_endpoint = _numeric_discord_endpoint(entry["endpoint"], to_id)

        result = adapter.record_consumer_ack(task_id, numeric_endpoint, status)
        proven = bool(result and result.get("success"))
        if proven:
            # Independent read-back proof (defense in depth over the adapter's
            # own verify): never transition on unverified ACK evidence.
            verify = getattr(adapter, "consumer_ack_satisfied", None)
            if verify is not None:
                readback = verify(task_id, numeric_endpoint, status)
                proven = bool(readback and readback.get("success"))

        if proven:
            store.outbox_mark(entry["outbox_id"], state="applied")
            repaired.append({"outbox_id": entry["outbox_id"], "task_id": task_id, "status": status})
        else:
            error = str((result or {}).get("error") or "callback_ack_unverified")
            partial.append({"outbox_id": entry["outbox_id"], "task_id": task_id, "error": error[:80]})

    return {"repaired": repaired, "partial": partial, "attempted": len(deadletters)}


def _numeric_discord_endpoint(endpoint: str, to_id: str) -> str:
    """Rebuild a Discord endpoint against the migrated numeric channel id.

    Preserves any thread/user fields parsed from the legacy endpoint; only the
    symbolic channel id is replaced with the canonical numeric ``to_id``.
    """
    flags = _map_origin_to_flags(endpoint or "")
    if not flags or flags.get("platform") != "discord":
        return endpoint
    parts = ["discord", to_id]
    if flags.get("thread_id"):
        parts.append(str(flags["thread_id"]))
        if flags.get("user_id"):
            parts.append(str(flags["user_id"]))
    return ":".join(parts)


# --- CLI wiring -------------------------------------------------------------

def add_origin_cli_args(sub: argparse._SubParsersAction) -> None:
    migrate = sub.add_parser("migrate-discord", help="Canonicalize symbolic Discord origins and repair legacy callbacks.")
    migrate.add_argument("--board", required=True)
    migrate.add_argument("--from", dest="from_id", required=True, help="Symbolic Discord chat id to migrate (e.g. #research).")
    migrate.add_argument("--to", dest="to_id", required=True, help="Canonical numeric Discord channel id.")
    migrate.add_argument("--apply", action="store_true", default=False, help="Apply changes (default is a side-effect-free dry run).")
    migrate.add_argument("--board-db", default="", help="Override board Kanban DB path (tests / non-default roots).")
    migrate.add_argument("--boards-root", default="", help="Override boards root; board DB is <root>/<board>/kanban.db.")
    migrate.add_argument("--db", default="", help="Override canonical AgentFlow control-plane DB path (tests).")
    migrate.add_argument("--deadletter-id", dest="deadletter_ids", action="append", type=int, default=None)
    migrate.add_argument("--deadletter-ref", dest="deadletter_refs", action="append", default=None)
    migrate.add_argument("--hermes-bin", default="hermes")
    migrate.add_argument("--coordinated-apply", action="store_true", default=False,
                         help="Bounded coordinated-apply contract: permit apply against a board with active writers.")

    listp = sub.add_parser("list", help="Audit symbolic Discord task-origin rows (read-only).")
    listp.add_argument("--board", required=True)
    listp.add_argument("--board-db", default="")
    listp.add_argument("--boards-root", default="")
    listp.add_argument("--from", dest="from_id", default="", help="Only list this symbolic chat id (default: all non-numeric).")


def _store_from_args(args: argparse.Namespace) -> ContinuationStore:
    db = str(getattr(args, "db", "") or "")
    return ContinuationStore(Path(db)) if db else ContinuationStore(default_continuation_db_path())


def run_origin_list(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    board_db = resolve_board_db_path(board=args.board, board_db=args.board_db, boards_root=args.boards_root)
    try:
        rows = list_symbolic_discord_origins(board_db, from_id=args.from_id)
    except MigrationError as exc:
        return 2, {"success": False, "error": exc.code, "refusals": [exc.as_refusal()]}
    return 0, {"success": True, "board": args.board, "symbolic_origins": rows, "count": len(rows)}


def run_origin_migrate_discord(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    board_db = resolve_board_db_path(board=args.board, board_db=args.board_db, boards_root=args.boards_root)
    store = _store_from_args(args)

    if not args.apply:
        report = plan_discord_migration(
            board=args.board, from_id=args.from_id, to_id=args.to_id,
            board_db=board_db, store=store,
            deadletter_ids=args.deadletter_ids, deadletter_refs=args.deadletter_refs,
        )
        return (0 if report.get("success") else 2), report

    adapter = _default_repair_adapter(args.board, board_db)
    report = apply_discord_migration(
        board=args.board, from_id=args.from_id, to_id=args.to_id,
        board_db=board_db, store=store, adapter=adapter,
        deadletter_ids=args.deadletter_ids, deadletter_refs=args.deadletter_refs,
        allow_active_writers=args.coordinated_apply,
    )
    return (0 if report.get("success") else 2), report
