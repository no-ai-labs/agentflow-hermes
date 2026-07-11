"""Board-aware event model and registry.

Cursor identity is always ``(board, db_identity)`` — never a global counter —
so overlapping event ids/sequence numbers across two different boards are
valid and independently tracked (see ``ContinuationStore.advance_cursor``).

``LiveBoardEventSource`` is the production, read-only event source: it reads a
real per-board Hermes Kanban sqlite DB (``task_events`` joined to ``tasks`` and
``task_runs``) and yields terminal-run ``BoardEvent`` records. It is the same
shape as ``FakeBoardEventSource`` so the engine never branches on live-vs-fake.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .roadmap_config import parse_minimal_yaml


@dataclass(frozen=True)
class BoardEvent:
    event_id: str
    event_seq: int
    source_task_id: str
    source_graph_id: str
    summary: str = ""
    run_metadata: dict[str, Any] | None = None
    origin_ref: str = ""
    return_to_ref: str = ""
    workspace_ref: str = ""
    assignee: str = ""
    occurred_at: float = 0.0


class BoardEventSource(Protocol):
    def db_identity(self) -> str: ...

    def fetch_events_since(self, last_seq: int) -> list[BoardEvent]: ...

    def current_max_seq(self) -> int: ...


class FakeBoardEventSource:
    """In-memory, controlled/synthetic event source for tests and canaries."""

    def __init__(self, *, db_identity: str, events: list[BoardEvent] | None = None) -> None:
        self._db_identity = db_identity
        self.events: list[BoardEvent] = list(events or [])

    def db_identity(self) -> str:
        return self._db_identity

    def fetch_events_since(self, last_seq: int) -> list[BoardEvent]:
        return sorted((e for e in self.events if e.event_seq > last_seq), key=lambda e: e.event_seq)

    def current_max_seq(self) -> int:
        return max((e.event_seq for e in self.events), default=0)


class LiveBoardEventSource:
    """Read-only production event source over a real per-board Kanban sqlite DB.

    Reads ``task_events`` (terminal run boundaries) joined to ``tasks`` and
    ``task_runs`` so the engine sees structured run metadata (``agentflow_outcome``)
    when present and the run summary as compatibility fallback. Never writes.

    Return-endpoint resolution is generic: a source task's own typed notify/ACK
    endpoint (``kanban_notify_subs``) wins; otherwise the board's declared
    ``default_endpoint`` from the registry is used. No per-board branch.
    """

    _TERMINAL_KINDS = ("completed", "blocked", "failed", "crashed", "timed_out")

    def __init__(
        self,
        *,
        board: str,
        db_path: str | Path,
        db_identity: str = "",
        default_endpoint: str = "",
        limit: int = 200,
    ) -> None:
        self.board = board
        self.db_path = Path(db_path)
        self._db_identity = db_identity or board
        self.default_endpoint = default_endpoint
        self.limit = max(1, int(limit))

    def db_identity(self) -> str:
        return self._db_identity

    def _connect(self) -> sqlite3.Connection | None:
        if not self.db_path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def current_max_seq(self) -> int:
        conn = self._connect()
        if conn is None:
            return 0
        try:
            row = conn.execute("select coalesce(max(id), 0) as m from task_events").fetchone()
            return int(row["m"] or 0)
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    def fetch_events_since(self, last_seq: int) -> list[BoardEvent]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            placeholders = ",".join("?" for _ in self._TERMINAL_KINDS)
            query = f"""
                select
                    e.id as event_id,
                    e.task_id as task_id,
                    e.run_id as run_id,
                    e.kind as event_kind,
                    e.payload as event_payload,
                    e.created_at as created_at,
                    t.assignee as assignee,
                    t.workspace_path as workspace_path,
                    t.workflow_template_id as workflow_template_id,
                    r.step_key as step_key,
                    r.summary as run_summary,
                    r.metadata as run_metadata
                from task_events e
                join tasks t on t.id = e.task_id
                left join task_runs r on r.id = e.run_id
                where e.id > ? and e.kind in ({placeholders})
                order by e.id asc
                limit ?
            """
            rows = conn.execute(query, (int(last_seq), *self._TERMINAL_KINDS, self.limit)).fetchall()
            return [self._row_to_event(conn, row) for row in rows]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def _row_to_event(self, conn: sqlite3.Connection, row: sqlite3.Row) -> BoardEvent:
        task_id = str(row["task_id"] or "")
        run_metadata = _parse_json_dict(row["run_metadata"] if "run_metadata" in row.keys() else "")
        summary = str(row["run_summary"] or "") or _payload_summary(row["event_payload"])
        endpoint = self._resolve_endpoint(conn, task_id)
        graph_id = (
            str(row["step_key"] or "")
            or str(row["workflow_template_id"] or "")
            or f"graph:{task_id}"
        )
        return BoardEvent(
            event_id=f"kanban-event-{int(row['event_id'])}",
            event_seq=int(row["event_id"]),
            source_task_id=task_id,
            source_graph_id=graph_id or f"graph:{task_id}",
            summary=summary,
            run_metadata=run_metadata,
            origin_ref=endpoint,
            return_to_ref=endpoint,
            workspace_ref=str(row["workspace_path"] or ""),
            assignee=str(row["assignee"] or ""),
            occurred_at=float(row["created_at"] or 0.0),
        )

    def _resolve_endpoint(self, conn: sqlite3.Connection, task_id: str) -> str:
        """Prefer the task's own typed notify endpoint; fall back to the board's
        declared default endpoint. Purely declarative — no per-board branch."""
        typed = _typed_notify_endpoint(conn, task_id)
        return typed or self.default_endpoint


def _parse_json_dict(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _payload_summary(raw: Any) -> str:
    parsed = _parse_json_dict(raw)
    if not parsed:
        return ""
    for key in ("summary", "verdict", "message"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _typed_notify_endpoint(conn: sqlite3.Connection, task_id: str) -> str:
    if not task_id:
        return ""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(kanban_notify_subs)")}
        if not cols:
            return ""
        row = conn.execute(
            "select platform, chat_id, thread_id from kanban_notify_subs where task_id=? order by created_at desc limit 1",
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    if not row:
        return ""
    platform = str(row["platform"] or "")
    chat_id = str(row["chat_id"] or "")
    thread_id = str(row["thread_id"] or "") if "thread_id" in row.keys() else ""
    if not platform or not chat_id:
        return ""
    endpoint = f"{platform}:{chat_id}"
    if thread_id:
        endpoint = f"{endpoint}:{thread_id}"
    return endpoint


@dataclass(frozen=True)
class BoardRegistryEntry:
    board: str
    db_identity: str
    outcome_handlers: tuple[str, ...] = ()
    enabled: bool = True
    default_endpoint: str = ""
    db_path: str = ""


def load_board_registry(path: str | Path) -> dict[str, BoardRegistryEntry]:
    text = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        payload = parse_minimal_yaml(text)
    boards = payload.get("boards") if isinstance(payload, dict) else None
    if not isinstance(boards, dict):
        return {}
    registry: dict[str, BoardRegistryEntry] = {}
    for board, spec in boards.items():
        if not isinstance(spec, dict):
            continue
        db = str(spec.get("db_identity") or spec.get("db") or "")
        handlers = spec.get("outcome_handlers")
        enabled = spec.get("enabled")
        registry[str(board)] = BoardRegistryEntry(
            board=str(board),
            db_identity=db or str(board),
            outcome_handlers=tuple(str(h) for h in handlers) if isinstance(handlers, (list, tuple)) else (),
            enabled=enabled if isinstance(enabled, bool) else True,
            default_endpoint=str(spec.get("default_endpoint") or ""),
            db_path=str(spec.get("db_path") or ""),
        )
    return registry
