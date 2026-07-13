"""systemd user service/timer install for ``agentflowd`` (plan 9.5/12).

The safe default is still render/print only, and file writes still require an
explicit ``--unit-dir``. The M27 live rollout path additionally exposes a
guarded ``enable(..., apply=True)`` helper that runs the exact
``systemctl --user`` commands needed to enable/start the single long-lived
``agentflowd.service`` plus the quiet reconciliation timer. ``status`` and
``uninstall`` remain file-presence helpers so most tests do not need a real
service manager.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SAFE_EXEC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_UNSAFE_CHARS_RE = re.compile(r"[\s\x00]")

SERVICE_NAME = "agentflowd.service"
RECONCILE_SERVICE_NAME = "agentflow-reconcile.service"
RECONCILE_TIMER_NAME = "agentflow-reconcile.timer"

_DEFAULT_RECONCILE_ON_UNIT_ACTIVE_SEC = "5min"


class ServiceRenderError(ValueError):
    """A unit could not be safely rendered; caller must fail closed."""


def _validate_absolute(path_str: str, *, label: str) -> Path:
    text = str(path_str)
    if _UNSAFE_CHARS_RE.search(text):
        raise ServiceRenderError(f"{label} contains unsafe whitespace/control characters")
    path = Path(text)
    if not path.is_absolute():
        raise ServiceRenderError(f"{label} must be an absolute path: {path_str!r}")
    return path


def _validate_exec_name(exec_name: str) -> str:
    if not isinstance(exec_name, str) or not _SAFE_EXEC_NAME_RE.fullmatch(exec_name):
        raise ServiceRenderError(f"unsafe or malformed executable name: {exec_name!r}")
    return exec_name


def render_agentflowd_service_unit(
    script_path: Path | str,
    *,
    python_exec: str = "python3",
    extra_args: str = "",
) -> str:
    """Render the long-lived agentflowd runtime unit. ExecStart only ever
    invokes ``scripts/agentflowd.py run`` (dry-run by construction unless the
    operator's own ``extra_args`` includes ``--apply``)."""
    validated_script = _validate_absolute(script_path, label="script path")
    _validate_exec_name(python_exec.rsplit("/", 1)[-1])
    exec_start = f"{python_exec} {validated_script} run"
    if extra_args:
        if _UNSAFE_CHARS_RE.search(extra_args.replace(" ", "")):
            raise ServiceRenderError("extra_args contains unsafe control characters")
        exec_start = f"{exec_start} {extra_args}"
    return (
        "[Unit]\n"
        "Description=AgentFlow Hermes zero-ceremony continuation daemon\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_reconcile_service_unit(script_path: Path | str, *, python_exec: str = "python3", extra_args: str = "") -> str:
    """Render the reconciliation oneshot service, invoked by the timer below.
    Shares the same ``extra_args`` (``--boards-root``/``--db``/etc.) as the
    long-lived service unit so both units point at the same store/boards."""
    validated_script = _validate_absolute(script_path, label="script path")
    _validate_exec_name(python_exec.rsplit("/", 1)[-1])
    exec_start = f"{python_exec} {validated_script} reconcile"
    if extra_args:
        if _UNSAFE_CHARS_RE.search(extra_args.replace(" ", "")):
            raise ServiceRenderError("extra_args contains unsafe control characters")
        exec_start = f"{exec_start} {extra_args}"
    return (
        "[Unit]\n"
        "Description=AgentFlow Hermes reconciliation pass (quiet recovery path only)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_start}\n"
    )


def render_reconcile_timer_unit(*, on_unit_active_sec: str = _DEFAULT_RECONCILE_ON_UNIT_ACTIVE_SEC) -> str:
    return (
        "[Unit]\n"
        "Description=AgentFlow Hermes reconciliation timer (5m quiet recovery, never the primary path)\n"
        "\n"
        "[Timer]\n"
        f"OnBootSec={on_unit_active_sec}\n"
        f"OnUnitActiveSec={on_unit_active_sec}\n"
        f"Unit={RECONCILE_SERVICE_NAME}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


@dataclass(frozen=True)
class RenderedServiceUnits:
    service_unit: str
    reconcile_service_unit: str
    reconcile_timer_unit: str


def render_units(script_path: Path | str, *, python_exec: str = "python3", extra_args: str = "") -> RenderedServiceUnits:
    return RenderedServiceUnits(
        service_unit=render_agentflowd_service_unit(script_path, python_exec=python_exec, extra_args=extra_args),
        reconcile_service_unit=render_reconcile_service_unit(script_path, python_exec=python_exec, extra_args=extra_args),
        reconcile_timer_unit=render_reconcile_timer_unit(),
    )


def render_install_plan(script_file: str, *, extra_args: str = "") -> dict[str, Any]:
    """Render unit contents only. No writes, no systemctl."""
    script_path = _validate_absolute(script_file, label="script file")
    units = render_units(script_path, extra_args=extra_args)
    return {
        "success": True,
        "script_path": str(script_path),
        "units": {
            SERVICE_NAME: units.service_unit,
            RECONCILE_SERVICE_NAME: units.reconcile_service_unit,
            RECONCILE_TIMER_NAME: units.reconcile_timer_unit,
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


def install(
    script_file: str,
    *,
    unit_dir: str | None = None,
    write_files: bool = False,
    extra_args: str = "",
) -> dict[str, Any]:
    """Render (and optionally write) agentflowd units.

    This function itself never executes ``systemctl``; callers that want the
    explicit live rollout path must call :func:`enable` separately.
    """
    plan = render_install_plan(script_file, extra_args=extra_args)
    written_files: list[str] = []
    if write_files:
        if not unit_dir:
            raise ServiceRenderError("--write-files requires an explicit --unit-dir")
        written_files = write_units(unit_dir, plan["units"])
    plan["written_files"] = written_files
    plan["wrote_files"] = bool(written_files)
    return plan


def status(unit_dir: str) -> dict[str, Any]:
    """File-presence-only status: which of the three unit files exist in
    ``unit_dir``. Never queries systemd."""
    dir_path = _validate_absolute(unit_dir, label="unit dir")
    names = (SERVICE_NAME, RECONCILE_SERVICE_NAME, RECONCILE_TIMER_NAME)
    installed = {name: (dir_path / name).exists() for name in names}
    return {
        "success": True,
        "unit_dir": str(dir_path),
        "installed": installed,
        "fully_installed": all(installed.values()),
    }


def enable(*, unit_dir: str, apply: bool = False, now: bool = False) -> dict[str, Any]:
    """Guarded ``systemctl --user`` path: daemon-reload + enable the long-
    lived ``agentflowd.service`` and the quiet ``agentflow-reconcile.timer``
    (never the reconcile *service* directly — that only runs via the
    timer). Dry-run by default (returns the exact commands without running
    them); pass ``apply=True`` to actually invoke systemctl. ``now=True``
    additionally starts the units immediately (``--now``) instead of only
    enabling them for next boot/login.

    Requires the unit files to already exist in ``unit_dir`` (via
    :func:`install` with ``write_files=True``) and requires that
    ``unit_dir`` be a real systemd user unit search path
    (``~/.config/systemd/user`` or an ``XDG_CONFIG_HOME`` equivalent) for
    ``systemctl --user`` to find them by name."""
    dir_path = _validate_absolute(unit_dir, label="unit dir")
    names = (SERVICE_NAME, RECONCILE_SERVICE_NAME, RECONCILE_TIMER_NAME)
    missing = [name for name in names if not (dir_path / name).exists()]
    if missing:
        raise ServiceRenderError(f"cannot enable — missing unit files in {dir_path}: {missing}")

    enable_targets = [SERVICE_NAME, RECONCILE_TIMER_NAME]
    now_flag = ["--now"] if now else []
    commands = [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", *now_flag, *enable_targets],
    ]
    result: dict[str, Any] = {"success": True, "unit_dir": str(dir_path), "commands": commands, "applied": False, "results": []}
    if not apply:
        return result

    result["applied"] = True
    for command in commands:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=30)
        result["results"].append(
            {"command": command, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
        )
        if proc.returncode != 0:
            result["success"] = False
            break
    return result


def uninstall(unit_dir: str, *, write: bool = False) -> dict[str, Any]:
    """Remove the three unit files from ``unit_dir`` if present. Dry-run by
    default (reports what would be removed); pass ``write=True`` to delete.
    Never calls systemctl (stop/disable remains an explicit separate step)."""
    dir_path = _validate_absolute(unit_dir, label="unit dir")
    names = (SERVICE_NAME, RECONCILE_SERVICE_NAME, RECONCILE_TIMER_NAME)
    present = [name for name in names if (dir_path / name).exists()]
    removed: list[str] = []
    if write:
        for name in present:
            (dir_path / name).unlink()
            removed.append(name)
    return {"success": True, "unit_dir": str(dir_path), "present": present, "removed": removed, "dry_run": not write}
