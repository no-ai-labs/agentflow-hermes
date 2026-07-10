"""Board-aware event model and registry.

Cursor identity is always ``(board, db_identity)`` — never a global counter —
so overlapping event ids/sequence numbers across two different boards are
valid and independently tracked (see ``ContinuationStore.advance_cursor``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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


class FakeBoardEventSource:
    """In-memory, controlled/synthetic event source for tests and canaries."""

    def __init__(self, *, db_identity: str, events: list[BoardEvent] | None = None) -> None:
        self._db_identity = db_identity
        self.events: list[BoardEvent] = list(events or [])

    def db_identity(self) -> str:
        return self._db_identity

    def fetch_events_since(self, last_seq: int) -> list[BoardEvent]:
        return sorted((e for e in self.events if e.event_seq > last_seq), key=lambda e: e.event_seq)


@dataclass(frozen=True)
class BoardRegistryEntry:
    board: str
    db_identity: str
    outcome_handlers: tuple[str, ...] = ()


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
        db = str(spec.get("db") or "")
        handlers = spec.get("outcome_handlers")
        registry[str(board)] = BoardRegistryEntry(
            board=str(board),
            db_identity=db or str(board),
            outcome_handlers=tuple(str(h) for h in handlers) if isinstance(handlers, (list, tuple)) else (),
        )
    return registry
