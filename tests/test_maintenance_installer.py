from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stdout

import pytest

from agentflow_hermes.cli import main as cli_main
from agentflow_hermes.maintenance.installer import (
    default_maintenance_config,
    install_runner,
    render_install_plan,
    write_units,
)
from agentflow_hermes.maintenance.runner import evaluate_runner, load_runner_config
from agentflow_hermes.maintenance.units import (
    RUNNER_SERVICE_NAME,
    RUNNER_TIMER_NAME,
    SLICE_NAME,
    UnitRenderError,
)


def test_render_install_plan_contains_expected_command_and_config_path(tmp_path):
    config_path = tmp_path / "maintenance.json"
    config_path.write_text(json.dumps(default_maintenance_config()), encoding="utf-8")

    plan = render_install_plan(str(config_path))

    assert plan["success"] is True
    assert plan["systemctl_calls"] == []
    service = plan["units"][RUNNER_SERVICE_NAME]
    assert "maintenance runner evaluate --input-file" in service
    assert str(config_path) in service
    assert SLICE_NAME in plan["units"]
    assert RUNNER_TIMER_NAME in plan["units"]


def test_default_config_is_request_only_and_blocks_service_cycle(tmp_path):
    config = default_maintenance_config()
    assert config["mode"] == "request_only"
    assert config["maintenance_kill_switch"] is False
    assert config["allowed_services"] == []
    assert not config.get("trust_grants")
    assert config["requested_action"] == "observe"

    config_path = tmp_path / "maintenance.json"
    # Prove the generated defaults block service_cycle even if something asked for it.
    config["requested_action"] = "service_cycle"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    report = evaluate_runner(load_runner_config(str(config_path)))

    assert report["status"] == "BLOCK"
    assert report["reason"] == "mode_not_guarded_cycle"
    assert report["actions"]["executed"] == []


def test_install_runner_default_never_writes_files(tmp_path):
    config_path = tmp_path / "maintenance.json"

    result = install_runner(str(config_path))

    assert result["wrote_files"] is False
    assert result["written_files"] == []
    # Default config was written since it did not exist yet.
    assert config_path.exists()
    loaded = json.loads(config_path.read_text(encoding="utf-8"))
    assert loaded["mode"] == "request_only"


def test_install_runner_never_calls_systemctl(tmp_path, monkeypatch):
    config_path = tmp_path / "maintenance.json"
    unit_dir = tmp_path / "units"

    def _forbidden(*args, **kwargs):
        raise AssertionError("systemctl/subprocess must never be invoked by the installer")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)

    result = install_runner(str(config_path), unit_dir=str(unit_dir), write_files=True)

    assert result["wrote_files"] is True
    assert len(result["written_files"]) == 3


def test_write_files_requires_explicit_unit_dir(tmp_path):
    config_path = tmp_path / "maintenance.json"
    with pytest.raises(UnitRenderError):
        install_runner(str(config_path), write_files=True)


def test_write_files_writes_only_into_explicit_unit_dir(tmp_path):
    config_path = tmp_path / "maintenance.json"
    unit_dir = tmp_path / "explicit_units"

    result = install_runner(str(config_path), unit_dir=str(unit_dir), write_files=True)

    for path_str in result["written_files"]:
        assert path_str.startswith(str(unit_dir))
    assert (unit_dir / RUNNER_SERVICE_NAME).exists()
    assert (unit_dir / RUNNER_TIMER_NAME).exists()
    assert (unit_dir / SLICE_NAME).exists()


def test_write_units_rejects_relative_unit_dir(tmp_path):
    with pytest.raises(UnitRenderError):
        write_units("relative/units", {"a.service": "x"})


def test_malformed_config_path_fails_closed_sanitized():
    with pytest.raises(UnitRenderError):
        render_install_plan("relative/maintenance.json")


def test_malformed_unit_dir_fails_closed_sanitized(tmp_path):
    config_path = tmp_path / "maintenance.json"
    with pytest.raises(UnitRenderError):
        install_runner(str(config_path), unit_dir="relative/units", write_files=True)


# CLI integration


def test_cli_render_units_prints_plan_without_writing_or_systemctl(tmp_path, monkeypatch):
    config_path = tmp_path / "maintenance.json"
    config_path.write_text(json.dumps(default_maintenance_config()), encoding="utf-8")

    def _forbidden(*args, **kwargs):
        raise AssertionError("render-units must never invoke systemctl/subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)

    captured = io.StringIO()
    with redirect_stdout(captured):
        rc = cli_main(["maintenance", "render-units", "--config-file", str(config_path)])
    data = json.loads(captured.getvalue())

    assert rc == 0
    assert data["systemctl_calls"] == []
    assert RUNNER_SERVICE_NAME in data["units"]


def test_cli_install_runner_default_does_not_write_files(tmp_path):
    config_path = tmp_path / "maintenance.json"

    captured = io.StringIO()
    with redirect_stdout(captured):
        rc = cli_main(["maintenance", "install-runner", "--config-file", str(config_path)])
    data = json.loads(captured.getvalue())

    assert rc == 0
    assert data["wrote_files"] is False
    assert data["written_files"] == []


def test_cli_install_runner_with_write_files_writes_only_to_unit_dir(tmp_path, monkeypatch):
    config_path = tmp_path / "maintenance.json"
    unit_dir = tmp_path / "cli_units"

    def _forbidden(*args, **kwargs):
        raise AssertionError("install-runner must never invoke systemctl/subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)

    captured = io.StringIO()
    with redirect_stdout(captured):
        rc = cli_main([
            "maintenance", "install-runner",
            "--config-file", str(config_path),
            "--unit-dir", str(unit_dir),
            "--write-files",
        ])
    data = json.loads(captured.getvalue())

    assert rc == 0
    assert data["wrote_files"] is True
    for path_str in data["written_files"]:
        assert path_str.startswith(str(unit_dir))


def test_cli_install_runner_malformed_config_path_fails_closed():
    captured = io.StringIO()
    with redirect_stdout(captured):
        rc = cli_main(["maintenance", "install-runner", "--config-file", "relative/maintenance.json"])
    data = json.loads(captured.getvalue())

    assert rc == 2
    assert data["success"] is False
