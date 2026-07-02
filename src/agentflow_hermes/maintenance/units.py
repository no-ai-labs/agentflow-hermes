"""M11 systemd user unit/timer templates for the external maintenance runner.

Renders string content only — no filesystem writes, no ``systemctl`` calls.
Only the validated absolute config path and executable name are substituted
into the templates (no f-string injection of untrusted data); every other
line is a fixed literal. The rendered ``ExecStart`` invokes the existing safe
CLI entrypoint, ``maintenance runner evaluate --input-file <config>``, which
is request-only/dry-run by construction (see ``maintenance/runner.py``).

The runner unit is placed in its own ``agentflow-maintenance.slice`` so a
gateway restart can never reap it (design doc §1, §8).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SLICE_NAME = "agentflow-maintenance.slice"
RUNNER_SERVICE_NAME = "agentflow-runner.service"
RUNNER_TIMER_NAME = "agentflow-runner.timer"

_DEFAULT_ON_UNIT_ACTIVE_SEC = "30min"
_DEFAULT_RANDOMIZED_DELAY_SEC = "300"

_SAFE_EXEC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_UNSAFE_CHARS_RE = re.compile(r"[\s\x00]")


class UnitRenderError(ValueError):
    """A unit could not be safely rendered; caller must fail closed."""


def _validate_config_path(config_path: Path | str) -> Path:
    text = str(config_path)
    if _UNSAFE_CHARS_RE.search(text):
        raise UnitRenderError("config path contains unsafe whitespace/control characters")
    path = Path(text)
    if not path.is_absolute():
        raise UnitRenderError(f"config path must be absolute: {config_path!r}")
    return path


def _validate_exec_name(exec_name: str) -> str:
    if not isinstance(exec_name, str) or not _SAFE_EXEC_NAME_RE.fullmatch(exec_name):
        raise UnitRenderError(f"unsafe or malformed executable name: {exec_name!r}")
    return exec_name

def _validate_exec_dir(exec_dir: str) -> str:
    if not isinstance(exec_dir, str) or _UNSAFE_CHARS_RE.search(exec_dir):
        raise UnitRenderError(f"unsafe or malformed executable dir: {exec_dir!r}")
    if not (exec_dir.startswith("/") or exec_dir.startswith("%h/")):
        raise UnitRenderError(f"executable dir must be absolute or %h-relative: {exec_dir!r}")
    return exec_dir.rstrip("/")


def render_maintenance_slice_unit() -> str:
    """Render the slice that keeps the runner outside the gateway cgroup."""
    return (
        "[Unit]\n"
        "Description=AgentFlow Hermes external maintenance runner slice\n"
        "Before=slices.target\n"
    )


def render_runner_service_unit(
    config_path: Path | str,
    *,
    exec_name: str = "agentflow-hermes",
    exec_dir: str = "%h/.local/bin",
) -> str:
    """Render the runner oneshot service unit.

    ExecStart is fixed to ``maintenance runner evaluate --input-file <config>``
    — the only entrypoint the current CLI exposes for the runner, and it is
    request-only/dry-run by construction. No other subcommand is reachable
    via this template.
    """
    validated_path = _validate_config_path(config_path)
    validated_exec = _validate_exec_name(exec_name)
    validated_exec_dir = _validate_exec_dir(exec_dir)
    exec_start = f"{validated_exec_dir}/{validated_exec} maintenance runner evaluate --input-file {validated_path}"
    return (
        "[Unit]\n"
        "Description=AgentFlow Hermes external maintenance runner (request-only evaluate)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Slice={SLICE_NAME}\n"
        f"ExecStart={exec_start}\n"
    )


def render_runner_timer_unit(
    *,
    on_unit_active_sec: str = _DEFAULT_ON_UNIT_ACTIVE_SEC,
    randomized_delay_sec: str = _DEFAULT_RANDOMIZED_DELAY_SEC,
) -> str:
    """Render the runner timer: conservative cadence plus randomized jitter."""
    return (
        "[Unit]\n"
        "Description=AgentFlow Hermes external maintenance runner timer\n"
        "\n"
        "[Timer]\n"
        f"OnBootSec={on_unit_active_sec}\n"
        f"OnUnitActiveSec={on_unit_active_sec}\n"
        f"RandomizedDelaySec={randomized_delay_sec}\n"
        f"Unit={RUNNER_SERVICE_NAME}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


@dataclass(frozen=True)
class RenderedUnits:
    slice_unit: str
    service_unit: str
    timer_unit: str


def render_runner_units(config_path: Path | str) -> RenderedUnits:
    """Render the slice + service + timer bundle for one config path."""
    return RenderedUnits(
        slice_unit=render_maintenance_slice_unit(),
        service_unit=render_runner_service_unit(config_path),
        timer_unit=render_runner_timer_unit(),
    )
