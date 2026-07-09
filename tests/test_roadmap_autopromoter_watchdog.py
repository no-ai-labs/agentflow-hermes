from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import agentflow_hermes.roadmap_cli as roadmap_cli
from agentflow_hermes.cli import main as cli_main


def _run(argv, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    captured = io.StringIO()
    with redirect_stdout(captured):
        rc = cli_main(argv)
    text = captured.getvalue()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {"raw": text}
    return rc, data


CONFIG_PAYLOAD = {
    "enabled": True,
    "kill_switch": False,
    "board": "agentflow-hermes",
    "same_board_only": True,
    "apply_mode": True,
    "expected_origin": "discord:#hermes-main",
    "expected_return_to": "discord:#hermes-main",
    "impl_assignee": "impl-agent",
    "review_assignee": "ccreviewer",
    "ack_trigger_agent": True,
    "trusted_assignees": ["ccreviewer"],
    "allowed_transitions": ["m14->m15.impl_review_fanin"],
    "max_chain_depth": 3,
    "max_promotions_per_roadmap": 6,
    "promote_cooldown_seconds": 900,
    "require_review_edge": True,
    "require_ack_edge": True,
    "require_trusted_assignee": True,
    "require_origin_match": True,
    "require_policy_resolution": True,
    "transitions": {
        "m14->m15.impl_review_fanin": {
            "transition_id": "m14->m15.impl_review_fanin",
            "roadmap_id": "hermes.live-migration",
            "from_slice": "m14",
            "to_slice": "m15",
            "slice_template": ["impl", "review", "fanin"],
            "policy_refs": ["design_opus", "implementation_default"],
            "max_chain_depth": 2,
            "version": "template-v1",
        }
    },
}


def _write_config(tmp_path, payload=None, name="roadmap.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload if payload is not None else CONFIG_PAYLOAD), encoding="utf-8")
    return str(path)


def _go_summary(**overrides):
    origin = overrides.get("origin", "discord:#hermes-main")
    return_to = overrides.get("return_to", "discord:#hermes-main")
    lines = [
        f"Verdict: {overrides.get('verdict', 'GO')}",
        f"Origin/return_to: {origin}",
        f"Return-To: {return_to}",
        f"Auto-Continue: {overrides.get('auto', 'true')}",
        f"Roadmap-Transition: {overrides.get('transition', 'm14->m15.impl_review_fanin')}",
    ]
    if "next_slice" not in overrides or overrides["next_slice"] is not None:
        lines.append(f"Next-Slice: {overrides.get('next_slice', 'm15')}")
    lines.extend(["Review-Edge: verified", "ACK-Edge: verified", "Parent-GO: verified"])
    return "\n".join(lines)


class _RecordingCliRunner:
    """Fake injectable CLI runner recording argv shape, no subprocess spawned."""

    def __init__(self, *, tasks=None, final_tasks=None, shows=None):
        self.calls = []
        self.tasks = tasks or {}
        self.final_tasks = final_tasks or []
        self.shows = shows or {}
        self._create_seq = 0

    def __call__(self, argv):
        self.calls.append(list(argv))
        if "show" in argv:
            task_id = argv[argv.index("show") + 1]
            if task_id in self.shows:
                return 0, json.dumps(self.shows[task_id]), ""
            task = self.tasks.get(task_id)
            if task is None:
                return 1, "", "not found"
            return 0, json.dumps({"task": task}), ""
        if "list" in argv:
            return 0, json.dumps({"tasks": [{"id": t} for t in self.final_tasks]}), ""
        if "create" in argv:
            self._create_seq += 1
            task_id = f"t_wd_{self._create_seq}"
            return 0, json.dumps({"success": True, "task_id": task_id}), ""
        raise AssertionError(f"unexpected argv: {argv}")


def _task(**overrides):
    defaults = {
        "id": "t_final_1",
        "result": _go_summary(),
        "origin": "discord:#hermes-main",
        "return_to": "discord:#hermes-main",
        "subscription_status": "verified",
        "policy_resolution_ref": "policy:model.implementation_default@v1",
        "assignee": "ccreviewer",
    }
    defaults.update(overrides)
    return defaults


def test_roadmap_promote_creates_impl_review_fanin_graph_via_real_adapter(monkeypatch, tmp_path):
    runner = _RecordingCliRunner(tasks={"t_final_1": _task()})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    config_path = _write_config(tmp_path)

    rc, data = _run(["roadmap", "promote", "--config", config_path, "--task", "t_final_1", "--apply"], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "stabilize"
    roadmap = data["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap["action"] == "apply"
    assert roadmap["applied"] is True
    assert roadmap["created_task_ids"] == ["t_wd_1", "t_wd_2", "t_wd_3"]
    create_calls = [c for c in runner.calls if "create" in c]
    assert len(create_calls) == 3
    assert all(c[:4] == ["hermes", "kanban", "--board", "agentflow-hermes"] for c in create_calls)


def test_roadmap_promote_reads_summary_from_latest_completed_run_when_task_summary_null(monkeypatch, tmp_path):
    # Mirrors real `hermes kanban show --json` output: task.result/task.summary are
    # null for done tasks, and the actual GO report lives in runs[].summary instead.
    task = _task(id="t_212ab12f", result=None, summary=None)
    show_payload = {
        "task": task,
        "runs": [
            {
                "id": 158,
                "status": "done",
                "outcome": "completed",
                "ended_at": 1783501411,
                "summary": _go_summary(),
            },
            {
                "id": 157,
                "status": "done",
                "outcome": "completed",
                "ended_at": 1783500000,
                "summary": "Verdict: BLOCK — earlier attempt",
            },
        ],
    }
    runner = _RecordingCliRunner(shows={"t_212ab12f": show_payload})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    config_path = _write_config(tmp_path)

    rc, data = _run(["roadmap", "promote", "--config", config_path, "--task", "t_212ab12f"], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "stabilize"
    assert data["verdict"] == "GO"
    roadmap = data["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap["action"] == "propose"
    assert roadmap["applied"] is False
    assert roadmap["created_task_ids"] == []
    assert not any("create" in c for c in runner.calls)


def test_roadmap_promote_prefers_run_summary_over_short_task_result(monkeypatch, tmp_path):
    # Mirrors real `hermes kanban show --json` output where task.result holds a
    # short verdict string (not null) but the rich GO report with the required
    # markers lives in runs[0].summary. The run summary must win so promote
    # doesn't fail with missing_next_slice.
    task = _task(
        id="t_124e64db",
        result="Verdict: GO",
        subscription_status=None,
        policy_resolution_ref=None,
    )
    show_payload = {
        "task": task,
        "runs": [
            {
                "id": 200,
                "status": "done",
                "outcome": "completed",
                "ended_at": 1783501500,
                "summary": _go_summary()
                + "\nSubscription-Status: verified\nPolicy-Resolution-Ref: repo-config:agentflow-roadmap.yaml",
            }
        ],
    }
    runner = _RecordingCliRunner(shows={"t_124e64db": show_payload})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    config_path = _write_config(tmp_path)

    rc, data = _run(["roadmap", "promote", "--config", config_path, "--task", "t_124e64db"], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "stabilize"
    assert data["verdict"] == "GO"
    roadmap = data["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap["action"] == "propose"
    assert roadmap.get("reason") != "missing_next_slice"
    assert roadmap["applied"] is False
    assert roadmap["created_task_ids"] == []
    assert not any("create" in c for c in runner.calls)

    rc_apply, data_apply = _run(
        ["roadmap", "promote", "--config", config_path, "--task", "t_124e64db", "--apply"], monkeypatch, tmp_path
    )
    assert rc_apply == 0
    roadmap_apply = data_apply["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap_apply["action"] == "apply"
    assert roadmap_apply["applied"] is True
    assert roadmap_apply.get("reason") != "missing_next_slice"


def test_roadmap_promote_without_apply_is_request_only_no_create(monkeypatch, tmp_path):
    runner = _RecordingCliRunner(tasks={"t_final_1": _task()})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    config_path = _write_config(tmp_path)

    rc, data = _run(["roadmap", "promote", "--config", config_path, "--task", "t_final_1"], monkeypatch, tmp_path)

    assert rc == 0
    roadmap = data["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap["applied"] is False
    assert roadmap["created_task_ids"] == []
    assert not any("create" in c for c in runner.calls)


def test_roadmap_promote_duplicate_run_with_receipts_file_creates_zero_new_tasks(monkeypatch, tmp_path):
    runner = _RecordingCliRunner(tasks={"t_final_1": _task()})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    config_path = _write_config(tmp_path)
    receipts_path = str(tmp_path / "receipts.json")

    rc1, data1 = _run(
        ["roadmap", "promote", "--config", config_path, "--task", "t_final_1", "--apply", "--receipts-file", receipts_path],
        monkeypatch, tmp_path,
    )
    assert rc1 == 0
    first_create_calls = [c for c in runner.calls if "create" in c]
    assert len(first_create_calls) == 3

    rc2, data2 = _run(
        ["roadmap", "promote", "--config", config_path, "--task", "t_final_1", "--apply", "--receipts-file", receipts_path],
        monkeypatch, tmp_path,
    )
    assert rc2 == 0
    total_create_calls = [c for c in runner.calls if "create" in c]
    assert len(total_create_calls) == 3  # no new create calls on the second run

    roadmap2 = data2["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap2["duplicate"] is True
    assert roadmap2["created_task_ids"] == ["t_wd_1", "t_wd_2", "t_wd_3"]


def test_roadmap_watch_once_scans_and_dedups_across_runs(monkeypatch, tmp_path):
    runner = _RecordingCliRunner(tasks={"t_final_1": _task()}, final_tasks=["t_final_1"])
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    config_path = _write_config(tmp_path)
    receipts_path = str(tmp_path / "receipts.json")

    rc1, data1 = _run(
        ["roadmap", "watch", "--config", config_path, "--once", "--apply", "--receipts-file", receipts_path],
        monkeypatch, tmp_path,
    )
    assert rc1 == 0
    assert data1["scanned"] == 1
    assert data1["created_task_ids"] == ["t_wd_1", "t_wd_2", "t_wd_3"]
    list_calls = [c for c in runner.calls if "list" in c]
    assert list_calls[0] == ["hermes", "kanban", "--board", "agentflow-hermes", "list", "--status", "done", "--json"]
    assert "--verdict" not in list_calls[0]
    first_create_calls = [c for c in runner.calls if "create" in c]
    assert len(first_create_calls) == 3

    rc2, data2 = _run(
        ["roadmap", "watch", "--config", config_path, "--once", "--apply", "--receipts-file", receipts_path],
        monkeypatch, tmp_path,
    )
    assert rc2 == 0
    total_create_calls = [c for c in runner.calls if "create" in c]
    assert len(total_create_calls) == 3  # no new create calls on the second scan
    # The duplicate result still reports the already-created ids, but no new
    # board writes happened (verified above via total_create_calls).
    assert data2["created_task_ids"] == ["t_wd_1", "t_wd_2", "t_wd_3"]


def test_roadmap_watch_requires_once_flag(monkeypatch, tmp_path):
    runner = _RecordingCliRunner()
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    config_path = _write_config(tmp_path)

    rc, data = _run(["roadmap", "watch", "--config", config_path, "--apply"], monkeypatch, tmp_path)

    assert rc != 0
    assert data["success"] is False
    assert data["error"] == "continuous_watch_not_supported"
    assert runner.calls == []


def test_roadmap_promote_disabled_config_kill_switch_no_board_call(monkeypatch, tmp_path):
    runner = _RecordingCliRunner(tasks={"t_final_1": _task()})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    payload = dict(CONFIG_PAYLOAD)
    payload["enabled"] = False
    config_path = _write_config(tmp_path, payload)

    rc, data = _run(["roadmap", "promote", "--config", config_path, "--task", "t_final_1", "--apply"], monkeypatch, tmp_path)

    assert rc == 0
    assert data["success"] is False
    assert data["reason"] == "config_disabled"
    assert data["created_task_ids"] == []
    assert runner.calls == []


def test_roadmap_promote_block_verdict_no_create(monkeypatch, tmp_path):
    task = _task(result="Verdict: BLOCK — needs more work")
    runner = _RecordingCliRunner(tasks={"t_final_1": task})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    config_path = _write_config(tmp_path)

    rc, data = _run(["roadmap", "promote", "--config", config_path, "--task", "t_final_1", "--apply"], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "escalate"
    assert data["mutations"] == []
    assert not any("create" in c for c in runner.calls)


def test_roadmap_promote_missing_next_slice_no_create(monkeypatch, tmp_path):
    task = _task(result=_go_summary(next_slice=None))
    runner = _RecordingCliRunner(tasks={"t_final_1": task})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    config_path = _write_config(tmp_path)

    rc, data = _run(["roadmap", "promote", "--config", config_path, "--task", "t_final_1", "--apply"], monkeypatch, tmp_path)

    assert rc == 0
    roadmap = data["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap["applied"] is False
    assert roadmap["created_task_ids"] == []
    assert not any("create" in c for c in runner.calls)


def test_roadmap_promote_missing_auto_continue_no_create(monkeypatch, tmp_path):
    task = _task(result=_go_summary(auto="false"))
    runner = _RecordingCliRunner(tasks={"t_final_1": task})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    config_path = _write_config(tmp_path)

    rc, data = _run(["roadmap", "promote", "--config", config_path, "--task", "t_final_1", "--apply"], monkeypatch, tmp_path)

    assert rc == 0
    roadmap = data["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap["applied"] is False
    assert roadmap["created_task_ids"] == []
    assert not any("create" in c for c in runner.calls)


def test_roadmap_promote_non_allowlisted_transition_no_create(monkeypatch, tmp_path):
    task = _task(result=_go_summary(transition="unknown.transition"))
    runner = _RecordingCliRunner(tasks={"t_final_1": task})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    config_path = _write_config(tmp_path)

    rc, data = _run(["roadmap", "promote", "--config", config_path, "--task", "t_final_1", "--apply"], monkeypatch, tmp_path)

    assert rc == 0
    roadmap = data["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap["applied"] is False
    assert roadmap["created_task_ids"] == []
    assert not any("create" in c for c in runner.calls)


def test_roadmap_promote_cross_board_never_used_show_or_create_use_config_board(monkeypatch, tmp_path):
    runner = _RecordingCliRunner(tasks={"t_final_1": _task()})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    config_path = _write_config(tmp_path)

    _run(["roadmap", "promote", "--config", config_path, "--task", "t_final_1", "--apply"], monkeypatch, tmp_path)

    for call in runner.calls:
        assert call[:4] == ["hermes", "kanban", "--board", "agentflow-hermes"]


def test_roadmap_promote_no_board_client_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: None)
    config_path = _write_config(tmp_path)

    rc, data = _run(["roadmap", "promote", "--config", config_path, "--task", "t_final_1", "--apply"], monkeypatch, tmp_path)

    assert rc != 0
    assert data["success"] is False
    assert data["error"] == "no_board_client"


def test_roadmap_promote_committed_yaml_config_loads_and_evaluates(monkeypatch, tmp_path):
    task = _task(
        result=_go_summary(
            transition="m16->m17.impl_review_fanin",
            next_slice="m17",
            origin="Discord Devhub / #hermes-main",
            return_to="Discord Devhub / #hermes-main",
        ),
        origin="Discord Devhub / #hermes-main",
        return_to="Discord Devhub / #hermes-main",
        assignee="ccreviewer",
    )
    runner = _RecordingCliRunner(tasks={"t_final_1": task})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    repo_config_path = str((__import__("pathlib").Path(__file__).parent.parent / "agentflow-roadmap.yaml").resolve())

    rc, data = _run(["roadmap", "promote", "--config", repo_config_path, "--task", "t_final_1", "--apply"], monkeypatch, tmp_path)

    assert rc == 0
    roadmap = data["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap["applied"] is True
    assert len(roadmap["created_task_ids"]) == 3
    create_calls = [c for c in runner.calls if "create" in c]
    assert all(c[:4] == ["hermes", "kanban", "--board", "agentflow-hermes"] for c in create_calls)


def test_roadmap_promote_research_preset_request_only_canary(monkeypatch, tmp_path):
    payload = dict(CONFIG_PAYLOAD)
    payload.update({
        "board": "warroom-os",
        "expected_origin": "Discord Devhub / #research",
        "expected_return_to": "Discord Devhub / #research",
        "allowed_transitions": ["research.default.scout_evidence_scorecard_review_brief"],
        "transitions": {
            "research.default.scout_evidence_scorecard_review_brief": {
                "roadmap_id": "research.roadmap",
                "from_slice": "research-current",
                "to_slice": "research-next",
                "template_preset": "research-loop",
                "slice_template": ["scout", "evidence", "scorecard", "review", "brief"],
                "policy_refs": ["design_opus", "implementation_default"],
                "max_chain_depth": 2,
                "version": "template-v2",
            }
        },
    })
    task = _task(
        result=_go_summary(
            transition="research.default.scout_evidence_scorecard_review_brief",
            next_slice="research-next",
            origin="Discord Devhub / #research",
            return_to="Discord Devhub / #research",
        ),
        origin="Discord Devhub / #research",
        return_to="Discord Devhub / #research",
    )
    runner = _RecordingCliRunner(tasks={"t_final_research": task})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    config_path = _write_config(tmp_path, payload, name="research-roadmap.json")

    rc, data = _run(["roadmap", "promote", "--config", config_path, "--task", "t_final_research"], monkeypatch, tmp_path)

    assert rc == 0
    roadmap = data["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap["action"] == "propose"
    assert roadmap["applied"] is False
    assert [c["kind"] for c in data["proposal"]["candidates"]] == ["scout", "evidence", "scorecard", "review", "brief"]
    assert "#research" in data["proposal"]["candidates"][0]["body"]
    assert not any("create" in c for c in runner.calls)


def test_roadmap_promote_shaman_preset_request_only_canary(monkeypatch, tmp_path):
    payload = dict(CONFIG_PAYLOAD)
    payload.update({
        "board": "oracle-lab",
        "expected_origin": "Discord Devhub / #shaman",
        "expected_return_to": "Discord Devhub / #shaman",
        "allowed_transitions": ["shaman.default.design_impl_browser_review_fanin"],
        "transitions": {
            "shaman.default.design_impl_browser_review_fanin": {
                "roadmap_id": "shaman.roadmap",
                "from_slice": "shaman-current",
                "to_slice": "shaman-next",
                "template_preset": "shaman-loop",
                "slice_template": ["design", "impl", "browser_e2e", "review", "fanin"],
                "policy_refs": ["design_opus", "implementation_default"],
                "max_chain_depth": 2,
                "version": "template-v2",
            }
        },
    })
    task = _task(
        result=_go_summary(
            transition="shaman.default.design_impl_browser_review_fanin",
            next_slice="shaman-next",
            origin="Discord Devhub / #shaman",
            return_to="Discord Devhub / #shaman",
        ),
        origin="Discord Devhub / #shaman",
        return_to="Discord Devhub / #shaman",
    )
    runner = _RecordingCliRunner(tasks={"t_final_shaman": task})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)
    config_path = _write_config(tmp_path, payload, name="shaman-roadmap.json")

    rc, data = _run(["roadmap", "promote", "--config", config_path, "--task", "t_final_shaman"], monkeypatch, tmp_path)

    assert rc == 0
    roadmap = data["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap["action"] == "propose"
    assert roadmap["applied"] is False
    assert [c["kind"] for c in data["proposal"]["candidates"]] == ["design", "impl", "browser_e2e", "review", "fanin"]
    assert "browser_e2e" in data["proposal"]["candidates"][2]["body"]
    assert not any("create" in c for c in runner.calls)
