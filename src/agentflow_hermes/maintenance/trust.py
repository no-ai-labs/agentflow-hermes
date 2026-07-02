"""Explicit maintenance trust-grant UX for guarded service cycles.

This module is operator-CLI only. It never calls systemctl and never performs a
service action; it only previews or atomically writes the local maintenance policy
file that the external runner later evaluates fail-closed.
"""
from __future__ import annotations

import json
import math
import os
import socket
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from agentflow_hermes.live.sanitize import safe_event_payload, safe_job_field, short_text
from agentflow_hermes.maintenance.installer import default_maintenance_config
from agentflow_hermes.maintenance.units import UnitRenderError, validate_config_path

TRUST_SCOPE = "service_cycle"
TRUST_MODE = "guarded_cycle"
_SAFE_UNIT_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@_.:-")


class TrustGrantError(ValueError):
    """Stable-code trust-grant failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def host_binding(config_file: str | Path | None = None) -> str:
    """Return a stable, non-secret host binding for the current user/config root."""
    override = os.environ.get("AGENTFLOW_MAINTENANCE_HOST_ID")
    if override:
        return safe_job_field(override, field="host_id")[0]
    if config_file:
        root = str(Path(config_file).expanduser().parent)
    else:
        root = str(Path(os.environ.get("AGENTFLOW_HOME") or Path.home() / ".agentflow"))
    material = f"{socket.gethostname()}:{root}"
    return sha256(material.encode("utf-8")).hexdigest()[:16]


def _grant_id(*, unit: str, host_id: str, created_at: float, expires_at: float) -> str:
    digest = sha256(f"{unit}:{host_id}:{created_at:.6f}:{expires_at:.6f}".encode("utf-8")).hexdigest()
    return f"grant_{digest[:16]}"


def _validate_unit(unit: Any) -> str:
    text = short_text(unit)
    if not text or any(ch not in _SAFE_UNIT_CHARS for ch in text):
        raise TrustGrantError("invalid_gateway_unit")
    if not text.endswith(".service"):
        raise TrustGrantError("invalid_gateway_unit")
    return text


def _strict_float(value: Any, *, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrustGrantError(code)
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise TrustGrantError(code)
    return parsed


def _sanitize_provenance(value: Any) -> str:
    text, _ = safe_job_field(value or "operator CLI trust grant", field="provenance")
    return text or "operator CLI trust grant"


def build_trust_grant(
    gateway_unit: str,
    *,
    host_id: str,
    created_at: float,
    expires_at: float,
    provenance: str,
) -> dict[str, Any]:
    """Build a complete M12 service-cycle grant record."""
    unit = _validate_unit(gateway_unit)
    created = _strict_float(created_at, code="invalid_created_at")
    expires = _strict_float(expires_at, code="invalid_expiry")
    if expires <= created:
        raise TrustGrantError("expiry_not_after_created")
    clean_host = safe_job_field(host_id, field="host_id")[0]
    return {
        "grant_id": _grant_id(unit=unit, host_id=clean_host, created_at=created, expires_at=expires),
        "mode": TRUST_MODE,
        "action": TRUST_SCOPE,
        "scope": TRUST_SCOPE,
        "gateway_unit": unit,
        "allowed_services": [unit],
        "host_id": clean_host,
        "created_at": created,
        "expires_at": expires,
        "provenance": _sanitize_provenance(provenance),
    }


def _load_policy_for_trust(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return default_maintenance_config()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise TrustGrantError("malformed_grant_file") from exc
    if not isinstance(raw, dict):
        raise TrustGrantError("malformed_grant_file")
    return raw


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _safe_result(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("policy_ref", "maintenance.json")
    payload.setdefault("systemctl_calls", [])
    return safe_event_payload(payload)


def _grant_summary(grant: dict[str, Any]) -> dict[str, Any]:
    return {
        "grant_id": grant.get("grant_id", ""),
        "mode": grant.get("mode", ""),
        "action": grant.get("action", ""),
        "scope": grant.get("scope", ""),
        "gateway_unit": grant.get("gateway_unit", ""),
        "allowed_services": grant.get("allowed_services", []),
        "host_id": grant.get("host_id", ""),
        "created_at": grant.get("created_at", 0.0),
        "expires_at": grant.get("expires_at", 0.0),
        "provenance": grant.get("provenance", ""),
    }


def create_trust_grant(
    config_file: str,
    *,
    gateway_unit: str,
    expires_at: float,
    provenance: str = "operator CLI trust grant",
    write: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Preview or atomically write a service-cycle trust grant."""
    try:
        config_path = validate_config_path(config_file)
        policy = _load_policy_for_trust(config_path)
        created_at = float(time.time() if now is None else now)
        unit = _validate_unit(gateway_unit)
        grant = build_trust_grant(
            unit,
            host_id=host_binding(config_path),
            created_at=created_at,
            expires_at=expires_at,
            provenance=provenance,
        )
    except (UnitRenderError, TrustGrantError) as exc:
        code = exc.code if isinstance(exc, TrustGrantError) else "invalid_config_path"
        return _safe_result({"success": False, "error": code, "dry_run": not write, "wrote_file": False})

    grants = [g for g in policy.get("trust_grants", []) if isinstance(g, dict)]
    grants = [g for g in grants if g.get("gateway_unit") != unit or g.get("scope") != TRUST_SCOPE]
    grants.append(grant)
    policy["mode"] = TRUST_MODE
    policy["requested_action"] = TRUST_SCOPE
    policy["target_unit"] = unit
    policy["allowed_services"] = [unit]
    policy["trust_grants"] = grants
    policy.setdefault("maintenance_kill_switch", False)

    if write:
        _atomic_write_json(config_path, policy)

    return _safe_result({
        "success": True,
        "error": "" if write else "dry_run",
        "dry_run": not write,
        "wrote_file": write,
        "grant": _grant_summary(grant),
        "policy": {
            "mode": policy.get("mode"),
            "requested_action": policy.get("requested_action"),
            "target_unit": policy.get("target_unit"),
            "allowed_services": policy.get("allowed_services", []),
            "trust_grants": [_grant_summary(g) for g in grants],
        },
    })


def inspect_trust_grants(config_file: str, *, now: float | None = None) -> dict[str, Any]:
    """Inspect trust posture without returning private config paths."""
    try:
        config_path = validate_config_path(config_file)
        policy = _load_policy_for_trust(config_path)
    except (UnitRenderError, TrustGrantError) as exc:
        code = exc.code if isinstance(exc, TrustGrantError) else "invalid_config_path"
        return _safe_result({"success": False, "error": code, "dry_run": True, "wrote_file": False})
    current = float(time.time() if now is None else now)
    host_id = host_binding(config_path)
    grants = policy.get("trust_grants", []) if isinstance(policy.get("trust_grants"), list) else []
    summaries = []
    for grant in grants:
        if isinstance(grant, dict):
            summary = _grant_summary(grant)
            summary["valid_now"] = is_valid_service_cycle_grant(
                grant,
                target_unit=str(grant.get("gateway_unit") or ""),
                allowed_services=tuple(policy.get("allowed_services") or ()),
                host_id=host_id,
                now=current,
            )
            summaries.append(summary)
    return _safe_result({
        "success": True,
        "error": "",
        "mode": policy.get("mode", "request_only"),
        "allowed_services": policy.get("allowed_services", []),
        "trust_grants": summaries,
        "dry_run": True,
        "wrote_file": False,
    })


def revoke_trust_grant(
    config_file: str,
    *,
    gateway_unit: str,
    write: bool = False,
) -> dict[str, Any]:
    """Preview or atomically revoke a service-cycle trust grant."""
    try:
        config_path = validate_config_path(config_file)
        policy = _load_policy_for_trust(config_path)
        unit = _validate_unit(gateway_unit)
    except (UnitRenderError, TrustGrantError) as exc:
        code = exc.code if isinstance(exc, TrustGrantError) else "invalid_config_path"
        return _safe_result({"success": False, "error": code, "dry_run": not write, "wrote_file": False})

    raw_grants = policy.get("trust_grants", []) if isinstance(policy.get("trust_grants"), list) else []
    grants = [
        g for g in raw_grants
        if not (isinstance(g, dict) and g.get("gateway_unit") == unit and g.get("scope") == TRUST_SCOPE)
    ]
    remaining_units = [
        s for s in (policy.get("allowed_services") or [])
        if isinstance(s, str) and s != unit
    ]
    policy["trust_grants"] = grants
    policy["allowed_services"] = remaining_units
    if not grants or not remaining_units:
        policy["mode"] = "request_only"
        policy["requested_action"] = "observe"
        policy["target_unit"] = ""

    if write:
        _atomic_write_json(config_path, policy)

    return _safe_result({
        "success": True,
        "error": "" if write else "dry_run",
        "dry_run": not write,
        "wrote_file": write,
        "revoked_gateway_unit": unit,
        "mode": policy.get("mode"),
        "allowed_services": remaining_units,
        "trust_grants": [_grant_summary(g) for g in grants if isinstance(g, dict)],
    })


_REQUIRED_GRANT_FIELDS = (
    "grant_id",
    "mode",
    "action",
    "scope",
    "gateway_unit",
    "allowed_services",
    "host_id",
    "created_at",
    "expires_at",
    "provenance",
)
_GRANT_ID_PREFIX = "grant_"
_GRANT_ID_HEX_LEN = 16
_HEX_CHARS = set("0123456789abcdef")


def _valid_grant_id_shape(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith(_GRANT_ID_PREFIX):
        return False
    suffix = value[len(_GRANT_ID_PREFIX):]
    return len(suffix) == _GRANT_ID_HEX_LEN and all(ch in _HEX_CHARS for ch in suffix)


def _non_empty_sanitized(value: Any, *, field: str) -> bool:
    if value is None:
        return False
    return bool(safe_job_field(value, field=field)[0])


def _is_trust_grant_record_shape(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    if any(field not in entry for field in _REQUIRED_GRANT_FIELDS):
        return False
    try:
        unit = _validate_unit(entry.get("gateway_unit"))
        created = _strict_float(entry.get("created_at"), code="invalid_created_at")
        expires = _strict_float(entry.get("expires_at"), code="invalid_expiry")
    except TrustGrantError:
        return False
    if expires <= created:
        return False
    if not _valid_grant_id_shape(entry.get("grant_id")):
        return False
    if entry.get("mode") != TRUST_MODE:
        return False
    if entry.get("action") != TRUST_SCOPE or entry.get("scope") != TRUST_SCOPE:
        return False
    if entry.get("allowed_services") != [unit]:
        return False
    if not _non_empty_sanitized(entry.get("host_id"), field="host_id"):
        return False
    if not _non_empty_sanitized(entry.get("provenance"), field="provenance"):
        return False
    return True


def validate_trust_grants_shape(raw: Any) -> bool:
    """Validate the entire trust_grants collection shape.

    Fail closed on any malformed entry: a single non-object or malformed grant
    record must invalidate the whole collection, even if another record is a
    valid grant for the requested service. Semantic grant eligibility (mode,
    host binding, expiry relative to now, exact target) remains in
    :func:`is_valid_service_cycle_grant`.
    """
    if not isinstance(raw, list):
        return False
    return all(_is_trust_grant_record_shape(entry) for entry in raw)


def is_valid_service_cycle_grant(
    grant: dict[str, Any],
    *,
    target_unit: str,
    allowed_services: tuple[str, ...],
    host_id: str,
    now: float,
) -> bool:
    """Validate one grant exactly for one target unit; any miss fails closed."""
    try:
        unit = _validate_unit(grant.get("gateway_unit"))
        created = _strict_float(grant.get("created_at"), code="invalid_created_at")
        expires = _strict_float(grant.get("expires_at"), code="invalid_expiry")
    except TrustGrantError:
        return False
    if unit != target_unit or unit not in allowed_services:
        return False
    if grant.get("mode") != TRUST_MODE:
        return False
    if grant.get("action") != TRUST_SCOPE or grant.get("scope") != TRUST_SCOPE:
        return False
    if grant.get("allowed_services") != [unit]:
        return False
    if not isinstance(grant.get("grant_id"), str) or not str(grant.get("grant_id")).startswith("grant_"):
        return False
    if safe_job_field(grant.get("host_id"), field="host_id")[0] != host_id:
        return False
    if expires <= created or expires <= now:
        return False
    if not _sanitize_provenance(grant.get("provenance")):
        return False
    return True
