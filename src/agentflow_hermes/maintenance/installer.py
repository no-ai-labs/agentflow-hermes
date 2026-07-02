"""M11 install-runner workflow: render/print or write-to-explicit-dir only.

The default path only renders unit content and prints it (or returns it as a
dict for the CLI to serialize) — no filesystem writes beyond the operator's
explicit config path, and no ``systemctl`` call anywhere in this module.
Writing unit files requires an explicit ``unit_dir`` *and* ``write_files=True``;
even then, writes never land outside that directory.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentflow_hermes.maintenance.units import (
    RUNNER_SERVICE_NAME,
    RUNNER_TIMER_NAME,
    SLICE_NAME,
    UnitRenderError,
    render_runner_units,
    validate_config_path,
)


def default_maintenance_config() -> dict[str, Any]:
    """Safe default runner config: request_only, kill switch off, nothing granted."""
    return {
        "mode": "request_only",
        "maintenance_kill_switch": False,
        "allowed_services": [],
        "trust_grants": [],
        "requested_action": "observe",
    }


def _validate_absolute(path_str: str, *, label: str) -> Path:
    if not path_str or not Path(path_str).is_absolute():
        raise UnitRenderError(f"{label} must be an absolute path: {path_str!r}")
    return Path(path_str)


def render_install_plan(config_file: str) -> dict[str, Any]:
    """Render unit contents for the given config path. No writes, no systemctl."""
    config_path = _validate_absolute(config_file, label="config file")
    units = render_runner_units(config_path)
    return {
        "success": True,
        "config_path": str(config_path),
        "units": {
            SLICE_NAME: units.slice_unit,
            RUNNER_SERVICE_NAME: units.service_unit,
            RUNNER_TIMER_NAME: units.timer_unit,
        },
        "systemctl_calls": [],
    }


def write_units(unit_dir: str, units: dict[str, str]) -> list[str]:
    """Write rendered unit contents into unit_dir only. Never calls systemctl."""
    dir_path = _validate_absolute(unit_dir, label="unit dir")
    dir_path.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, content in units.items():
        target = dir_path / name
        target.write_text(content, encoding="utf-8")
        written.append(str(target))
    return written


def install_runner(
    config_file: str,
    *,
    unit_dir: str | None = None,
    write_files: bool = False,
) -> dict[str, Any]:
    """Render (and optionally write) runner units. Never executes systemctl.

    Default is render/print only. Writing unit files requires both
    ``write_files=True`` and an explicit ``unit_dir``. If the config file does
    not yet exist, a safe default request_only config is written there first;
    an existing operator config is never overwritten.
    """
    # Apply the same whitespace/control-char + absolute checks as unit rendering
    # BEFORE any filesystem write, so a malformed path can never leave a stray
    # default-config or unit/timer file behind.
    config_path = validate_config_path(config_file)
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(default_maintenance_config(), indent=2), encoding="utf-8")

    plan = render_install_plan(str(config_path))

    written_files: list[str] = []
    if write_files:
        if not unit_dir:
            raise UnitRenderError("--write-files requires an explicit --unit-dir")
        written_files = write_units(unit_dir, plan["units"])

    plan["written_files"] = written_files
    plan["wrote_files"] = bool(written_files)
    return plan
