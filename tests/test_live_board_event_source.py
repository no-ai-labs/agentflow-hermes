"""LiveBoardEventSource reads a real per-board Kanban sqlite DB (production
path), not a fixture list. Builds a minimal real-shaped kanban.db in a tmp dir
and proves terminal-event reading, structured-metadata pass-through, cursor
bounds, and generic endpoint resolution (typed notify endpoint first, board
default fallback)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agentflow_hermes.board_events import LiveBoardEventSource

_SCHEMA = """
create table tasks (
    id text primary key, title text not null, body text, assignee text,
    status text not null, workspace_path text, workflow_template_id text
);
create table task_runs (
    id integer primary key autoincrement, task_id text not null, step_key text,
    status text not null, summary text, metadata text
);
create table task_events (
    id integer primary key autoincrement, task_id text not null, run_id integer,
    kind text not null, payload text, created_at integer not null
);
create table kanban_notify_subs (
    task_id text not null, platform text not null, chat_id text not null,
    thread_id text not null default '', created_at integer not null,
    primary key (task_id, platform, chat_id, thread_id)
);
"""


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.execute(
            "insert into tasks(id, title, status, assignee, workspace_path) values(?,?,?,?,?)",
            ("t_live1", "needs input task", "blocked", "worker", "/tmp/ws"),
        )
        meta = {
            "agentflow_outcome": {
                "schema_version": 1,
                "verdict": "BLOCK",
                "continuation_kind": "needs_input",
                "contract_ref": "generic.owner-input.v1",
            }
        }
        conn.execute(
            "insert into task_runs(id, task_id, step_key, status, summary, metadata) values(?,?,?,?,?,?)",
            (7, "t_live1", "impl", "blocked", "Verdict: BLOCK owner input", json.dumps(meta)),
        )
        conn.execute(
            "insert into task_events(id, task_id, run_id, kind, payload, created_at) values(?,?,?,?,?,?)",
            (41, "t_live1", 7, "blocked", "{}", 1000),
        )
        # a non-terminal event that must be ignored
        conn.execute(
            "insert into task_events(id, task_id, run_id, kind, payload, created_at) values(?,?,?,?,?,?)",
            (42, "t_live1", 7, "running", "{}", 1001),
        )
        conn.commit()
    finally:
        conn.close()


def test_live_source_reads_terminal_events_with_structured_metadata(tmp_path):
    db = tmp_path / "kanban.db"
    _make_db(db)
    source = LiveBoardEventSource(board="warroom-os", db_path=db, db_identity="warroom-os", default_endpoint="discord:#research:999")

    assert source.current_max_seq() == 42
    events = source.fetch_events_since(0)
    assert len(events) == 1  # only the terminal 'blocked' event
    ev = events[0]
    assert ev.event_seq == 41
    assert ev.source_task_id == "t_live1"
    assert ev.source_graph_id  # non-empty (required by OutcomeEnvelope)
    assert ev.run_metadata["agentflow_outcome"]["continuation_kind"] == "needs_input"
    # no typed sub -> board default endpoint
    assert ev.origin_ref == "discord:#research:999"


def test_live_source_cursor_bounds_exclude_seen(tmp_path):
    db = tmp_path / "kanban.db"
    _make_db(db)
    source = LiveBoardEventSource(board="warroom-os", db_path=db, db_identity="warroom-os")
    assert source.fetch_events_since(41) == []


def test_live_source_defers_terminal_event_until_run_row_is_visible(tmp_path):
    db = tmp_path / "kanban.db"
    _make_db(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "insert into tasks(id, title, status, assignee, workspace_path) values(?,?,?,?,?)",
            ("t_race", "race terminal", "done", "ccreviewer", "/tmp/ws"),
        )
        conn.execute(
            "insert into task_events(id, task_id, run_id, kind, payload, created_at) values(?,?,?,?,?,?)",
            (43, "t_race", 999, "completed", json.dumps({"summary": "Verdict: GO"}), 1002),
        )
        conn.commit()
    finally:
        conn.close()

    source = LiveBoardEventSource(board="warroom-os", db_path=db, db_identity="warroom-os")
    assert [ev.event_seq for ev in source.fetch_events_since(41)] == []

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "insert into task_runs(id, task_id, step_key, status, summary, metadata) values(?,?,?,?,?,?)",
            (999, "t_race", "review", "completed", "Verdict: GO\nRoadmap-Transition: research.default.impl_review", "{}"),
        )
        conn.commit()
    finally:
        conn.close()

    events = source.fetch_events_since(41)
    assert [ev.event_seq for ev in events] == [43]
    assert "Roadmap-Transition: research.default.impl_review" in events[0].summary


def test_live_source_typed_endpoint_wins_over_default(tmp_path):
    db = tmp_path / "kanban.db"
    _make_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, created_at) values(?,?,?,?,?)",
        ("t_live1", "discord", "1499390151393284106", "", 500),
    )
    conn.commit()
    conn.close()

    source = LiveBoardEventSource(board="warroom-os", db_path=db, db_identity="warroom-os", default_endpoint="discord:#research:999")
    ev = source.fetch_events_since(0)[0]
    assert ev.origin_ref == "discord:1499390151393284106"


def test_live_source_missing_db_is_empty(tmp_path):
    source = LiveBoardEventSource(board="ghost", db_path=tmp_path / "nope.db", db_identity="ghost")
    assert source.current_max_seq() == 0
    assert source.fetch_events_since(0) == []
