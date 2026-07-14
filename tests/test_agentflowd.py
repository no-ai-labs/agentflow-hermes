from __future__ import annotations

import asyncio
import json
import sqlite3

from agentflow_hermes.board_adapter import FakeBoardAdapter
from agentflow_hermes.board_events import BoardEvent, FakeBoardEventSource
from agentflow_hermes.continuation_config import load_contract_registry
from agentflow_hermes.continuation_store import ContinuationStore
from agentflow_hermes.graph_creator import FakeKanbanGraphAdapter
from agentflow_hermes.roadmap_config import load_repo_roadmap_config
from agentflow_hermes.daemon import (
    AgentflowDaemon,
    DaemonConfig,
    SingleInstanceLock,
    discover_boards,
    poll_external_wait_conditions,
    reconcile_outbox,
    route_board_events,
)
from agentflow_hermes.board_events import BoardRegistryEntry
from agentflow_hermes import service_install
from agentflow_hermes.outcome import GENERIC_OWNER_INPUT_CONTRACT
from pathlib import Path as _Path

_GENERIC_CONTRACT_YAML = _Path(__file__).resolve().parents[1] / "contracts" / "generic.owner-input.v1.yaml"


def _contracts_with_generic():
    return load_contract_registry([_GENERIC_CONTRACT_YAML])


def _fake_source_factory(events):
    def factory(board, entry):
        return FakeBoardEventSource(db_identity=f"{board}-db", events=events)

    return factory


def _route_primed(*, board, entry, store, contract_registry, adapter, events, **kwargs):
    """route_board_events seeds a never-before-seen board's cursor to its
    current max event id on first call (no historical replay, plan 2.6/9.1).
    Tests that want to observe a fresh event being *processed* must prime the
    cursor at 0 first, exactly like a board agentflowd already knows about."""
    store.advance_cursor(board, f"{board}-db", 0)
    return route_board_events(
        board=board, entry=entry, store=store, contract_registry=contract_registry, adapter=adapter,
        source_factory=_fake_source_factory(events), **kwargs,
    )


def _go_event() -> BoardEvent:
    return BoardEvent(
        event_id="ev_go",
        event_seq=1,
        source_task_id="t_go",
        source_graph_id="g_1",
        summary="Verdict: GO",
    )


def _roadmap_go_event() -> BoardEvent:
    return BoardEvent(
        event_id="ev_go",
        event_seq=1,
        source_task_id="t_go",
        source_graph_id="g_1",
        assignee="ccreviewer",
        summary="""Verdict: GO
Roadmap-Transition: b1.default.impl_review
Next-Slice: next
Review-Edge: verified
Ack-Edge: verified
Parent-GO: verified
Auto-Continue: true
Origin/return_to: Discord Devhub / #b1
Return-To: Discord Devhub / #b1
Subscription-Status: verified
Policy-Resolution-Ref: policy:test
""",
    )


def _roadmap_config_file(tmp_path) -> str:
    path = tmp_path / "roadmap.yaml"
    path.write_text(
        """enabled: true
kill_switch: false
board: b1
same_board_only: true
apply_mode: true
expected_origin: "Discord Devhub / #b1"
expected_return_to: "Discord Devhub / #b1"
impl_assignee: ccsupervisor
review_assignee: ccreviewer
ack_trigger_agent: true
trusted_assignees:
  - ccreviewer
allowed_transitions:
  - b1.default.impl_review
max_chain_depth: 3
max_promotions_per_roadmap: 6
promote_cooldown_seconds: 0
require_review_edge: true
require_ack_edge: true
require_trusted_assignee: true
require_origin_match: true
require_policy_resolution: true
transitions:
  b1.default.impl_review:
    roadmap_id: b1.roadmap
    from_slice: current
    to_slice: next
    slice_template:
      - impl
      - review
      - fanin
    policy_refs:
      - design_opus
      - implementation_default
    max_chain_depth: 3
    version: template-v1
""",
        encoding="utf-8",
    )
    return str(path)


def _code_fix_event() -> BoardEvent:
    return BoardEvent(
        event_id="ev_fix",
        event_seq=1,
        source_task_id="t_fix",
        source_graph_id="g_1",
        summary="Verdict: BLOCK — stale_inline_route detected",
    )


def _needs_input_event(*, requirements=None, extra_metadata=None) -> BoardEvent:
    metadata = {
        "agentflow_outcome": {
            "schema_version": 1,
            "verdict": "BLOCK",
            "continuation_kind": "needs_input",
            "required_inputs": requirements or [{"name": "result_url", "kind": "fact", "authority": "owner"}],
        }
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return BoardEvent(
        event_id="ev_ni",
        event_seq=1,
        source_task_id="t_ni",
        source_graph_id="g_1",
        run_metadata=metadata,
        origin_ref="discord:1234",
        return_to_ref="discord:1234",
    )


# -- board discovery ---------------------------------------------------------


def test_discover_boards_scans_root_and_auto_enrolls(tmp_path):
    root = tmp_path / "boards"
    for board in ("alpha", "beta"):
        (root / board).mkdir(parents=True)
        sqlite3.connect(root / board / "kanban.db").close()
    # non-board directory (no kanban.db) must be ignored.
    (root / "not-a-board").mkdir(parents=True)

    registry = discover_boards(boards_root=root)

    assert set(registry) == {"alpha", "beta"}
    assert all(entry.enabled for entry in registry.values())


def test_discover_boards_respects_disable_override(tmp_path):
    root = tmp_path / "boards"
    for board in ("alpha", "beta"):
        (root / board).mkdir(parents=True)
        sqlite3.connect(root / board / "kanban.db").close()
    overrides = tmp_path / "boards.yaml"
    overrides.write_text("boards:\n  alpha:\n    enabled: false\n    default_endpoint: discord:999\n")

    registry = discover_boards(boards_root=root, overrides_path=overrides)

    assert set(registry) == {"beta"}


def test_discover_boards_applies_endpoint_override(tmp_path):
    root = tmp_path / "boards"
    (root / "alpha").mkdir(parents=True)
    sqlite3.connect(root / "alpha" / "kanban.db").close()
    overrides = tmp_path / "boards.yaml"
    overrides.write_text("boards:\n  alpha:\n    default_endpoint: discord:555\n")

    registry = discover_boards(boards_root=root, overrides_path=overrides)

    assert registry["alpha"].default_endpoint == "discord:555"


def test_discover_boards_preserves_roadmap_transition_config_override(tmp_path):
    root = tmp_path / "boards"
    (root / "alpha").mkdir(parents=True)
    sqlite3.connect(root / "alpha" / "kanban.db").close()
    overrides = tmp_path / "boards.yaml"
    overrides.write_text(
        "boards:\n  alpha:\n    roadmap_config_path: /tmp/alpha-roadmap.yaml\n    roadmap_receipts_file: /tmp/alpha-receipts.json\n"
    )

    registry = discover_boards(boards_root=root, overrides_path=overrides)

    assert registry["alpha"].roadmap_config_path == "/tmp/alpha-roadmap.yaml"
    assert registry["alpha"].roadmap_receipts_file == "/tmp/alpha-receipts.json"


def test_discover_boards_missing_root_returns_empty(tmp_path):
    assert discover_boards(boards_root=tmp_path / "does-not-exist") == {}


# -- unified handler router --------------------------------------------------


def test_router_routes_go_and_code_fix_and_needs_input(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    contracts = _contracts_with_generic()
    entry = BoardRegistryEntry(board="b1", db_identity="b1", default_endpoint="discord:1")

    go_result = _route_primed(board="b1", entry=entry, store=store, contract_registry=contracts, adapter=adapter, events=[_go_event()])
    assert go_result["results"][0]["action"] == "roadmap_routed"

    store2 = ContinuationStore(tmp_path / "agentflow2.sqlite")
    fix_result = _route_primed(board="b1", entry=entry, store=store2, contract_registry=contracts, adapter=adapter, events=[_code_fix_event()])
    assert fix_result["results"][0]["action"] == "code_fix_routed"


def test_router_go_without_transition_config_is_deterministic_noop(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    contracts = _contracts_with_generic()
    entry = BoardRegistryEntry(board="b1", db_identity="b1", default_endpoint="discord:1")

    result = _route_primed(board="b1", entry=entry, store=store, contract_registry=contracts, adapter=adapter, events=[_go_event()])

    item = result["results"][0]
    assert item["action"] == "roadmap_routed"
    assert item["roadmap"]["reason"] == "no_transition_config"
    assert item["roadmap"]["created_task_ids"] == []


def test_router_configured_go_applies_graph_and_replay_is_deduped(tmp_path):
    receipts = tmp_path / "receipts.json"
    roadmap_config = load_repo_roadmap_config(_roadmap_config_file(tmp_path))
    contracts = _contracts_with_generic()
    adapter = FakeKanbanGraphAdapter()
    entry = BoardRegistryEntry(board="b1", db_identity="b1", default_endpoint="discord:1")

    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    result = _route_primed(
        board="b1", entry=entry, store=store, contract_registry=contracts, adapter=FakeBoardAdapter(),
        events=[_roadmap_go_event()], roadmap_config=roadmap_config, roadmap_receipts_file=str(receipts),
        roadmap_graph_adapter=adapter, apply=True,
    )

    roadmap = result["results"][0]["roadmap"]
    assert roadmap["action"] == "apply"
    assert roadmap["applied"] is True
    assert len(roadmap["created_task_ids"]) == 3
    assert len(adapter.create_calls) == 3
    saved = json.loads(receipts.read_text(encoding="utf-8"))
    assert roadmap["idempotency_key"] in saved

    replay_store = ContinuationStore(tmp_path / "agentflow-replay.sqlite")
    replay_adapter = FakeKanbanGraphAdapter()
    replay = _route_primed(
        board="b1", entry=entry, store=replay_store, contract_registry=contracts, adapter=FakeBoardAdapter(),
        events=[_roadmap_go_event()], roadmap_config=roadmap_config, roadmap_receipts_file=str(receipts),
        roadmap_graph_adapter=replay_adapter, apply=True,
    )

    replay_roadmap = replay["results"][0]["roadmap"]
    assert replay_roadmap["reason"] == "duplicate_graph"
    assert replay_roadmap["created_task_ids"] == roadmap["created_task_ids"]
    assert replay_adapter.create_calls == []


def test_router_needs_input_h1_creates_owner_anchor_when_unresolved(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    contracts = _contracts_with_generic()
    entry = BoardRegistryEntry(board="b1", db_identity="b1", default_endpoint="discord:1")

    result = _route_primed(board="b1", entry=entry, store=store, contract_registry=contracts, adapter=adapter, events=[_needs_input_event()])

    item = result["results"][0]
    assert item["action"] == "owner_input_planned"
    assert item["h0"] is False
    assert len(adapter.tasks) == 1
    instances = store.list_instances()
    assert instances[0]["state"] == "waiting_owner"


def test_router_needs_input_h0_auto_resolves_with_zero_owner_anchor(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    contracts = _contracts_with_generic()
    entry = BoardRegistryEntry(board="b1", db_identity="b1", default_endpoint="discord:1")
    event = _needs_input_event(extra_metadata={"system_derived": {"result_url": "https://evidence.example/x"}})

    result = _route_primed(board="b1", entry=entry, store=store, contract_registry=contracts, adapter=adapter, events=[event])

    item = result["results"][0]
    assert item["action"] == "auto_resolved"
    assert item["h0"] is True
    # H0 per plan 4.4/13.2: no owner anchor at all — create_task only fires
    # for the auto materialization step, not a blocked owner-input anchor.
    assert not any(t.get("status") == "blocked" for t in adapter.tasks.values())
    instances = store.list_instances()
    satisfactions = store.list_requirement_satisfactions(instances[0]["id"])
    assert satisfactions[0]["source_kind"] == "system_derived"


def test_router_external_wait_registers_condition_and_never_asks_owner(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    contracts = _contracts_with_generic()
    entry = BoardRegistryEntry(board="b1", db_identity="b1")
    event = BoardEvent(
        event_id="ev_wait",
        event_seq=1,
        source_task_id="t_wait",
        source_graph_id="g_1",
        run_metadata={
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
        },
    )

    result = _route_primed(board="b1", entry=entry, store=store, contract_registry=contracts, adapter=adapter, events=[event])

    assert result["results"][0]["action"] == "external_wait_registered"
    assert adapter.tasks == {}
    assert adapter.blocked == []

    poll_report = poll_external_wait_conditions(store, checker=lambda condition: "satisfied")
    assert poll_report["checked"][0]["outcome"] == "satisfied"
    instance = store.get_instance(result["results"][0]["instance_id"])
    assert instance["state"] == "resumed"


def test_unknown_outcome_is_noop(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    contracts = _contracts_with_generic()
    entry = BoardRegistryEntry(board="b1", db_identity="b1")
    event = BoardEvent(event_id="ev_vague", event_seq=1, source_task_id="t_1", source_graph_id="g_1", summary="hmm")

    result = _route_primed(board="b1", entry=entry, store=store, contract_registry=contracts, adapter=adapter, events=[event])

    assert result["results"][0]["action"] == "noop"
    assert store.list_instances() == []


# -- outbox reconciliation ---------------------------------------------------


def test_reconcile_outbox_retries_pending_rows(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    created = store.create_instance(board="b1", source_task_id="t_1", source_event_id="ev_1", source_graph_id="g_1")
    instance_id = created["instance"]["id"]
    store.outbox_enqueue(
        instance_id, step_id="1", operation="create_task",
        payload={"title": "x", "idempotency_key": "k1"}, idempotency_key="k1",
    )
    adapter = FakeBoardAdapter()

    report = reconcile_outbox(store, adapter_by_board={"b1": adapter})

    assert report["pending_before"] == 1
    assert report["retried"] == ["k1"]
    assert [row["state"] for row in store.list_outbox()] == ["applied"]


def test_reconcile_outbox_is_idempotent_on_restart(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    created = store.create_instance(board="b1", source_task_id="t_1", source_event_id="ev_1", source_graph_id="g_1")
    instance_id = created["instance"]["id"]
    adapter = FakeBoardAdapter()
    store.outbox_enqueue(
        instance_id, step_id="1", operation="create_task",
        payload={"title": "x", "idempotency_key": "k1"}, idempotency_key="k1",
    )

    reconcile_outbox(store, adapter_by_board={"b1": adapter})
    # Simulate a daemon restart: reconcile again. No duplicate task created.
    reconcile_outbox(store, adapter_by_board={"b1": adapter})

    assert len(adapter.tasks) == 1


# -- single instance lock ----------------------------------------------------


def test_single_instance_lock_prevents_second_acquire(tmp_path):
    lock_path = tmp_path / "run" / "agentflowd.pid"
    lock1 = SingleInstanceLock(lock_path)
    lock2 = SingleInstanceLock(lock_path)

    assert lock1.acquire() is True
    assert lock2.acquire() is False
    lock1.release()
    assert lock2.acquire() is True
    lock2.release()


# -- AgentflowDaemon tick/reconcile -------------------------------------------


def test_daemon_tick_processes_discovered_board(tmp_path):
    boards_root = tmp_path / "boards"
    (boards_root / "alpha").mkdir(parents=True)
    db_path = boards_root / "alpha" / "kanban.db"
    _seed_live_kanban_db(db_path)

    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    config = DaemonConfig(store=store, boards_root=boards_root, contracts_dir=None)
    daemon = AgentflowDaemon(config)

    # First tick discovers "alpha" for the first time and seeds its cursor to
    # the current max event id (no historical replay, plan 2.6/9.1) — the
    # pre-existing "GO" event written by _seed_live_kanban_db is history at
    # discovery time, not a live wake, so it must not be processed here.
    seed_report = daemon.tick()
    assert seed_report["boards"][0]["seeded_cursor"] >= 1
    assert store.list_instances() == []

    # A fresh event after discovery IS processed on the next tick.
    _write_live_event(db_path, task_id="t2", summary="Verdict: GO")
    report = daemon.tick()

    assert report["success"] is True
    assert report["boards"][0]["board"] == "alpha"
    assert report["boards"][0]["processed"] == 1
    # GO/CODE_FIX route directly through graph_creator (no continuation_instances
    # row, same as the pre-existing continuation_engine.ingest_board_once
    # behavior) — the material proof here is the router dispatch itself.
    assert report["boards"][0]["results"][0]["action"] == "roadmap_routed"


def test_daemon_reconcile_replays_outbox(tmp_path):
    boards_root = tmp_path / "boards"
    (boards_root / "alpha").mkdir(parents=True)
    _seed_live_kanban_db(boards_root / "alpha" / "kanban.db")
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    config = DaemonConfig(store=store, boards_root=boards_root, contracts_dir=None)
    daemon = AgentflowDaemon(config)

    report = daemon.reconcile()

    assert "outbox" in report
    assert report["outbox"]["pending_before"] == 0


def test_daemon_run_loop_stops_after_max_ticks(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    config = DaemonConfig(
        store=store, boards_root=tmp_path / "empty-boards", poll_interval_seconds=0.01,
        reconcile_interval_seconds=999,
    )
    daemon = AgentflowDaemon(config)

    asyncio.run(daemon.run(max_ticks=3))
    # No assertion needed beyond "returns" — proves the loop terminates
    # cleanly without a real signal and without hanging.


def _seed_live_kanban_db(path):
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
    con.execute("insert into tasks(id, title, assignee) values('t1', 'demo task', 'agent')")
    con.execute("insert into task_runs(id, step_key, summary, metadata) values('r1', 'g1', 'Verdict: GO', '{}')")
    con.execute(
        "insert into task_events(task_id, run_id, kind, payload, created_at) values('t1', 'r1', 'completed', '{}', 0)"
    )
    con.commit()
    con.close()


def _write_live_event(path, *, task_id: str, summary: str) -> None:
    con = sqlite3.connect(path)
    con.execute("insert or replace into tasks(id, title, assignee) values(?, 'demo task', 'agent')", (task_id,))
    con.execute(
        "insert into task_runs(id, step_key, summary, metadata) values(?, 'g1', ?, '{}')", (f"{task_id}-run", summary)
    )
    con.execute(
        "insert into task_events(task_id, run_id, kind, payload, created_at) values(?, ?, 'completed', '{}', 0)",
        (task_id, f"{task_id}-run"),
    )
    con.commit()
    con.close()


# -- service install ----------------------------------------------------------


def test_service_install_render_never_writes(tmp_path):
    script = tmp_path / "agentflowd.py"
    script.write_text("# stub\n")

    plan = service_install.render_install_plan(str(script))

    assert plan["success"] is True
    assert service_install.SERVICE_NAME in plan["units"]
    assert "ExecStart=" in plan["units"][service_install.SERVICE_NAME]
    assert "agentflow-daemon.sqlite" not in plan["units"][service_install.SERVICE_NAME]
    assert "agentflow.sqlite" not in plan["units"][service_install.SERVICE_NAME]
    assert not (tmp_path / service_install.SERVICE_NAME).exists()


def test_service_install_write_files_requires_unit_dir(tmp_path):
    script = tmp_path / "agentflowd.py"
    script.write_text("# stub\n")

    try:
        service_install.install(str(script), write_files=True)
        assert False, "expected ServiceRenderError"
    except service_install.ServiceRenderError:
        pass


def test_service_install_status_and_uninstall_roundtrip(tmp_path):
    script = tmp_path / "agentflowd.py"
    script.write_text("# stub\n")
    unit_dir = tmp_path / "units"

    plan = service_install.install(str(script), unit_dir=str(unit_dir), write_files=True)
    assert plan["wrote_files"] is True

    status = service_install.status(str(unit_dir))
    assert status["fully_installed"] is True

    dry = service_install.uninstall(str(unit_dir))
    assert dry["dry_run"] is True
    assert set(dry["present"]) == {
        service_install.SERVICE_NAME, service_install.RECONCILE_SERVICE_NAME, service_install.RECONCILE_TIMER_NAME,
    }
    assert (unit_dir / service_install.SERVICE_NAME).exists()

    real = service_install.uninstall(str(unit_dir), write=True)
    assert set(real["removed"]) == set(dry["present"])
    assert not (unit_dir / service_install.SERVICE_NAME).exists()


def test_service_install_extra_args_applied_to_both_service_units(tmp_path):
    script = tmp_path / "agentflowd.py"
    script.write_text("# stub\n")

    plan = service_install.render_install_plan(str(script), extra_args="--boards-root /x --db /y/agentflow.sqlite")

    service_unit = plan["units"][service_install.SERVICE_NAME]
    reconcile_unit = plan["units"][service_install.RECONCILE_SERVICE_NAME]
    assert "--boards-root /x --db /y/agentflow.sqlite" in service_unit
    assert "--boards-root /x --db /y/agentflow.sqlite" in reconcile_unit


def test_service_install_enable_requires_units_present(tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    try:
        service_install.enable(unit_dir=str(unit_dir))
        assert False, "expected ServiceRenderError for missing unit files"
    except service_install.ServiceRenderError:
        pass


def test_service_install_enable_dry_run_never_calls_systemctl(tmp_path, monkeypatch):
    script = tmp_path / "agentflowd.py"
    script.write_text("# stub\n")
    unit_dir = tmp_path / "units"
    service_install.install(str(script), unit_dir=str(unit_dir), write_files=True)

    def _boom(*a, **kw):
        raise AssertionError("dry-run enable must never call subprocess.run")

    monkeypatch.setattr(service_install.subprocess, "run", _boom)

    result = service_install.enable(unit_dir=str(unit_dir))
    assert result["applied"] is False
    assert result["commands"][0] == ["systemctl", "--user", "daemon-reload"]
    assert result["commands"][1][:3] == ["systemctl", "--user", "enable"]
    assert service_install.SERVICE_NAME in result["commands"][1]
    assert service_install.RECONCILE_TIMER_NAME in result["commands"][1]
    assert service_install.RECONCILE_SERVICE_NAME not in result["commands"][1], (
        "the reconcile oneshot service must only ever run via its timer, never enabled directly"
    )


def test_service_install_enable_apply_invokes_systemctl(tmp_path, monkeypatch):
    script = tmp_path / "agentflowd.py"
    script.write_text("# stub\n")
    unit_dir = tmp_path / "units"
    service_install.install(str(script), unit_dir=str(unit_dir), write_files=True)

    calls = []

    class _FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(command, **kw):
        calls.append(command)
        return _FakeProc()

    monkeypatch.setattr(service_install.subprocess, "run", _fake_run)

    result = service_install.enable(unit_dir=str(unit_dir), apply=True, now=True)
    assert result["success"] is True
    assert result["applied"] is True
    assert len(calls) == 2
    assert "--now" in calls[1]
    assert len(result["results"]) == 2
