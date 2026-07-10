from __future__ import annotations

import json
import sqlite3

from agentflow_hermes.board_adapter import FakeBoardAdapter, RealBoardAdapter, default_board_kanban_db_path


def _intent(**overrides):
    kwargs = dict(
        kind="owner_anchor",
        title="[owner-input] evidence anchor",
        idempotency_key="anchor:1",
        assignee="warroom-owner",
        origin_ref="discord:#research",
        status="blocked",
        blocked_reason="awaiting_owner_input",
        required_owner_fields=["resolution_basis", "approval_receipt_id"],
    )
    kwargs.update(overrides)
    return kwargs


def test_default_board_db_path_uses_inherited_shared_board_root(monkeypatch, tmp_path):
    current = tmp_path / ".hermes" / "kanban" / "boards" / "agentflow-hermes" / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "ccsupervisor"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(current))

    expected = tmp_path / ".hermes" / "kanban" / "boards" / "warroom-os" / "kanban.db"
    assert default_board_kanban_db_path("warroom-os") == expected


def test_fake_create_task_is_idempotent_by_key():
    adapter = FakeBoardAdapter()
    first = adapter.create_task(_intent())
    second = adapter.create_task(_intent())
    assert first["success"] is True
    assert first["task_id"] == second["task_id"]
    assert len(adapter.tasks) == 1


def test_fake_create_task_distinct_keys_create_distinct_tasks():
    adapter = FakeBoardAdapter()
    first = adapter.create_task(_intent(idempotency_key="anchor:1"))
    second = adapter.create_task(_intent(idempotency_key="anchor:2"))
    assert first["task_id"] != second["task_id"]


def test_fake_subscribe_block_comment_complete_are_idempotent():
    adapter = FakeBoardAdapter()
    task_id = adapter.create_task(_intent())["task_id"]

    adapter.subscribe(task_id, "discord:#research")
    adapter.subscribe(task_id, "discord:#research")
    assert adapter.subscriptions.count((task_id, "discord:#research")) == 1

    adapter.block_task(task_id, "awaiting_owner_input")
    adapter.block_task(task_id, "awaiting_owner_input")
    assert adapter.blocked.count((task_id, "awaiting_owner_input")) == 1

    adapter.comment(task_id, "materialization created")
    adapter.comment(task_id, "materialization created")
    assert adapter.comments.count((task_id, "materialization created")) == 1

    adapter.complete_owner_anchor(task_id, receipt_ref="receipt:1")
    adapter.complete_owner_anchor(task_id, receipt_ref="receipt:1")
    assert adapter.completed.count((task_id, "receipt:1")) == 1


# --- RealBoardAdapter: argv shapes verified against the installed `hermes`
# CLI's own --help output and hermes_cli/kanban.py source (not invented). See
# board_adapter.py's module docstring for the verification notes.


def test_real_adapter_create_task_uses_initial_status_blocked_and_body_checklist():
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, json.dumps({"id": "t_abc123"}), ""

    adapter = RealBoardAdapter(runner=runner, board="warroom-os", hermes_bin="hermes")
    result = adapter.create_task(_intent())

    assert result == {"success": True, "task_id": "t_abc123"}
    argv = calls[0]
    assert argv[:4] == ["hermes", "kanban", "--board", "warroom-os"]
    assert argv[4] == "create"
    assert "--initial-status" in argv and "blocked" in argv
    assert "--idempotency-key" in argv and "anchor:1" in argv
    # No such flags exist on the real `create` command.
    assert "--status" not in argv
    assert "--blocked-reason" not in argv
    assert "--origin-platform" not in argv
    # The awaiting-owner reason/checklist is folded into --body instead.
    body_index = argv.index("--body") + 1
    body = argv[body_index]
    assert "awaiting_owner_input" in body
    assert "resolution_basis" in body
    assert "approval_receipt_id" in body


def test_real_adapter_create_task_is_cached_locally_and_skips_second_cli_call():
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, json.dumps({"id": "t_abc123"}), ""

    adapter = RealBoardAdapter(runner=runner, board="warroom-os")
    first = adapter.create_task(_intent())
    second = adapter.create_task(_intent())

    assert first["task_id"] == second["task_id"]
    assert len(calls) == 1  # second call resolved from local idempotency cache


def test_real_adapter_create_task_runner_failure_is_reported():
    def runner(argv):
        return 1, "", "boom"

    adapter = RealBoardAdapter(runner=runner, board="warroom-os")
    result = adapter.create_task(_intent(idempotency_key="anchor:fail"))
    assert result["success"] is False


def test_real_adapter_block_uses_positional_reason_and_kind_flag():
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, "t:1 blocked", ""

    adapter = RealBoardAdapter(runner=runner, board="warroom-os")
    result = adapter.block_task("t:1", "awaiting_owner_input", kind="needs_input")

    assert result == {"success": True}
    argv = calls[0]
    assert argv == ["hermes", "kanban", "--board", "warroom-os", "block", "t:1", "awaiting_owner_input", "--kind", "needs_input"]


def test_real_adapter_subscribe_uses_notify_subscribe_not_subscribe():
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, "Subscribed discord:research to t:1", ""

    adapter = RealBoardAdapter(runner=runner, board="warroom-os")
    result = adapter.subscribe("t:1", "discord:#research")

    assert result == {"success": True}
    argv = calls[0]
    assert argv[:4] == ["hermes", "kanban", "--board", "warroom-os"]
    assert argv[4] == "notify-subscribe"
    assert "subscribe" == argv[4] or argv[4] == "notify-subscribe"
    assert "--platform" in argv and "discord" in argv
    assert "--chat-id" in argv and "1499390151393284106" in argv
    assert "research" not in argv
    assert "--origin-platform" not in argv


def test_real_adapter_subscribe_numeric_research_creates_durable_ack_rows(tmp_path):
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        create table kanban_notify_subs (
            task_id text not null,
            platform text not null,
            chat_id text not null,
            thread_id text not null default '',
            user_id text,
            notifier_profile text,
            created_at integer not null,
            last_event_id integer not null default 0,
            trigger_agent integer not null default 0,
            primary key(task_id, platform, chat_id, thread_id)
        );
        create table ack_subscription (
            id integer primary key autoincrement,
            task_id text not null,
            subscription_id integer,
            platform text,
            chat_id text,
            thread_id text,
            notifier_profile text,
            desired_delivery_mode text,
            active_wake_required integer not null default 0,
            operator_receipt_required integer not null default 0,
            created_at integer not null
        );
        create table ack_active_wake (
            id integer primary key autoincrement,
            task_id text not null,
            subscription_id integer,
            triggered_agent integer not null default 0,
            trigger_error text,
            correlation_id text,
            created_at integer not null,
            status text,
            accepted_by_session integer not null default 0,
            started_by_session integer not null default 0,
            target_session_key text
        );
        """
    )
    con.close()
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, "Subscribed", ""

    adapter = RealBoardAdapter(runner=runner, board="warroom-os", board_db_path=db)
    result = adapter.subscribe("t_owner", "discord:#research")

    assert result["success"] is True
    assert result["ack"] == {"success": True}
    assert calls[0][calls[0].index("--chat-id") + 1] == "1499390151393284106"
    con = sqlite3.connect(db)
    try:
        notify = con.execute("select platform, chat_id, trigger_agent from kanban_notify_subs where task_id='t_owner'").fetchone()
        sub = con.execute("select id, platform, chat_id, active_wake_required from ack_subscription where task_id='t_owner'").fetchone()
        wake = con.execute("select task_id, subscription_id, status from ack_active_wake where task_id='t_owner'").fetchone()
    finally:
        con.close()
    assert notify == ("discord", "1499390151393284106", 1)
    assert sub[1:] == ("discord", "1499390151393284106", 1)
    assert wake == ("t_owner", sub[0], "pending")


def test_real_adapter_subscribe_refuses_unparseable_endpoint_without_calling_cli():
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, "", ""

    adapter = RealBoardAdapter(runner=runner, board="warroom-os")
    result = adapter.subscribe("t:1", "not a valid endpoint")

    assert result["success"] is False
    assert calls == []


def test_real_adapter_comment_uses_positional_text_not_body_flag():
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, "Comment added to t:1", ""

    adapter = RealBoardAdapter(runner=runner, board="warroom-os")
    result = adapter.comment("t:1", "note")

    assert result == {"success": True}
    assert calls[0] == ["hermes", "kanban", "--board", "warroom-os", "comment", "t:1", "note"]


def test_real_adapter_complete_uses_summary_and_metadata_not_receipt_ref():
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, "Completed t:1", ""

    adapter = RealBoardAdapter(runner=runner, board="warroom-os")
    result = adapter.complete_owner_anchor("t:1", receipt_ref="receipt:1")

    assert result == {"success": True}
    argv = calls[0]
    assert argv[:6] == ["hermes", "kanban", "--board", "warroom-os", "complete", "t:1"]
    assert "--summary" in argv
    assert "--metadata" in argv
    assert "--receipt-ref" not in argv
    metadata_index = argv.index("--metadata") + 1
    assert json.loads(argv[metadata_index]) == {"receipt_ref": "receipt:1"}


def test_real_adapter_plain_text_commands_do_not_require_json_output():
    """block/comment/complete/notify-subscribe print plain text on this CLI
    version, not JSON — the adapter must not try to json.loads their stdout."""

    def runner(argv):
        return 0, "some human-readable confirmation, not json", ""

    adapter = RealBoardAdapter(runner=runner, board="warroom-os")
    assert adapter.block_task("t:1", "reason")["success"] is True
    assert adapter.comment("t:1", "note")["success"] is True
    assert adapter.complete_owner_anchor("t:1", receipt_ref="r:1")["success"] is True
    assert adapter.subscribe("t:1", "discord:#research")["success"] is True
