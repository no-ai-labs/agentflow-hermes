from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def default_home() -> Path:
    return Path(os.environ.get("AGENTFLOW_HOME") or Path.home() / ".agentflow")


def policy_path() -> Path:
    return default_home() / "policy.json"


@dataclass(frozen=True)
class LivePolicy:
    live_dispatch_enabled: bool = False
    active_wake_enabled: bool = False
    kanban_apply_enabled: bool = False
    allowed_targets: tuple[str, ...] = ()
    canary_targets: tuple[str, ...] = ()
    max_sends_per_min: int = 3
    max_sends_per_target_per_hour: int = 10
    kill_switch: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "live_dispatch_enabled": self.live_dispatch_enabled,
            "active_wake_enabled": self.active_wake_enabled,
            "kanban_apply_enabled": self.kanban_apply_enabled,
            "allowed_targets": list(self.allowed_targets),
            "canary_targets": list(self.canary_targets),
            "max_sends_per_min": self.max_sends_per_min,
            "max_sends_per_target_per_hour": self.max_sends_per_target_per_hour,
            "kill_switch": self.kill_switch,
        }


_MISSING = object()


def _strict_bool(value: Any, default: bool, *, malformed_default: bool | None = None) -> bool:
    """Accept literal JSON booleans only; fail closed on malformed types.

    ``bool()`` coerces truthy strings/ints/lists/dicts (e.g. ``bool("false")``
    is ``True``), which could silently enable operator/live behavior. Missing
    values fall back to the safe default. Malformed values fall back to
    ``malformed_default`` when provided, otherwise to ``default``.
    """
    if value is _MISSING:
        return default
    if isinstance(value, bool):
        return value
    if malformed_default is not None:
        return malformed_default
    return default


def _env_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None:
        return None
    return value.strip().lower() in ("1", "true")


def load_policy() -> LivePolicy:
    """Resolve effective policy, fail-closed.

    Precedence (later overrides earlier):
      1. Built-in defaults (all off).
      2. AGENTFLOW_HOME/policy.json, if present and well-formed.
      3. Environment overrides for *_ENABLED flags (only literal "1"/"true").
      4. Per-call opt-in is evaluated by callers, not here.
    """
    policy = LivePolicy()

    path = policy_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = {}
        if isinstance(raw, dict):
            policy = LivePolicy(
                live_dispatch_enabled=_strict_bool(
                    raw.get("live_dispatch_enabled", _MISSING), policy.live_dispatch_enabled
                ),
                active_wake_enabled=_strict_bool(raw.get("active_wake_enabled", _MISSING), policy.active_wake_enabled),
                kanban_apply_enabled=_strict_bool(raw.get("kanban_apply_enabled", _MISSING), policy.kanban_apply_enabled),
                allowed_targets=tuple(raw.get("allowed_targets") or []),
                canary_targets=tuple(raw.get("canary_targets") or []),
                max_sends_per_min=int(raw.get("max_sends_per_min", policy.max_sends_per_min)),
                max_sends_per_target_per_hour=int(raw.get("max_sends_per_target_per_hour", policy.max_sends_per_target_per_hour)),
                kill_switch=_strict_bool(
                    raw.get("kill_switch", _MISSING), policy.kill_switch, malformed_default=True
                ),
            )

    # Environment overrides.
    for field, env_name in [
        ("live_dispatch_enabled", "AGENTFLOW_LIVE_DISPATCH"),
        ("active_wake_enabled", "AGENTFLOW_LIVE_WAKE"),
        ("kanban_apply_enabled", "AGENTFLOW_LIVE_KANBAN_APPLY"),
        ("kill_switch", "AGENTFLOW_KILL_SWITCH"),
    ]:
        env_value = _env_bool(env_name)
        if env_value is not None:
            policy = _replace(policy, **{field: env_value})

    return policy


def save_policy(policy: LivePolicy) -> None:
    """Persist operator config to AGENTFLOW_HOME/policy.json."""
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy.as_dict(), indent=2), encoding="utf-8")


def _replace(policy: LivePolicy, **kwargs: Any) -> LivePolicy:
    return LivePolicy(
        live_dispatch_enabled=kwargs.get("live_dispatch_enabled", policy.live_dispatch_enabled),
        active_wake_enabled=kwargs.get("active_wake_enabled", policy.active_wake_enabled),
        kanban_apply_enabled=kwargs.get("kanban_apply_enabled", policy.kanban_apply_enabled),
        allowed_targets=kwargs.get("allowed_targets", policy.allowed_targets),
        canary_targets=kwargs.get("canary_targets", policy.canary_targets),
        max_sends_per_min=kwargs.get("max_sends_per_min", policy.max_sends_per_min),
        max_sends_per_target_per_hour=kwargs.get("max_sends_per_target_per_hour", policy.max_sends_per_target_per_hour),
        kill_switch=kwargs.get("kill_switch", policy.kill_switch),
    )
