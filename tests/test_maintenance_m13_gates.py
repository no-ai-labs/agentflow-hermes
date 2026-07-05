"""M13 non-privileged real-executor-canary prerequisites: adversarial tests.

Proves the remaining fake-only policy gates (activity, cycle cap, min
interval, quiet hours, reviewed sync GO), DB-backed cross-process
idempotency claimed before adapter construction, the immediate pre-effect
recheck, and the fake post-smoke success/failure path — all without any
real systemctl call, gateway restart, or timer-driven execution.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from agentflow_hermes.maintenance.adapter import FakeActionAdapter, FakeServiceExecutor, NoopActionAdapter
from agentflow_hermes.maintenance.runner import evaluate_runner, load_runner_config
from agentflow_hermes.maintenance.trust import build_trust_grant


def _write_config(tmp_path, payload, name="runner.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _guarded_config(**overrides):
    unit = overrides.get("target_unit", "hermes-gateway.service")
    config = {
        "mode": "guarded_cycle",
        "maintenance_kill_switch": False,
        "allowed_services": [unit],
        "trust_grants": [build_trust_grant(
            unit, host_id="test-host", created_at=1000.0,
            expires_at=9999999999.0, provenance="pytest explicit grant",
        )],
        "requested_action": "service_cycle",
        "target_unit": unit,
        "attempt_budget": 1,
        "host_id": "test-host",
        "reviewed_summary": "Verdict: GO — pytest reviewed",
    }
    config.update(overrides)
    return config


class RecordingAdapter:
    is_live = False

    def __init__(self):
        self.calls = []

    def consider(self, request, *, now=0.0):
        self.calls.append(request)
        return NoopActionAdapter().consider(request, now=now)


# --- reviewed GO ---------------------------------------------------------

def test_no_reviewed_go_refused_no_adapter(tmp_path):
    from agentflow_hermes.maintenance import durable

    db_path = str(tmp_path / "maintenance.db")
    spy = RecordingAdapter()
    path = _write_config(tmp_path, _guarded_config(
        db_path=db_path, repo_id="repo1", cycle_ref="sha-no-go", reviewed_summary="",
    ))
    report = evaluate_runner(load_runner_config(path), adapter=spy, now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "no_reviewed_go"
    assert spy.calls == []
    assert report["actions"]["executed"] == []

    key = durable.build_cycle_key(repo_id="repo1", target_unit="hermes-gateway.service", cycle_ref="sha-no-go")
    row = durable.get_cycle(db_path, key)
    assert row is not None
    assert row["status"] == "refused"
    assert row["reason"] == "no_reviewed_go"

    # A duplicate with the same key returns the prior durable refusal and never
    # reaches a fresh adapter instance.
    spy2 = RecordingAdapter()
    second = evaluate_runner(load_runner_config(path), adapter=spy2, now=2001.0)
    assert second["status"] == "BLOCK"
    assert second["reason"] == "no_reviewed_go"
    assert spy2.calls == []


def test_reviewed_go_semantic_block_refused_no_adapter(tmp_path):
    spy = RecordingAdapter()
    path = _write_config(tmp_path, _guarded_config(
        reviewed_summary="Verdict: BLOCK — stale_inline_route",
    ))
    report = evaluate_runner(load_runner_config(path), adapter=spy, now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "no_reviewed_go"
    assert spy.calls == []


# --- activity / no active workers ----------------------------------------

def test_active_workers_present_refused_no_adapter(tmp_path):
    spy = RecordingAdapter()
    path = _write_config(tmp_path, _guarded_config(activity={"active": True}))
    report = evaluate_runner(load_runner_config(path), adapter=spy, now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "active_workers_present"
    assert spy.calls == []
    assert report["actions"]["executed"] == []


# --- max cycles per day ---------------------------------------------------

def test_max_cycles_per_day_exhausted_refused_no_adapter(tmp_path):
    from agentflow_hermes.maintenance import durable

    db_path = str(tmp_path / "maintenance.db")
    unit = "hermes-gateway.service"
    for i in range(2):  # default policy max_cycles_per_day == 2
        key = durable.build_cycle_key(repo_id="repo1", target_unit=unit, cycle_ref=f"sha{i}")
        durable.claim_cycle(
            db_path, idempotency_key=key, target_unit=unit, repo_id="repo1",
            reason="", dry_run=False, fake=True, source_ref="", policy_ref="", now=1000.0 + i,
        )
        durable.update_cycle_status(db_path, key, status="applied", reason="ok")

    spy = RecordingAdapter()
    path = _write_config(tmp_path, _guarded_config(
        db_path=db_path, repo_id="repo1", cycle_ref="sha-new",
    ))
    report = evaluate_runner(load_runner_config(path), adapter=spy, now=1500.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "max_cycles_exhausted"
    assert spy.calls == []


# --- min seconds between cycles / cooldown --------------------------------

def test_min_interval_not_elapsed_refused_no_adapter(tmp_path):
    from agentflow_hermes.maintenance import durable

    db_path = str(tmp_path / "maintenance.db")
    unit = "hermes-gateway.service"
    key = durable.build_cycle_key(repo_id="repo1", target_unit=unit, cycle_ref="sha-old")
    durable.claim_cycle(
        db_path, idempotency_key=key, target_unit=unit, repo_id="repo1",
        reason="", dry_run=False, fake=True, source_ref="", policy_ref="", now=1000.0,
    )
    durable.update_cycle_status(db_path, key, status="applied", reason="ok")

    spy = RecordingAdapter()
    # default policy min_seconds_between_cycles == 1800; only 100s have passed.
    path = _write_config(tmp_path, _guarded_config(
        db_path=db_path, repo_id="repo1", cycle_ref="sha-new",
    ))
    report = evaluate_runner(load_runner_config(path), adapter=spy, now=1100.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "min_interval_not_elapsed"
    assert spy.calls == []


# --- quiet hours ------------------------------------------------------------

def test_quiet_hours_miss_refused_no_adapter(tmp_path):
    spy = RecordingAdapter()
    path = _write_config(tmp_path, _guarded_config(
        quiet_hours={"enabled": True, "start_hour": 22, "end_hour": 6},
    ))
    report = evaluate_runner(
        load_runner_config(path), adapter=spy, now=2000.0, clock_hour=lambda: 23,
    )

    assert report["status"] == "BLOCK"
    assert report["reason"] == "quiet_hours_active"
    assert spy.calls == []


def test_quiet_hours_enabled_without_injected_clock_fails_closed(tmp_path):
    spy = RecordingAdapter()
    path = _write_config(tmp_path, _guarded_config(
        quiet_hours={"enabled": True, "start_hour": 22, "end_hour": 6},
    ))
    report = evaluate_runner(load_runner_config(path), adapter=spy, now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "quiet_hours_clock_missing"
    assert spy.calls == []


def test_quiet_hours_outside_window_passes_through(tmp_path):
    path = _write_config(tmp_path, _guarded_config(
        quiet_hours={"enabled": True, "start_hour": 22, "end_hour": 6},
        allow_fake_execute=True,
    ))
    report = evaluate_runner(
        load_runner_config(path), now=2000.0, clock_hour=lambda: 12,
    )
    assert report["status"] == "GO"


# --- DB-backed cross-process idempotency ------------------------------------

def test_duplicate_idempotency_across_fresh_runner_instance_no_second_adapter_call(tmp_path):
    db_path = str(tmp_path / "maintenance.db")
    path = _write_config(tmp_path, _guarded_config(
        db_path=db_path, repo_id="repo1", cycle_ref="sha-fixed", attempt_budget=1,
        min_seconds_between_cycles=0,
    ))

    fake1 = FakeServiceExecutor(healthy=True)
    adapter1 = FakeActionAdapter(fake1)
    first = evaluate_runner(load_runner_config(path), adapter=adapter1, now=1000.0)
    assert first["status"] == "GO"
    assert fake1.calls == ["hermes-gateway.service"]

    # Fresh runner instance: new config load, new adapter/executor, same key.
    fake2 = FakeServiceExecutor(healthy=True)
    adapter2 = FakeActionAdapter(fake2)
    second = evaluate_runner(load_runner_config(path), adapter=adapter2, now=1001.0)

    assert second["status"] == "GO"
    assert second["actions"]["executed"] == []
    assert fake2.calls == []  # no duplicate adapter call/action


def test_crash_after_attempt_before_terminal_never_double_fires(tmp_path):
    from agentflow_hermes.maintenance import durable

    db_path = str(tmp_path / "maintenance.db")
    key = durable.build_cycle_key(repo_id="repo1", target_unit="hermes-gateway.service", cycle_ref="sha-crash")
    # Simulate a crash: claimed but never reached a terminal status.
    durable.claim_cycle(
        db_path, idempotency_key=key, target_unit="hermes-gateway.service", repo_id="repo1",
        reason="eligible", dry_run=True, fake=True, source_ref="", policy_ref="", now=1000.0,
    )

    spy = RecordingAdapter()
    path = _write_config(tmp_path, _guarded_config(
        db_path=db_path, repo_id="repo1", cycle_ref="sha-crash",
    ))
    report = evaluate_runner(load_runner_config(path), adapter=spy, now=1001.0)

    assert report["status"] == "BLOCK"
    assert spy.calls == []


# --- immediate pre-effect recheck -------------------------------------------

def test_pre_effect_recheck_activity_trips_after_initial_gates(tmp_path):
    spy = RecordingAdapter()
    path = _write_config(tmp_path, _guarded_config())
    calls = {"n": 0}

    def activity_provider():
        calls["n"] += 1
        return calls["n"] > 1  # clear on the initial gate check, active on recheck

    report = evaluate_runner(
        load_runner_config(path), adapter=spy, now=2000.0, activity_provider=activity_provider,
    )

    assert report["status"] == "BLOCK"
    assert report["reason"] == "active_workers_present"
    assert spy.calls == []
    assert calls["n"] >= 2


def test_pre_effect_recheck_kill_switch_trips_after_initial_gates(tmp_path):
    spy = RecordingAdapter()
    path = _write_config(tmp_path, _guarded_config())
    calls = {"n": 0}

    def kill_switch_provider():
        calls["n"] += 1
        return calls["n"] > 1  # clear initially, tripped by the pre-effect recheck

    report = evaluate_runner(
        load_runner_config(path), adapter=spy, now=2000.0, kill_switch_provider=kill_switch_provider,
    )

    assert report["status"] == "BLOCK"
    assert report["reason"] == "kill_switch"
    assert spy.calls == []
    assert calls["n"] >= 2


# --- fake post-smoke success / failure --------------------------------------

def test_fake_post_smoke_success_terminal_applied_receipt(tmp_path):
    from agentflow_hermes.maintenance import durable

    db_path = str(tmp_path / "maintenance.db")
    path = _write_config(tmp_path, _guarded_config(
        db_path=db_path, repo_id="repo1", cycle_ref="sha-smoke-ok",
    ))
    fake = FakeServiceExecutor(healthy=True)
    adapter = FakeActionAdapter(fake)
    report = evaluate_runner(
        load_runner_config(path), adapter=adapter, now=1000.0, post_smoke_provider=lambda: True,
    )

    assert report["status"] == "GO"
    assert report["reason"] == "service_action_applied"
    key = durable.build_cycle_key(repo_id="repo1", target_unit="hermes-gateway.service", cycle_ref="sha-smoke-ok")
    row = durable.get_cycle(db_path, key)
    assert row["status"] == "applied"
    assert durable.is_degraded(db_path) is False


def test_fake_post_smoke_failure_degraded_deadletter_no_retry_storm(tmp_path):
    from agentflow_hermes.maintenance import durable

    db_path = str(tmp_path / "maintenance.db")
    path = _write_config(tmp_path, _guarded_config(
        db_path=db_path, repo_id="repo1", cycle_ref="sha-smoke-bad",
    ))
    fake = FakeServiceExecutor(healthy=True)
    adapter = FakeActionAdapter(fake)
    report = evaluate_runner(
        load_runner_config(path), adapter=adapter, now=1000.0, post_smoke_provider=lambda: False,
    )

    assert report["status"] == "BLOCK"
    assert report["reason"] == "post_smoke_failed"
    key = durable.build_cycle_key(repo_id="repo1", target_unit="hermes-gateway.service", cycle_ref="sha-smoke-bad")
    row = durable.get_cycle(db_path, key)
    assert row["status"] == "degraded"
    assert durable.is_degraded(db_path) is True
    # Exactly one fake restart call: no retry storm on post-smoke failure.
    assert fake.calls == ["hermes-gateway.service"]

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    dead = con.execute("select * from maintenance_deadletter").fetchall()
    assert len(dead) == 1
    assert dead[0]["reason"] == "post_smoke_failed"


# --- receipt sanitization ----------------------------------------------------

def test_durable_receipts_sanitize_paths_and_secrets(tmp_path):
    from agentflow_hermes.maintenance import durable

    db_path = str(tmp_path / "maintenance.db")
    path = _write_config(tmp_path, _guarded_config(
        db_path=db_path, repo_id="repo1", cycle_ref="sha-secret",
        provenance="TOKEN=abc123 /home/alice/secret",
        repo_path="/home/alice/private/hermes",
    ))
    fake = FakeServiceExecutor(healthy=True)
    adapter = FakeActionAdapter(fake)
    report = evaluate_runner(load_runner_config(path), adapter=adapter, now=1000.0)

    blob = json.dumps(report)
    assert "/home/alice" not in blob
    assert "TOKEN=abc123" not in blob

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("select * from maintenance_cycles").fetchall()
    dump = json.dumps([dict(r) for r in rows])
    assert "/home/alice" not in dump
    assert "TOKEN=abc123" not in dump
