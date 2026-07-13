"""M27 final remediation: strictly side-effect-free ``apply=false`` dry-runs.

Adversarial live-event regression (plan blocker 2): a dry-run cadence over a
board that already has a durable cursor MUST still produce preview output for a
genuinely new live event, while leaving every durable sqlite table byte-for-byte
unchanged — no cursor advance, no continuation_instances/steps/receipts/events,
no board_outbox, no interaction cases/members. Both the watchdog entrypoint
(``scripts/agentflow_needs_input_watchdog.py``) and the agentflowd daemon
(``AgentflowDaemon.tick``) are exercised, plus the low-level
``isolated_preview_store`` primitive they share.

The proof is a hash+count snapshot of *every* table in the durable store taken
before and after the dry-run; any leaked row or content change fails the test.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

from types import SimpleNamespace

from agentflow_hermes.continuation_store import ContinuationStore, isolated_preview_store

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "agentflow_needs_input_watchdog.py"
_DAEMON_SCRIPT = _REPO / "scripts" / "agentflowd.py"
_CONTRACTS = _REPO / "contracts"


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_watchdog():
    return _load_script("agentflow_needs_input_watchdog_sef", _SCRIPT)


def _daemon_args(*, boards_root: Path, db: Path, apply: bool) -> SimpleNamespace:
    return SimpleNamespace(
        boards_root=str(boards_root),
        overrides="",
        contracts_dir=str(_CONTRACTS),
        db=str(db),
        apply=apply,
        poll_interval_seconds=0.5,
        reconcile_interval_seconds=999.0,
    )


def _table_snapshot(db_path: Path) -> dict[str, tuple[int, str]]:
    """Count + content hash of every table plus the schema version. A truly
    side-effect-free dry-run leaves this identical before and after."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        snap: dict[str, tuple[int, str]] = {}
        version = con.execute("pragma user_version").fetchone()[0]
        snap["__user_version__"] = (int(version), "")
        tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table' order by name"
        )]
        for table in tables:
            rows = con.execute(f"select * from {table}").fetchall()
            digest = hashlib.sha256()
            for row in rows:
                digest.update(repr(tuple(row)).encode("utf-8"))
                digest.update(b"\x1e")
            snap[table] = (len(rows), digest.hexdigest())
        return snap
    finally:
        con.close()


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


# -- the shared primitive ----------------------------------------------------


def test_isolated_preview_store_never_writes_source(tmp_path):
    source = tmp_path / "durable.sqlite"
    store = ContinuationStore(source)
    # Give the durable store real prior state (a cursor + an instance).
    store.advance_cursor("b1", "b1", 42)
    store.create_instance(board="b1", source_task_id="t_real", source_event_id="ev_real")
    before = _table_snapshot(source)

    with isolated_preview_store(source) as preview:
        # Preview inherits the durable prior state...
        assert preview.get_cursor("b1", "b1") == 42
        assert len(preview.list_instances()) == 1
        # ...and mutations land only in the throwaway copy.
        preview.advance_cursor("b1", "b1", 999)
        preview.create_instance(board="b1", source_task_id="t_preview", source_event_id="ev_preview")
        assert preview.get_cursor("b1", "b1") == 999
        assert len(preview.list_instances()) == 2
        assert preview.path != source

    after = _table_snapshot(source)
    assert before == after
    assert store.get_cursor("b1", "b1") == 42
    assert len(store.list_instances()) == 1


def test_isolated_preview_store_handles_absent_source(tmp_path):
    source = tmp_path / "nonexistent.sqlite"
    with isolated_preview_store(source) as preview:
        preview.advance_cursor("b1", "b1", 7)
        assert preview.get_cursor("b1", "b1") == 7
    # The dry-run must not have created the durable file at all.
    assert not source.exists()


# -- watchdog entrypoint (blocker 1 + 2) -------------------------------------


def test_watchdog_dryrun_previews_new_event_without_touching_durable(tmp_path):
    wd = _load_watchdog()
    boards_root = tmp_path / "boards"
    db_path = _write_kanban_board(boards_root, "compat-board")
    registry_path = tmp_path / "boards.yaml"
    registry_path.write_text(f"boards:\n  compat-board:\n    db_path: {db_path}\n    enabled: true\n")
    durable = tmp_path / "agentflow.sqlite"

    # Prime the durable cursor with a real apply seed: first-sight seeding only
    # advances the cursor (no events yet, so no board adapter is ever built).
    code, out = wd.run_once(registry_path=registry_path, db_path=durable, apply=True, all_kinds=False)
    assert (code, out) == (0, "")
    assert durable.exists()
    before = _table_snapshot(durable)

    # A genuinely new live needs_input event arrives on the board.
    _write_needs_input_run(db_path, task_id="t_live1")

    # Dry-run cadence: preview output IS produced for the new live event...
    code, out = wd.run_once(registry_path=registry_path, db_path=durable, apply=False, all_kinds=False)
    assert code == 0
    assert "OWNER-INPUT board=compat-board" in out

    # ...yet not a single durable table changed.
    after = _table_snapshot(durable)
    assert before == after


# -- agentflowd CLI dry-run boundary (blocker 1) -----------------------------


def test_agentflowd_cli_dryrun_tick_previews_without_touching_durable(tmp_path):
    """The agentflowd CLI is where a dry-run can point at the durable/canonical
    ledger. Without --apply, ``effective_store`` hands the daemon an isolated
    preview copy, so a diagnostic tick previews the new live event yet mutates
    nothing durable. With --apply the daemon persists to the durable store."""
    daemon_mod = _load_script("agentflowd_sef", _DAEMON_SCRIPT)
    boards_root = tmp_path / "boards"
    db_path = _write_kanban_board(boards_root, "alpha")
    durable = tmp_path / "agentflow.sqlite"

    # Prime the durable cursor with an --apply seed tick (first sight seeds
    # only, no events, no board mutation).
    apply_args = _daemon_args(boards_root=boards_root, db=durable, apply=True)
    with daemon_mod.effective_store(apply_args) as store:
        assert store.path == durable  # --apply uses the durable store directly
        daemon_mod.build_daemon(apply_args, store).tick()
    before = _table_snapshot(durable)

    _write_needs_input_run(db_path, task_id="t_live_daemon")

    # Dry-run tick (no --apply): preview processes the new event...
    dry_args = _daemon_args(boards_root=boards_root, db=durable, apply=False)
    with daemon_mod.effective_store(dry_args) as store:
        assert store.path != durable  # dry-run runs against an isolated copy
        report = daemon_mod.build_daemon(dry_args, store).tick()
    assert report["boards"][0]["processed"] == 1
    # ...but leaves the durable store byte-for-byte unchanged.
    after = _table_snapshot(durable)
    assert before == after
