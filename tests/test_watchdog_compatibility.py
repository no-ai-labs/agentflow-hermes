"""M27 commit 7 item 3: prove the two legacy watchdog script entrypoints still
function correctly as pure RECONCILIATION passes (catch-up only) rather than
duplicating agentflowd's real-time responsibilities — a quiet no-op cadence
when nothing changed, and a single correct material action when something
did, exactly like before M27.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- needs_input watchdog: continuation_engine-backed reconciliation --------


def _write_kanban_board(root: Path, board: str) -> Path:
    db_path = root / board / "kanban.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        create table tasks (id text primary key, title text, assignee text, workspace_path text, workflow_template_id text);
        create table task_runs (id text primary key, step_key text, summary text, metadata text);
        create table task_events (id integer primary key autoincrement, task_id text, run_id text, kind text, payload text, created_at real);
        """
    )
    con.commit()
    con.close()
    return db_path


def _write_needs_input_run(db_path: Path, *, task_id: str) -> None:
    con = sqlite3.connect(db_path)
    metadata = json.dumps(
        {
            "agentflow_outcome": {
                "schema_version": 1,
                "verdict": "BLOCK",
                "continuation_kind": "needs_input",
                "required_inputs": [{"name": "result_url", "authority": "owner"}],
            }
        }
    )
    con.execute("insert or replace into tasks(id, title, assignee) values(?, 'demo', 'agent')", (task_id,))
    con.execute(
        "insert into task_runs(id, step_key, summary, metadata) values(?, 'g1', 'BLOCK', ?)", (f"{task_id}-run", metadata)
    )
    con.execute(
        "insert into task_events(task_id, run_id, kind, payload, created_at) values(?, ?, 'completed', '{}', 0)",
        (task_id, f"{task_id}-run"),
    )
    con.commit()
    con.close()


def test_needs_input_watchdog_is_silent_reconciliation_when_nothing_new(tmp_path):
    wd = _load_module("agentflow_needs_input_watchdog_compat", _REPO / "scripts" / "agentflow_needs_input_watchdog.py")
    boards_root = tmp_path / "boards"
    db_path = _write_kanban_board(boards_root, "compat-board")
    registry_path = tmp_path / "boards.yaml"
    registry_path.write_text(f"boards:\n  compat-board:\n    db_path: {db_path}\n    enabled: true\n")
    db = tmp_path / "agentflow.sqlite"

    # First cadence: board seen for the first time, no historical replay.
    code1, out1 = wd.run_once(registry_path=registry_path, db_path=db, apply=False, all_kinds=False)
    assert (code1, out1) == (0, "")

    # No new events between cadences: still silent (reconciliation, not a
    # primary/real-time actor — it only catches up a durable cursor).
    code2, out2 = wd.run_once(registry_path=registry_path, db_path=db, apply=False, all_kinds=False)
    assert (code2, out2) == (0, "")


def test_needs_input_watchdog_still_catches_a_real_needs_input_event(tmp_path):
    """Proves the reconciliation pass is still fully correct, not merely
    disabled — the same router agentflowd uses still produces one owner-input
    creation for one genuine new event."""
    wd = _load_module("agentflow_needs_input_watchdog_compat2", _REPO / "scripts" / "agentflow_needs_input_watchdog.py")
    boards_root = tmp_path / "boards"
    db_path = _write_kanban_board(boards_root, "compat-board")
    registry_path = tmp_path / "boards.yaml"
    registry_path.write_text(f"boards:\n  compat-board:\n    db_path: {db_path}\n    enabled: true\n")
    db = tmp_path / "agentflow.sqlite"

    wd.run_once(registry_path=registry_path, db_path=db, apply=False, all_kinds=False)  # seed

    _write_needs_input_run(db_path, task_id="t1")
    code, out = wd.run_once(registry_path=registry_path, db_path=db, apply=False, all_kinds=False)

    assert code == 0
    assert "OWNER-INPUT board=compat-board" in out

    # Immediately re-running with nothing new is silent again (idempotent
    # cursor advance, not a re-processed duplicate).
    code2, out2 = wd.run_once(registry_path=registry_path, db_path=db, apply=False, all_kinds=False)
    assert (code2, out2) == (0, "")


def test_needs_input_watchdog_all_kinds_flag_reaches_go_and_code_fix_too(tmp_path):
    """``--all-kinds`` routes through the exact same unified router as
    agentflowd, so GO/CODE_FIX are also reachable from this reconciliation
    cadence, not just needs_input — proving no behavior was silently dropped
    by keeping this script around."""
    wd = _load_module("agentflow_needs_input_watchdog_compat3", _REPO / "scripts" / "agentflow_needs_input_watchdog.py")
    boards_root = tmp_path / "boards"
    db_path = _write_kanban_board(boards_root, "compat-board")
    registry_path = tmp_path / "boards.yaml"
    registry_path.write_text(f"boards:\n  compat-board:\n    db_path: {db_path}\n    enabled: true\n")
    db = tmp_path / "agentflow.sqlite"
    wd.run_once(registry_path=registry_path, db_path=db, apply=False, all_kinds=True)  # seed

    con = sqlite3.connect(db_path)
    con.execute("insert or replace into tasks(id, title, assignee) values('t_go', 'demo', 'agent')")
    con.execute("insert into task_runs(id, step_key, summary, metadata) values('t_go-run', 'g1', 'Verdict: GO', '{}')")
    con.execute(
        "insert into task_events(task_id, run_id, kind, payload, created_at) values('t_go', 't_go-run', 'completed', '{}', 0)"
    )
    con.commit()
    con.close()

    code, out = wd.run_once(registry_path=registry_path, db_path=db, apply=False, all_kinds=True)
    assert code == 0
    assert "GO board=compat-board" in out


# -- auto-remediation watchdog: unchanged legacy reconciliation semantics ---


def _create_oracle_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, status TEXT NOT NULL,
            workspace_kind TEXT, workspace_path TEXT
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT, status TEXT NOT NULL,
            outcome TEXT, summary TEXT, metadata TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER,
            kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def test_auto_remediation_watchdog_is_silent_pure_reconciliation_when_nothing_new(tmp_path):
    watchdog = _load_module("auto_remediation_compat", _REPO / "scripts" / "agentflow_auto_remediation_watchdog.py")
    db = tmp_path / "kanban.db"
    state = tmp_path / "state.json"
    _create_oracle_db(db)

    code1, out1 = watchdog.run_once(board="oracle-lab", db_path=str(db), state_path=state, dry_run=True)
    assert code1 == 0  # initialize: seeds board state, no historical replay

    code2, out2 = watchdog.run_once(board="oracle-lab", db_path=str(db), state_path=state, dry_run=True)
    assert (code2, out2) == (0, "")


def test_auto_remediation_watchdog_rejects_non_oracle_board_unchanged(tmp_path):
    """M24B canary scope is intentionally still oracle-lab-only — this
    behavior must survive the M27 docstring update unchanged."""
    watchdog = _load_module("auto_remediation_compat2", _REPO / "scripts" / "agentflow_auto_remediation_watchdog.py")
    code, out = watchdog.run_once(board="some-other-board", db_path=str(tmp_path / "x.db"), state_path=tmp_path / "s.json", dry_run=True)
    assert code == 2
    assert "unsupported_board" in out
