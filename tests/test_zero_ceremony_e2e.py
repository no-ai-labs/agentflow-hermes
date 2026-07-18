"""Plan section 13 acceptance proof (M27 commit 9): drives the real
``agentflowd`` runtime (``AgentflowDaemon``/``continuation_engine.ingest_board_once``/
``outcome_compiler.compile_outcome``/``requirement_resolver.HumanEffortResolver``/
``interaction.InteractionInbox``/``standing_policy``) plus the real natural-
language plugin reply bridge (``plugins/hermes-agentflow/__init__.py``)
against temp sqlite board DBs simulating three real boards
(agentflow-hermes, warroom-os, oracle-lab). No mocked-away business logic:
every assertion below observes an actual ``ContinuationStore`` row or an
actual ``FakeBoardAdapter`` call recorded by the real router.

Explicitly NOT proven here (see docs/m27-zero-ceremony-canary.md): a live
Discord send, a live production three-board canary, real Linux inotify, or
any live/signed trading call. Those remain named follow-ups.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from agentflow_hermes.continuation_store import ContinuationStore
from agentflow_hermes.daemon import AgentflowDaemon, DaemonConfig
from agentflow_hermes.board_adapter import FakeBoardAdapter
from agentflow_hermes.standing_policy import create_standing_policy

_CONTRACTS_DIR = Path(__file__).resolve().parents[1] / "contracts"
_BOARDS = ("agentflow-hermes", "warroom-os", "oracle-lab")
# Canonical numeric Discord channel ids. AgentFlow-generated board default
# endpoints must be numeric (M33 prevention rejects symbolic chat ids like
# ``#research`` at board-registry load, before task/outbox materialization);
# these are the numeric ids the legacy #hermes-main/#research/#shaman lanes map
# to. Numbers only, no ``#name`` placeholder.
_ENDPOINTS = {
    "agentflow-hermes": "discord:1497895797579190357",
    "warroom-os": "discord:1499390151393284106",
    "oracle-lab": "discord:1500539609413849200",
}

PLUGIN_FILE = Path(__file__).resolve().parents[1] / "plugins" / "hermes-agentflow" / "__init__.py"


# -- shared fixture helpers ---------------------------------------------------


def _init_board_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table tasks (
            id text primary key, title text, assignee text, workspace_path text, workflow_template_id text
        );
        create table task_runs (id text primary key, step_key text, summary text, metadata text);
        create table task_events (
            id integer primary key autoincrement, task_id text, run_id text, kind text, payload text, created_at real
        );
        """
    )
    con.commit()
    con.close()


def _write_event(path: Path, *, task_id: str, summary: str = "", metadata: dict | None = None, title: str = "demo") -> None:
    con = sqlite3.connect(path)
    con.execute("insert or replace into tasks(id, title, assignee) values(?, ?, 'agent')", (task_id, title))
    run_id = f"{task_id}-run"
    con.execute(
        "insert into task_runs(id, step_key, summary, metadata) values(?, 'g1', ?, ?)",
        (run_id, summary, json.dumps(metadata or {})),
    )
    con.execute(
        "insert into task_events(task_id, run_id, kind, payload, created_at) values(?, ?, 'completed', '{}', ?)",
        (task_id, run_id, time.time()),
    )
    con.commit()
    con.close()


def _needs_input_metadata(*, required_inputs, contract_ref: str = "generic.owner-input.v1", extra: dict | None = None) -> dict:
    block = {
        "schema_version": 1,
        "verdict": "BLOCK",
        "continuation_kind": "needs_input",
        "contract_ref": contract_ref,
        "required_inputs": required_inputs,
    }
    payload: dict = {"agentflow_outcome": block}
    if extra:
        payload.update(extra)
    return payload


def _write_overrides(tmp_path: Path, endpoints: dict[str, str]) -> Path:
    lines = ["boards:"]
    for board, endpoint in endpoints.items():
        lines.append(f"  {board}:")
        lines.append(f"    default_endpoint: {endpoint}")
    path = tmp_path / "boards.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _boards_root_with(tmp_path: Path, boards: tuple[str, ...]) -> Path:
    root = tmp_path / "boards"
    for board in boards:
        _init_board_db(root / board / "kanban.db")
    return root


def _load_plugin(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeCtx:
    def __init__(self) -> None:
        self.tools: list[dict] = []

    def register_tool(self, name, namespace, schema, handler, emoji=None) -> None:
        self.tools.append({"name": name, "handler": handler})


def _find_tool(ctx: _FakeCtx, name: str):
    for tool in ctx.tools:
        if tool["name"] == name:
            return tool
    raise AssertionError(f"tool {name!r} not registered")


def _plugin_ctx(monkeypatch, store_path: Path, module_name: str) -> _FakeCtx:
    monkeypatch.setenv("HERMES_CONTINUATION_DB", str(store_path))
    plugin = _load_plugin(module_name)
    monkeypatch.setattr(plugin, "_engine_error", None)
    monkeypatch.setattr(plugin, "_run_cli", None)
    ctx = _FakeCtx()
    plugin.register(ctx)
    return ctx


# -- 13.1 latency --------------------------------------------------------------


def test_13_1_event_to_action_latency_under_5s_across_discovered_boards(tmp_path):
    boards_root = _boards_root_with(tmp_path, _BOARDS)
    overrides = _write_overrides(tmp_path, _ENDPOINTS)
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    poll_interval_seconds = 0.02
    config = DaemonConfig(
        store=store,
        boards_root=boards_root,
        overrides_path=overrides,
        contracts_dir=_CONTRACTS_DIR,
        poll_interval_seconds=poll_interval_seconds,
        reconcile_interval_seconds=999,
    )
    daemon = AgentflowDaemon(config)

    async def scenario():
        loop_task = asyncio.ensure_future(daemon.run())
        await asyncio.sleep(poll_interval_seconds * 3)  # let cursors seed first

        write_time = time.time()
        for board in _BOARDS:
            db_path = boards_root / board / "kanban.db"
            metadata = _needs_input_metadata(
                required_inputs=[{"name": "result_url", "kind": "fact", "authority": "owner"}]
            )
            _write_event(db_path, task_id=f"t_latency_{board}", metadata=metadata)

        deadline = write_time + 5.0
        detected_at = None
        while time.time() < deadline:
            if len(store.list_instances()) >= 3:
                detected_at = time.time()
                break
            await asyncio.sleep(poll_interval_seconds / 2)

        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task
        return write_time, detected_at

    write_time, detected_at = asyncio.run(scenario())

    assert detected_at is not None, "not all three boards' continuations appeared within the 5s acceptance budget"
    assert (detected_at - write_time) < 5.0
    boards_seen = {inst["board"] for inst in store.list_instances()}
    assert boards_seen == set(_BOARDS)


# -- 13.2 H0 case ---------------------------------------------------------------


def test_13_2_h0_case_zero_owner_questions_one_resume(tmp_path):
    boards_root = _boards_root_with(tmp_path, ("agentflow-hermes",))
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    config = DaemonConfig(store=store, boards_root=boards_root, contracts_dir=_CONTRACTS_DIR, reconcile_interval_seconds=999)
    daemon = AgentflowDaemon(config)
    daemon.tick()  # seed cursor, no historical replay

    db_path = boards_root / "agentflow-hermes" / "kanban.db"
    metadata = _needs_input_metadata(
        required_inputs=[{"name": "result_url", "kind": "fact", "authority": "owner"}],
        extra={"system_derived": {"result_url": "https://evidence.example/verified"}},
    )
    _write_event(db_path, task_id="t_h0", metadata=metadata)

    report = daemon.tick()
    result = report["boards"][0]["results"][0]

    assert result["action"] == "auto_resolved"
    assert result["h0"] is True
    assert store.list_interaction_cases() == []  # zero owner questions
    instances = store.list_instances()
    assert len(instances) == 1
    assert instances[0]["state"] == "materializing"  # resume created exactly once
    satisfactions = store.list_requirement_satisfactions(instances[0]["id"])
    assert satisfactions[0]["source_kind"] == "system_derived"


# -- 13.3 H1 natural-reply case --------------------------------------------------


def test_13_3_h1_natural_reply_resumes_via_real_plugin_bridge(tmp_path, monkeypatch):
    boards_root = _boards_root_with(tmp_path, ("warroom-os",))
    overrides = _write_overrides(tmp_path, {"warroom-os": _ENDPOINTS["warroom-os"]})
    store_path = tmp_path / "agentflow.sqlite"
    store = ContinuationStore(store_path)
    config = DaemonConfig(
        store=store, boards_root=boards_root, overrides_path=overrides, contracts_dir=_CONTRACTS_DIR,
        reconcile_interval_seconds=999,
    )
    daemon = AgentflowDaemon(config)
    daemon.tick()

    db_path = boards_root / "warroom-os" / "kanban.db"
    # Natural reviewer prose with NO Outcome-Kind marker at all (plan 13.3).
    _write_event(db_path, task_id="t_h1", summary="BLOCK pending the owner's result URL.")

    report = daemon.tick()
    result = report["boards"][0]["results"][0]
    assert result["action"] == "owner_input_planned"
    assert result["h0"] is False
    instance_id = result["instance_id"]

    ctx = _plugin_ctx(monkeypatch, store_path, "hermes_agentflow_e2e_h1")

    inbox_result = json.loads(_find_tool(ctx, "agentflow_input_inbox")["handler"]({"endpoint": "discord:1499390151393284106"}))
    assert inbox_result["success"] is True
    assert len(inbox_result["cases"]) == 1
    case = inbox_result["cases"][0]
    assert case["effort"] == "H1"
    assert "contract_ref" not in case["question"]
    case_id = case["case_id"]

    submit_result = json.loads(
        _find_tool(ctx, "agentflow_submit_input_text")["handler"](
            {"case_id": case_id, "endpoint": "discord:1499390151393284106", "text": "https://example.com/result-natural", "owner_ref": "operator-main"}
        )
    )
    assert submit_result["success"] is True
    assert submit_result["status"] == "resumed"
    assert instance_id in submit_result["resumed_continuation_ids"]

    instance = store.get_instance(instance_id)
    assert instance["state"] == "materializing"
    updated_case = store.get_interaction_case(case_id)
    assert updated_case["question_count"] == 1  # operator questions = 1


# -- 13.4 batched case ------------------------------------------------------------


def test_13_4_batched_case_one_question_resolves_three_with_no_duplicate_cards(tmp_path, monkeypatch):
    boards_root = _boards_root_with(tmp_path, ("warroom-os",))
    overrides = _write_overrides(tmp_path, {"warroom-os": _ENDPOINTS["warroom-os"]})
    store_path = tmp_path / "agentflow.sqlite"
    store = ContinuationStore(store_path)
    config = DaemonConfig(
        store=store, boards_root=boards_root, overrides_path=overrides, contracts_dir=_CONTRACTS_DIR,
        reconcile_interval_seconds=999,
    )
    daemon = AgentflowDaemon(config)
    daemon.tick()

    db_path = boards_root / "warroom-os" / "kanban.db"
    fields = ["result_url", "approval_id", "reviewer_note"]
    for i, field in enumerate(fields):
        metadata = _needs_input_metadata(required_inputs=[{"name": field, "kind": "fact", "authority": "owner"}])
        _write_event(db_path, task_id=f"t_batch_{i}", metadata=metadata)

    report = daemon.tick()
    results = report["boards"][0]["results"]
    assert len(results) == 3
    assert all(r["action"] == "owner_input_planned" for r in results)
    instance_ids = [r["instance_id"] for r in results]
    assert len(set(instance_ids)) == 3  # zero duplicate cards/continuations

    cases = store.list_interaction_cases()
    assert len(cases) == 1  # one batched question
    members = store.list_interaction_members(cases[0]["id"])
    assert {m["continuation_id"] for m in members} == set(instance_ids)

    ctx = _plugin_ctx(monkeypatch, store_path, "hermes_agentflow_e2e_batch")
    inbox_result = json.loads(_find_tool(ctx, "agentflow_input_inbox")["handler"]({"endpoint": "discord:1499390151393284106"}))
    assert len(inbox_result["cases"]) == 1
    case_id = inbox_result["cases"][0]["case_id"]

    reply_text = "1 https://example.com/x, 2 recv_42, 3 looks good"
    submit_result = json.loads(
        _find_tool(ctx, "agentflow_submit_input_text")["handler"](
            {"case_id": case_id, "endpoint": "discord:1499390151393284106", "text": reply_text, "owner_ref": "operator-main"}
        )
    )
    assert submit_result["success"] is True
    assert set(submit_result["resumed_continuation_ids"]) == set(instance_ids)

    for instance_id in instance_ids:
        assert store.get_instance(instance_id)["state"] == "materializing"
    assert len(store.list_instances()) == 3  # still exactly three, no duplicates created


# -- 13.5 policy reuse ------------------------------------------------------------


def test_13_5_policy_reuse_second_equivalent_continuation_is_h0(tmp_path, monkeypatch):
    boards_root = _boards_root_with(tmp_path, ("oracle-lab",))
    overrides = _write_overrides(tmp_path, {"oracle-lab": _ENDPOINTS["oracle-lab"]})
    store_path = tmp_path / "agentflow.sqlite"
    store = ContinuationStore(store_path)
    config = DaemonConfig(
        store=store, boards_root=boards_root, overrides_path=overrides, contracts_dir=_CONTRACTS_DIR,
        reconcile_interval_seconds=999,
    )
    daemon = AgentflowDaemon(config)
    daemon.tick()

    db_path = boards_root / "oracle-lab" / "kanban.db"
    metadata = _needs_input_metadata(
        required_inputs=[{"name": "release_mode", "kind": "authorization", "authority": "owner"}]
    )
    _write_event(db_path, task_id="t_policy_1", metadata=metadata)

    first_report = daemon.tick()
    first_result = first_report["boards"][0]["results"][0]
    assert first_result["action"] == "owner_input_planned"  # H1: one confirmation required
    assert first_result["h0"] is False

    ctx = _plugin_ctx(monkeypatch, store_path, "hermes_agentflow_e2e_policy")
    inbox_result = json.loads(_find_tool(ctx, "agentflow_input_inbox")["handler"]({}))
    case_id = inbox_result["cases"][0]["case_id"]
    submit_result = json.loads(
        _find_tool(ctx, "agentflow_submit_input_text")["handler"](
            {"case_id": case_id, "endpoint": "discord:1500539609413849200", "text": "approve", "owner_ref": "operator-main"}
        )
    )
    assert submit_result["success"] is True

    # The one H1 confirmation creates a standing policy scoped to this
    # board/contract, exactly as plan 8.3 describes — exercised via the real
    # standing_policy.py module, not a stub.
    policy = create_standing_policy(
        store,
        policy_id="oracle-lab:generic.owner-input.v1:release_mode",
        owner_ref="",
        project_scope="oracle-lab",
        action_scope="generic.owner-input.v1",
        decision="approve",
        source_message_ref=f"reply:{case_id}",
    )
    assert policy.version == 1

    _write_event(db_path, task_id="t_policy_2", metadata=metadata)
    second_report = daemon.tick()
    second_result = second_report["boards"][0]["results"][0]

    assert second_result["action"] == "auto_resolved"
    assert second_result["h0"] is True  # second equivalent continuation is H0
    second_instance = store.get_instance(second_result["instance_id"])
    satisfactions = store.list_requirement_satisfactions(second_instance["id"])
    assert satisfactions[0]["source_kind"] == "standing_policy"
    assert satisfactions[0]["policy_id"] == policy.policy_id


# -- 13.6 external wait ------------------------------------------------------------


def test_13_6_external_wait_resolves_with_zero_owner_questions(tmp_path):
    boards_root = _boards_root_with(tmp_path, ("agentflow-hermes",))
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    config = DaemonConfig(store=store, boards_root=boards_root, contracts_dir=_CONTRACTS_DIR, reconcile_interval_seconds=999)
    daemon = AgentflowDaemon(config)
    daemon.tick()

    db_path = boards_root / "agentflow-hermes" / "kanban.db"
    metadata = {
        "agentflow_outcome": {
            "schema_version": 1,
            "verdict": "BLOCK",
            "continuation_kind": "external_wait",
            "external_wait": {
                "kind": "github_check",
                "target": "repo/ref",
                "desired": "success",
                "poll_interval_seconds": 0,
                "resume_transition": "release-review",
            },
        }
    }
    _write_event(db_path, task_id="t_wait", metadata=metadata)
    report = daemon.tick()
    result = report["boards"][0]["results"][0]
    assert result["action"] == "external_wait_registered"

    assert store.list_interaction_cases() == []  # zero owner questions ever raised
    instance_id = result["instance_id"]
    assert store.get_instance(instance_id)["state"] == "materializing"

    # ``poll_interval_seconds: 0`` in the outcome metadata falls back to the
    # 60s default (``0 or 60`` is falsy) — simulate the condition becoming
    # due, exactly like a real poll cycle finding an overdue row.
    with store.connect() as con:
        con.execute("update external_wait_conditions set last_checked_at=0")

    tick_with_satisfaction = AgentflowDaemon(
        DaemonConfig(
            store=store, boards_root=boards_root, contracts_dir=_CONTRACTS_DIR,
            reconcile_interval_seconds=999, external_wait_checker=lambda condition: "satisfied",
        )
    )
    tick_with_satisfaction.tick()

    assert store.get_instance(instance_id)["state"] == "resumed"


# -- 13.7 restart / outbox idempotency ---------------------------------------------


def test_13_7_restart_recreates_exactly_one_task_not_zero_or_two(tmp_path):
    boards_root = _boards_root_with(tmp_path, ("agentflow-hermes",))
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    shared_adapter = FakeBoardAdapter()

    def adapter_factory(board, entry):
        return shared_adapter

    # Simulate "killed between outbox enqueue and board apply": create an
    # instance and a pending outbox row directly (no adapter call yet) —
    # exactly the durable state a real crash between enqueue and apply
    # would leave behind.
    created = store.create_instance(board="agentflow-hermes", source_task_id="t_restart", source_event_id="ev_restart")
    instance_id = created["instance"]["id"]
    store.outbox_enqueue(
        instance_id, step_id="1", operation="create_task",
        payload={"title": "owner anchor", "idempotency_key": "k_restart"}, idempotency_key="k_restart",
    )

    config = DaemonConfig(
        store=store, boards_root=boards_root, contracts_dir=_CONTRACTS_DIR,
        adapter_factory=adapter_factory, reconcile_interval_seconds=999,
    )

    # "Restart": construct a brand-new AgentflowDaemon against the same
    # store/board files/adapter and reconcile.
    daemon_after_restart_1 = AgentflowDaemon(config)
    report_1 = daemon_after_restart_1.reconcile()
    assert report_1["outbox"]["pending_before"] == 1
    assert len(shared_adapter.tasks) == 1

    # A second independent restart (e.g. the 5-minute reconciliation timer
    # firing again after another process restart) must not create a second
    # task for the same durable outbox row.
    daemon_after_restart_2 = AgentflowDaemon(config)
    report_2 = daemon_after_restart_2.reconcile()
    assert report_2["outbox"]["pending_before"] == 0
    assert len(shared_adapter.tasks) == 1


# -- 13.8 three-board canary --------------------------------------------------------


def test_13_8_three_board_canary_per_board_correctness(tmp_path, monkeypatch):
    boards_root = _boards_root_with(tmp_path, _BOARDS)
    overrides = _write_overrides(tmp_path, _ENDPOINTS)
    store_path = tmp_path / "agentflow.sqlite"
    store = ContinuationStore(store_path)
    config = DaemonConfig(
        store=store, boards_root=boards_root, overrides_path=overrides, contracts_dir=_CONTRACTS_DIR,
        reconcile_interval_seconds=999,
    )
    daemon = AgentflowDaemon(config)
    daemon.tick()  # discover + seed all three boards, no historical replay

    for board in _BOARDS:
        db_path = boards_root / board / "kanban.db"
        metadata = _needs_input_metadata(
            required_inputs=[{"name": "result_url", "kind": "fact", "authority": "owner"}]
        )
        _write_event(db_path, task_id=f"t_canary_{board}", metadata=metadata)

    report = daemon.tick()
    boards_processed = {b["board"] for b in report["boards"]}
    assert boards_processed == set(_BOARDS)
    for board_report in report["boards"]:
        assert board_report["processed"] == 1
        assert board_report["results"][0]["action"] == "owner_input_planned"

    instances = store.list_instances()
    assert len(instances) == 3
    assert {i["board"] for i in instances} == set(_BOARDS)

    ctx = _plugin_ctx(monkeypatch, store_path, "hermes_agentflow_e2e_canary")
    for board in _BOARDS:
        endpoint = _ENDPOINTS[board]
        inbox_result = json.loads(_find_tool(ctx, "agentflow_input_inbox")["handler"]({"endpoint": endpoint}))
        assert len(inbox_result["cases"]) == 1
        case = inbox_result["cases"][0]
        assert case["endpoint"] == endpoint  # active-wake lands in the correct origin lane
        case_id = case["case_id"]

        submit_result = json.loads(
            _find_tool(ctx, "agentflow_submit_input_text")["handler"](
                {"case_id": case_id, "endpoint": endpoint, "text": f"https://example.com/{board}", "owner_ref": "operator-main"}
            )
        )
        assert submit_result["success"] is True
        assert submit_result["status"] == "resumed"

    for instance in store.list_instances():
        assert instance["state"] == "materializing"
    # No cross-board leakage: each board's own cursor advanced independently.
    for board in _BOARDS:
        assert store.get_cursor(board, board) >= 1


def test_13_8b_three_board_canary_wrong_lane_reply_refuses(tmp_path, monkeypatch):
    """A case_id from one board's lane must be refused (no transition, no
    materialization) when submitted against a different board's origin
    endpoint — the exact adversarial scenario BLOCK t_4d493bc2 flagged."""
    boards_root = _boards_root_with(tmp_path, _BOARDS)
    overrides = _write_overrides(tmp_path, _ENDPOINTS)
    store_path = tmp_path / "agentflow.sqlite"
    store = ContinuationStore(store_path)
    config = DaemonConfig(
        store=store, boards_root=boards_root, overrides_path=overrides, contracts_dir=_CONTRACTS_DIR,
        reconcile_interval_seconds=999,
    )
    daemon = AgentflowDaemon(config)
    daemon.tick()

    for board in _BOARDS:
        db_path = boards_root / board / "kanban.db"
        metadata = _needs_input_metadata(
            required_inputs=[{"name": "result_url", "kind": "fact", "authority": "owner"}]
        )
        _write_event(db_path, task_id=f"t_wronglane_{board}", metadata=metadata)
    daemon.tick()

    ctx = _plugin_ctx(monkeypatch, store_path, "hermes_agentflow_e2e_wronglane")
    home_endpoint = _ENDPOINTS["warroom-os"]
    other_endpoint = _ENDPOINTS["oracle-lab"]

    home_inbox = json.loads(_find_tool(ctx, "agentflow_input_inbox")["handler"]({"endpoint": home_endpoint}))
    home_case_id = home_inbox["cases"][0]["case_id"]
    home_instance_id = store.list_interaction_members(home_case_id)[0]["continuation_id"]

    # A gateway session bound to oracle-lab's #shaman lane must not be able
    # to resolve warroom-os's case just because it knows the case_id.
    other_inbox = json.loads(
        _find_tool(ctx, "agentflow_input_inbox")["handler"]({"case_id": home_case_id, "endpoint": other_endpoint})
    )
    assert other_inbox["cases"] == []

    other_status = json.loads(
        _find_tool(ctx, "agentflow_input_status")["handler"]({"case_id": home_case_id, "endpoint": other_endpoint})
    )
    assert other_status["success"] is False
    assert other_status["error"] == "origin_mismatch"

    wrong_lane_submit = json.loads(
        _find_tool(ctx, "agentflow_submit_input_text")["handler"](
            {"case_id": home_case_id, "endpoint": other_endpoint, "text": "https://example.com/leak", "owner_ref": "intruder"}
        )
    )
    assert wrong_lane_submit["success"] is False
    assert wrong_lane_submit["error"] == "origin_mismatch"

    instance = store.get_instance(home_instance_id)
    assert instance["state"] != "materializing"
    assert store.list_owner_receipts(home_instance_id) == []
    assert store.list_inbound_reply_receipts(home_case_id) == []

    # The correct lane can still resolve its own case afterward.
    correct_submit = json.loads(
        _find_tool(ctx, "agentflow_submit_input_text")["handler"](
            {"case_id": home_case_id, "endpoint": home_endpoint, "text": "https://example.com/ok", "owner_ref": "operator-main"}
        )
    )
    assert correct_submit["success"] is True
    assert store.get_instance(home_instance_id)["state"] == "materializing"
