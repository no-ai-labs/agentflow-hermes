"""M27 blocker 4: legacy incident-row quarantine only removes dry-run-leaked
``t_m27live_*`` incident instances and their dependent rows, preserves every
legitimate legacy instance, and is idempotent."""
from __future__ import annotations

import importlib.util
import sqlite3
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "m27_quarantine_legacy_incident_rows.py"


def _load():
    spec = importlib.util.spec_from_file_location("m27_quarantine", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_legacy(db: Path) -> None:
    con = sqlite3.connect(db)
    con.executescript(
        """
        create table continuation_instances (
            id integer primary key autoincrement, board text, source_task_id text,
            source_event_id text, contract_ref text default '', state text default 'detected'
        );
        create table board_outbox (
            id integer primary key autoincrement, continuation_id integer, operation text,
            state text default 'pending', board_task_id text default ''
        );
        create table continuation_events (
            id integer primary key autoincrement, continuation_id integer, seq integer, kind text
        );
        """
    )
    # Two legitimate legacy instances (real task ids) + their outbox.
    for tid in ("t_4e1cd2b8", "t_e5930ee3"):
        cur = con.execute(
            "insert into continuation_instances(board, source_task_id, source_event_id) values('agentflow-hermes', ?, 'ev')",
            (tid,),
        )
        con.execute("insert into board_outbox(continuation_id, operation, board_task_id) values(?, 'create_task', 't_legit')", (cur.lastrowid,))
    # Three dry-run-leaked incident instances (t_m27live_* probe ids) + anchors.
    for tid in ("t_m27live_x_agentflow_hermes", "t_m27live_x_warroom_os", "t_m27live_x_oracle_lab"):
        cur = con.execute(
            "insert into continuation_instances(board, source_task_id, source_event_id) values('agentflow-hermes', ?, 'ev')",
            (tid,),
        )
        con.execute("insert into board_outbox(continuation_id, operation, board_task_id) values(?, 'create_task', 'task:abc')", (cur.lastrowid,))
        con.execute("insert into board_outbox(continuation_id, operation, board_task_id) values(?, 'subscribe', '')", (cur.lastrowid,))
        con.execute("insert into continuation_events(continuation_id, seq, kind) values(?, 1, 'created')", (cur.lastrowid,))
    con.commit()
    con.close()


def test_quarantine_removes_only_incident_rows_and_preserves_legit(tmp_path):
    mod = _load()
    db = tmp_path / "legacy.sqlite"
    _seed_legacy(db)

    result = mod.quarantine(legacy_db=db, apply=True, receipt_dir=tmp_path / "artifacts")

    assert result["quarantined"] is True
    assert result["incident_instance_ids"] == [3, 4, 5]
    assert result["incident_outbox_ids"] == [3, 4, 5, 6, 7, 8]

    con = sqlite3.connect(db)
    try:
        remaining = [r[0] for r in con.execute("select source_task_id from continuation_instances order by id")]
        assert remaining == ["t_4e1cd2b8", "t_e5930ee3"]  # legit rows untouched
        # No incident outbox/events remain in the live tables.
        assert con.execute("select count(*) from board_outbox where board_task_id='task:abc'").fetchone()[0] == 0
        assert con.execute("select count(*) from continuation_events").fetchone()[0] == 0
        # The legit outbox anchor survived.
        assert con.execute("select count(*) from board_outbox where board_task_id='t_legit'").fetchone()[0] == 2
        # Everything removed is preserved in the in-DB audit table.
        assert con.execute("select count(*) from quarantined_incident_rows").fetchone()[0] == 3 + 6 + 3
    finally:
        con.close()


def test_quarantine_is_idempotent(tmp_path):
    mod = _load()
    db = tmp_path / "legacy.sqlite"
    _seed_legacy(db)

    mod.quarantine(legacy_db=db, apply=True, receipt_dir=tmp_path / "a1")
    second = mod.quarantine(legacy_db=db, apply=True, receipt_dir=tmp_path / "a2")

    assert second["incident_instance_ids"] == []
    assert second["quarantined"] is False


def test_quarantine_plan_mode_mutates_nothing(tmp_path):
    mod = _load()
    db = tmp_path / "legacy.sqlite"
    _seed_legacy(db)

    con = sqlite3.connect(db)
    before = con.execute("select count(*) from continuation_instances").fetchone()[0]
    con.close()

    result = mod.quarantine(legacy_db=db, apply=False, receipt_dir=tmp_path / "artifacts")
    assert result["incident_instance_ids"] == [3, 4, 5]  # identified...

    con = sqlite3.connect(db)
    after = con.execute("select count(*) from continuation_instances").fetchone()[0]
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    con.close()
    assert after == before  # ...but nothing removed
    assert "quarantined_incident_rows" not in tables  # plan mode never even creates the audit table
