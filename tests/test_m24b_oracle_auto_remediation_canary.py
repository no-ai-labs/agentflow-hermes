from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG_PATH = ROOT / "scripts" / "agentflow_auto_remediation_watchdog.py"
ADAPTER_PATH = ROOT / "scripts" / "kanban_auto_remediation_adapter.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            workspace_kind TEXT,
            workspace_path TEXT
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            profile TEXT,
            status TEXT NOT NULL,
            outcome TEXT,
            summary TEXT,
            metadata TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def _insert_block(db: Path, *, task_id: str, run_id: int, summary: str) -> int:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO tasks(id,title,body,assignee,status,workspace_kind,workspace_path) VALUES(?,?,?,?,?,?,?)",
        (task_id, "controlled reviewer BLOCK", "raw body must not be scanned", "ccreviewer", "blocked", "dir", "/home/duckran/oracle-lab"),
    )
    conn.execute(
        "INSERT INTO task_runs(id,task_id,profile,status,outcome,summary,metadata) VALUES(?,?,?,?,?,?,?)",
        (run_id, task_id, "ccreviewer", "completed", "blocked", summary, json.dumps({"next_action": "Fix oracle-lab canary ACK edge."})),
    )
    cur = conn.execute(
        "INSERT INTO task_events(task_id,run_id,kind,payload,created_at) VALUES(?,?,?,?,?)",
        (
            task_id,
            run_id,
            "blocked",
            json.dumps({"summary": "Verdict: BLOCK", "origin_return": {"origin_ref": "discord:#shaman:1500539609413849200", "return_to_ref": "discord:#shaman:1500539609413849200", "correlation_id": f"oracle-lab:{task_id}"}}),
            1234567890 + run_id,
        ),
    )
    event_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return event_id


def test_watchdog_initializes_board_state_without_historical_replay(tmp_path):
    watchdog = _load_module("m24b_watchdog", WATCHDOG_PATH)
    db = tmp_path / "kanban.db"
    state = tmp_path / "state.json"
    _create_db(db)
    historical_event = _insert_block(
        db,
        task_id="t_historical",
        run_id=1,
        summary="Verdict: BLOCK. Next action: Fix oracle-lab historical canary.",
    )

    code, output = watchdog.run_once(board="oracle-lab", db_path=str(db), state_path=state, dry_run=True)

    assert code == 0
    assert f"last_seen={historical_event}" in output
    data = json.loads(state.read_text())
    assert data["boards"]["oracle-lab"]["last_seen_event_id"] == historical_event
    assert "GO auto-remediation" not in output


def test_watchdog_board_scoped_dry_run_only_scans_new_oracle_events(tmp_path):
    watchdog = _load_module("m24b_watchdog_new", WATCHDOG_PATH)
    db = tmp_path / "kanban.db"
    state = tmp_path / "state.json"
    _create_db(db)
    first = _insert_block(db, task_id="t_old", run_id=1, summary="Verdict: BLOCK. Next action: Fix oracle-lab old canary.")
    state.write_text(json.dumps({"boards": {"oracle-lab": {"last_seen_event_id": first}}}))
    new_event = _insert_block(db, task_id="t_new", run_id=2, summary="Verdict: BLOCK. Next action: Fix oracle-lab new canary.")

    code, output = watchdog.run_once(board="oracle-lab", db_path=str(db), state_path=state, dry_run=True)

    assert code == 0
    assert "board=oracle-lab dry_run=True new=1 actionable=1" in output
    assert f"oracle-lab:t_new:kanban-event-{new_event}" in output
    data = json.loads(state.read_text())
    assert data["boards"]["oracle-lab"]["last_seen_event_id"] == new_event
    assert "last_seen_event_id" not in data


def _create_ack_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE kanban_notify_subs (
            task_id       TEXT NOT NULL,
            platform      TEXT NOT NULL,
            chat_id       TEXT NOT NULL,
            thread_id     TEXT NOT NULL DEFAULT '',
            user_id       TEXT,
            notifier_profile TEXT,
            trigger_agent INTEGER NOT NULL DEFAULT 0,
            created_at    INTEGER NOT NULL,
            last_event_id INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (task_id, platform, chat_id, thread_id)
        );
        CREATE TABLE ack_subscription (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id                TEXT NOT NULL,
            subscription_id        INTEGER,
            platform               TEXT,
            chat_id                TEXT,
            thread_id              TEXT,
            notifier_profile       TEXT,
            desired_delivery_mode  TEXT,
            active_wake_required   INTEGER NOT NULL DEFAULT 0,
            operator_receipt_required INTEGER NOT NULL DEFAULT 0,
            created_at             INTEGER NOT NULL
        );
        CREATE TABLE ack_active_wake (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id         TEXT NOT NULL,
            subscription_id INTEGER,
            triggered_agent INTEGER NOT NULL DEFAULT 0,
            trigger_error   TEXT,
            correlation_id  TEXT,
            created_at      INTEGER NOT NULL,
            status TEXT, accepted_by_session INTEGER NOT NULL DEFAULT 0,
            started_by_session INTEGER NOT NULL DEFAULT 0, target_session_key TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def test_adapter_materialize_creates_durable_ack_rows(tmp_path, monkeypatch):
    adapter = _load_module("m24b_adapter_ack", ADAPTER_PATH)
    monkeypatch.setattr(adapter, "NOTIFY_CHATS", "discord:1500539609413849200,discord:1497895797579190357")
    state = tmp_path / "map.json"
    db = tmp_path / "kanban.db"
    _create_ack_db(db)
    created = {"fix": "t_fix_canary", "review": "t_review_canary"}

    def fake_run(cmd: list[str]):
        if "create" in cmd:
            title = cmd[cmd.index("create") + 1]
            return {"id": created["review" if "Review" in title else "fix"]}
        return {"ok": True}

    fix = {
        "idempotency_key": "kanban-auto-remediation:auto_remediation_fix:oracle-lab:t_src:kanban-event-1:abc",
        "title": "[auto-remediation] Fix BLOCK for oracle-lab:t_src:kanban-event-1",
        "body": "Next action: Fix oracle-lab canary ACK edge.",
        "assignee": "ccsupervisor",
        "workspace_kind": "dir",
        "workspace_path": "/home/duckran/oracle-lab",
        "origin_ref": "discord:#shaman:1500539609413849200",
        "return_to_ref": "discord:#shaman:1500539609413849200",
    }
    review = {
        **fix,
        "idempotency_key": "kanban-auto-remediation:auto_remediation_review:oracle-lab:t_src:kanban-event-1:def",
        "parent_idempotency_key": fix["idempotency_key"],
        "title": "[auto-remediation] Review fix for oracle-lab:t_src:kanban-event-1",
        "assignee": "ccreviewer",
    }

    code1, _ = adapter.materialize(fix, board="oracle-lab", state_path=state, run=fake_run, db_path=db)
    code2, _ = adapter.materialize(review, board="oracle-lab", state_path=state, run=fake_run, db_path=db)

    assert (code1, code2) == (0, 0)
    conn = sqlite3.connect(db)
    notify_rows = conn.execute("SELECT task_id, trigger_agent FROM kanban_notify_subs").fetchall()
    ack_sub_rows = conn.execute("SELECT task_id, active_wake_required, desired_delivery_mode FROM ack_subscription").fetchall()
    ack_wake_rows = conn.execute("SELECT task_id, triggered_agent, status, correlation_id, subscription_id FROM ack_active_wake").fetchall()
    conn.close()

    assert len(notify_rows) == 4  # 2 targets x 2 cards
    assert all(trigger_agent == 1 for _, trigger_agent in notify_rows)
    assert len(ack_sub_rows) == 4
    assert all(active_wake_required == 1 for _, active_wake_required, _ in ack_sub_rows)
    assert all(mode == "passive+active_wake" for _, _, mode in ack_sub_rows)
    assert len(ack_wake_rows) == 4
    assert all(triggered_agent == 0 for _, triggered_agent, _, _, _ in ack_wake_rows)
    assert all(status in ("pending", "queued") for _, _, status, _, _ in ack_wake_rows)
    assert all(correlation_id for _, _, _, correlation_id, _ in ack_wake_rows)
    assert all(subscription_id is not None for _, _, _, _, subscription_id in ack_wake_rows)
    task_ids = {row[0] for row in notify_rows}
    assert task_ids == {"t_fix_canary", "t_review_canary"}


def test_adapter_materialize_rerun_does_not_duplicate_durable_ack_rows(tmp_path, monkeypatch):
    adapter = _load_module("m24b_adapter_ack_rerun", ADAPTER_PATH)
    monkeypatch.setattr(adapter, "NOTIFY_CHATS", "discord:1500539609413849200,discord:1497895797579190357")
    state = tmp_path / "map.json"
    db = tmp_path / "kanban.db"
    _create_ack_db(db)

    def fake_run(cmd: list[str]):
        if "create" in cmd:
            return {"id": "t_fix_canary"}
        return {"ok": True}

    fix = {
        "idempotency_key": "kanban-auto-remediation:auto_remediation_fix:oracle-lab:t_src:kanban-event-1:abc",
        "title": "[auto-remediation] Fix BLOCK for oracle-lab:t_src:kanban-event-1",
        "body": "Next action: Fix oracle-lab canary ACK edge.",
        "assignee": "ccsupervisor",
        "workspace_kind": "dir",
        "workspace_path": "/home/duckran/oracle-lab",
        "origin_ref": "discord:#shaman:1500539609413849200",
        "return_to_ref": "discord:#shaman:1500539609413849200",
    }

    code1, result1 = adapter.materialize(fix, board="oracle-lab", state_path=state, run=fake_run, db_path=db)
    code2, result2 = adapter.materialize(fix, board="oracle-lab", state_path=state, run=fake_run, db_path=db)
    code3, result3 = adapter.materialize(fix, board="oracle-lab", state_path=state, run=fake_run, db_path=db)

    assert (code1, code2, code3) == (0, 0, 0)
    assert result1["action"] == "created"
    assert result2["action"] == "deduped"
    assert result3["action"] == "deduped"

    conn = sqlite3.connect(db)
    notify_count = conn.execute("SELECT COUNT(*) FROM kanban_notify_subs").fetchone()[0]
    ack_sub_count = conn.execute("SELECT COUNT(*) FROM ack_subscription").fetchone()[0]
    ack_wake_count = conn.execute("SELECT COUNT(*) FROM ack_active_wake").fetchone()[0]
    conn.close()

    assert notify_count == 2
    assert ack_sub_count == 2
    assert ack_wake_count == 2


def test_adapter_materialize_rejects_unsupported_board_with_ack_db(tmp_path):
    adapter = _load_module("m24b_adapter_ack_board", ADAPTER_PATH)
    state = tmp_path / "map.json"
    db = tmp_path / "kanban.db"
    _create_ack_db(db)

    code, result = adapter.materialize(
        {"idempotency_key": "kanban-auto-remediation:x"},
        board="other-board",
        state_path=state,
        run=lambda cmd: {"ok": True},
        db_path=db,
    )

    assert code == 2
    assert result["action"] == "unsupported_board"
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM ack_subscription").fetchone()[0] == 0
    conn.close()


def test_adapter_uses_explicit_board_notify_subscribe_and_idempotent_mapping(tmp_path, monkeypatch):
    adapter = _load_module("m24b_adapter", ADAPTER_PATH)
    monkeypatch.setattr(adapter, "NOTIFY_CHATS", "discord:1500539609413849200,discord:1497895797579190357")
    state = tmp_path / "map.json"
    db = tmp_path / "kanban.db"
    _create_ack_db(db)
    calls: list[list[str]] = []
    created = {"fix": "t_fix_canary", "review": "t_review_canary"}

    def fake_run(cmd: list[str]):
        calls.append(cmd)
        if "create" in cmd:
            title = cmd[cmd.index("create") + 1]
            return {"id": created["review" if "Review" in title else "fix"]}
        return {"ok": True}

    fix = {
        "idempotency_key": "kanban-auto-remediation:auto_remediation_fix:oracle-lab:t_src:kanban-event-1:abc",
        "title": "[auto-remediation] Fix BLOCK for oracle-lab:t_src:kanban-event-1",
        "body": "Next action: Fix oracle-lab canary ACK edge.",
        "assignee": "ccsupervisor",
        "workspace_kind": "dir",
        "workspace_path": "/home/duckran/oracle-lab",
        "origin_ref": "discord:#shaman:1500539609413849200",
        "return_to_ref": "discord:#shaman:1500539609413849200",
    }
    review = {
        **fix,
        "idempotency_key": "kanban-auto-remediation:auto_remediation_review:oracle-lab:t_src:kanban-event-1:def",
        "parent_idempotency_key": fix["idempotency_key"],
        "title": "[auto-remediation] Review fix for oracle-lab:t_src:kanban-event-1",
        "assignee": "ccreviewer",
    }

    code1, result1 = adapter.materialize(fix, board="oracle-lab", state_path=state, run=fake_run, db_path=db)
    code2, result2 = adapter.materialize(review, board="oracle-lab", state_path=state, run=fake_run, db_path=db)
    code3, result3 = adapter.materialize(review, board="oracle-lab", state_path=state, run=fake_run, db_path=db)

    assert (code1, code2, code3) == (0, 0, 0)
    assert result1["id"] == "t_fix_canary"
    assert result2["id"] == "t_review_canary"
    assert result3["action"] == "deduped"
    create_calls = [c for c in calls if "create" in c]
    notify_calls = [c for c in calls if "notify-subscribe" in c]
    assert len(create_calls) == 2
    assert all(c[:4] == ["hermes", "kanban", "--board", "oracle-lab"] for c in calls)
    assert ["--parent", "t_fix_canary"] in [create_calls[1][i:i+2] for i in range(len(create_calls[1]) - 1)]
    assert len(notify_calls) == 6  # two targets for fix, review, duplicate review ensure
    assert {c[c.index("--chat-id") + 1] for c in notify_calls} == {"1500539609413849200", "1497895797579190357"}
    saved = json.loads(state.read_text())
    assert saved[fix["idempotency_key"]] == "t_fix_canary"
    assert saved[review["idempotency_key"]] == "t_review_canary"
