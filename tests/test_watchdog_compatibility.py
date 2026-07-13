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


def _durable_snapshot(db_path: Path) -> dict:
    """Count + content hash of every table in the durable ledger. Identical
    before/after a dry-run proves apply=false was strictly side-effect-free."""
    import hashlib

    con = sqlite3.connect(db_path)
    try:
        snap = {"__user_version__": con.execute("pragma user_version").fetchone()[0]}
        for (table,) in con.execute("select name from sqlite_master where type='table' order by name"):
            rows = con.execute(f"select * from {table}").fetchall()
            digest = hashlib.sha256()
            for row in rows:
                digest.update(repr(row).encode("utf-8"))
            snap[table] = (len(rows), digest.hexdigest())
        return snap
    finally:
        con.close()


def test_needs_input_watchdog_dryrun_is_silent_when_nothing_new(tmp_path):
    """apply=false is a pure, side-effect-free preview (plan M27 blocker 1): a
    board with no events past its durable cursor previews nothing and stays
    silent, and the durable ledger is never mutated by the dry-run itself."""
    wd = _load_module("agentflow_needs_input_watchdog_compat", _REPO / "scripts" / "agentflow_needs_input_watchdog.py")
    boards_root = tmp_path / "boards"
    db_path = _write_kanban_board(boards_root, "compat-board")
    registry_path = tmp_path / "boards.yaml"
    registry_path.write_text(f"boards:\n  compat-board:\n    db_path: {db_path}\n    enabled: true\n")
    db = tmp_path / "agentflow.sqlite"

    # Prime the durable cursor via an apply seed (first sight seeds only, no
    # board adapter is ever built when there are no events to process).
    code0, out0 = wd.run_once(registry_path=registry_path, db_path=db, apply=True, all_kinds=False)
    assert (code0, out0) == (0, "")
    before = _durable_snapshot(db)

    # No new events on the board -> dry-run previews nothing -> silent.
    code1, out1 = wd.run_once(registry_path=registry_path, db_path=db, apply=False, all_kinds=False)
    assert (code1, out1) == (0, "")
    # Idempotent: repeating the dry-run is still silent and still durable-safe.
    code2, out2 = wd.run_once(registry_path=registry_path, db_path=db, apply=False, all_kinds=False)
    assert (code2, out2) == (0, "")
    assert _durable_snapshot(db) == before


def test_needs_input_watchdog_dryrun_previews_real_event_without_persisting(tmp_path):
    """Proves the reconciliation router is still fully correct, not merely
    disabled — the same router agentflowd uses previews one owner-input line
    for one genuine new event — while apply=false leaves the durable ledger
    byte-for-byte unchanged, and repeated dry-runs re-preview idempotently
    (a preview never advances the durable cursor)."""
    wd = _load_module("agentflow_needs_input_watchdog_compat2", _REPO / "scripts" / "agentflow_needs_input_watchdog.py")
    boards_root = tmp_path / "boards"
    db_path = _write_kanban_board(boards_root, "compat-board")
    registry_path = tmp_path / "boards.yaml"
    registry_path.write_text(f"boards:\n  compat-board:\n    db_path: {db_path}\n    enabled: true\n")
    db = tmp_path / "agentflow.sqlite"

    wd.run_once(registry_path=registry_path, db_path=db, apply=True, all_kinds=False)  # seed durable cursor
    before = _durable_snapshot(db)

    _write_needs_input_run(db_path, task_id="t1")
    code, out = wd.run_once(registry_path=registry_path, db_path=db, apply=False, all_kinds=False)
    assert code == 0
    assert "OWNER-INPUT board=compat-board" in out
    assert _durable_snapshot(db) == before  # dry-run mutated nothing durable

    # A dry-run is a stateless preview: the durable cursor never advanced, so
    # re-running previews the same still-pending event again (not a persisted
    # duplicate) and again touches nothing durable.
    code2, out2 = wd.run_once(registry_path=registry_path, db_path=db, apply=False, all_kinds=False)
    assert code2 == 0
    assert "OWNER-INPUT board=compat-board" in out2
    assert _durable_snapshot(db) == before


def test_needs_input_watchdog_all_kinds_flag_reaches_go_and_code_fix_too(tmp_path):
    """``--all-kinds`` routes through the exact same unified router as
    agentflowd, so GO/CODE_FIX are also reachable from this reconciliation
    cadence, not just needs_input — proving no behavior was silently dropped
    by keeping this script around. Still a side-effect-free dry-run preview."""
    wd = _load_module("agentflow_needs_input_watchdog_compat3", _REPO / "scripts" / "agentflow_needs_input_watchdog.py")
    boards_root = tmp_path / "boards"
    db_path = _write_kanban_board(boards_root, "compat-board")
    registry_path = tmp_path / "boards.yaml"
    registry_path.write_text(f"boards:\n  compat-board:\n    db_path: {db_path}\n    enabled: true\n")
    db = tmp_path / "agentflow.sqlite"
    wd.run_once(registry_path=registry_path, db_path=db, apply=True, all_kinds=True)  # seed durable cursor
    before = _durable_snapshot(db)

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
    assert _durable_snapshot(db) == before  # dry-run mutated nothing durable


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
