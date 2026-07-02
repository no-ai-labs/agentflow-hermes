"""M10 external maintenance runner MVP — gateway-outside, policy-gated.

The runner is the *external* actor described in
``docs/maintenance-plugin-runner-design.md`` §5. It is deliberately not part of
the gateway process/cgroup and never imports Hermes core. In this MVP it only
performs safe dry-run/proposal mechanics:

- no real ``systemctl``/service restart (the real executor is a stub that is
  never reachable by default),
- no gateway restart, live send, active wake, or board rewrite,
- observe / request-only by default.

It reads a small JSON policy/config fixture and produces a machine-readable,
sanitized report. Fail-closed gate order for any service-cycle path:

  1. kill switch first,
  2. mode (default ``request_only``) must be ``guarded_cycle``,
  3. target unit must be a verbatim member of the exact service allowlist,
  4. a valid ``service_cycle`` trust grant must exist for that exact unit,
  5. a malformed/missing allowlist blocks the service path.

Any miss fails closed to a ``BLOCK`` refusal with no executed action.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentflow_hermes.live.sanitize import (
    safe_durable_ref,
    safe_event_payload,
    short_text,
)
from agentflow_hermes.maintenance.adapter import (
    ActionAdapter,
    ActionReceipt,
    ActionRequest,
    ExecResult,
    FakeActionAdapter,
    FakeServiceExecutor,
    LiveActionAdapter,
    NoopActionAdapter,
    ServiceExecutor,
    UnavailableSystemctlExecutor,
)
from agentflow_hermes.loop_supervisor import (
    InMemoryLoopLedger,
    LoopEvent,
    LoopPolicy,
    build_loop_report,
    evaluate_loop_event,
)
from agentflow_hermes.maintenance.policy import MaintenancePolicy, load_maintenance_policy
from agentflow_hermes.maintenance.trust import (
    host_binding,
    is_valid_service_cycle_grant,
    validate_trust_grants_shape,
)

# Absolute ceiling on service-action attempts, independent of any config value.
# Proves the runner can never storm real services even if a config asks for it.
HARD_ATTEMPT_CAP = 3


# Re-exported so existing imports (``from ...runner import FakeServiceExecutor``)
# keep working after the adapter boundary was split into ``adapter.py``.
__all__ = [
    "HARD_ATTEMPT_CAP",
    "ActionAdapter",
    "ActionReceipt",
    "ActionRequest",
    "ExecResult",
    "FakeActionAdapter",
    "FakeServiceExecutor",
    "LiveActionAdapter",
    "NoopActionAdapter",
    "RunnerConfig",
    "ServiceExecutor",
    "UnavailableSystemctlExecutor",
    "evaluate_runner",
    "load_runner_config",
    "run_runner_evaluate",
]


@dataclass(frozen=True)
class RunnerConfig:
    mode: str = "request_only"
    kill_switch: bool = False
    allowed_services: tuple[str, ...] = ()
    trust_grants: tuple[dict[str, Any], ...] = ()
    trust_grants_malformed: bool = False
    host_id: str = ""
    requested_action: str = "observe"
    target_unit: str = ""
    attempt_budget: int = 1
    cooldown_seconds: float = 0.0
    allow_fake_execute: bool = False
    loop_fixture: dict[str, Any] | None = None
    error: str = ""


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _valid_grants(raw: Any) -> tuple[dict[str, Any], ...]:
    """Keep dict grant records; exact M12 validation happens at evaluation time."""
    if not isinstance(raw, list):
        return ()
    return tuple(entry for entry in raw if isinstance(entry, dict))


def _strict_budget(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0  # fail closed: unknown budget => no attempts
    return max(0, value)


def _strict_cooldown(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0  # unknown cooldown => no extra gating beyond budget/hard cap
    parsed = float(value)
    if parsed != parsed or parsed < 0:  # NaN or negative => no cooldown
        return 0.0
    return parsed


def load_runner_config(path: str) -> RunnerConfig:
    """Load and fail-closed parse the runner config fixture.

    The base maintenance policy fields (mode/kill_switch/allowlist) are parsed by
    the committed :func:`load_maintenance_policy` fail-closed loader so this
    module never widens allowlist safety. Runner-only keys (requested action,
    target unit, trust grants, loop fixture) are parsed here.
    """
    raw = _read_json(path)
    if raw is None:
        return RunnerConfig(kill_switch=True, error="malformed_config")

    policy: MaintenancePolicy = load_maintenance_policy(Path(path))

    action = raw.get("requested_action")
    requested_action = "service_cycle" if action == "service_cycle" else "observe"

    target_unit = short_text(raw.get("target_unit") or "")

    loop_fixture = raw.get("loop") if isinstance(raw.get("loop"), dict) else None

    return RunnerConfig(
        mode=policy.mode,
        kill_switch=policy.maintenance_kill_switch,
        allowed_services=policy.allowed_services,
        trust_grants=_valid_grants(raw.get("trust_grants")),
        trust_grants_malformed=not validate_trust_grants_shape(raw.get("trust_grants", [])),
        host_id=short_text(raw.get("host_id") or host_binding(path)),
        requested_action=requested_action,
        target_unit=target_unit,
        attempt_budget=_strict_budget(raw.get("attempt_budget", 1)),
        cooldown_seconds=_strict_cooldown(raw.get("cooldown_seconds", 0)),
        allow_fake_execute=raw.get("allow_fake_execute") is True,
        loop_fixture=loop_fixture,
        error=policy.error,
    )


def _has_service_cycle_grant(config: RunnerConfig, *, now: float) -> bool:
    for grant in config.trust_grants:
        if is_valid_service_cycle_grant(
            grant,
            target_unit=config.target_unit,
            allowed_services=config.allowed_services,
            host_id=config.host_id,
            now=now,
        ):
            return True
    return False


def _loop_summary(config: RunnerConfig) -> dict[str, Any] | None:
    """Run a request-only MP4c loop evaluation over the embedded fixture.

    Never applies: no adapter is passed, so the loop stays proposal/dry-run.
    Failures fail closed to a noop summary rather than leaking a traceback.
    """
    if not config.loop_fixture:
        return None
    fx = config.loop_fixture
    event_raw = fx.get("event") if isinstance(fx.get("event"), dict) else {}
    policy_raw = fx.get("policy") if isinstance(fx.get("policy"), dict) else {}
    try:
        allowed_event = set(LoopEvent.__dataclass_fields__)
        event = LoopEvent(**{k: v for k, v in event_raw.items() if k in allowed_event})
        allowed_policy = set(LoopPolicy.__dataclass_fields__)
        policy_kwargs = {k: v for k, v in policy_raw.items() if k in allowed_policy}
        blockers = policy_kwargs.get("allowlisted_blockers")
        if isinstance(blockers, list):
            policy_kwargs["allowlisted_blockers"] = tuple(blockers)
        policy = LoopPolicy(**policy_kwargs)
        decision = evaluate_loop_event(event, InMemoryLoopLedger(receipts=[]), policy, adapter=None)
        report = build_loop_report(decision)
    except Exception:  # fail closed
        return {"status": "noop", "action": "noop", "reason": "loop_eval_failed", "verdict": ""}
    return {
        "status": _status_from_verdict(report.get("action", ""), report.get("verdict", "")),
        "action": report.get("action", ""),
        "reason": report.get("reason", ""),
        "verdict": report.get("verdict", ""),
        "idempotency_key": report.get("idempotency_key", ""),
        "policy_resolution_ref": (report.get("receipt") or {}).get("policy_resolution_ref", ""),
        "dry_run": report.get("dry_run", True),
    }


def _status_from_verdict(action: str, verdict: str) -> str:
    if action == "noop":
        return "noop"
    if verdict in {"GO", "BLOCK", "NEED_MORE"}:
        return verdict
    return "noop"


def _synth_receipt(config: RunnerConfig, *, status: str, reason: str, dry_run: bool,
                   executed: list[dict[str, Any]], attempts: int,
                   idempotency_key: str) -> dict[str, Any]:
    """Build a machine-readable receipt for a non-adapter (block/observe) path."""
    applied = bool(executed)
    return {
        "action_id": "",
        "idempotency_key": short_text(idempotency_key),
        "target": short_text(config.target_unit),
        "status": status,
        "dry_run": dry_run,
        "fake": False,
        "noop": not applied,
        "applied": applied,
        "executed": applied,
        "attempts": attempts,
        "noop_reason": "" if applied else reason,
        "detail": "",
    }


def _base_report(config: RunnerConfig, *, status: str, reason: str, gates: dict[str, bool],
                 loop: dict[str, Any] | None, idempotency_key: str, dry_run: bool,
                 proposed: list[dict[str, Any]], executed: list[dict[str, Any]],
                 attempts: int, service_requested: bool,
                 receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    if receipt is None:
        receipt = _synth_receipt(
            config, status=status, reason=reason, dry_run=dry_run,
            executed=executed, attempts=attempts, idempotency_key=idempotency_key,
        )
    report = {
        "success": True,
        "status": status,
        "reason": reason,
        "mode": short_text(config.mode),
        "dry_run": dry_run,
        "actions": {"proposed": proposed, "executed": executed},
        "service_action": {
            "requested": service_requested,
            "target": short_text(config.target_unit),
            "attempts": attempts,
            "attempt_budget": min(config.attempt_budget, HARD_ATTEMPT_CAP),
            "executed": bool(executed),
            "dry_run": dry_run,
        },
        "receipt": receipt,
        "loop": loop,
        "safety_gates": gates,
        "policy_refs": {
            "maintenance_policy": "maintenance.json",
            "loop_policy_resolution_ref": (loop or {}).get("policy_resolution_ref", ""),
        },
        "idempotency_key": idempotency_key,
    }
    return safe_event_payload(report)


def _resolve_adapter(adapter: ActionAdapter | None, executor: ServiceExecutor | None,
                     config: RunnerConfig) -> ActionAdapter | None:
    """Pick the adapter that may execute, or ``None`` for proposal-only.

    An explicitly injected ``adapter`` is honored (post-gate). Otherwise a
    :class:`FakeServiceExecutor` is only wired when ``allow_fake_execute`` is set,
    wrapped in a bounded :class:`FakeActionAdapter`. With neither, the runner is
    proposal/dry-run only — the production CLI path.
    """
    if adapter is not None:
        return adapter
    if executor is not None and config.allow_fake_execute:
        return FakeActionAdapter(executor, cooldown_seconds=config.cooldown_seconds)
    return None


def evaluate_runner(config: RunnerConfig, *, executor: ServiceExecutor | None = None,
                    adapter: ActionAdapter | None = None,
                    now: float = 0.0) -> dict[str, Any]:
    """Evaluate one runner invocation and return a sanitized machine report.

    Never performs a real service action: an adapter is reached only after every
    fail-closed gate passes, and the only executing adapter wired by default is a
    :class:`FakeActionAdapter` over a :class:`FakeServiceExecutor` combined with
    ``allow_fake_execute``. An explicit ``adapter`` may be injected for tests.
    """
    service_requested = config.requested_action == "service_cycle"
    gates = {
        "kill_switch_clear": not config.kill_switch,
        "mode_guarded_cycle": config.mode == "guarded_cycle",
        "service_allowlisted": bool(config.target_unit) and config.target_unit in config.allowed_services,
        "trust_grant": _has_service_cycle_grant(config, now=now),
        "fake_executor_only": True,
    }

    def block(reason: str) -> dict[str, Any]:
        return _base_report(
            config, status="BLOCK", reason=reason, gates=gates, loop=loop,
            idempotency_key=idem, dry_run=True, proposed=[], executed=[],
            attempts=0, service_requested=service_requested,
        )

    idem = safe_durable_ref(
        f"maint:runner:{config.mode}:{config.requested_action}:{config.target_unit or 'observe'}",
        field="idempotency_key",
    )[0]

    loop = _loop_summary(config)

    # Malformed config fails closed before anything else.
    if config.error:
        gates["kill_switch_clear"] = False
        return block("malformed_config")

    if config.trust_grants_malformed:
        gates["trust_grant"] = False
        return block("malformed_trust_grants")

    # 1. Kill switch first.
    if config.kill_switch:
        return block("kill_switch")

    # Observe / request-only default path: never a service action.
    if not service_requested:
        status = (loop or {}).get("status", "noop")
        reason = (loop or {}).get("reason", "no_request")
        proposed = [{"kind": "loop_proposal", "action": (loop or {}).get("action", "noop")}] if loop else []
        return _base_report(
            config, status=status, reason=reason, gates=gates, loop=loop,
            idempotency_key=idem, dry_run=True, proposed=proposed, executed=[],
            attempts=0, service_requested=False,
        )

    # Service-cycle path — fail-closed gate order.
    # 2. mode must be guarded_cycle.
    if config.mode != "guarded_cycle":
        return block("mode_not_guarded_cycle")
    # 3. exact service allowlist (also covers malformed/missing allowlist).
    if not gates["service_allowlisted"]:
        return block("service_not_allowlisted")
    # 4. valid service_cycle trust grant for the exact unit.
    if not gates["trust_grant"]:
        return block("no_trust_grant")

    # Gates pass: the runner is eligible. Default is proposal/dry-run only.
    proposal = {"kind": "service_restart", "target": short_text(config.target_unit)}
    action_id = safe_durable_ref(
        f"action:service_cycle:{config.target_unit or 'observe'}", field="action_id"
    )[0]

    active = _resolve_adapter(adapter, executor, config)
    if active is None:
        # No executing adapter wired: eligible proposal, no action considered.
        receipt = _synth_receipt(
            config, status="GO", reason="eligible_proposal", dry_run=True,
            executed=[], attempts=0, idempotency_key=idem,
        )
        receipt.update({"action_id": action_id, "noop": True, "noop_reason": "proposal_only"})
        return _base_report(
            config, status="GO", reason="eligible_proposal", gates=gates, loop=loop,
            idempotency_key=idem, dry_run=True, proposed=[proposal], executed=[],
            attempts=0, service_requested=True, receipt=receipt,
        )

    # Gated adapter call: bounded attempt budget, never a real service by default.
    budget = min(config.attempt_budget, HARD_ATTEMPT_CAP)
    request = ActionRequest(
        action_id=action_id,
        idempotency_key=idem,
        target_unit=config.target_unit,
        attempt_budget=budget,
    )
    receipt = active.consider(request, now=now)
    return _report_from_receipt(config, receipt, gates=gates, loop=loop,
                                idempotency_key=idem, proposal=proposal)


def _report_from_receipt(config: RunnerConfig, receipt: ActionReceipt, *,
                         gates: dict[str, bool], loop: dict[str, Any] | None,
                         idempotency_key: str, proposal: dict[str, Any]) -> dict[str, Any]:
    """Map a bounded adapter receipt onto the sanitized runner report."""
    if receipt.noop:
        # Noop family: noop adapter, cooldown, idempotent replay, disabled live.
        # Checked first so an idempotent replay never re-emits an executed action.
        status = receipt.status
        reason = receipt.noop_reason or "noop"
        dry_run, executed = True, []
    elif receipt.applied:
        status, reason, dry_run = "GO", "service_action_applied", False
        executed = [{"kind": "service_restart", "target": receipt.target, "result": "ok"}]
    else:
        # Attempted a fake action but it failed; budget consumed, safe failure.
        status, reason, dry_run, executed = "BLOCK", "service_action_failed", True, []
    return _base_report(
        config, status=status, reason=reason, gates=gates, loop=loop,
        idempotency_key=idempotency_key, dry_run=dry_run, proposed=[proposal],
        executed=executed, attempts=receipt.attempts, service_requested=True,
        receipt=receipt.as_dict(),
    )


def run_runner_evaluate(args: Any) -> tuple[int, dict[str, Any]]:
    """CLI entrypoint: evaluate a runner config file, proposal/dry-run only.

    The CLI never injects an executor, so it can only ever produce a
    proposal/dry-run report — a real service action is unreachable from here.
    """
    config = load_runner_config(args.input_file)
    report = evaluate_runner(config, executor=None)
    # A gated BLOCK is a valid refusal, not a CLI error. Only a malformed config
    # fixture is a nonzero exit.
    rc = 2 if config.error else 0
    return rc, report
