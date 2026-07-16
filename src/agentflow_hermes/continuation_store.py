"""Durable continuation ledger: instances, steps, owner receipts, events,
board cursors, and outbox. One canonical store, selected explicitly rather
than silently split across two default DB paths (see ``doctor_store_selection``).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .migrations import CONTINUATION_LEDGER_TABLES, migrate
from .store import default_db_path as fallback_job_db_path

# Human Effort Resolver / Standing Policy control-plane tables (plan section
# 10). Scoped to ContinuationStore only (not the shared jobs.db migration
# chain in migrations.py, which AgentFlowStore also uses) — applied via
# idempotent DDL rather than bumping the shared SCHEMA_VERSION. Refs, hashes,
# and short sanitized values only; never raw transcripts.
_REQUIREMENT_RESOLVER_DDL = """
create table if not exists requirement_satisfactions (
    id integer primary key autoincrement,
    continuation_id integer not null,
    field_name text not null,
    value_json text not null default 'null',
    source_kind text not null,
    source_ref text not null default '',
    policy_id text not null default '',
    created_at real not null,
    foreign key(continuation_id) references continuation_instances(id)
);

create table if not exists standing_policies (
    id integer primary key autoincrement,
    policy_id text not null,
    version integer not null default 1,
    owner_ref text not null default '',
    project_scope text not null default '',
    action_scope text not null default '',
    conditions_json text not null default '{}',
    decision_json text not null default '{}',
    enabled integer not null default 1,
    source_message_ref text not null default '',
    created_at real not null
);

create unique index if not exists uniq_requirement_satisfaction
    on requirement_satisfactions(continuation_id, field_name);
create index if not exists idx_requirement_satisfactions_continuation
    on requirement_satisfactions(continuation_id);
create unique index if not exists uniq_standing_policy_version
    on standing_policies(policy_id, version);
create index if not exists idx_standing_policies_scope
    on standing_policies(owner_ref, project_scope, action_scope, enabled);
"""

# Interaction Inbox control-plane tables (plan section 6/10): batching,
# question-count/H-classification tracking, and reply provenance. Same
# idempotent-DDL pattern as _REQUIREMENT_RESOLVER_DDL above — scoped to
# ContinuationStore only, never the shared jobs.db migration chain.
# ``interaction_members.requirement_names_json`` stores the full requirement
# dict list (not just bare names) so a case can be reconstructed with kind/
# question/answer_hint intact; the column name follows the plan's schema
# sketch even though the payload is richer than the name alone.
_INTERACTION_INBOX_DDL = """
create table if not exists interaction_cases (
    id text primary key,
    endpoint text not null default '',
    batch_key text not null default '',
    state text not null default 'collecting',
    question_count integer not null default 0,
    created_at real not null,
    asked_at real,
    answered_at real,
    applied_at real
);

create table if not exists interaction_members (
    id integer primary key autoincrement,
    case_id text not null,
    continuation_id integer not null,
    requirement_names_json text not null default '[]',
    created_at real not null,
    foreign key(case_id) references interaction_cases(id)
);

create table if not exists inbound_reply_receipts (
    id integer primary key autoincrement,
    case_id text not null,
    message_ref text not null default '',
    content_sha256 text not null,
    compile_result_json text not null default '{}',
    created_at real not null,
    foreign key(case_id) references interaction_cases(id)
);

create index if not exists idx_interaction_cases_batch on interaction_cases(batch_key, state);
create index if not exists idx_interaction_cases_endpoint on interaction_cases(endpoint, state);
create unique index if not exists uniq_interaction_member on interaction_members(case_id, continuation_id);
create index if not exists idx_interaction_members_continuation on interaction_members(continuation_id);
create index if not exists idx_inbound_reply_receipts_case on inbound_reply_receipts(case_id);
"""

# Canonical control-plane store migration receipts (plan section 10):
# records every ``migrate_legacy_store`` run so an operator/doctor surface
# can see what was migrated and when without re-scanning the legacy DB. Same
# idempotent-DDL pattern as the two blocks above.
_STORE_MIGRATION_DDL = """
create table if not exists store_migration_receipts (
    id integer primary key autoincrement,
    legacy_path text not null,
    migrated_at real not null,
    counts_json text not null default '{}',
    verification_json text not null default '{}'
);

create index if not exists idx_store_migration_receipts_path on store_migration_receipts(legacy_path, migrated_at);
"""

_OUTBOX_RETRY_COLUMNS: dict[str, str] = {
    "next_attempt_at": "REAL NOT NULL DEFAULT 0",
    "last_error": "TEXT NOT NULL DEFAULT ''",
}


def _ensure_outbox_retry_columns(con: sqlite3.Connection) -> None:
    """Add durable retry/backoff metadata to existing continuation stores.

    The shared migration chain is intentionally conservative because it also
    serves the older AgentFlow jobs DB. These columns are scoped to the
    ContinuationStore init path and are additive/idempotent for live recovery.
    """
    existing = {str(row[1]) for row in con.execute("PRAGMA table_info(board_outbox)")}
    for name, ddl in _OUTBOX_RETRY_COLUMNS.items():
        if name not in existing:
            con.execute(f"ALTER TABLE board_outbox ADD COLUMN {name} {ddl}")


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
    return default_continuation_home() / "agentflow" / "agentflow-control-plane.sqlite"


def fallback_continuation_db_path() -> Path:
    return fallback_job_db_path()


def legacy_daemon_db_path() -> Path:
    """The M27 live daemon's DB path (``agentflowd.py run/reconcile --db``):
    a pre-cutover physical location that held canonical continuation state
    before this path became the control-plane canonical. Same shape as the
    canonical store, just a different file — only read via ``continuation
    migrate-store``/``doctor``, never written implicitly."""
    return default_continuation_home() / "agentflow" / "agentflow-daemon.sqlite"


def legacy_pre_control_plane_db_path() -> Path:
    """The old canonical default (``agentflow.sqlite``) from before the
    control-plane cutover. In practice this path is also shared with an
    older, unrelated AgentFlow jobs DB that has a conflicting jobs/job_events
    schema on some hosts; ``migrate_legacy_store`` treats anything without a
    ``continuation_instances`` table as a no-op so that collision is never
    mutated (see plan: preserve old agentflow.sqlite untouched)."""
    return default_continuation_home() / "agentflow" / "agentflow.sqlite"


def legacy_needs_input_db_path() -> Path:
    """The M26 needs-input watchdog's historical default DB (plan 1.7/10):
    ``scripts/agentflow_needs_input_watchdog.py`` reads
    ``AGENTFLOW_NEEDS_INPUT_DB`` or falls back to this fixed path. Same shape
    as the canonical store (a plain ``ContinuationStore``), just a different
    physical file — never touched implicitly, only read when an operator
    runs ``continuation migrate-store`` or ``continuation doctor``."""
    explicit = os.environ.get("AGENTFLOW_NEEDS_INPUT_DB")
    if explicit:
        return Path(explicit)
    return default_continuation_home() / "state" / "agentflow_needs_input_continuations.sqlite"


def default_legacy_continuation_db_paths() -> tuple[Path, ...]:
    """Every known historical continuation-store location (plan 1.7): the
    M27 live daemon DB, the pre-control-plane ``agentflow.sqlite`` default
    (which may collide with an unrelated older jobs DB on some hosts — see
    ``legacy_pre_control_plane_db_path``), the M26 needs-input watchdog DB,
    and the older shared jobs.db fallback path. Deduplicated and never
    including the canonical path itself."""
    canonical = default_continuation_db_path()
    candidates = (
        legacy_daemon_db_path(),
        legacy_pre_control_plane_db_path(),
        legacy_needs_input_db_path(),
        fallback_continuation_db_path(),
    )
    seen: list[Path] = []
    for path in candidates:
        if path != canonical and path not in seen:
            seen.append(path)
    return tuple(seen)


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
            con.executescript(_REQUIREMENT_RESOLVER_DDL)
            con.executescript(_INTERACTION_INBOX_DDL)
            con.executescript(_STORE_MIGRATION_DDL)
            _ensure_outbox_retry_columns(con)

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

    def outbox_mark(
        self,
        outbox_id: int,
        *,
        state: str,
        board_task_id: str = "",
        next_attempt_at: float | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        """Record one durable attempt outcome. Marking a row ``applied``
        atomically clears the retry schedule (``next_attempt_at=0``) and the
        stale ``last_error`` from the attempts that preceded convergence, so a
        converged row never reads back as a still-failing one (M30I)."""
        self.init()
        now = time.time()
        applied = state == "applied"
        with self.connect() as con:
            con.execute(
                """
                update board_outbox set
                    state=?,
                    board_task_id=coalesce(nullif(?,''), board_task_id),
                    attempts=attempts+1,
                    next_attempt_at=case when ? then 0 else coalesce(?, next_attempt_at) end,
                    last_error=case when ? then '' else coalesce(?, last_error) end,
                    updated_at=?
                where id=?
                """,
                (state, board_task_id, applied, next_attempt_at, applied, last_error, now, outbox_id),
            )
            row = con.execute("select * from board_outbox where id=?", (outbox_id,)).fetchone()
        return dict(row)

    def outbox_reinterpret_pending(
        self,
        instance_id: int,
        *,
        from_operation: str,
        key_prefix: str,
        to_operation: str,
        new_idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Deterministically reinterpret a stale not-yet-applied legacy outbox
        row (e.g. an old ``operation='subscribe'`` semantic-refusal notify
        attempt) onto a new operation/idempotency key in place. Preserves the
        row id, attempts, and created_at as durable evidence; never touches an
        already-``applied`` row (left untouched as historical evidence of its
        original semantics) and never deletes rows or edits cursors."""
        self.init()
        with self.connect() as con:
            existing_new = con.execute(
                "select * from board_outbox where idempotency_key=?", (new_idempotency_key,)
            ).fetchone()
            if existing_new is not None:
                return dict(existing_new)
            row = con.execute(
                """
                select * from board_outbox
                where continuation_id=? and operation=? and idempotency_key like ? and state!='applied'
                order by id desc limit 1
                """,
                (instance_id, from_operation, f"{key_prefix}%"),
            ).fetchone()
            if row is None:
                return None
            con.execute(
                "update board_outbox set operation=?, idempotency_key=?, updated_at=? where id=?",
                (to_operation, new_idempotency_key, time.time(), row["id"]),
            )
            updated = con.execute("select * from board_outbox where id=?", (row["id"],)).fetchone()
            return dict(updated)

    def outbox_pending_retry_at(self, instance_id: int, *, now: float | None = None) -> float | None:
        """Earliest ``next_attempt_at`` when this instance has pending outbox
        rows and *none* of them are due yet; ``None`` when nothing is pending or
        at least one row is due.

        Callers use this to skip an entire materialize attempt while the durable
        backoff is still running, so repeated event-loop scans cost zero state
        transitions and durable growth stays O(due attempts) rather than
        O(scans) (M30I).
        """
        self.init()
        at = time.time() if now is None else now
        with self.connect() as con:
            row = con.execute(
                """
                select min(coalesce(next_attempt_at, 0)) as earliest
                from board_outbox where continuation_id=? and state='pending'
                """,
                (instance_id,),
            ).fetchone()
        earliest = None if row is None else row["earliest"]
        if earliest is None or float(earliest) <= at:
            return None
        return float(earliest)

    def list_outbox(self, *, state: str | None = None) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as con:
            if state:
                rows = con.execute("select * from board_outbox where state=? order by id", (state,)).fetchall()
            else:
                rows = con.execute("select * from board_outbox order by id").fetchall()
        return [dict(r) for r in rows]

    def list_due_outbox(self, *, state: str = "pending", now: float | None = None) -> list[dict[str, Any]]:
        self.init()
        due_at = time.time() if now is None else now
        with self.connect() as con:
            rows = con.execute(
                """
                select * from board_outbox
                where state=? and coalesce(next_attempt_at, 0) <= ?
                order by id
                """,
                (state, due_at),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- requirement satisfactions (requirement_resolver.py) --------------

    def record_requirement_satisfaction(
        self,
        instance_id: int,
        *,
        field_name: str,
        value: Any,
        source_kind: str,
        source_ref: str = "",
        policy_id: str = "",
    ) -> dict[str, Any]:
        self.init()
        now = time.time()
        with self.connect() as con:
            con.execute(
                """
                insert into requirement_satisfactions(
                    continuation_id, field_name, value_json, source_kind, source_ref, policy_id, created_at
                ) values(?, ?, ?, ?, ?, ?, ?)
                on conflict(continuation_id, field_name) do update set
                    value_json=excluded.value_json,
                    source_kind=excluded.source_kind,
                    source_ref=excluded.source_ref,
                    policy_id=excluded.policy_id,
                    created_at=excluded.created_at
                """,
                (instance_id, field_name, json.dumps(value, ensure_ascii=False), source_kind, source_ref, policy_id, now),
            )
            row = con.execute(
                "select * from requirement_satisfactions where continuation_id=? and field_name=?",
                (instance_id, field_name),
            ).fetchone()
        return self._satisfaction_row_to_dict(row)

    def list_requirement_satisfactions(self, instance_id: int) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as con:
            rows = con.execute(
                "select * from requirement_satisfactions where continuation_id=? order by id", (instance_id,)
            ).fetchall()
        return [self._satisfaction_row_to_dict(r) for r in rows]

    @staticmethod
    def _satisfaction_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["value"] = json.loads(d.pop("value_json") or "null")
        return d

    # -- standing policies (standing_policy.py) ----------------------------

    def create_standing_policy(
        self,
        *,
        policy_id: str,
        owner_ref: str,
        project_scope: str,
        action_scope: str,
        conditions: dict[str, Any],
        decision: dict[str, Any],
        source_message_ref: str = "",
        enabled: bool = True,
    ) -> dict[str, Any]:
        self.init()
        now = time.time()
        with self.connect() as con:
            row = con.execute(
                "select coalesce(max(version), 0) as v from standing_policies where policy_id=?", (policy_id,)
            ).fetchone()
            version = int(row["v"]) + 1
            cur = con.execute(
                """
                insert into standing_policies(
                    policy_id, version, owner_ref, project_scope, action_scope,
                    conditions_json, decision_json, enabled, source_message_ref, created_at
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_id, version, owner_ref, project_scope, action_scope,
                    json.dumps(conditions, ensure_ascii=False), json.dumps(decision, ensure_ascii=False),
                    1 if enabled else 0, source_message_ref, now,
                ),
            )
            row = con.execute("select * from standing_policies where id=?", (cur.lastrowid,)).fetchone()
        return self._policy_row_to_dict(row)

    def list_standing_policies(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as con:
            if enabled_only:
                rows = con.execute("select * from standing_policies where enabled=1 order by id").fetchall()
            else:
                rows = con.execute("select * from standing_policies order by id").fetchall()
        return [self._policy_row_to_dict(r) for r in rows]

    def latest_standing_policy(self, policy_id: str) -> dict[str, Any] | None:
        self.init()
        with self.connect() as con:
            row = con.execute(
                "select * from standing_policies where policy_id=? order by version desc limit 1", (policy_id,)
            ).fetchone()
        return self._policy_row_to_dict(row) if row else None

    @staticmethod
    def _policy_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["conditions"] = json.loads(d.pop("conditions_json") or "{}")
        d["decision"] = json.loads(d.pop("decision_json") or "{}")
        d["enabled"] = bool(d["enabled"])
        return d

    # -- interaction inbox (interaction.py) --------------------------------

    def create_interaction_case(
        self,
        *,
        id: str,
        endpoint: str,
        batch_key: str,
        state: str = "collecting",
        question_count: int = 0,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        self.init()
        now = created_at if created_at is not None else time.time()
        with self.connect() as con:
            con.execute(
                """
                insert into interaction_cases(
                    id, endpoint, batch_key, state, question_count, created_at
                ) values(?, ?, ?, ?, ?, ?)
                """,
                (id, endpoint, batch_key, state, question_count, now),
            )
            row = con.execute("select * from interaction_cases where id=?", (id,)).fetchone()
        return dict(row)

    def get_interaction_case(self, case_id: str) -> dict[str, Any] | None:
        self.init()
        with self.connect() as con:
            row = con.execute("select * from interaction_cases where id=?", (case_id,)).fetchone()
        return dict(row) if row else None

    def list_interaction_cases(self, *, state: str | None = None, endpoint: str | None = None) -> list[dict[str, Any]]:
        self.init()
        query = "select * from interaction_cases where 1=1"
        params: list[Any] = []
        if state:
            query += " and state=?"
            params.append(state)
        if endpoint:
            query += " and endpoint=?"
            params.append(endpoint)
        query += " order by created_at"
        with self.connect() as con:
            rows = con.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def update_interaction_case(
        self,
        case_id: str,
        *,
        state: str | None = None,
        question_count: int | None = None,
        asked_at: float | None = None,
        answered_at: float | None = None,
        applied_at: float | None = None,
    ) -> dict[str, Any]:
        self.init()
        with self.connect() as con:
            con.execute(
                """
                update interaction_cases set
                    state = coalesce(?, state),
                    question_count = coalesce(?, question_count),
                    asked_at = coalesce(?, asked_at),
                    answered_at = coalesce(?, answered_at),
                    applied_at = coalesce(?, applied_at)
                where id=?
                """,
                (state, question_count, asked_at, answered_at, applied_at, case_id),
            )
            row = con.execute("select * from interaction_cases where id=?", (case_id,)).fetchone()
        return dict(row) if row else {}

    def add_interaction_member(
        self, case_id: str, *, continuation_id: int, requirements: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.init()
        now = time.time()
        with self.connect() as con:
            con.execute(
                """
                insert into interaction_members(case_id, continuation_id, requirement_names_json, created_at)
                values(?, ?, ?, ?)
                on conflict(case_id, continuation_id) do update set
                    requirement_names_json = excluded.requirement_names_json
                """,
                (case_id, continuation_id, json.dumps(requirements, ensure_ascii=False), now),
            )
            row = con.execute(
                "select * from interaction_members where case_id=? and continuation_id=?",
                (case_id, continuation_id),
            ).fetchone()
        return self._member_row_to_dict(row)

    def list_interaction_members(self, case_id: str) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as con:
            rows = con.execute(
                "select * from interaction_members where case_id=? order by id", (case_id,)
            ).fetchall()
        return [self._member_row_to_dict(r) for r in rows]

    @staticmethod
    def _member_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["requirements"] = json.loads(d.pop("requirement_names_json") or "[]")
        return d

    def record_inbound_reply_receipt(
        self, case_id: str, *, message_ref: str, content_sha256: str, compile_result: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist reply provenance only: a content hash and message ref, plus
        the already-validated typed compile result. Raw user text is never
        passed to or stored by this method (plan 6/7: raw text is not the
        receipt)."""
        self.init()
        now = time.time()
        with self.connect() as con:
            cur = con.execute(
                """
                insert into inbound_reply_receipts(
                    case_id, message_ref, content_sha256, compile_result_json, created_at
                ) values(?, ?, ?, ?, ?)
                """,
                (case_id, message_ref, content_sha256, json.dumps(compile_result, ensure_ascii=False), now),
            )
            row = con.execute("select * from inbound_reply_receipts where id=?", (cur.lastrowid,)).fetchone()
        return self._reply_receipt_row_to_dict(row)

    def list_inbound_reply_receipts(self, case_id: str) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as con:
            rows = con.execute(
                "select * from inbound_reply_receipts where case_id=? order by id", (case_id,)
            ).fetchall()
        return [self._reply_receipt_row_to_dict(r) for r in rows]

    @staticmethod
    def _reply_receipt_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["compile_result"] = json.loads(d.pop("compile_result_json") or "{}")
        return d

    # -- store migration receipts (plan section 10) -------------------------

    def list_migration_receipts(self) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as con:
            rows = con.execute("select * from store_migration_receipts order by id").fetchall()
        return [self._migration_receipt_row_to_dict(r) for r in rows]

    @staticmethod
    def _migration_receipt_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["counts"] = json.loads(d.pop("counts_json") or "{}")
        d["verification"] = json.loads(d.pop("verification_json") or "{}")
        return d


@contextlib.contextmanager
def isolated_preview_store(source_path: Path | str) -> Iterator[ContinuationStore]:
    """Yield a ``ContinuationStore`` backed by an ephemeral throwaway copy of
    ``source_path`` for strictly side-effect-free dry-runs (plan M27 blocker 1).

    ``apply=false`` cadences must never mutate the durable/legacy continuation
    ledger, yet must still see its prior state so preview output is realistic
    (a new live event past an existing cursor still previews as OWNER-INPUT).
    This copies the source ledger's *committed* content — via sqlite's backup
    API, so WAL-pending rows are included — into a temp DB, hands back a store
    pointed at the copy, and deletes the copy on exit. The durable
    ``source_path`` is opened read-only (backup source) and never written; when
    it does not exist yet, a fresh empty temp DB is used and the durable file is
    never created."""
    source_path = Path(source_path)
    tmpdir = Path(tempfile.mkdtemp(prefix="agentflow-preview-"))
    try:
        preview_path = tmpdir / "preview.sqlite"
        if source_path.exists():
            src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
            try:
                dst = sqlite3.connect(preview_path)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        yield ContinuationStore(preview_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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


def legacy_residue_report(*, paths: tuple[Path, ...] | None = None) -> list[dict[str, Any]]:
    """Report stale legacy continuation-store residue without requiring
    manual cleanup (plan section 10 item 6): for every known legacy path
    that still exists and looks like a ``ContinuationStore``-shaped DB,
    count total and still-active (non-terminal) ``continuation_instances``
    rows. Read-only — never mutates the legacy DB."""
    report: list[dict[str, Any]] = []
    for path in (paths if paths is not None else default_legacy_continuation_db_paths()):
        if not path.exists():
            continue
        con = sqlite3.connect(path)
        try:
            con.row_factory = sqlite3.Row
            tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'").fetchall()}
            if "continuation_instances" not in tables:
                continue
            terminal = tuple(s.value for s in TERMINAL_STATES)
            placeholders = ",".join("?" for _ in terminal)
            total = int(con.execute("select count(*) as n from continuation_instances").fetchone()["n"])
            active = int(
                con.execute(
                    f"select count(*) as n from continuation_instances where state not in ({placeholders})", terminal
                ).fetchone()["n"]
            )
            report.append({"path": str(path), "total_rows": total, "active_rows": active})
        finally:
            con.close()
    return report


def _verify_migration(canonical: "ContinuationStore", *, legacy_path: Path, id_map: dict[int, int]) -> dict[str, Any]:
    """Post-copy verification (plan section 10 item 3): every legacy
    ``continuation_instances`` row must have a canonical mapping, and the
    canonical store's own unique source tuple must never hold duplicates."""
    legacy_con = sqlite3.connect(legacy_path)
    legacy_con.row_factory = sqlite3.Row
    try:
        legacy_instance_ids = [r["id"] for r in legacy_con.execute("select id from continuation_instances").fetchall()]
    finally:
        legacy_con.close()
    missing = [lid for lid in legacy_instance_ids if lid not in id_map]
    with canonical.connect() as con:
        dup_rows = con.execute(
            """
            select board, source_task_id, source_event_id, contract_ref, count(*) as n
            from continuation_instances
            group by board, source_task_id, source_event_id, contract_ref
            having n > 1
            """
        ).fetchall()
    return {
        "ok": not missing and not dup_rows,
        "legacy_instances": len(legacy_instance_ids),
        "mapped_instances": len(id_map),
        "missing_instance_ids": missing,
        "duplicate_source_tuples": len(dup_rows),
    }


def _record_migration_receipt(
    canonical: "ContinuationStore", *, legacy_path: Path, counts: dict[str, int], verification: dict[str, Any]
) -> dict[str, Any]:
    now = time.time()
    with canonical.connect() as con:
        cur = con.execute(
            "insert into store_migration_receipts(legacy_path, migrated_at, counts_json, verification_json) values(?,?,?,?)",
            (str(legacy_path), now, json.dumps(counts, ensure_ascii=False), json.dumps(verification, ensure_ascii=False)),
        )
        row = con.execute("select * from store_migration_receipts where id=?", (cur.lastrowid,)).fetchone()
    return ContinuationStore._migration_receipt_row_to_dict(row)


def migrate_legacy_store(*, canonical: "ContinuationStore", legacy_path: Path) -> dict[str, Any]:
    """Copy instances/steps/receipts/events/cursors/outbox from one legacy
    ``ContinuationStore``-shaped DB into ``canonical`` (plan section 10,
    commit 8). Idempotent: every write is guarded by the same unique
    constraints/dedupe keys the live store already relies on (source tuple
    for instances, ``idempotency_key``/``(continuation_id, seq|version)`` for
    steps/events/receipts/outbox, ``(board, db_identity)`` upsert-max for
    cursors), so running this twice against the same legacy DB never
    duplicates a row. Writes exactly one migration receipt row per run."""
    legacy_path = Path(legacy_path)
    if not legacy_path.exists():
        return {"success": True, "migrated": False, "reason": "legacy_path_missing", "legacy_path": str(legacy_path)}
    canonical.init()
    if legacy_path.resolve() == canonical.path.resolve():
        return {"success": True, "migrated": False, "reason": "legacy_path_is_canonical", "legacy_path": str(legacy_path)}

    legacy_con = sqlite3.connect(legacy_path)
    legacy_con.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in legacy_con.execute("select name from sqlite_master where type='table'").fetchall()}
        if "continuation_instances" not in tables:
            return {"success": True, "migrated": False, "reason": "no_continuation_tables", "legacy_path": str(legacy_path)}

        id_map: dict[int, int] = {}
        counts = {
            "instances": 0,
            "steps": 0,
            "receipts": 0,
            "events": 0,
            "cursors": 0,
            "outbox": 0,
            "satisfactions": 0,
            "standing_policies": 0,
            "interaction_cases": 0,
            "interaction_members": 0,
            "inbound_reply_receipts": 0,
            "idempotency_keys": 0,
        }

        with canonical.connect() as con:
            for row in legacy_con.execute("select * from continuation_instances order by id"):
                existing = con.execute(
                    "select id from continuation_instances where board=? and source_task_id=? and source_event_id=? and contract_ref=?",
                    (row["board"], row["source_task_id"], row["source_event_id"], row["contract_ref"]),
                ).fetchone()
                if existing is not None:
                    id_map[row["id"]] = existing["id"]
                    continue
                cur = con.execute(
                    """
                    insert into continuation_instances(
                        board, source_task_id, source_event_id, source_graph_id, contract_ref, verdict,
                        continuation_kind, state, origin_ref, return_to_ref, workspace_ref, idempotency_key,
                        created_at, updated_at
                    ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        row["board"], row["source_task_id"], row["source_event_id"], row["source_graph_id"],
                        row["contract_ref"], row["verdict"], row["continuation_kind"], row["state"],
                        row["origin_ref"], row["return_to_ref"], row["workspace_ref"], row["idempotency_key"],
                        row["created_at"], row["updated_at"],
                    ),
                )
                id_map[row["id"]] = cur.lastrowid
                counts["instances"] += 1

            if "continuation_steps" in tables:
                for row in legacy_con.execute("select * from continuation_steps order by id"):
                    new_cid = id_map.get(row["continuation_id"])
                    if new_cid is None:
                        continue
                    cur = con.execute(
                        """
                        insert or ignore into continuation_steps(
                            continuation_id, step_kind, state, board_task_id, parent_step_id, idempotency_key,
                            created_at, updated_at
                        ) values(?,?,?,?,?,?,?,?)
                        """,
                        (new_cid, row["step_kind"], row["state"], row["board_task_id"], row["parent_step_id"],
                         row["idempotency_key"], row["created_at"], row["updated_at"]),
                    )
                    counts["steps"] += max(cur.rowcount, 0)

            if "owner_input_receipts" in tables:
                for row in legacy_con.execute("select * from owner_input_receipts order by id"):
                    new_cid = id_map.get(row["continuation_id"])
                    if new_cid is None:
                        continue
                    cur = con.execute(
                        """
                        insert or ignore into owner_input_receipts(
                            continuation_id, version, owner_ref, fields_json, source_ref, created_at, supersedes_receipt_id
                        ) values(?,?,?,?,?,?,?)
                        """,
                        (new_cid, row["version"], row["owner_ref"], row["fields_json"], row["source_ref"],
                         row["created_at"], row["supersedes_receipt_id"]),
                    )
                    counts["receipts"] += max(cur.rowcount, 0)

            if "continuation_events" in tables:
                for row in legacy_con.execute("select * from continuation_events order by id"):
                    new_cid = id_map.get(row["continuation_id"])
                    if new_cid is None:
                        continue
                    cur = con.execute(
                        """
                        insert or ignore into continuation_events(
                            continuation_id, seq, kind, payload_json, created_at
                        ) values(?,?,?,?,?)
                        """,
                        (new_cid, row["seq"], row["kind"], row["payload_json"], row["created_at"]),
                    )
                    counts["events"] += max(cur.rowcount, 0)

            if "board_cursors" in tables:
                for row in legacy_con.execute("select * from board_cursors"):
                    existing = con.execute(
                        "select last_event_id from board_cursors where board=? and db_identity=?",
                        (row["board"], row["db_identity"]),
                    ).fetchone()
                    new_value = max(int(existing["last_event_id"]) if existing else 0, int(row["last_event_id"]))
                    con.execute(
                        """
                        insert into board_cursors(board, db_identity, last_event_id, updated_at) values(?,?,?,?)
                        on conflict(board, db_identity) do update set
                            last_event_id = excluded.last_event_id,
                            updated_at = excluded.updated_at
                        where excluded.last_event_id > board_cursors.last_event_id
                        """,
                        (row["board"], row["db_identity"], new_value, row["updated_at"]),
                    )
                    counts["cursors"] += 1

            if "board_outbox" in tables:
                for row in legacy_con.execute("select * from board_outbox order by id"):
                    new_cid = id_map.get(row["continuation_id"])
                    if new_cid is None:
                        continue
                    cur = con.execute(
                        """
                        insert or ignore into board_outbox(
                            continuation_id, step_id, operation, payload_json, idempotency_key, state, board_task_id,
                            attempts, created_at, updated_at
                        ) values(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (new_cid, row["step_id"], row["operation"], row["payload_json"], row["idempotency_key"],
                         row["state"], row["board_task_id"], row["attempts"], row["created_at"], row["updated_at"]),
                    )
                    counts["outbox"] += max(cur.rowcount, 0)

            if "requirement_satisfactions" in tables:
                for row in legacy_con.execute("select * from requirement_satisfactions order by id"):
                    new_cid = id_map.get(row["continuation_id"])
                    if new_cid is None:
                        continue
                    cur = con.execute(
                        """
                        insert or ignore into requirement_satisfactions(
                            continuation_id, field_name, value_json, source_kind, source_ref, policy_id, created_at
                        ) values(?,?,?,?,?,?,?)
                        """,
                        (new_cid, row["field_name"], row["value_json"], row["source_kind"], row["source_ref"],
                         row["policy_id"], row["created_at"]),
                    )
                    counts["satisfactions"] += max(cur.rowcount, 0)

            if "standing_policies" in tables:
                for row in legacy_con.execute("select * from standing_policies order by id"):
                    cur = con.execute(
                        """
                        insert or ignore into standing_policies(
                            policy_id, version, owner_ref, project_scope, action_scope,
                            conditions_json, decision_json, enabled, source_message_ref, created_at
                        ) values(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (row["policy_id"], row["version"], row["owner_ref"], row["project_scope"], row["action_scope"],
                         row["conditions_json"], row["decision_json"], row["enabled"], row["source_message_ref"], row["created_at"]),
                    )
                    counts["standing_policies"] += max(cur.rowcount, 0)

            if "interaction_cases" in tables:
                for row in legacy_con.execute("select * from interaction_cases order by created_at, id"):
                    cur = con.execute(
                        """
                        insert or ignore into interaction_cases(
                            id, endpoint, batch_key, state, question_count, created_at, asked_at, answered_at, applied_at
                        ) values(?,?,?,?,?,?,?,?,?)
                        """,
                        (row["id"], row["endpoint"], row["batch_key"], row["state"], row["question_count"],
                         row["created_at"], row["asked_at"], row["answered_at"], row["applied_at"]),
                    )
                    counts["interaction_cases"] += max(cur.rowcount, 0)

            if "interaction_members" in tables:
                for row in legacy_con.execute("select * from interaction_members order by id"):
                    new_cid = id_map.get(row["continuation_id"])
                    if new_cid is None:
                        continue
                    cur = con.execute(
                        """
                        insert or ignore into interaction_members(
                            case_id, continuation_id, requirement_names_json, created_at
                        ) values(?,?,?,?)
                        """,
                        (row["case_id"], new_cid, row["requirement_names_json"], row["created_at"]),
                    )
                    counts["interaction_members"] += max(cur.rowcount, 0)

            if "inbound_reply_receipts" in tables:
                for row in legacy_con.execute("select * from inbound_reply_receipts order by id"):
                    existing = con.execute(
                        """
                        select id from inbound_reply_receipts
                        where case_id=? and message_ref=? and content_sha256=?
                        """,
                        (row["case_id"], row["message_ref"], row["content_sha256"]),
                    ).fetchone()
                    if existing is not None:
                        continue
                    cur = con.execute(
                        """
                        insert into inbound_reply_receipts(
                            case_id, message_ref, content_sha256, compile_result_json, created_at
                        ) values(?,?,?,?,?)
                        """,
                        (row["case_id"], row["message_ref"], row["content_sha256"], row["compile_result_json"], row["created_at"]),
                    )
                    counts["inbound_reply_receipts"] += max(cur.rowcount, 0)

            if "idempotency_keys" in tables:
                for row in legacy_con.execute("select * from idempotency_keys order by key"):
                    cur = con.execute(
                        """
                        insert or ignore into idempotency_keys(
                            key, job_id, channel, target, delivery_ref, created_at
                        ) values(?,?,?,?,?,?)
                        """,
                        (row["key"], row["job_id"], row["channel"], row["target"], row["delivery_ref"], row["created_at"]),
                    )
                    counts["idempotency_keys"] += max(cur.rowcount, 0)
    finally:
        legacy_con.close()

    verification = _verify_migration(canonical, legacy_path=legacy_path, id_map=id_map)
    receipt = _record_migration_receipt(canonical, legacy_path=legacy_path, counts=counts, verification=verification)
    return {
        "success": verification["ok"],
        "migrated": True,
        "legacy_path": str(legacy_path),
        "counts": counts,
        "verification": verification,
        "receipt": receipt,
    }


def migrate_all_legacy_stores(
    *, canonical: "ContinuationStore | None" = None, legacy_paths: tuple[Path, ...] | None = None
) -> dict[str, Any]:
    """Run ``migrate_legacy_store`` against every known legacy path (plan
    section 10) and return one combined report."""
    store = canonical or ContinuationStore.canonical()
    paths = legacy_paths if legacy_paths is not None else default_legacy_continuation_db_paths()
    results = [migrate_legacy_store(canonical=store, legacy_path=path) for path in paths]
    return {
        "success": all(r["success"] for r in results),
        "canonical_db": str(store.path),
        "results": results,
    }
