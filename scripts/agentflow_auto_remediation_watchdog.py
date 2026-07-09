#!/usr/bin/env python3
"""Board-aware AgentFlow -> Hermes Kanban auto-remediation watchdog.

M24B canary scope: oracle-lab only, board-scoped event state, no historical
replay on activation. The planner dependency lives in the legacy AgentFlow repo;
this wrapper is the narrow board/cron boundary and does not enable Discord live
send.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

STATE = Path(os.environ.get("AGENTFLOW_AUTO_REMEDIATION_STATE", "/home/duckran/.hermes/state/agentflow_auto_remediation_watchdog.json"))
BOARD = os.environ.get("AGENTFLOW_AUTO_REMEDIATION_BOARD", "oracle-lab")
KANBAN_DB = os.environ.get("AGENTFLOW_AUTO_REMEDIATION_KANBAN_DB", f"/home/duckran/.hermes/kanban/boards/{BOARD}/kanban.db")
AGENTFLOW_DIR = os.environ.get("AGENTFLOW_AUTO_REMEDIATION_AGENTFLOW_DIR", "/home/duckran/dev/agentflow")
ADAPTER = os.environ.get("AGENTFLOW_AUTO_REMEDIATION_ADAPTER", "/home/duckran/.hermes/scripts/kanban_auto_remediation_adapter.py")
DEFAULT_ORIGIN_REF = os.environ.get("AGENTFLOW_AUTO_REMEDIATION_ORIGIN_REF", "discord:#shaman:1500539609413849200")
DEFAULT_RETURN_TO_REF = os.environ.get("AGENTFLOW_AUTO_REMEDIATION_RETURN_TO_REF", "discord:#shaman:1500539609413849200")
EVENT_RE = re.compile(r"kanban-event-(\d+)")

sys.path.insert(0, AGENTFLOW_DIR)
from agentflow.kanban_auto_remediation import (  # noqa: E402
    APPROVE_REAL_KANBAN_WRITE_ENV,
    APPROVE_REAL_KANBAN_WRITE_MARKER,
    GatedKanbanSubprocessWriter,
    read_kanban_block_events_from_sqlite,
    scan_sources,
)


def _state(path: Path = STATE) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict[str, Any], path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2))
    tmp.replace(path)


def _event_id(source_review_id: str) -> int:
    match = EVENT_RE.search(source_review_id or "")
    return int(match.group(1)) if match else 0


def _current_max_event_id(db_path: str | Path) -> int:
    db = Path(db_path)
    if not db.exists():
        return 0
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        value = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
    return int(value or 0)


def _board_state(data: dict[str, Any], board: str) -> dict[str, Any]:
    boards = data.setdefault("boards", {})
    if not isinstance(boards, dict):
        boards = {}
        data["boards"] = boards
    entry = boards.setdefault(board, {})
    if not isinstance(entry, dict):
        entry = {}
        boards[board] = entry
    return entry


def _last_seen(data: dict[str, Any], board: str) -> int | None:
    entry = _board_state(data, board)
    if "last_seen_event_id" not in entry:
        return None
    try:
        return int(entry.get("last_seen_event_id") or 0)
    except Exception:
        return 0


def _set_last_seen(data: dict[str, Any], board: str, value: int) -> None:
    _board_state(data, board)["last_seen_event_id"] = int(value)


def _load_sources(db_path: str, board: str, limit: int) -> list[dict[str, Any]]:
    return read_kanban_block_events_from_sqlite(
        db_path,
        board=board,
        limit=limit,
        default_origin_ref=DEFAULT_ORIGIN_REF,
        default_return_to_ref=DEFAULT_RETURN_TO_REF,
    )


def run_once(*, board: str = BOARD, db_path: str = KANBAN_DB, state_path: Path = STATE, dry_run: bool = False, limit: int = 50) -> tuple[int, str]:
    if board != "oracle-lab":
        return 2, f"BLOCK unsupported_board board={board}"

    st = _state(state_path)
    last_seen = _last_seen(st, board)
    current_max = _current_max_event_id(db_path)
    if last_seen is None:
        _set_last_seen(st, board, current_max)
        _save(st, state_path)
        return 0, f"initialized board={board} last_seen={current_max} historical_replay=0"

    sources = _load_sources(db_path, board, limit)
    max_seen = max([last_seen] + [_event_id(s.get("review_id", "")) for s in sources])
    new_sources = [s for s in sources if _event_id(s.get("review_id", "")) > last_seen]
    if max_seen > last_seen:
        _set_last_seen(st, board, max_seen)
        _save(st, state_path)
    if not new_sources:
        return 0, ""

    env = dict(os.environ)
    env["HERMES_AUTO_REMEDIATION_BOARD"] = board
    env["HERMES_AUTO_REMEDIATION_NOTIFY_CHATS"] = env.get(
        "HERMES_AUTO_REMEDIATION_NOTIFY_CHATS",
        "discord:1500539609413849200,discord:1497895797579190357",
    )
    if not dry_run:
        env[APPROVE_REAL_KANBAN_WRITE_ENV] = "1"
    writer = GatedKanbanSubprocessWriter(
        adapter_command=["python3", ADAPTER],
        marker=APPROVE_REAL_KANBAN_WRITE_MARKER if not dry_run else None,
        allow_real_write_once=not dry_run,
        env=env,
    )
    results = scan_sources(new_sources, writer)
    actionable = [r for r in results if r.action == "applied" and r.specs]
    proposals = [r for r in results if r.proposal_spec is not None]
    failures = [r for r in results if r.ok is False]
    if not actionable and not proposals and not failures:
        return 0, ""
    lines = [
        "AGENTFLOW AUTO-REMEDIATION WATCHDOG",
        f"board={board} dry_run={dry_run} new={len(new_sources)} actionable={len(actionable)} proposals={len(proposals)} failures={len(failures)} last_seen={_last_seen(st, board)}",
    ]
    for r in actionable[:5]:
        lines.append(f"GO auto-remediation applied source={r.source_review_id} specs={len(r.specs)}")
    for r in proposals[:5]:
        lines.append(f"NEED_MORE proposal source={r.source_review_id} reasons={','.join(r.blocked_reasons)}")
    for r in failures[:5]:
        lines.append(f"BLOCK source={r.source_review_id} reasons={','.join(r.blocked_reasons)}")
    return (1 if failures else 0), "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", default=BOARD)
    parser.add_argument("--kanban-db", default=KANBAN_DB)
    parser.add_argument("--state", default=str(STATE))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    code, output = run_once(board=args.board, db_path=args.kanban_db, state_path=Path(args.state), dry_run=args.dry_run, limit=args.limit)
    if output:
        print(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
