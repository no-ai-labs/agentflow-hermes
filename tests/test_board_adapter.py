from __future__ import annotations

import json

from agentflow_hermes.board_adapter import FakeBoardAdapter, RealBoardAdapter


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
    assert "--chat-id" in argv and "research" in argv
    assert "--origin-platform" not in argv


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
