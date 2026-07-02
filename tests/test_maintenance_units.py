from __future__ import annotations

import pytest

from agentflow_hermes.maintenance.units import (
    RUNNER_SERVICE_NAME,
    RUNNER_TIMER_NAME,
    SLICE_NAME,
    UnitRenderError,
    render_maintenance_slice_unit,
    render_runner_service_unit,
    render_runner_timer_unit,
    render_runner_units,
)


def test_slice_unit_names_the_maintenance_slice():
    assert SLICE_NAME == "agentflow-maintenance.slice"
    content = render_maintenance_slice_unit()
    assert "[Unit]" in content


def test_runner_service_unit_uses_existing_evaluate_entrypoint_and_config_path(tmp_path):
    config_path = tmp_path / "maintenance.json"
    config_path.write_text("{}", encoding="utf-8")

    content = render_runner_service_unit(config_path)

    assert f"Slice={SLICE_NAME}" in content
    assert "Type=oneshot" in content
    assert "maintenance runner evaluate --input-file" in content
    assert str(config_path) in content
    # Only the existing safe evaluate entrypoint is ever substituted — no other
    # subcommand (e.g. a mutating "run" or "restart") is reachable via this template.
    assert "restart" not in content


def test_runner_timer_unit_has_conservative_cadence_and_randomized_delay():
    content = render_runner_timer_unit()

    assert "OnUnitActiveSec=" in content
    assert "RandomizedDelaySec=" in content
    assert f"Unit={RUNNER_SERVICE_NAME}" in content


def test_render_runner_units_bundles_slice_service_timer(tmp_path):
    config_path = tmp_path / "maintenance.json"
    config_path.write_text("{}", encoding="utf-8")

    units = render_runner_units(config_path)

    assert units.slice_unit
    assert units.service_unit
    assert units.timer_unit
    assert str(config_path) in units.service_unit


def test_relative_config_path_fails_closed():
    with pytest.raises(UnitRenderError):
        render_runner_service_unit("relative/maintenance.json")


def test_config_path_with_control_characters_fails_closed(tmp_path):
    malformed = str(tmp_path / "maintenance.json") + "\ninject=evil"
    with pytest.raises(UnitRenderError):
        render_runner_service_unit(malformed)


def test_config_path_with_whitespace_fails_closed(tmp_path):
    malformed = tmp_path / "maintenance config.json"
    with pytest.raises(UnitRenderError):
        render_runner_service_unit(malformed)


def test_malformed_exec_name_fails_closed(tmp_path):
    config_path = tmp_path / "maintenance.json"
    config_path.write_text("{}", encoding="utf-8")
    with pytest.raises(UnitRenderError):
        render_runner_service_unit(config_path, exec_name="agentflow-hermes; rm -rf /")


def test_malformed_exec_dir_fails_closed(tmp_path):
    config_path = tmp_path / "maintenance.json"
    config_path.write_text("{}", encoding="utf-8")
    with pytest.raises(UnitRenderError):
        render_runner_service_unit(config_path, exec_dir="relative/bin")


def test_no_secrets_or_unexpected_private_paths_leak_into_units(tmp_path):
    config_path = tmp_path / "maintenance.json"
    config_path.write_text("{}", encoding="utf-8")

    content = render_runner_service_unit(config_path)

    assert "TOKEN" not in content
    assert "SECRET" not in content
