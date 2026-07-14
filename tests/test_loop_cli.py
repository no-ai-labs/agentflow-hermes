from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

import agentflow_hermes.loop_cli as loop_cli
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


def _write_fixture(tmp_path, payload, name="fixture.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


SAFE_POLICY = {
    "active_mode": "request_only",
    "allowlisted_blockers": ["stale_inline_route", "missing_subscription", "stale_final_fanin"],
    "expected_origin": "discord:#hermes-main",
    "expected_return_to": "discord:#hermes-main",
    "cooldown_seconds": 900,
}


def _roadmap_registry_fixture():
    return {
        "version": "test-v1",
        "source_ref": "test-registry",
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


def _go_summary(**overrides):
    lines = [
        f"Verdict: {overrides.get('verdict', 'GO')}",
        "Origin/return_to: discord:#hermes-main",
        "Return-To: discord:#hermes-main",
        f"Auto-Continue: {overrides.get('auto', 'true')}",
        f"Roadmap-Transition: {overrides.get('transition', 'm14->m15.impl_review_fanin')}",
    ]
    if "next_slice" not in overrides or overrides["next_slice"] is not None:
        lines.append(f"Next-Slice: {overrides.get('next_slice', 'm15')}")
    lines.extend(["Review-Edge: verified", "ACK-Edge: verified", "Parent-GO: verified"])
    return "\n".join(lines)


class _RecordingCliRunner:
    """Fake injectable CLI runner recording argv shape, no subprocess spawned."""

    def __init__(self) -> None:
        self.calls = []
        self._create_seq = 0

    def __call__(self, argv):
        self.calls.append(list(argv))
        if "notify-subscribe" in argv:
            return 0, "", ""
        self._create_seq += 1
        task_id = f"t_cli_real_{self._create_seq}"
        return 0, json.dumps({"success": True, "task_id": task_id}), ""


def _event(**kwargs):
    defaults = {
        "event_id": "evt-1",
        "source_graph_id": "graph-1",
        "source_task_id": "t_source",
        "origin": "discord:#hermes-main",
        "return_to": "discord:#hermes-main",
        "subscription_status": "verified",
        "policy_resolution_ref": "policy:model.implementation_default@v1",
        "occurred_at": 1000.0,
    }
    defaults.update(kwargs)
    return defaults


def test_cli_loop_evaluate_default_block_is_request_only_proposal_no_mutation(monkeypatch, tmp_path):
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route old route", blocker_class="stale_inline_route"),
        "policy": SAFE_POLICY,
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc == 0
    assert data["success"] is True
    assert data["action"] == "propose"
    assert data["dry_run"] is True
    assert data["applied"] is False
    assert data["mutations"] == []
    assert data["adapter_attempts"] == 0
    assert data["proposal"]["candidate_count"] > 0


def test_cli_loop_evaluate_go_returns_stop_stable(monkeypatch, tmp_path):
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="GO", summary="Verdict: GO"),
        "policy": SAFE_POLICY,
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "stabilize"
    assert data["dry_run"] is True
    assert data["applied"] is False
    assert data["mutations"] == []


def test_cli_loop_evaluate_go_roadmap_apply_fixture_creates_fake_graph(monkeypatch, tmp_path):
    policy = dict(SAFE_POLICY)
    policy.update({
        "active_mode": "apply",
        "apply_enabled": True,
        "roadmap_auto_continue": True,
        "roadmap_allowlisted_transitions": ["m14->m15.impl_review_fanin"],
        "roadmap_trusted_assignees": ["ccreviewer"],
        "roadmap_apply_enabled": True,
        "roadmap_impl_assignee": "impl-agent",
        "roadmap_review_assignee": "ccreviewer",
        "roadmap_ack_trigger_agent": "ack-agent",
    })
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="GO", summary=_go_summary(), source_final_id="t_final_cli", source_assignee="ccreviewer"),
        "policy": policy,
        "roadmap_registry": _roadmap_registry_fixture(),
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "stabilize"
    receipt = data["receipt"]
    assert isinstance(receipt, dict)
    roadmap = receipt["decision_payload"]["roadmap_autopromote"]
    assert isinstance(roadmap, dict)
    assert roadmap["action"] == "apply"
    assert roadmap["applied"] is True
    assert len(roadmap["created_task_ids"]) == 3
    assert "roadmap-autopromote applied" in roadmap["source_comment"]


def test_cli_loop_evaluate_need_more_escalates_no_auto_create(monkeypatch, tmp_path):
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="NEED_MORE", summary="Verdict: NEED_MORE — operator choice needed"),
        "policy": SAFE_POLICY,
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "escalate"
    assert data["reason"] == "needs_input"
    assert data["applied"] is False
    assert data["adapter_attempts"] == 0
    assert data["mutations"] == []


def test_cli_loop_evaluate_max_rounds_stops(monkeypatch, tmp_path):
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route", round_no=2),
        "policy": SAFE_POLICY,
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "escalate"
    assert data["reason"] == "max_rounds"
    assert data["mutations"] == []


def test_cli_loop_evaluate_repeated_blocker_stops_at_cap_via_ledger_receipts(monkeypatch, tmp_path):
    receipt = {
        "event_id": "evt-prev",
        "source_graph_id": "graph-1",
        "source_task_id": "",
        "source_final_id": "",
        "blocker_class": "stale_inline_route",
        "round_no": 0,
        "same_blocker_count": 0,
        "final_vn": 1,
        "decision": "propose",
        "idempotency_key": "loop:propose:graph-1:evt-prev",
        "policy_resolution_ref": "",
        "origin_ref": "",
        "return_to_ref": "",
        "subscription_status": "verified",
        "reason": "bounded_remediation",
        "created_at": 100.0,
        "mode": "request_only",
    }
    fixture = _write_fixture(tmp_path, {
        "event": _event(event_id="evt-new", verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route", occurred_at=3000.0),
        "policy": SAFE_POLICY,
        "ledger_receipts": [receipt],
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "escalate"
    assert data["reason"] == "max_same_blocker"


def test_cli_loop_evaluate_cooldown_active_noops(monkeypatch, tmp_path):
    receipt = {
        "event_id": "evt-prev",
        "source_graph_id": "graph-1",
        "source_task_id": "",
        "source_final_id": "",
        "blocker_class": "stale_inline_route",
        "round_no": 0,
        "same_blocker_count": 0,
        "final_vn": 1,
        "decision": "propose",
        "idempotency_key": "loop:propose:graph-1:evt-prev",
        "policy_resolution_ref": "",
        "origin_ref": "",
        "return_to_ref": "",
        "subscription_status": "verified",
        "reason": "bounded_remediation",
        "created_at": 1000.0,
        "mode": "request_only",
    }
    policy = dict(SAFE_POLICY)
    policy["max_same_blocker"] = 3
    fixture = _write_fixture(tmp_path, {
        "event": _event(event_id="evt-new", verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route", occurred_at=1200.0),
        "policy": policy,
        "ledger_receipts": [receipt],
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "noop"
    assert data["reason"] == "cooldown"


def test_cli_loop_evaluate_duplicate_event_noops(monkeypatch, tmp_path):
    receipt = {
        "event_id": "evt-1",
        "source_graph_id": "graph-1",
        "source_task_id": "",
        "source_final_id": "",
        "blocker_class": "",
        "round_no": 0,
        "same_blocker_count": 0,
        "final_vn": 1,
        "decision": "stabilize",
        "idempotency_key": "loop:stabilize:graph-1:evt-1",
        "policy_resolution_ref": "",
        "origin_ref": "",
        "return_to_ref": "",
        "subscription_status": "verified",
        "reason": "go_terminal",
        "created_at": 100.0,
        "mode": "request_only",
        "decision_payload": {"action": "stabilize", "reason": "go_terminal", "idempotency_key": "loop:stabilize:graph-1:evt-1", "verdict": "GO", "blocker_class": ""},
    }
    fixture = _write_fixture(tmp_path, {
        "event": _event(event_id="evt-1", verdict="GO", summary="Verdict: GO"),
        "policy": SAFE_POLICY,
        "ledger_receipts": [receipt],
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "noop"
    assert data["reason"] == "duplicate_event"


def test_cli_loop_evaluate_stale_final_fanin_allowlisted_provenance_dry_run_proposal(monkeypatch, tmp_path):
    fixture = _write_fixture(tmp_path, {
        "event": _event(
            event_id="evt-final-go",
            event_type="remediation_review_go",
            source_graph_id="graph-final",
            source_final_id="t_final_v1",
            remediation_review_id="t_review_go",
            old_final_card={"id": "t_final_v1", "status": "blocked"},
            remediation_review_card={"id": "t_review_go", "body": "Verdict: GO — remediation passed."},
        ),
        "policy": SAFE_POLICY,
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "supersede"
    assert data["dry_run"] is True
    assert data["applied"] is False
    assert data["mutations"] == []
    assert data["proposal"]["candidate"]["kind"] == "final-v2"


def test_cli_loop_evaluate_active_mode_apply_without_apply_enabled_no_adapter_call(monkeypatch, tmp_path):
    policy = dict(SAFE_POLICY)
    policy["active_mode"] = "apply"
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route"),
        "policy": policy,
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "escalate"
    assert data["reason"] == "apply_disabled_by_policy"
    assert data["applied"] is False
    assert data["adapter_attempts"] == 0
    assert data["mutations"] == []


def test_cli_loop_evaluate_apply_flag_alone_does_not_enable_apply_gate(monkeypatch, tmp_path):
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route"),
        "policy": SAFE_POLICY,
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture, "--apply"], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "escalate"
    assert data["reason"] == "apply_disabled_by_policy"
    assert data["adapter_attempts"] == 0


def test_cli_loop_evaluate_apply_with_apply_enabled_uses_fake_adapter(monkeypatch, tmp_path):
    policy = dict(SAFE_POLICY)
    policy["active_mode"] = "apply"
    policy["apply_enabled"] = True
    policy["max_auto_creates_per_run"] = 5
    policy["max_tasks_per_graph"] = 9
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route old route", blocker_class="stale_inline_route"),
        "policy": policy,
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "apply"
    assert data["applied"] is True
    assert data["dry_run"] is False
    assert data["adapter_attempts"] == 3


def test_cli_loop_evaluate_real_adapter_mode_calls_board_client(monkeypatch, tmp_path):
    runner = _RecordingCliRunner()
    monkeypatch.setattr(loop_cli, "resolve_kanban_board_client", lambda: runner)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "main")
    policy = dict(SAFE_POLICY)
    policy.update({
        "active_mode": "apply",
        "apply_enabled": True,
        "roadmap_auto_continue": True,
        "roadmap_allowlisted_transitions": ["m14->m15.impl_review_fanin"],
        "roadmap_trusted_assignees": ["ccreviewer"],
        "roadmap_apply_enabled": True,
        "roadmap_board_adapter_mode": "real",
        "roadmap_impl_assignee": "impl-agent",
        "roadmap_review_assignee": "ccreviewer",
        "roadmap_ack_trigger_agent": "ack-agent",
    })
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="GO", summary=_go_summary(), source_final_id="t_final_cli_real", source_assignee="ccreviewer"),
        "policy": policy,
        "roadmap_registry": _roadmap_registry_fixture(),
    })

    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc == 0
    roadmap = data["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap["applied"] is True
    assert roadmap["created_task_ids"] == ["t_cli_real_1", "t_cli_real_2", "t_cli_real_3"]
    create_calls = [c for c in runner.calls if "create" in c]
    subscribe_calls = [c for c in runner.calls if "notify-subscribe" in c]
    assert len(create_calls) == 3
    assert len(subscribe_calls) == 3
    assert all(argv[:4] == ["hermes", "kanban", "--board", "main"] for argv in runner.calls)


def test_cli_loop_evaluate_real_adapter_mode_requires_all_apply_gates(monkeypatch, tmp_path):
    runner = _RecordingCliRunner()
    monkeypatch.setattr(loop_cli, "resolve_kanban_board_client", lambda: runner)
    policy = dict(SAFE_POLICY)
    policy.update({
        "active_mode": "apply",
        "apply_enabled": True,
        "roadmap_auto_continue": True,
        "roadmap_allowlisted_transitions": ["m14->m15.impl_review_fanin"],
        "roadmap_trusted_assignees": ["ccreviewer"],
        "roadmap_apply_enabled": False,
        "roadmap_board_adapter_mode": "real",
    })
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="GO", summary=_go_summary(), source_final_id="t_final_cli_real_off", source_assignee="ccreviewer"),
        "policy": policy,
        "roadmap_registry": _roadmap_registry_fixture(),
    })

    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc == 0
    roadmap = data["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap["applied"] is False
    assert roadmap["created_task_ids"] == []
    assert runner.calls == []


def test_cli_loop_evaluate_malformed_json_input_fails_closed_nonzero(monkeypatch, tmp_path):
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{not-json", encoding="utf-8")
    rc, data = _run(["loop", "evaluate", "--input-file", str(bad_path)], monkeypatch, tmp_path)

    assert rc != 0
    assert data["success"] is False


def test_cli_loop_evaluate_malformed_policy_fails_closed_no_mutation(monkeypatch, tmp_path):
    policy = dict(SAFE_POLICY)
    policy["active_mode"] = "not_a_real_mode"
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route"),
        "policy": policy,
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert data["action"] == "escalate"
    assert data["reason"] == "malformed_policy"
    assert data["mutations"] == []
    assert data["adapter_attempts"] == 0


def test_cli_loop_evaluate_malformed_apply_enabled_type_fails_closed_no_mutation(monkeypatch, tmp_path):
    policy = dict(SAFE_POLICY)
    policy["active_mode"] = "apply"
    policy["apply_enabled"] = "true"
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route"),
        "policy": policy,
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "escalate"
    assert data["reason"] == "malformed_policy"
    assert data["mutations"] == []
    assert data["adapter_attempts"] == 0


def test_cli_loop_evaluate_explicit_args_without_fixture_build_event_and_policy(monkeypatch, tmp_path):
    rc, data = _run([
        "loop", "evaluate",
        "--event-id", "evt-args",
        "--source-graph-id", "graph-args",
        "--source-task-id", "t_source_args",
        "--verdict", "BLOCK",
        "--summary", "Verdict: BLOCK — stale_inline_route",
        "--blocker-class", "stale_inline_route",
        "--origin", "discord:#hermes-main",
        "--return-to", "discord:#hermes-main",
        "--subscription-status", "verified",
        "--policy-resolution-ref", "policy:model.implementation_default@v1",
        "--active-mode", "request_only",
        "--allowlisted-blockers", "stale_inline_route,missing_subscription",
        "--expected-origin", "discord:#hermes-main",
        "--expected-return-to", "discord:#hermes-main",
    ], monkeypatch, tmp_path)

    assert rc == 0
    assert data["action"] == "propose"
    assert data["dry_run"] is True
    assert data["mutations"] == []


def test_cli_loop_evaluate_malformed_round_no_type_fails_closed_no_traceback(monkeypatch, tmp_path):
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route", round_no="not-a-number"),
        "policy": SAFE_POLICY,
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc != 0
    assert data["success"] is False
    assert data["error"] == "malformed_event"


def test_cli_loop_evaluate_ledger_receipts_non_object_entry_fails_closed_no_traceback(monkeypatch, tmp_path):
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route"),
        "policy": SAFE_POLICY,
        "ledger_receipts": ["not-an-object"],
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc != 0
    assert data["success"] is False
    assert data["error"] == "malformed_ledger_receipts"
    assert data["detail"] == "invalid_receipts:1"


def test_cli_loop_evaluate_ledger_receipts_malformed_fields_fail_closed_no_traceback(monkeypatch, tmp_path):
    bad_receipt = {
        "event_id": "evt-prev",
        "source_graph_id": "graph-1",
        "blocker_class": "stale_inline_route",
        "decision": "propose",
        "round_no": "not-a-number",
        "created_at": {"nested": "object"},
    }
    fixture = _write_fixture(tmp_path, {
        "event": _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route"),
        "policy": SAFE_POLICY,
        "ledger_receipts": [bad_receipt],
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    assert rc != 0
    assert data["success"] is False
    assert data["error"] == "malformed_ledger_receipts"
    assert data["detail"] == "invalid_receipts:1"


def test_cli_loop_evaluate_missing_private_input_file_path_sanitized(monkeypatch, tmp_path):
    missing_path = "/home/alice/private/TOKEN=abc123.json"
    rc, data = _run(["loop", "evaluate", "--input-file", missing_path], monkeypatch, tmp_path)

    assert rc != 0
    assert data["success"] is False
    assert data["error"] == "malformed_input"
    assert data["detail"] == "input_file_unreadable"

    blob = json.dumps(data)
    assert missing_path not in blob
    assert "/home/alice" not in blob
    assert "TOKEN" not in blob
    assert "TOKEN=abc123" not in blob
    assert "abc123" not in blob


def test_cli_loop_evaluate_malformed_json_input_file_path_sanitized(monkeypatch, tmp_path):
    secret_dir = tmp_path / "home" / "alice" / "private"
    secret_dir.mkdir(parents=True)
    bad_path = secret_dir / "TOKEN=abc123.json"
    bad_path.write_text("{not-json /home/alice/private/TOKEN=abc123", encoding="utf-8")
    rc, data = _run(["loop", "evaluate", "--input-file", str(bad_path)], monkeypatch, tmp_path)

    assert rc != 0
    assert data["success"] is False
    assert data["error"] == "malformed_input"
    assert data["detail"] == "input_json_invalid"

    blob = json.dumps(data)
    assert str(bad_path) not in blob
    assert "/home/alice" not in blob
    assert "TOKEN" not in blob
    assert "TOKEN=abc123" not in blob
    assert "abc123" not in blob


def test_cli_loop_evaluate_non_object_root_fixture_sanitized(monkeypatch, tmp_path):
    bad_path = tmp_path / "root.json"
    bad_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    rc, data = _run(["loop", "evaluate", "--input-file", str(bad_path)], monkeypatch, tmp_path)

    assert rc != 0
    assert data["success"] is False
    assert data["error"] == "malformed_input"
    assert data["detail"] == "input_json_invalid"


def test_cli_loop_evaluate_report_has_no_private_paths_or_secrets(monkeypatch, tmp_path):
    fixture = _write_fixture(tmp_path, {
        "event": _event(
            verdict="BLOCK",
            summary="Verdict: BLOCK — stale_inline_route /home/alice/private TOKEN=abc123 claude-openrouter-opus",
            source_task_id="/home/alice/private/TOKEN=abc123",
            blocker_class="stale_inline_route",
        ),
        "policy": SAFE_POLICY,
    })
    rc, data = _run(["loop", "evaluate", "--input-file", fixture], monkeypatch, tmp_path)

    blob = json.dumps(data)
    assert "/home/alice" not in blob
    assert "TOKEN=abc123" not in blob
    assert "claude-openrouter-opus" not in blob
