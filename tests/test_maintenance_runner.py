from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from agentflow_hermes.cli import main as cli_main
from agentflow_hermes.maintenance.trust import build_trust_grant
from agentflow_hermes.maintenance.runner import (
    HARD_ATTEMPT_CAP,
    FakeServiceExecutor,
    UnavailableSystemctlExecutor,
    evaluate_runner,
    load_runner_config,
)


def _write_config(tmp_path, payload, name="runner.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


SAFE_LOOP_FIXTURE = {
    "event": {
        "event_id": "evt-1",
        "source_graph_id": "graph-1",
        "source_task_id": "t_source",
        "verdict": "BLOCK",
        "summary": "Verdict: BLOCK — stale_inline_route old route",
        "blocker_class": "stale_inline_route",
        "origin": "discord:#hermes-main",
        "return_to": "discord:#hermes-main",
        "subscription_status": "verified",
        "policy_resolution_ref": "policy:model.implementation_default@v1",
        "occurred_at": 1000.0,
    },
    "policy": {
        "active_mode": "request_only",
        "allowlisted_blockers": ["stale_inline_route"],
        "expected_origin": "discord:#hermes-main",
        "expected_return_to": "discord:#hermes-main",
    },
}


def _guarded_config(**overrides):
    unit = overrides.get("target_unit", "hermes-gateway.service")
    config = {
        "mode": "guarded_cycle",
        "maintenance_kill_switch": False,
        "allowed_services": [unit],
        "trust_grants": [build_trust_grant(
            unit,
            host_id="test-host",
            created_at=1000.0,
            expires_at=9999999999.0,
            provenance="pytest explicit grant",
        )],
        "requested_action": "service_cycle",
        "target_unit": unit,
        "attempt_budget": 1,
        "host_id": "test-host",
    }
    config.update(overrides)
    return config


# 1. default runner => dry_run / no executed actions
def test_default_runner_is_request_only_dry_run_no_executed_actions(tmp_path):
    path = _write_config(tmp_path, {"loop": SAFE_LOOP_FIXTURE})
    config = load_runner_config(path)
    report = evaluate_runner(config)

    assert report["success"] is True
    assert report["dry_run"] is True
    assert report["actions"]["executed"] == []
    assert report["service_action"]["executed"] is False
    assert report["service_action"]["attempts"] == 0
    # loop decision summary is surfaced (request-only BLOCK proposal)
    assert report["status"] == "BLOCK"
    assert report["loop"]["action"] == "propose"


def test_default_runner_without_loop_fixture_noops(tmp_path):
    path = _write_config(tmp_path, {"mode": "request_only"})
    report = evaluate_runner(load_runner_config(path))

    assert report["status"] == "noop"
    assert report["dry_run"] is True
    assert report["actions"]["executed"] == []


# 2. malformed policy => fail closed, sanitized report
def test_malformed_policy_fails_closed(tmp_path):
    path = _write_config(tmp_path, _guarded_config(maintenance_kill_switch="yes-please"))
    report = evaluate_runner(load_runner_config(path))

    assert report["status"] == "BLOCK"
    assert report["actions"]["executed"] == []
    assert report["service_action"]["attempts"] == 0
    assert report["safety_gates"]["kill_switch_clear"] is False


def test_malformed_json_config_fails_closed(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    report = evaluate_runner(load_runner_config(str(bad)))

    assert report["status"] == "BLOCK"
    assert report["reason"] == "malformed_config"
    assert report["actions"]["executed"] == []


# 3. kill_switch true => no action
def test_kill_switch_true_blocks_all_action(tmp_path):
    path = _write_config(tmp_path, _guarded_config(maintenance_kill_switch=True))
    fake = FakeServiceExecutor()
    report = evaluate_runner(load_runner_config(path), executor=fake)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "kill_switch"
    assert report["service_action"]["attempts"] == 0
    assert fake.calls == []


# 4. guarded_cycle without trust grant / allowlist => BLOCK
def test_service_cycle_without_allowlist_blocks(tmp_path):
    path = _write_config(tmp_path, _guarded_config(allowed_services=[], trust_grants=[]))
    report = evaluate_runner(load_runner_config(path))

    assert report["status"] == "BLOCK"
    assert report["reason"] == "service_not_allowlisted"
    assert report["safety_gates"]["service_allowlisted"] is False


def test_service_cycle_malformed_allowlist_blocks(tmp_path):
    path = _write_config(tmp_path, _guarded_config(allowed_services="hermes-gateway.service"))
    report = evaluate_runner(load_runner_config(path))

    assert report["status"] == "BLOCK"
    assert report["reason"] == "service_not_allowlisted"
    assert report["actions"]["executed"] == []


def test_service_cycle_allowlisted_but_no_trust_grant_blocks(tmp_path):
    path = _write_config(tmp_path, _guarded_config(trust_grants=[]))
    report = evaluate_runner(load_runner_config(path))

    assert report["status"] == "BLOCK"
    assert report["reason"] == "no_trust_grant"
    assert report["safety_gates"]["trust_grant"] is False


def test_service_cycle_expired_trust_grant_blocks(tmp_path):
    grant = build_trust_grant(
        "hermes-gateway.service",
        host_id="test-host",
        created_at=1000.0,
        expires_at=1500.0,
        provenance="expired grant",
    )
    path = _write_config(tmp_path, _guarded_config(trust_grants=[grant]))
    report = evaluate_runner(load_runner_config(path), now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "no_trust_grant"
    assert report["safety_gates"]["trust_grant"] is False


def test_service_cycle_host_mismatch_trust_grant_blocks(tmp_path):
    grant = build_trust_grant(
        "hermes-gateway.service",
        host_id="other-host",
        created_at=1000.0,
        expires_at=9999999999.0,
        provenance="copied grant",
    )
    path = _write_config(tmp_path, _guarded_config(trust_grants=[grant]))
    report = evaluate_runner(load_runner_config(path), now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "no_trust_grant"


def test_service_cycle_allowlist_mismatch_blocks_even_with_grant(tmp_path):
    path = _write_config(tmp_path, _guarded_config(allowed_services=["other.service"]))
    report = evaluate_runner(load_runner_config(path), now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "service_not_allowlisted"


def test_malformed_trust_grants_fail_closed_sanitized(tmp_path):
    path = _write_config(tmp_path, _guarded_config(
        trust_grants={"gateway_unit": "/home/alice/private TOKEN=abc123"},
    ))
    report = evaluate_runner(load_runner_config(path), now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "malformed_trust_grants"
    blob = json.dumps(report)
    assert "/home/alice" not in blob
    assert "TOKEN=abc123" not in blob


def test_mixed_valid_and_malformed_trust_grants_fail_closed(tmp_path):
    valid_grant = build_trust_grant(
        "hermes-gateway.service",
        host_id="test-host",
        created_at=1000.0,
        expires_at=9999999999.0,
        provenance="pytest explicit grant",
    )
    path = _write_config(tmp_path, _guarded_config(
        trust_grants=[valid_grant, "/home/alice/private TOKEN=abc123"],
        allow_fake_execute=True,
    ))
    report = evaluate_runner(load_runner_config(path), now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "malformed_trust_grants"
    assert report["actions"]["executed"] == []
    assert report["dry_run"] is True
    blob = json.dumps(report)
    assert "/home/alice" not in blob
    assert "TOKEN=abc123" not in blob


def test_valid_trust_grant_only_still_yields_go_eligible_proposal(tmp_path):
    path = _write_config(tmp_path, _guarded_config(allow_fake_execute=True))
    report = evaluate_runner(load_runner_config(path), now=2000.0)

    assert report["status"] == "GO"
    assert report["reason"] == "eligible_proposal"
    assert report["dry_run"] is True
    assert report["actions"]["executed"] == []


def test_malformed_trust_grant_only_blocks(tmp_path):
    path = _write_config(tmp_path, _guarded_config(
        trust_grants=["/home/alice/private TOKEN=abc123"],
        allow_fake_execute=True,
    ))
    report = evaluate_runner(load_runner_config(path), now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "malformed_trust_grants"
    assert report["dry_run"] is True
    assert report["actions"]["executed"] == []


def test_mixed_valid_and_malformed_object_trust_grants_fail_closed_sanitized(tmp_path):
    valid_grant = build_trust_grant(
        "hermes-gateway.service",
        host_id="test-host",
        created_at=1000.0,
        expires_at=9999999999.0,
        provenance="pytest explicit grant",
    )
    malformed_grant = {
        "grant_id": "grant_bad",
        "gateway_unit": "/home/alice/private TOKEN=abc123",
        "created_at": 1000.0,
        "expires_at": 9999999999.0,
        "allowed_services": ["hermes-gateway.service"],
        "host_id": "test-host",
    }
    path = _write_config(tmp_path, _guarded_config(
        trust_grants=[valid_grant, malformed_grant],
        allow_fake_execute=True,
    ))
    report = evaluate_runner(load_runner_config(path), now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "malformed_trust_grants"
    assert report["dry_run"] is True
    assert report["actions"]["executed"] == []
    blob = json.dumps(report)
    assert "/home/alice" not in blob
    assert "TOKEN=abc123" not in blob


def test_service_cycle_mode_not_guarded_blocks(tmp_path):
    path = _write_config(tmp_path, _guarded_config(mode="request_only"))
    report = evaluate_runner(load_runner_config(path))

    assert report["status"] == "BLOCK"
    assert report["reason"] == "mode_not_guarded_cycle"


# 5. guarded_cycle fully granted still dry-run/proposal unless fake executor explicitly enabled
def test_fully_granted_service_cycle_is_proposal_only_without_executor(tmp_path):
    path = _write_config(tmp_path, _guarded_config(allow_fake_execute=True))
    report = evaluate_runner(load_runner_config(path))  # no executor injected

    assert report["status"] == "GO"
    assert report["dry_run"] is True
    assert report["actions"]["executed"] == []
    assert report["actions"]["proposed"][0]["kind"] == "service_restart"
    assert report["service_action"]["attempts"] == 0


def test_runner_consumes_valid_m12_grant_but_stays_dry_run_without_executor(tmp_path):
    path = _write_config(tmp_path, _guarded_config(allow_fake_execute=True))
    report = evaluate_runner(load_runner_config(path), now=2000.0)

    assert report["status"] == "GO"
    assert report["reason"] == "eligible_proposal"
    assert report["dry_run"] is True
    assert report["actions"]["executed"] == []
    assert report["safety_gates"]["trust_grant"] is True


def test_fully_granted_with_executor_but_flag_off_is_proposal_only(tmp_path):
    path = _write_config(tmp_path, _guarded_config(allow_fake_execute=False))
    fake = FakeServiceExecutor()
    report = evaluate_runner(load_runner_config(path), executor=fake)

    assert report["status"] == "GO"
    assert report["dry_run"] is True
    assert report["actions"]["executed"] == []
    assert fake.calls == []


# 6. fake executor path proves bounded attempt budget without touching real services
def test_fake_executor_healthy_executes_single_bounded_action(tmp_path):
    path = _write_config(tmp_path, _guarded_config(allow_fake_execute=True, attempt_budget=3))
    fake = FakeServiceExecutor(healthy=True)
    report = evaluate_runner(load_runner_config(path), executor=fake)

    assert report["status"] == "GO"
    assert report["dry_run"] is False
    assert report["service_action"]["executed"] is True
    assert report["service_action"]["attempts"] == 1
    assert report["actions"]["executed"][0]["kind"] == "service_restart"
    assert fake.calls == ["hermes-gateway.service"]


def test_fake_executor_failing_bounds_attempts_to_budget(tmp_path):
    path = _write_config(tmp_path, _guarded_config(allow_fake_execute=True, attempt_budget=2))
    fake = FakeServiceExecutor(healthy=False)
    report = evaluate_runner(load_runner_config(path), executor=fake)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "service_action_failed"
    assert report["service_action"]["executed"] is False
    assert report["service_action"]["attempts"] == 2
    assert len(fake.calls) == 2


def test_attempt_budget_never_exceeds_hard_cap(tmp_path):
    path = _write_config(tmp_path, _guarded_config(allow_fake_execute=True, attempt_budget=999))
    fake = FakeServiceExecutor(healthy=False)
    report = evaluate_runner(load_runner_config(path), executor=fake)

    assert report["service_action"]["attempts"] == HARD_ATTEMPT_CAP
    assert len(fake.calls) == HARD_ATTEMPT_CAP


def test_real_systemctl_executor_is_not_reachable_and_raises_if_forced(tmp_path):
    executor = UnavailableSystemctlExecutor()
    with pytest.raises(RuntimeError):
        executor.restart_unit("hermes-gateway.service")


# 7. no raw private path / secret in reports
def test_report_has_no_private_paths_or_secrets(tmp_path):
    loop_fixture = json.loads(json.dumps(SAFE_LOOP_FIXTURE))
    loop_fixture["event"]["summary"] = "Verdict: BLOCK — stale_inline_route /home/alice/private TOKEN=abc123"
    loop_fixture["event"]["source_task_id"] = "/home/alice/private/TOKEN=abc123"
    path = _write_config(tmp_path, _guarded_config(
        repo_path="/home/alice/private/hermes",
        loop=loop_fixture,
        target_unit="hermes-gateway.service",
    ))
    report = evaluate_runner(load_runner_config(path))

    blob = json.dumps(report)
    assert "/home/alice" not in blob
    assert "TOKEN=abc123" not in blob
    assert "abc123" not in blob


# CLI integration: proposal-only, never touches real services
def test_cli_maintenance_runner_evaluate_is_proposal_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    path = _write_config(tmp_path, _guarded_config(allow_fake_execute=True))
    captured = io.StringIO()
    with redirect_stdout(captured):
        rc = cli_main(["maintenance", "runner", "evaluate", "--input-file", path])
    data = json.loads(captured.getvalue())

    assert rc == 0
    assert data["status"] == "GO"
    assert data["dry_run"] is True
    assert data["actions"]["executed"] == []
