"""MaintenancePolicy: fail-closed loader mirroring live/policy.py patterns."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MISSING = object()
_VALID_MODES = {"disabled", "observe_only", "request_only", "guarded_cycle"}


def _maintenance_policy_path() -> Path:
    home = Path(os.environ.get("AGENTFLOW_HOME") or Path.home() / ".agentflow")
    return home / "maintenance.json"


def _strict_bool(value: Any, default: bool, *, malformed_default: bool | None = None) -> bool:
    if value is _MISSING:
        return default
    if isinstance(value, bool):
        return value
    if malformed_default is not None:
        return malformed_default
    return default


class _MalformedField(ValueError):
    """A numeric policy field could not be parsed; fail closed."""


def _strict_int(value: Any, default: int) -> int:
    """Parse an int field, raising _MalformedField on malformed input.

    bool is rejected explicitly (it is an int subclass) so a stray ``true``
    cannot masquerade as a numeric limit.
    """
    if value is _MISSING:
        return default
    if isinstance(value, bool):
        raise _MalformedField("bool is not a valid integer field")
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise _MalformedField(str(exc)) from exc


@dataclass(frozen=True)
class MaintenancePolicy:
    mode: str = "request_only"
    maintenance_kill_switch: bool = False
    allowed_services: tuple[str, ...] = ()
    repo_path: str = ""
    error: str = ""
    max_cycles_per_day: int = 2
    min_seconds_between_cycles: int = 1800
    require_no_active_workers: bool = True
    require_reviewed_sync_go: bool = True
    canary_before_cycle: bool = True


def load_maintenance_policy(path: Path | None = None) -> MaintenancePolicy:
    """Load maintenance policy from JSON, fail closed on malformed values.

    Mirrors the _strict_bool / malformed->True pattern from live/policy.py.
    Unknown mode values resolve to request_only (not guarded_cycle).
    Malformed maintenance_kill_switch resolves to True (hard stop).
    """
    actual_path = path or _maintenance_policy_path()
    if not actual_path.exists():
        return MaintenancePolicy()

    try:
        raw = json.loads(actual_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return MaintenancePolicy(maintenance_kill_switch=True, error="malformed_policy")

    if not isinstance(raw, dict):
        return MaintenancePolicy(maintenance_kill_switch=True, error="malformed_policy")

    mode = raw.get("mode", "request_only")
    if mode not in _VALID_MODES:
        mode = "request_only"

    kill_switch = _strict_bool(raw.get("maintenance_kill_switch", _MISSING), False, malformed_default=True)
    services = tuple(s for s in (raw.get("allowed_services") or []) if isinstance(s, str))

    # Malformed numeric limits must not raise. Fail closed: force the kill switch
    # on and drop to request_only safe mode, while preserving the exact (string-
    # filtered) service allowlist so allowlist safety is never widened.
    try:
        max_cycles_per_day = _strict_int(raw.get("max_cycles_per_day", _MISSING), 2)
        min_seconds_between_cycles = _strict_int(raw.get("min_seconds_between_cycles", _MISSING), 1800)
    except _MalformedField:
        safe_mode = "disabled" if mode == "disabled" else "request_only"
        return MaintenancePolicy(
            mode=safe_mode,
            maintenance_kill_switch=True,
            allowed_services=services,
            repo_path=str(raw.get("repo_path") or ""),
            error="malformed_numeric_field",
        )

    return MaintenancePolicy(
        mode=mode,
        maintenance_kill_switch=kill_switch,
        allowed_services=services,
        repo_path=str(raw.get("repo_path") or ""),
        max_cycles_per_day=max_cycles_per_day,
        min_seconds_between_cycles=min_seconds_between_cycles,
        require_no_active_workers=_strict_bool(raw.get("require_no_active_workers", _MISSING), True),
        require_reviewed_sync_go=_strict_bool(raw.get("require_reviewed_sync_go", _MISSING), True),
        canary_before_cycle=_strict_bool(raw.get("canary_before_cycle", _MISSING), True),
    )
