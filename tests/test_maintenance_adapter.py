"""MP4d: live policy-gated adapter interface tests.

The external runner may only reach an action adapter after every fail-closed
policy gate passes (trust grant, exact allowlist, host binding, expiry, mode,
kill switch). The default adapter is fake/noop; the live-capable adapter is
disabled and never wired by the production CLI. These tests prove the boundary,
the bounded/idempotent/cooldown protections, and receipt sanitization.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from agentflow_hermes.cli import main as cli_main
from agentflow_hermes.maintenance.adapter import (
    ActionRequest,
    FakeActionAdapter,
    FakeServiceExecutor,
    LiveActionAdapter,
    NoopActionAdapter,
    UnavailableSystemctlExecutor,
)
from agentflow_hermes.maintenance.runner import evaluate_runner, load_runner_config
from agentflow_hermes.maintenance.trust import build_trust_grant


def _write_config(tmp_path, payload, name="runner.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _grant(unit="hermes-gateway.service", *, host_id="test-host",
           created_at=1000.0, expires_at=9999999999.0, provenance="pytest grant"):
    return build_trust_grant(
        unit, host_id=host_id, created_at=created_at,
        expires_at=expires_at, provenance=provenance,
    )


def _guarded_config(**overrides):
    unit = overrides.get("target_unit", "hermes-gateway.service")
    config = {
        "mode": "guarded_cycle",
        "maintenance_kill_switch": False,
        "allowed_services": [unit],
        "trust_grants": [_grant(unit)],
        "requested_action": "service_cycle",
        "target_unit": unit,
        "attempt_budget": 1,
        "host_id": "test-host",
        "reviewed_summary": "Verdict: GO — pytest reviewed",
    }
    config.update(overrides)
    return config


class RecordingAdapter:
    """Spy adapter: proves the runner never calls it when a gate fails."""

    is_live = False

    def __init__(self):
        self.calls: list[ActionRequest] = []

    def consider(self, request, *, now=0.0):
        self.calls.append(request)
        # Should never be reached in BLOCK-before-adapter tests.
        return NoopActionAdapter().consider(request, now=now)


# --- 1-4: gates fail closed and the adapter is never called ------------------

def test_no_trust_grant_blocks_and_never_calls_adapter(tmp_path):
    spy = RecordingAdapter()
    path = _write_config(tmp_path, _guarded_config(trust_grants=[]))
    report = evaluate_runner(load_runner_config(path), adapter=spy, now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "no_trust_grant"
    assert report["actions"]["executed"] == []
    assert spy.calls == []
    assert report["receipt"]["executed"] is False
    assert report["receipt"]["noop"] is True


def test_malformed_grant_blocks_and_never_calls_adapter(tmp_path):
    spy = RecordingAdapter()
    path = _write_config(tmp_path, _guarded_config(
        trust_grants=["/home/alice/private TOKEN=abc123"],
    ))
    report = evaluate_runner(load_runner_config(path), adapter=spy, now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "malformed_trust_grants"
    assert spy.calls == []


def test_expired_grant_blocks_and_never_calls_adapter(tmp_path):
    spy = RecordingAdapter()
    path = _write_config(tmp_path, _guarded_config(
        trust_grants=[_grant(created_at=1000.0, expires_at=1500.0)],
    ))
    report = evaluate_runner(load_runner_config(path), adapter=spy, now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "no_trust_grant"
    assert spy.calls == []


def test_allowlist_mismatch_blocks_and_never_calls_adapter(tmp_path):
    spy = RecordingAdapter()
    path = _write_config(tmp_path, _guarded_config(allowed_services=["other.service"]))
    report = evaluate_runner(load_runner_config(path), adapter=spy, now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "service_not_allowlisted"
    assert spy.calls == []


# --- 5: valid grant + fake executor -> bounded canary receipt ----------------

def test_valid_grant_fake_adapter_bounded_canary_receipt(tmp_path):
    fake = FakeServiceExecutor(healthy=True)
    adapter = FakeActionAdapter(fake)
    path = _write_config(tmp_path, _guarded_config(attempt_budget=3))
    report = evaluate_runner(load_runner_config(path), adapter=adapter, now=1000.0)

    assert report["status"] == "GO"
    assert report["reason"] == "service_action_applied"
    receipt = report["receipt"]
    assert receipt["fake"] is True
    assert receipt["executed"] is True
    assert receipt["applied"] is True
    assert receipt["dry_run"] is False
    assert receipt["attempts"] == 1
    assert receipt["action_id"]
    assert receipt["idempotency_key"]
    # Bounded canary: exactly one fake call, no real systemctl anywhere.
    assert fake.calls == ["hermes-gateway.service"]


# --- 6: valid grant + real executor disabled -> BLOCK/NOOP, no systemctl -----

def test_valid_grant_live_adapter_disabled_blocks_without_systemctl(tmp_path):
    # Even injected with the real (raising) executor, the disabled live adapter
    # must not call it.
    adapter = LiveActionAdapter(UnavailableSystemctlExecutor(), enabled=False)
    path = _write_config(tmp_path, _guarded_config())
    report = evaluate_runner(load_runner_config(path), adapter=adapter, now=2000.0)

    assert report["status"] in {"BLOCK", "NOOP"}
    assert report["reason"] == "live_adapter_disabled"
    assert report["actions"]["executed"] == []
    receipt = report["receipt"]
    assert receipt["executed"] is False
    assert receipt["applied"] is False
    assert receipt["noop"] is True
    assert receipt["attempts"] == 0
    assert receipt["fake"] is False


def test_disabled_live_adapter_direct_never_touches_executor():
    real = UnavailableSystemctlExecutor()
    adapter = LiveActionAdapter(real, enabled=False)
    receipt = adapter.consider(ActionRequest(
        action_id="a", idempotency_key="k", target_unit="hermes-gateway.service",
        attempt_budget=1,
    ))
    assert receipt.status == "BLOCK"
    assert receipt.noop_reason == "live_adapter_disabled"
    assert receipt.executed is False


def test_enabled_live_adapter_with_real_executor_fails_closed():
    # A future enable flag with the default unavailable executor must fail closed
    # (the executor raises), never surfacing a real service action.
    adapter = LiveActionAdapter(enabled=True)
    receipt = adapter.consider(ActionRequest(
        action_id="a", idempotency_key="k", target_unit="hermes-gateway.service",
        attempt_budget=1,
    ))
    assert receipt.status == "BLOCK"
    assert receipt.executed is False
    assert receipt.noop_reason == "live_execution_unavailable"


# --- 7: repeated request with same idempotency key -> no duplicate action ----

def test_idempotent_replay_no_duplicate_fake_action(tmp_path):
    fake = FakeServiceExecutor(healthy=True)
    adapter = FakeActionAdapter(fake)
    path = _write_config(tmp_path, _guarded_config(attempt_budget=2))

    first = evaluate_runner(load_runner_config(path), adapter=adapter, now=1000.0)
    assert first["status"] == "GO"
    assert first["receipt"]["executed"] is True
    assert fake.calls == ["hermes-gateway.service"]

    # Same config -> same idempotency key; second consider must not re-execute.
    assert first["idempotency_key"]
    second = evaluate_runner(load_runner_config(path), adapter=adapter, now=1001.0)
    assert second["idempotency_key"] == first["idempotency_key"]
    assert second["receipt"]["noop"] is True
    assert second["receipt"]["noop_reason"] == "idempotent_replay"
    assert second["receipt"]["attempts"] == 0
    # No duplicate fake action recorded.
    assert fake.calls == ["hermes-gateway.service"]
    assert second["actions"]["executed"] == []


# --- 8: failed fake executor consumes budget and reports failure safely ------

def test_failed_fake_executor_consumes_budget_reports_failure(tmp_path):
    fake = FakeServiceExecutor(healthy=False)
    adapter = FakeActionAdapter(fake)
    path = _write_config(tmp_path, _guarded_config(attempt_budget=2))
    report = evaluate_runner(load_runner_config(path), adapter=adapter, now=1000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "service_action_failed"
    receipt = report["receipt"]
    assert receipt["applied"] is False
    assert receipt["executed"] is False
    assert receipt["attempts"] == 2
    assert receipt["dry_run"] is True
    assert len(fake.calls) == 2
    assert report["actions"]["executed"] == []


# --- 9: cooldown / max attempts still gate adapter calls ---------------------

def test_cooldown_gates_second_action_within_window():
    fake = FakeServiceExecutor(healthy=True)
    adapter = FakeActionAdapter(fake, cooldown_seconds=100.0)

    first = adapter.consider(ActionRequest(
        action_id="a", idempotency_key="k1", target_unit="hermes-gateway.service",
        attempt_budget=1,
    ), now=1000.0)
    assert first.applied is True
    assert fake.calls == ["hermes-gateway.service"]

    # Distinct key but within the cooldown window -> gated, no new fake action.
    second = adapter.consider(ActionRequest(
        action_id="b", idempotency_key="k2", target_unit="hermes-gateway.service",
        attempt_budget=1,
    ), now=1050.0)
    assert second.noop is True
    assert second.noop_reason == "cooldown_active"
    assert second.attempts == 0
    assert fake.calls == ["hermes-gateway.service"]

    # After the window elapses a fresh key may act again.
    third = adapter.consider(ActionRequest(
        action_id="c", idempotency_key="k3", target_unit="hermes-gateway.service",
        attempt_budget=1,
    ), now=1200.0)
    assert third.applied is True
    assert fake.calls == ["hermes-gateway.service", "hermes-gateway.service"]


def test_max_attempts_hard_cap_bounds_fake_adapter():
    from agentflow_hermes.maintenance.runner import HARD_ATTEMPT_CAP

    fake = FakeServiceExecutor(healthy=False)
    adapter = FakeActionAdapter(fake)
    # Direct: adapter honors the budget it is handed (runner caps it at the hard cap).
    receipt = adapter.consider(ActionRequest(
        action_id="a", idempotency_key="k", target_unit="hermes-gateway.service",
        attempt_budget=HARD_ATTEMPT_CAP,
    ), now=1000.0)
    assert receipt.attempts == HARD_ATTEMPT_CAP
    assert len(fake.calls) == HARD_ATTEMPT_CAP


def test_runner_caps_adapter_budget_at_hard_cap(tmp_path):
    from agentflow_hermes.maintenance.runner import HARD_ATTEMPT_CAP

    fake = FakeServiceExecutor(healthy=False)
    adapter = FakeActionAdapter(fake)
    path = _write_config(tmp_path, _guarded_config(attempt_budget=999))
    report = evaluate_runner(load_runner_config(path), adapter=adapter, now=1000.0)

    assert report["receipt"]["attempts"] == HARD_ATTEMPT_CAP
    assert len(fake.calls) == HARD_ATTEMPT_CAP


# --- 10: receipts sanitize private paths / secrets ---------------------------

def test_adapter_receipt_sanitizes_private_paths_and_secrets(tmp_path):
    fake = FakeServiceExecutor(healthy=True)
    adapter = FakeActionAdapter(fake)
    path = _write_config(tmp_path, _guarded_config(
        repo_path="/home/alice/private/hermes",
        provenance="TOKEN=abc123 /home/alice/secret",
    ))
    report = evaluate_runner(load_runner_config(path), adapter=adapter, now=1000.0)

    blob = json.dumps(report)
    assert "/home/alice" not in blob
    assert "TOKEN=abc123" not in blob
    assert "abc123" not in blob
    # Receipt is present and machine-readable.
    for field in ("dry_run", "fake", "noop", "applied", "executed", "action_id",
                  "idempotency_key", "attempts", "noop_reason"):
        assert field in report["receipt"]


# --- default adapter posture -------------------------------------------------

def test_default_noop_adapter_never_executes(tmp_path):
    adapter = NoopActionAdapter()
    path = _write_config(tmp_path, _guarded_config())
    report = evaluate_runner(load_runner_config(path), adapter=adapter, now=2000.0)

    assert report["status"] == "NOOP"
    assert report["receipt"]["noop"] is True
    assert report["receipt"]["noop_reason"] == "noop_adapter"
    assert report["actions"]["executed"] == []


def test_cli_evaluate_never_wires_executing_adapter(tmp_path, monkeypatch):
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
    # Production CLI receipt is proposal-only: considered, never executed.
    assert data["receipt"]["executed"] is False
    assert data["receipt"]["noop"] is True
    assert data["receipt"]["noop_reason"] == "proposal_only"
