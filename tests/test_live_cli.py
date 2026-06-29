from __future__ import annotations

import json

from agentflow_hermes.cli import main as cli_main
from agentflow_hermes.live.policy import LivePolicy, load_policy, save_policy


CANARY_TARGET = "discord:#hermes-canary"


def _run(argv, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    import io

    captured = io.StringIO()
    from contextlib import redirect_stdout

    with redirect_stdout(captured):
        rc = cli_main(argv)
    text = captured.getvalue()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {"raw": text}
    return rc, data


def test_cli_live_status_defaults_off(monkeypatch, tmp_path):
    rc, data = _run(["live", "status"], monkeypatch, tmp_path)
    assert rc == 0
    assert data["success"] is True
    assert data["policy"]["live_dispatch_enabled"] is False
    assert data["policy"]["kill_switch"] is False


def test_cli_live_enable_disable_dispatch(monkeypatch, tmp_path):
    rc, data = _run(["live", "enable", "--dispatch"], monkeypatch, tmp_path)
    assert rc == 0
    assert data["policy"]["live_dispatch_enabled"] is True
    assert data["policy"]["kill_switch"] is False

    policy = load_policy()
    assert policy.live_dispatch_enabled is True

    rc, data = _run(["live", "disable", "--dispatch"], monkeypatch, tmp_path)
    assert rc == 0
    assert data["policy"]["live_dispatch_enabled"] is False
    assert data["policy"]["kill_switch"] is True


def test_cli_doctor_includes_policy_and_schema_version(monkeypatch, tmp_path):
    rc, data = _run(["doctor"], monkeypatch, tmp_path)
    assert rc == 0
    assert data["success"] is True
    assert data["schema_version"] == 3
    assert "policy" in data


def test_cli_dispatch_without_live_is_dry_run(monkeypatch, tmp_path):
    rc, data = _run(["enqueue", "--title", "t"], monkeypatch, tmp_path)
    assert rc == 0
    job_id = data["job_id"]

    rc, data = _run(["dispatch", "--job-id", job_id], monkeypatch, tmp_path)
    assert rc == 0
    assert data["success"] is True
    assert data["mode"] == "dry-run"
    assert "prompt" in data


def test_cli_dispatch_live_without_gateway_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    save_policy(LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
    ))
    rc, data = _run(["enqueue", "--title", "t", "--target", CANARY_TARGET], monkeypatch, tmp_path)
    assert rc == 0
    job_id = data["job_id"]

    rc, data = _run(["dispatch", "--job-id", job_id, "--live"], monkeypatch, tmp_path)
    assert rc == 2
    assert data["success"] is False
    assert data["error"] == "gateway_unavailable"


def test_cli_live_canary_refuses_without_policy(monkeypatch, tmp_path):
    rc, data = _run(["live", "canary", "--target", CANARY_TARGET, "--live"], monkeypatch, tmp_path)
    assert rc == 2
    assert data["success"] is False
    assert data["error"] == "live_dispatch_disabled"


def test_cli_live_canary_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    save_policy(LivePolicy(
        live_dispatch_enabled=True,
        allowed_targets=(CANARY_TARGET,),
        canary_targets=(CANARY_TARGET,),
    ))
    rc, data = _run(["live", "canary", "--target", CANARY_TARGET, "--live"], monkeypatch, tmp_path)
    assert rc == 0
    assert data["success"] is True
    assert data["mode"] == "live"
    assert data["gateway_calls"] == 1
