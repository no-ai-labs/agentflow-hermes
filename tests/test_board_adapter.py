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


def test_real_adapter_create_task_builds_argv_and_parses_json():
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, json.dumps({"task_id": "task:abc123"}), ""

    adapter = RealBoardAdapter(runner=runner, board="warroom-os", hermes_bin="hermes")
    result = adapter.create_task(_intent())

    assert result == {"success": True, "task_id": "task:abc123"}
    argv = calls[0]
    assert argv[0] == "hermes"
    assert "--board" in argv and "warroom-os" in argv
    assert "--idempotency-key" in argv and "anchor:1" in argv
    assert "--origin-platform" in argv and "discord" in argv


def test_real_adapter_create_task_is_cached_locally_and_skips_second_cli_call():
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, json.dumps({"task_id": "task:abc123"}), ""

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


def test_real_adapter_block_subscribe_comment_complete_build_expected_argv():
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, json.dumps({"success": True}), ""

    adapter = RealBoardAdapter(runner=runner, board="warroom-os")
    adapter.block_task("task:1", "awaiting_owner_input")
    adapter.subscribe("task:1", "discord:#research")
    adapter.comment("task:1", "note")
    adapter.complete_owner_anchor("task:1", receipt_ref="receipt:1")

    assert len(calls) == 4
    assert any("block" in c for c in calls)
    assert any("subscribe" in c for c in calls)
    assert any("comment" in c for c in calls)
    assert any("complete" in c for c in calls)
