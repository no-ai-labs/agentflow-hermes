from __future__ import annotations

import json
import sqlite3

from agentflow_hermes.cli import main as cli_main
from agentflow_hermes.live.policy import LivePolicy, load_policy, save_policy
from agentflow_hermes import service_install


CANARY_TARGET = "discord:#hermes-canary"


def _run(argv, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    import io

    captured = io.StringIO()
    from contextlib import redirect_stdout

    with redirect_stdout(captured):
        rc = cli_main(argv)
    text = captured.getvalue()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {"raw": text}
    return rc, data


def _seed_board(db_path, *, delivery_mode: str = "notify+wake"):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        create table tasks (id text primary key, title text, assignee text, workspace_path text, workflow_template_id text);
        create table task_runs (id text primary key, step_key text, summary text, metadata text);
        create table task_events (id integer primary key autoincrement, task_id text, run_id text, kind text, payload text, created_at real);
        create table kanban_notify_subs (
            task_id text not null,
            platform text not null,
            chat_id text not null,
            thread_id text not null default '',
            notifier_profile text,
            trigger_agent integer not null default 0,
            delivery_mode text,
            chat_type text,
            user_id text,
            created_at integer not null default 0
        );
        """
    )
    con.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, delivery_mode, chat_type) values('t1', 'discord', '123', '', ?, 'channel')",
        (delivery_mode,),
    )
    con.commit()
    con.close()


def _write_agentflowd_units(unit_dir, *, boards_root, continuation_db, apply=True):
    unit_dir.mkdir(parents=True, exist_ok=True)
    script = unit_dir / "agentflowd.py"
    script.write_text("# test script\n", encoding="utf-8")
    extra = f"--boards-root {boards_root} --db {continuation_db}"
    if apply:
        extra += " --apply"
    return service_install.install(str(script), unit_dir=str(unit_dir), write_files=True, extra_args=extra)


def test_cli_live_status_defaults_off(monkeypatch, tmp_path):
    rc, data = _run(["live", "status"], monkeypatch, tmp_path)
    assert rc == 0
    assert data["success"] is True
    assert data["policy"]["live_dispatch_enabled"] is False
    assert data["policy"]["kill_switch"] is False


def test_cli_live_enable_disable_dispatch(monkeypatch, tmp_path):
    rc, data = _run(["live", "enable", "--dispatch"], monkeypatch, tmp_path)
    assert rc == 0
    assert data["policy"]["live_dispatch_enabled"] is True
    assert data["policy"]["kill_switch"] is False

    policy = load_policy()
    assert policy.live_dispatch_enabled is True

    rc, data = _run(["live", "disable", "--dispatch"], monkeypatch, tmp_path)
    assert rc == 0
    assert data["policy"]["live_dispatch_enabled"] is False
    assert data["policy"]["kill_switch"] is True


def test_cli_doctor_includes_policy_and_schema_version(monkeypatch, tmp_path):
    rc, data = _run(["doctor"], monkeypatch, tmp_path)
    assert rc == 0
    assert data["success"] is True
    assert data["schema_version"] == 5
    assert "policy" in data
    assert "direct_dispatch_policy" in data
    assert "continuation_runtime" in data


def test_cli_doctor_separates_direct_canary_from_global_continuation_runtime(monkeypatch, tmp_path):
    boards_root = tmp_path / "boards"
    _seed_board(boards_root / "alpha" / "kanban.db")
    continuation_db = tmp_path / "agentflow-daemon.sqlite"
    unit_dir = tmp_path / "units"
    _write_agentflowd_units(unit_dir, boards_root=boards_root, continuation_db=continuation_db, apply=True)
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path / "agentflow-home"))
    save_policy(LivePolicy(canary_targets=(CANARY_TARGET,), allowed_targets=(CANARY_TARGET,)))

    rc, data = _run(["doctor", "--agentflowd-unit-dir", str(unit_dir)], monkeypatch, tmp_path / "agentflow-home")

    assert rc == 0
    assert data["direct_dispatch_policy"]["scope"] == "legacy_canary_only"
    assert data["direct_dispatch_policy"]["canary_targets"] == [CANARY_TARGET]
    runtime = data["continuation_runtime"]
    assert runtime["apply"] is True
    assert runtime["canonical_db"] == str(continuation_db)
    assert runtime["service"]["apply"] is True
    assert runtime["discovered_boards"] == 1
    assert runtime["boards"][0]["board"] == "alpha"
    verdict = runtime["boards"][0]["protection_verdict"]
    assert verdict["typed_origin_available"] is True
    assert verdict["notify_wake_available"] is True
    assert verdict["apply_available"] is True
    assert verdict["effective_semantic_protection"] is True
    assert data["warnings"] == []


def test_cli_live_status_warns_when_board_lacks_effective_semantic_protection(monkeypatch, tmp_path):
    boards_root = tmp_path / "boards"
    broken = boards_root / "broken" / "kanban.db"
    broken.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(broken).close()
    continuation_db = tmp_path / "agentflow-daemon.sqlite"
    unit_dir = tmp_path / "units"
    _write_agentflowd_units(unit_dir, boards_root=boards_root, continuation_db=continuation_db, apply=True)

    rc, data = _run(["live", "status", "--agentflowd-unit-dir", str(unit_dir)], monkeypatch, tmp_path)

    assert rc == 0
    assert data["policy"]["live_dispatch_enabled"] is False
    assert data["continuation_runtime"]["apply"] is True
    assert data["warnings"] == [
        {
            "board": "broken",
            "warning": "board_lacks_effective_semantic_protection",
            "missing": ["typed_origin_missing", "notify_wake_unavailable"],
        }
    ]


def _seed_canonical_cursor(db_path, *, board: str, db_identity: str, last_event_id: int):
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        create table board_cursors (
            board text not null,
            db_identity text not null,
            last_event_id integer not null default 0,
            updated_at real not null,
            primary key(board, db_identity)
        );
        """
    )
    con.execute(
        "insert into board_cursors(board, db_identity, last_event_id, updated_at) values(?,?,?,0)",
        (board, db_identity, last_event_id),
    )
    con.commit()
    con.close()


def test_status_surfaces_report_canonical_board_cursor(monkeypatch, tmp_path):
    """M30E: doctor/live status must read the canonical ``last_event_id``
    cursor for a discovered board, not collapse it to a plausible zero."""
    boards_root = tmp_path / "boards"
    _seed_board(boards_root / "alpha" / "kanban.db")
    continuation_db = tmp_path / "agentflow-daemon.sqlite"
    _seed_canonical_cursor(continuation_db, board="alpha", db_identity="alpha", last_event_id=42)
    unit_dir = tmp_path / "units"
    _write_agentflowd_units(unit_dir, boards_root=boards_root, continuation_db=continuation_db, apply=True)

    for argv in (["doctor"], ["live", "status"]):
        rc, data = _run(argv + ["--agentflowd-unit-dir", str(unit_dir)], monkeypatch, tmp_path)

        assert rc == 0
        board_row = data["boards"][0]
        assert board_row["board"] == "alpha"
        assert board_row["cursor"] == 42
        assert board_row["cursor_seeded"] is True
        assert board_row["cursor_status"]["status"] == "ok"
        assert data["warnings"] == []
        # Direct dispatch policy stays a separate, legacy canary-only surface.
        assert data["direct_dispatch_policy"]["scope"] == "legacy_canary_only"


def test_status_surfaces_report_missing_cursor_row_as_unseeded(monkeypatch, tmp_path):
    boards_root = tmp_path / "boards"
    _seed_board(boards_root / "alpha" / "kanban.db")
    continuation_db = tmp_path / "agentflow-daemon.sqlite"
    _seed_canonical_cursor(continuation_db, board="other", db_identity="other", last_event_id=7)
    unit_dir = tmp_path / "units"
    _write_agentflowd_units(unit_dir, boards_root=boards_root, continuation_db=continuation_db, apply=True)

    rc, data = _run(["live", "status", "--agentflowd-unit-dir", str(unit_dir)], monkeypatch, tmp_path)

    assert rc == 0
    board_row = data["boards"][0]
    assert board_row["cursor"] == 0
    assert board_row["cursor_seeded"] is False
    assert board_row["cursor_status"]["status"] == "ok"
    assert data["warnings"] == []


def test_status_surfaces_fail_closed_on_malformed_cursor_table(monkeypatch, tmp_path):
    """A cursor table without any known cursor column must be surfaced as an
    error, never reported as a healthy ``cursor=0, cursor_seeded=false``."""
    boards_root = tmp_path / "boards"
    _seed_board(boards_root / "alpha" / "kanban.db")
    continuation_db = tmp_path / "agentflow-daemon.sqlite"
    con = sqlite3.connect(continuation_db)
    con.executescript("create table board_cursors (board text, db_identity text, updated_at real);")
    con.commit()
    con.close()
    unit_dir = tmp_path / "units"
    _write_agentflowd_units(unit_dir, boards_root=boards_root, continuation_db=continuation_db, apply=True)

    for argv in (["doctor"], ["live", "status"]):
        rc, data = _run(argv + ["--agentflowd-unit-dir", str(unit_dir)], monkeypatch, tmp_path)

        assert rc == 0
        board_row = data["boards"][0]
        assert board_row["cursor_status"]["status"] == "error"
        assert board_row["cursor_status"]["error"] == "cursor_column_unknown"
        assert board_row["cursor"] is None
        assert board_row["cursor_seeded"] is None
        assert {
            "board": "alpha",
            "warning": "cursor_status_unavailable",
            "missing": ["cursor_column_unknown"],
        } in data["warnings"]


def test_cli_dispatch_without_live_is_dry_run(monkeypatch, tmp_path):
    rc, data = _run(["enqueue", "--title", "t"], monkeypatch, tmp_path)
    assert rc == 0
    job_id = data["job_id"]

    rc, data = _run(["dispatch", "--job-id", job_id], monkeypatch, tmp_path)
    assert rc == 0
    assert data["success"] is True
    assert data["mode"] == "dry-run"
    assert "prompt" in data


def test_cli_dispatch_live_without_gateway_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    save_policy(LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
    ))
    rc, data = _run(["enqueue", "--title", "t", "--target", CANARY_TARGET], monkeypatch, tmp_path)
    assert rc == 0
    job_id = data["job_id"]

    rc, data = _run(["dispatch", "--job-id", job_id, "--live"], monkeypatch, tmp_path)
    assert rc == 2
    assert data["success"] is False
    assert data["error"] == "gateway_unavailable"


def test_cli_live_canary_refuses_without_policy(monkeypatch, tmp_path):
    rc, data = _run(["live", "canary", "--target", CANARY_TARGET, "--live"], monkeypatch, tmp_path)
    assert rc == 2
    assert data["success"] is False
    assert data["error"] == "live_dispatch_disabled"


def test_cli_live_canary_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    save_policy(LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
    ))
    rc, data = _run(["live", "canary", "--target", CANARY_TARGET, "--live"], monkeypatch, tmp_path)
    assert rc == 0
    assert data["success"] is True
    assert data["mode"] == "live"
    assert data["gateway_calls"] == 1
