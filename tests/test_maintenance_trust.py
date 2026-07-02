from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

from agentflow_hermes.cli import main as cli_main
from agentflow_hermes.maintenance.installer import default_maintenance_config
from agentflow_hermes.maintenance.runner import evaluate_runner, load_runner_config


def _run_cli(argv):
    captured = io.StringIO()
    with redirect_stdout(captured):
        rc = cli_main(argv)
    return rc, json.loads(captured.getvalue())


def test_create_grant_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    config_path = tmp_path / "maintenance.json"

    rc, data = _run_cli([
        "maintenance", "trust-grant",
        "--config-file", str(config_path),
        "--gateway", "hermes-gateway.service",
        "--expires-at", "9999999999",
        "--comment", "operator approved",
    ])

    assert rc == 0
    assert data["success"] is True
    assert data["dry_run"] is True
    assert data["wrote_file"] is False
    assert data["error"] == "dry_run"
    assert data["grant"]["gateway_unit"] == "hermes-gateway.service"
    assert not config_path.exists()


def test_create_grant_explicit_write_stores_atomic_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    config_path = tmp_path / "maintenance.json"

    rc, data = _run_cli([
        "maintenance", "trust-grant",
        "--config-file", str(config_path),
        "--gateway", "hermes-gateway.service",
        "--expires-at", "9999999999",
        "--comment", "operator approved",
        "--write",
    ])

    assert rc == 0
    assert data["success"] is True
    assert data["dry_run"] is False
    assert data["wrote_file"] is True
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["mode"] == "guarded_cycle"
    assert stored["requested_action"] == "service_cycle"
    assert stored["target_unit"] == "hermes-gateway.service"
    assert stored["allowed_services"] == ["hermes-gateway.service"]
    assert len(stored["trust_grants"]) == 1
    grant = stored["trust_grants"][0]
    assert grant["grant_id"].startswith("grant_")
    assert grant["gateway_unit"] == "hermes-gateway.service"
    assert grant["action"] == "service_cycle"
    assert grant["scope"] == "service_cycle"
    assert grant["host_id"]
    assert grant["created_at"] > 0
    assert grant["expires_at"] == 9999999999.0
    assert grant["provenance"] == "operator approved"


def test_revoke_disables_grant_and_resets_request_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    config_path = tmp_path / "maintenance.json"
    _run_cli([
        "maintenance", "trust-grant",
        "--config-file", str(config_path),
        "--gateway", "hermes-gateway.service",
        "--expires-at", "9999999999",
        "--comment", "operator approved",
        "--write",
    ])

    rc, data = _run_cli([
        "maintenance", "trust-revoke",
        "--config-file", str(config_path),
        "--gateway", "hermes-gateway.service",
        "--write",
    ])

    assert rc == 0
    assert data["success"] is True
    assert data["wrote_file"] is True
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["mode"] == "request_only"
    assert stored["requested_action"] == "observe"
    assert stored["target_unit"] == ""
    assert stored["allowed_services"] == []
    assert stored["trust_grants"] == []


def test_inspect_malformed_grant_file_fails_closed_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    config_path = tmp_path / "bad.json"
    config_path.write_text("{not-json /home/alice/private TOKEN=abc123", encoding="utf-8")

    rc, data = _run_cli(["maintenance", "trust-inspect", "--config-file", str(config_path)])

    assert rc == 2
    assert data["success"] is False
    assert data["error"] == "malformed_grant_file"
    blob = json.dumps(data)
    assert "/home/alice" not in blob
    assert "TOKEN=abc123" not in blob
    assert str(config_path) not in blob


def test_trust_cli_outputs_do_not_leak_raw_path_or_secret_comment(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    config_path = tmp_path / "maintenance.json"

    rc, data = _run_cli([
        "maintenance", "trust-grant",
        "--config-file", str(config_path),
        "--gateway", "hermes-gateway.service",
        "--expires-at", "9999999999",
        "--comment", "/home/alice/private TOKEN=abc123",
        "--write",
    ])

    assert rc == 0
    blob = json.dumps(data)
    assert "/home/alice" not in blob
    assert "TOKEN=abc123" not in blob
    assert str(config_path) not in blob


def test_default_config_has_no_trust_grant_and_blocks_service_cycle(tmp_path):
    config = default_maintenance_config()
    config["mode"] = "guarded_cycle"
    config["requested_action"] = "service_cycle"
    config["target_unit"] = "hermes-gateway.service"
    config["allowed_services"] = ["hermes-gateway.service"]
    config_path = tmp_path / "maintenance.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    report = evaluate_runner(load_runner_config(str(config_path)), now=2000.0)

    assert report["status"] == "BLOCK"
    assert report["reason"] == "no_trust_grant"
    assert report["actions"]["executed"] == []
