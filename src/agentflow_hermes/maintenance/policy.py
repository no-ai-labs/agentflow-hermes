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


@dataclass(frozen=True)
class MaintenancePolicy:
    mode: str = "request_only"
    maintenance_kill_switch: bool = False
    allowed_services: tuple[str, ...] = ()
    repo_path: str = ""
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
        return MaintenancePolicy(maintenance_kill_switch=True)

    if not isinstance(raw, dict):
        return MaintenancePolicy(maintenance_kill_switch=True)

    mode = raw.get("mode", "request_only")
    if mode not in _VALID_MODES:
        mode = "request_only"

    kill_switch = _strict_bool(raw.get("maintenance_kill_switch", _MISSING), False, malformed_default=True)
    services = tuple(s for s in (raw.get("allowed_services") or []) if isinstance(s, str))

    return MaintenancePolicy(
        mode=mode,
        maintenance_kill_switch=kill_switch,
        allowed_services=services,
        repo_path=str(raw.get("repo_path") or ""),
        max_cycles_per_day=int(raw.get("max_cycles_per_day", 2)),
        min_seconds_between_cycles=int(raw.get("min_seconds_between_cycles", 1800)),
        require_no_active_workers=_strict_bool(raw.get("require_no_active_workers", _MISSING), True),
        require_reviewed_sync_go=_strict_bool(raw.get("require_reviewed_sync_go", _MISSING), True),
        canary_before_cycle=_strict_bool(raw.get("canary_before_cycle", _MISSING), True),
    )
