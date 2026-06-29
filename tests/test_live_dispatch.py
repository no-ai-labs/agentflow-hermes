from __future__ import annotations

import json
import time

import pytest

from agentflow_hermes.live.gateway import DeliveryResult, FakeGateway, GatewayUnavailable
from agentflow_hermes.live.policy import LivePolicy, load_policy, policy_path, save_policy
from agentflow_hermes.live.throttle import set_degraded
from agentflow_hermes.store import AgentFlowStore


CANARY_TARGET = "discord:#hermes-canary"


def _events(store, job_id, kind=None):
    with store.connect() as con:
        if kind:
            rows = con.execute(
                "select * from job_events where job_id=? and kind=? order by seq",
                (job_id, kind),
            ).fetchall()
        else:
            rows = con.execute("select * from job_events where job_id=? order by seq", (job_id,)).fetchall()
    return [dict(r) for r in rows]


def _receipts(store, job_id, phase=None):
    with store.connect() as con:
        if phase:
            rows = con.execute(
                "select * from operator_receipts where job_id=? and phase=? order by id",
                (job_id, phase),
            ).fetchall()
        else:
            rows = con.execute("select * from operator_receipts where job_id=? order by id", (job_id,)).fetchall()
    return [dict(r) for r in rows]


def test_policy_defaults_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    policy = load_policy()
    assert policy.live_dispatch_enabled is False
    assert policy.active_wake_enabled is False
    assert policy.kanban_apply_enabled is False
    assert policy.kill_switch is False
    assert policy.allowed_targets == ()
    assert policy.canary_targets == ()


def test_save_and_load_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    policy = LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
    )
    save_policy(policy)
    loaded = load_policy()
    assert loaded.live_dispatch_enabled is True
    assert loaded.allowed_targets == (CANARY_TARGET,)


MALFORMED_TRUTHY_VALUES = [
    "false",  # non-empty string is truthy under bool()
    "true",
    "0",
    1,
    -1,
    [0],
    {"enabled": False},
]

BOOLEAN_FLAGS = [
    "live_dispatch_enabled",
    "active_wake_enabled",
    "kanban_apply_enabled",
    "kill_switch",
]


def _write_policy_json(tmp_path, payload):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("flag", BOOLEAN_FLAGS)
@pytest.mark.parametrize("value", MALFORMED_TRUTHY_VALUES)
def test_policy_flags_fail_closed_on_malformed_types(tmp_path, monkeypatch, flag, value):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    _write_policy_json(tmp_path, {flag: value})
    policy = load_policy()
    # Malformed (non-boolean) values must never enable operator/live behavior.
    assert getattr(policy, flag) is False


@pytest.mark.parametrize("flag", BOOLEAN_FLAGS)
def test_policy_flags_accept_literal_booleans(tmp_path, monkeypatch, flag):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    _write_policy_json(tmp_path, {flag: True})
    assert getattr(load_policy(), flag) is True
    _write_policy_json(tmp_path, {flag: False})
    assert getattr(load_policy(), flag) is False


def test_policy_missing_flags_default_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    _write_policy_json(tmp_path, {"allowed_targets": [CANARY_TARGET]})
    policy = load_policy()
    for flag in BOOLEAN_FLAGS:
        assert getattr(policy, flag) is False
    assert policy.allowed_targets == (CANARY_TARGET,)


def test_dispatch_without_live_flag_is_dry_run(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="dry job", target=CANARY_TARGET)
    gateway = FakeGateway()
    result = store.dispatch_live(created["job_id"], gateway=gateway, live=False)
    assert result["success"] is True
    assert result.get("mode") == "dry-run"
    assert gateway.calls == []
    events = _events(store, created["job_id"])
    assert any(e["kind"] == "dispatched_dry_run" for e in events)


def test_live_refused_when_gate_off(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="live job", target=CANARY_TARGET)
    gateway = FakeGateway()
    policy = LivePolicy(
        live_dispatch_enabled=False,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
    )
    result = store.dispatch_live(created["job_id"], gateway=gateway, live=True, policy=policy)
    assert result["success"] is False
    assert result["error"] == "live_dispatch_disabled"
    assert gateway.calls == []
    receipts = _receipts(store, created["job_id"])
    assert len(receipts) == 1
    assert receipts[0]["phase"] == "refused"
    assert receipts[0]["reason"] == "live_dispatch_disabled"


def test_live_refused_when_target_not_allowed(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="live job", target=CANARY_TARGET)
    gateway = FakeGateway()
    policy = LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=("discord:#other",),
        canary_targets=(CANARY_TARGET,),
    )
    result = store.dispatch_live(created["job_id"], gateway=gateway, live=True, policy=policy)
    assert result["success"] is False
    assert result["error"] == "target_not_allowed"
    assert gateway.calls == []
    receipts = _receipts(store, created["job_id"])
    assert receipts[0]["phase"] == "refused"


def test_live_refused_when_kill_switch(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="live job", target=CANARY_TARGET)
    gateway = FakeGateway()
    policy = LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
        kill_switch=True,
    )
    result = store.dispatch_live(created["job_id"], gateway=gateway, live=True, policy=policy)
    assert result["success"] is False
    assert result["error"] == "kill_switch"
    assert gateway.calls == []
    receipts = _receipts(store, created["job_id"])
    assert receipts[0]["phase"] == "refused"
    assert receipts[0]["reason"] == "kill_switch"


def test_live_dispatch_happy_path_canary(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="live job", target=CANARY_TARGET, body="do the thing")
    gateway = FakeGateway()
    policy = LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
    )
    result = store.dispatch_live(created["job_id"], gateway=gateway, live=True, policy=policy)
    assert result["success"] is True
    assert result["applied"] is True
    assert result["mode"] == "live"
    assert result["delivery_ref"].startswith("fake:")
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["target"] == CANARY_TARGET

    job = store.get_job(created["job_id"])
    assert job["live_delivered_at"] is not None
    assert job["live_delivery_ref"].startswith("fake:")
    assert job["status"] == "dispatched"

    receipts = _receipts(store, created["job_id"])
    phases = [r["phase"] for r in receipts]
    assert phases == ["attempt", "applied"]

    events = _events(store, created["job_id"], "passive_delivery")
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload["channel"] == "live_dispatch"
    assert payload["delivery_ref"] == result["delivery_ref"]


def test_idempotent_live_send(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="live job", target=CANARY_TARGET)
    gateway = FakeGateway()
    policy = LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
    )
    r1 = store.dispatch_live(created["job_id"], gateway=gateway, live=True, policy=policy)
    r2 = store.dispatch_live(created["job_id"], gateway=gateway, live=True, policy=policy)
    assert r1["success"] is True
    assert r1["applied"] is True
    assert r2["success"] is True
    assert r2["applied"] is False
    assert r2["duplicate"] is True
    assert r2["delivery_ref"] == r1["delivery_ref"]
    assert len(gateway.calls) == 1
    receipts = _receipts(store, created["job_id"])
    assert len(receipts) == 3  # attempt, applied, duplicate refused
    assert receipts[2]["phase"] == "refused"
    assert receipts[2]["reason"] == "duplicate"


def test_throttle_blocks_storm(tmp_path, monkeypatch):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    policy = LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
        max_sends_per_min=1,
        max_sends_per_target_per_hour=10,
    )
    gateway = FakeGateway()
    jobs = []
    for i in range(3):
        created = store.enqueue(title=f"storm {i}", target=CANARY_TARGET)
        jobs.append(created["job_id"])

    results = []
    for job_id in jobs:
        results.append(store.dispatch_live(job_id, gateway=gateway, live=True, policy=policy))

    successes = [r for r in results if r["success"]]
    failures = [r for r in results if not r["success"]]
    assert len(successes) == 1
    assert len(failures) == 2
    assert all(r["error"].startswith("throttled") for r in failures)
    assert len(gateway.calls) == 1


def test_gateway_unavailable_degrades_to_dry_run(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="live job", target=CANARY_TARGET)
    policy = LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
    )
    result = store.dispatch_live(created["job_id"], gateway=None, live=True, policy=policy)
    assert result["success"] is False
    assert result["error"] == "gateway_unavailable"
    receipts = _receipts(store, created["job_id"])
    assert len(receipts) == 1
    assert receipts[0]["phase"] == "refused"


def test_gateway_failure_records_failed_and_breaker(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    gateway = FakeGateway()
    gateway.set_result(CANARY_TARGET, DeliveryResult(success=False, receipt_ref="", target=CANARY_TARGET, detail="boom", delivered=False))
    policy = LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
    )
    created = store.enqueue(title="fail job", target=CANARY_TARGET)
    result = store.dispatch_live(created["job_id"], gateway=gateway, live=True, policy=policy)
    assert result["success"] is False
    assert result["error"] == "delivery_failed"
    receipts = _receipts(store, created["job_id"])
    assert receipts[0]["phase"] == "attempt"
    assert receipts[1]["phase"] == "failed"


def test_circuit_breaker_degrades_after_failures(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    gateway = FakeGateway()
    gateway.set_result(CANARY_TARGET, DeliveryResult(success=False, receipt_ref="", target=CANARY_TARGET, detail="boom", delivered=False))
    policy = LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
    )
    for _ in range(3):
        created = store.enqueue(title="fail job", target=CANARY_TARGET)
        store.dispatch_live(created["job_id"], gateway=gateway, live=True, policy=policy)
    with store.connect() as con:
        degraded = con.execute("select value from agentflow_meta where key='degraded'").fetchone()
    assert degraded is not None
    assert degraded["value"] == "1"


def test_no_secret_or_private_path_in_receipts_or_events(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(
        title="secret job",
        target=CANARY_TARGET,
        body="TOKEN=super-secret from /home/operator/private/key.txt",
    )
    gateway = FakeGateway()
    policy = LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
    )
    store.dispatch_live(created["job_id"], gateway=gateway, live=True, policy=policy)
    with store.connect() as con:
        rows = con.execute("select * from operator_receipts").fetchall()
    durable = json.dumps([dict(r) for r in rows], ensure_ascii=False)
    assert "super-secret" not in durable
    assert "/home/operator" not in durable
    assert "TOKEN=" not in durable
    body_sent = gateway.calls[0]["body"]
    assert "super-secret" not in body_sent
    assert "/home/operator" not in body_sent


def test_private_source_ref_is_redacted_from_delivery_body(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(
        title="private ref",
        target=CANARY_TARGET,
        body="bounded body",
        source_ref="file:///home/operator/private/run.log",
        source_hash="hash1",
    )
    gateway = FakeGateway()
    policy = LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
    )
    store.dispatch_live(created["job_id"], gateway=gateway, live=True, policy=policy)
    body_sent = gateway.calls[0]["body"]
    assert "/home/operator" not in body_sent
    assert "ref:sha256:" in body_sent


def test_policy_snapshot_redacts_sensitive_allowlist_values(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="refuse", target=CANARY_TARGET)
    policy = LivePolicy(
        live_dispatch_enabled=False,
        allowed_targets=("/home/operator/private",),
        canary_targets=("TOKEN=secret1234567890",),
    )
    store.dispatch_live(created["job_id"], gateway=FakeGateway(), live=True, policy=policy)
    durable = json.dumps(_receipts(store, created["job_id"]), ensure_ascii=False)
    assert "/home/operator" not in durable
    assert "TOKEN=" not in durable
    assert "redacted" in durable


def test_per_target_hour_throttle_blocks_second_send(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    policy = LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
        max_sends_per_min=10,
        max_sends_per_target_per_hour=1,
    )
    gateway = FakeGateway()
    first = store.enqueue(title="first", target=CANARY_TARGET)["job_id"]
    second = store.enqueue(title="second", target=CANARY_TARGET)["job_id"]
    r1 = store.dispatch_live(first, gateway=gateway, live=True, policy=policy)
    r2 = store.dispatch_live(second, gateway=gateway, live=True, policy=policy)
    assert r1["success"] is True
    assert r2["success"] is False
    assert r2["error"] == "throttled_per_target_hour"
    assert len(gateway.calls) == 1


def test_resolve_gateway_from_fake_context():
    class Ctx:
        def send_message(self, *, target, body, idempotency_key):
            return DeliveryResult(success=True, receipt_ref="ctx:1", target=target, delivered=True)

    gw = __import__("agentflow_hermes.live.gateway", fromlist=["resolve_gateway"]).resolve_gateway(Ctx())
    result = gw.send_message(target=CANARY_TARGET, body="hi", idempotency_key="k")
    assert result.success is True
    assert result.receipt_ref == "ctx:1"


def test_resolve_gateway_unavailable_for_none():
    with pytest.raises(GatewayUnavailable):
        __import__("agentflow_hermes.live.gateway", fromlist=["resolve_gateway"]).resolve_gateway(None)
