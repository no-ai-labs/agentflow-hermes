"""Durable continuation ledger: instances, steps, owner receipts, events,
board cursors, and outbox. One canonical store, selected explicitly rather
than silently split across two default DB paths (see ``doctor_store_selection``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .migrations import migrate
from .store import default_db_path as fallback_job_db_path


class ContinuationState(str, Enum):
    DETECTED = "detected"
    WAITING_OWNER = "waiting_owner"
    INPUT_ACCEPTED = "input_accepted"
    MATERIALIZING = "materializing"
    WAITING_REVIEW = "waiting_review"
    RESUMABLE = "resumable"
    RESUMED = "resumed"
    BLOCKED_INVALID = "blocked_invalid"
    FAILED_RETRYABLE = "failed_retryable"


TERMINAL_STATES = {ContinuationState.RESUMED, ContinuationState.BLOCKED_INVALID}

_MATERIALIZATION_STATES = {ContinuationState.MATERIALIZING, ContinuationState.WAITING_REVIEW}

ALLOWED_TRANSITIONS: dict[ContinuationState, set[ContinuationState]] = {
    ContinuationState.DETECTED: {ContinuationState.WAITING_OWNER, ContinuationState.MATERIALIZING},
    ContinuationState.WAITING_OWNER: {ContinuationState.INPUT_ACCEPTED},
    ContinuationState.INPUT_ACCEPTED: {ContinuationState.MATERIALIZING},
    ContinuationState.MATERIALIZING: {ContinuationState.WAITING_REVIEW, ContinuationState.RESUMABLE, ContinuationState.FAILED_RETRYABLE},
    ContinuationState.WAITING_REVIEW: {ContinuationState.RESUMABLE, ContinuationState.FAILED_RETRYABLE},
    ContinuationState.RESUMABLE: {ContinuationState.RESUMED},
    ContinuationState.FAILED_RETRYABLE: {ContinuationState.MATERIALIZING, ContinuationState.WAITING_REVIEW},
    ContinuationState.RESUMED: set(),
    ContinuationState.BLOCKED_INVALID: set(),
}


def _legal(current: ContinuationState, new: ContinuationState) -> bool:
    if current in TERMINAL_STATES:
        return False
    if new == ContinuationState.BLOCKED_INVALID:
        return True
    return new in ALLOWED_TRANSITIONS.get(current, set())


def default_continuation_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def default_continuation_db_path() -> Path:
    explicit = os.environ.get("HERMES_CONTINUATION_DB")
    if explicit:
        return Path(explicit)
    return default_continuation_home() / "agentflow" / "agentflow.sqlite"


def fallback_continuation_db_path() -> Path:
    return fallback_job_db_path()


@dataclass(frozen=True)
class ContinuationStore:
    path: Path

    @classmethod
    def canonical(cls) -> "ContinuationStore":
        return cls(default_continuation_db_path())

    def connect(self, *, timeout: float = 30.0) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=timeout)
        con.row_factory = sqlite3.Row
        return con

    def init(self) -> None:
        with self.connect() as con:
            migrate(con)

    # -- instances -----------------------------------------------------

    def create_instance(
        self,
        *,
        board: str,
        source_task_id: str,
        source_event_id: str,
        source_graph_id: str = "",
        contract_ref: str = "",
        verdict: str = "",
        continuation_kind: str = "",
        origin_ref: str = "",
        return_to_ref: str = "",
        workspace_ref: str = "",
    ) -> dict[str, Any]:
        self.init()
        now = time.time()
        idempotency_key = f"continuation:{board}:{source_task_id}:{source_event_id}:{contract_ref}"
        with self.connect() as con:
            existing = con.execute(
                "select * from continuation_instances where board=? and source_task_id=? and source_event_id=? and contract_ref=?",
                (board, source_task_id, source_event_id, contract_ref),
            ).fetchone()
            if existing is not None:
                return {"created": False, "instance": dict(existing)}
            cur = con.execute(
                """
                insert into continuation_instances(
                    board, source_task_id, source_event_id, source_graph_id,
                    contract_ref, verdict, continuation_kind, state,
                    origin_ref, return_to_ref, workspace_ref,
                    idempotency_key, created_at, updated_at
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    board, source_task_id, source_event_id, source_graph_id,
                    contract_ref, verdict, continuation_kind, ContinuationState.DETECTED.value,
                    origin_ref, return_to_ref, workspace_ref,
                    idempotency_key, now, now,
                ),
            )
            instance_id = cur.lastrowid
            self._record_event(con, instance_id, "created", {"state": ContinuationState.DETECTED.value})
            row = con.execute("select * from continuation_instances where id=?", (instance_id,)).fetchone()
            return {"created": True, "instance": dict(row)}

    def get_instance(self, instance_id: int) -> dict[str, Any] | None:
        self.init()
        with self.connect() as con:
            row = con.execute("select * from continuation_instances where id=?", (instance_id,)).fetchone()
        return dict(row) if row else None

    def list_instances(self, *, state: str | None = None) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as con:
            if state:
                rows = con.execute("select * from continuation_instances where state=? order by id", (state,)).fetchall()
            else:
                rows = con.execute("select * from continuation_instances order by id").fetchall()
        return [dict(r) for r in rows]

    def transition(self, instance_id: int, new_state: ContinuationState, *, reason: str = "") -> dict[str, Any]:
        self.init()
        now = time.time()
        with self.connect() as con:
            row = con.execute("select * from continuation_instances where id=?", (instance_id,)).fetchone()
            if row is None:
                return {"success": False, "error": "unknown_instance"}
            current = ContinuationState(row["state"])
            if current in TERMINAL_STATES:
                return {"success": False, "error": "already_terminal"}
            if not _legal(current, new_state):
                return {"success": False, "error": "illegal_transition"}
            con.execute(
                "update continuation_instances set state=?, updated_at=? where id=?",
                (new_state.value, now, instance_id),
            )
            self._record_event(con, instance_id, "state_transition", {"from": current.value, "to": new_state.value, "reason": reason})
            return {"success": True, "applied": True, "state": new_state.value}

    # -- steps -----------------------------------------------------------

    def add_step(
        self,
        instance_id: int,
        *,
        step_kind: str,
        idempotency_key: str,
        board_task_id: str = "",
        parent_step_id: int | None = None,
    ) -> dict[str, Any]:
        self.init()
        now = time.time()
        with self.connect() as con:
            existing = con.execute(
                "select * from continuation_steps where continuation_id=? and idempotency_key=?",
                (instance_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return {"created": False, "step": dict(existing)}
            cur = con.execute(
                """
                insert into continuation_steps(
                    continuation_id, step_kind, state, board_task_id, parent_step_id,
                    idempotency_key, created_at, updated_at
                ) values(?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (instance_id, step_kind, board_task_id, parent_step_id, idempotency_key, now, now),
            )
            row = con.execute("select * from continuation_steps where id=?", (cur.lastrowid,)).fetchone()
            return {"created": True, "step": dict(row)}

    def mark_step(self, step_id: int, *, state: str | None = None, board_task_id: str | None = None) -> dict[str, Any]:
        self.init()
        now = time.time()
        with self.connect() as con:
            con.execute(
                "update continuation_steps set state=coalesce(?, state), board_task_id=coalesce(nullif(?,''), board_task_id), updated_at=? where id=?",
                (state, board_task_id or "", now, step_id),
            )
            row = con.execute("select * from continuation_steps where id=?", (step_id,)).fetchone()
        return dict(row)

    def count_steps(self, instance_id: int, *, step_kind: str | None = None) -> int:
        self.init()
        with self.connect() as con:
            if step_kind:
                row = con.execute(
                    "select count(*) as n from continuation_steps where continuation_id=? and step_kind=?",
                    (instance_id, step_kind),
                ).fetchone()
            else:
                row = con.execute(
                    "select count(*) as n from continuation_steps where continuation_id=?", (instance_id,)
                ).fetchone()
        return int(row["n"])

    def list_steps(self, instance_id: int) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as con:
            rows = con.execute(
                "select * from continuation_steps where continuation_id=? order by id", (instance_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- owner receipts ----------------------------------------------------

    def add_owner_receipt(
        self,
        instance_id: int,
        *,
        owner_ref: str,
        fields: dict[str, Any],
        source_ref: str = "",
        supersedes_receipt_id: int | None = None,
    ) -> dict[str, Any]:
        self.init()
        now = time.time()
        with self.connect() as con:
            row = con.execute(
                "select coalesce(max(version), 0) as v from owner_input_receipts where continuation_id=?",
                (instance_id,),
            ).fetchone()
            version = int(row["v"]) + 1
            cur = con.execute(
                """
                insert into owner_input_receipts(
                    continuation_id, version, owner_ref, fields_json, source_ref, created_at, supersedes_receipt_id
                ) values(?, ?, ?, ?, ?, ?, ?)
                """,
                (instance_id, version, owner_ref, json.dumps(fields, ensure_ascii=False), source_ref, now, supersedes_receipt_id),
            )
            self._record_event(con, instance_id, "owner_receipt", {"version": version, "owner_ref": owner_ref})
            receipt = self._receipt_row_to_dict(con.execute("select * from owner_input_receipts where id=?", (cur.lastrowid,)).fetchone())
            return receipt

    def list_owner_receipts(self, instance_id: int) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as con:
            rows = con.execute(
                "select * from owner_input_receipts where continuation_id=? order by version", (instance_id,)
            ).fetchall()
        return [self._receipt_row_to_dict(r) for r in rows]

    def latest_owner_receipt(self, instance_id: int) -> dict[str, Any] | None:
        receipts = self.list_owner_receipts(instance_id)
        return receipts[-1] if receipts else None

    @staticmethod
    def _receipt_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["fields"] = json.loads(d.pop("fields_json") or "{}")
        return d

    # -- events --------------------------------------------------------

    def _record_event(self, con: sqlite3.Connection, instance_id: int, kind: str, payload: dict[str, Any]) -> None:
        seq_row = con.execute(
            "select coalesce(max(seq), 0) as s from continuation_events where continuation_id=?", (instance_id,)
        ).fetchone()
        seq = int(seq_row["s"]) + 1
        con.execute(
            "insert into continuation_events(continuation_id, seq, kind, payload_json, created_at) values(?, ?, ?, ?, ?)",
            (instance_id, seq, kind, json.dumps(payload, ensure_ascii=False), time.time()),
        )

    def list_events(self, instance_id: int) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as con:
            rows = con.execute(
                "select * from continuation_events where continuation_id=? order by seq", (instance_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- board cursors ---------------------------------------------------

    def cursor_exists(self, board: str, db_identity: str) -> bool:
        """Whether a cursor row already exists for ``(board, db_identity)``.

        Distinct from ``get_cursor`` returning 0: a board seen for the first
        time has NO row, and the global scan loop seeds it to the current max
        event id so historical events are never replayed."""
        self.init()
        with self.connect() as con:
            row = con.execute(
                "select 1 from board_cursors where board=? and db_identity=?", (board, db_identity)
            ).fetchone()
        return row is not None

    def get_cursor(self, board: str, db_identity: str) -> int:
        self.init()
        with self.connect() as con:
            row = con.execute(
                "select last_event_id from board_cursors where board=? and db_identity=?", (board, db_identity)
            ).fetchone()
        return int(row["last_event_id"]) if row else 0

    def advance_cursor(self, board: str, db_identity: str, last_event_id: int) -> int:
        self.init()
        now = time.time()
        with self.connect() as con:
            current = self.get_cursor(board, db_identity)
            new_value = max(current, int(last_event_id))
            con.execute(
                """
                insert into board_cursors(board, db_identity, last_event_id, updated_at) values(?, ?, ?, ?)
                on conflict(board, db_identity) do update set
                    last_event_id = excluded.last_event_id,
                    updated_at = excluded.updated_at
                where excluded.last_event_id > board_cursors.last_event_id
                """,
                (board, db_identity, new_value, now),
            )
        return new_value

    # -- outbox ----------------------------------------------------------

    def outbox_enqueue(
        self,
        instance_id: int,
        *,
        step_id: str,
        operation: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.init()
        now = time.time()
        with self.connect() as con:
            existing = con.execute(
                "select * from board_outbox where idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                return {"created": False, "outbox": dict(existing)}
            cur = con.execute(
                """
                insert into board_outbox(
                    continuation_id, step_id, operation, payload_json, idempotency_key,
                    state, board_task_id, attempts, created_at, updated_at
                ) values(?, ?, ?, ?, ?, 'pending', '', 0, ?, ?)
                """,
                (instance_id, step_id, operation, json.dumps(payload, ensure_ascii=False), idempotency_key, now, now),
            )
            row = con.execute("select * from board_outbox where id=?", (cur.lastrowid,)).fetchone()
            return {"created": True, "outbox": dict(row)}

    def outbox_mark(self, outbox_id: int, *, state: str, board_task_id: str = "") -> dict[str, Any]:
        self.init()
        now = time.time()
        with self.connect() as con:
            con.execute(
                "update board_outbox set state=?, board_task_id=coalesce(nullif(?,''), board_task_id), attempts=attempts+1, updated_at=? where id=?",
                (state, board_task_id, now, outbox_id),
            )
            row = con.execute("select * from board_outbox where id=?", (outbox_id,)).fetchone()
        return dict(row)

    def list_outbox(self, *, state: str | None = None) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as con:
            if state:
                rows = con.execute("select * from board_outbox where state=? order by id", (state,)).fetchall()
            else:
                rows = con.execute("select * from board_outbox order by id").fetchall()
        return [dict(r) for r in rows]


def _has_active_continuation_state(path: Path) -> bool:
    if not path.exists():
        return False
    con = sqlite3.connect(path)
    try:
        con.row_factory = sqlite3.Row
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'").fetchall()}
        active = False
        if "continuation_instances" in tables:
            terminal = tuple(s.value for s in TERMINAL_STATES)
            placeholders = ",".join("?" for _ in terminal)
            row = con.execute(
                f"select count(*) as n from continuation_instances where state not in ({placeholders})", terminal
            ).fetchone()
            active = active or int(row["n"]) > 0
        if "jobs" in tables:
            row = con.execute(
                "select count(*) as n from jobs where status not in ('succeeded','failed')"
            ).fetchone()
            active = active or int(row["n"]) > 0
        return active
    finally:
        con.close()


def doctor_store_selection(
    *,
    canonical_path: Path | None = None,
    fallback_path: Path | None = None,
    explicit_db: str | None = None,
) -> dict[str, Any]:
    """Select the canonical continuation store, refusing to silently pick one
    when both known paths hold active state and none was explicitly configured."""
    canonical = Path(canonical_path) if canonical_path else default_continuation_db_path()
    fallback = Path(fallback_path) if fallback_path else fallback_continuation_db_path()
    canonical_active = _has_active_continuation_state(canonical)
    fallback_active = _has_active_continuation_state(fallback) if fallback != canonical else False

    if explicit_db:
        return {
            "success": True,
            "selected": str(Path(explicit_db)),
            "split_brain": False,
            "canonical_active": canonical_active,
            "fallback_active": fallback_active,
        }

    if canonical_active and fallback_active:
        return {
            "success": False,
            "error": "split_store_both_active",
            "candidates": [str(canonical), str(fallback)],
        }

    selected = canonical if canonical_active or not fallback_active else fallback
    return {
        "success": True,
        "selected": str(selected),
        "split_brain": False,
        "canonical_active": canonical_active,
        "fallback_active": fallback_active,
    }
