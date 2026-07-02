"""MP4d live policy-gated action adapter boundary for the external runner.

This module draws the boundary between a *fake/noop* action adapter (the only
adapters ever wired by the production CLI) and a *live-capable* adapter whose
real-execution path is intentionally left unbuilt and disabled by default.

The runner only ever reaches an adapter *after* every fail-closed policy gate
has passed (kill switch, guarded mode, exact allowlist, host-bound trust grant,
expiry). Even then:

- :class:`NoopActionAdapter` (the production default) never executes anything.
- :class:`FakeActionAdapter` records bounded canary attempts against a
  :class:`FakeServiceExecutor` for tests only. It enforces the attempt budget,
  idempotency (no duplicate fake action for a repeated key), and an optional
  cooldown. It never touches a real service.
- :class:`LiveActionAdapter` is the live-capable boundary. It is disabled unless
  an explicit ``enabled`` flag is set and is never constructed by the CLI. When
  disabled it returns a BLOCK/NOOP receipt and never calls ``systemctl``.

Every adapter returns a machine-readable, sanitized :class:`ActionReceipt`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from agentflow_hermes.live.sanitize import short_text

# Adapter-level receipt statuses.
GO = "GO"
BLOCK = "BLOCK"
NOOP = "NOOP"


@dataclass(frozen=True)
class ExecResult:
    ok: bool
    detail: str = ""


class ServiceExecutor(Protocol):
    """Boundary that would perform a real ``systemctl --user`` restart.

    In the MVP only :class:`FakeServiceExecutor` is ever injected (tests/canary);
    the real path is intentionally unbuilt and unreachable by default.
    """

    def restart_unit(self, unit: str) -> ExecResult: ...


class UnavailableSystemctlExecutor:
    """Stub standing in for the real privileged executor.

    Deliberately not implemented in this MVP: it must never be reachable by the
    default code path. Calling it raises so a misconfiguration fails loudly
    instead of silently touching a real service.
    """

    def restart_unit(self, unit: str) -> ExecResult:  # pragma: no cover - guard
        raise RuntimeError(
            "real systemctl executor is not available in the M10 runner MVP; "
            "production service restart is not supported"
        )


class FakeServiceExecutor:
    """Test/canary executor. Records calls, never touches a real service."""

    def __init__(self, *, healthy: bool = True, fail_times: int = 0) -> None:
        self.healthy = healthy
        self.fail_times = fail_times
        self.calls: list[str] = []

    def restart_unit(self, unit: str) -> ExecResult:
        self.calls.append(short_text(unit))
        if len(self.calls) <= self.fail_times:
            return ExecResult(ok=False, detail="canary_unhealthy")
        return ExecResult(ok=self.healthy, detail="ok" if self.healthy else "unhealthy")


@dataclass(frozen=True)
class ActionRequest:
    """A gated request to consider a single bounded service action."""

    action_id: str
    idempotency_key: str
    target_unit: str
    attempt_budget: int


@dataclass(frozen=True)
class ActionReceipt:
    """Machine-readable, sanitized outcome of an adapter consideration.

    ``noop``/``applied``/``executed`` are mutually informative: a fake execution
    that succeeded is ``applied`` and ``executed``; a blocked, cooled-down, or
    replayed request is a ``noop`` with a stable ``noop_reason``.
    """

    action_id: str
    idempotency_key: str
    target: str
    status: str
    dry_run: bool
    fake: bool
    noop: bool
    applied: bool
    executed: bool
    attempts: int
    noop_reason: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "idempotency_key": self.idempotency_key,
            "target": self.target,
            "status": self.status,
            "dry_run": self.dry_run,
            "fake": self.fake,
            "noop": self.noop,
            "applied": self.applied,
            "executed": self.executed,
            "attempts": self.attempts,
            "noop_reason": self.noop_reason,
            "detail": self.detail,
        }


class ActionAdapter(Protocol):
    """Boundary the gated runner invokes to consider one service action."""

    is_live: bool

    def consider(self, request: ActionRequest, *, now: float = 0.0) -> ActionReceipt: ...


def _base_fields(request: ActionRequest) -> dict[str, Any]:
    return {
        "action_id": short_text(request.action_id),
        "idempotency_key": short_text(request.idempotency_key),
        "target": short_text(request.target_unit),
    }


class NoopActionAdapter:
    """Default production adapter: never executes, always a NOOP receipt."""

    is_live = False

    def consider(self, request: ActionRequest, *, now: float = 0.0) -> ActionReceipt:
        return ActionReceipt(
            **_base_fields(request),
            status=NOOP,
            dry_run=True,
            fake=False,
            noop=True,
            applied=False,
            executed=False,
            attempts=0,
            noop_reason="noop_adapter",
        )


class FakeActionAdapter:
    """Test/canary adapter over a :class:`FakeServiceExecutor`.

    Enforces the attempt budget, idempotency, and an optional cooldown. Records
    bounded canary attempts only; it never touches a real service. A repeated
    request with an already-applied idempotency key replays the prior receipt
    without a new fake action.
    """

    is_live = False

    def __init__(self, executor: ServiceExecutor, *, cooldown_seconds: float = 0.0) -> None:
        self._executor = executor
        try:
            self._cooldown = max(0.0, float(cooldown_seconds))
        except (TypeError, ValueError):
            self._cooldown = 0.0
        self._applied: dict[str, ActionReceipt] = {}
        self._last_action_at: float | None = None

    def consider(self, request: ActionRequest, *, now: float = 0.0) -> ActionReceipt:
        base = _base_fields(request)

        # Idempotency: an already-applied key replays with no new fake action.
        prior = self._applied.get(request.idempotency_key)
        if prior is not None:
            return ActionReceipt(
                **base, status=NOOP, dry_run=True, fake=True, noop=True,
                applied=prior.applied, executed=prior.executed, attempts=0,
                noop_reason="idempotent_replay", detail=prior.detail,
            )

        # Cooldown: gate repeated requests inside the window with no action.
        if (
            self._cooldown > 0
            and self._last_action_at is not None
            and (now - self._last_action_at) < self._cooldown
        ):
            return ActionReceipt(
                **base, status=NOOP, dry_run=True, fake=True, noop=True,
                applied=False, executed=False, attempts=0, noop_reason="cooldown_active",
            )

        budget = max(0, int(request.attempt_budget))
        if budget == 0:
            return ActionReceipt(
                **base, status=NOOP, dry_run=True, fake=True, noop=True,
                applied=False, executed=False, attempts=0, noop_reason="no_attempt_budget",
            )

        attempts = 0
        result = ExecResult(ok=False, detail="not_attempted")
        for _ in range(budget):
            attempts += 1
            result = self._executor.restart_unit(request.target_unit)
            if result.ok:
                break
        # A consumed attempt starts the cooldown window whether or not it applied.
        self._last_action_at = now

        if result.ok:
            receipt = ActionReceipt(
                **base, status=GO, dry_run=False, fake=True, noop=False,
                applied=True, executed=True, attempts=attempts,
                noop_reason="", detail=short_text(result.detail),
            )
            self._applied[request.idempotency_key] = receipt
            return receipt

        # Failed fake execution consumes the budget and reports failure safely;
        # it is not recorded as applied, so a later request may retry.
        return ActionReceipt(
            **base, status=BLOCK, dry_run=True, fake=True, noop=False,
            applied=False, executed=False, attempts=attempts,
            noop_reason="", detail="service_action_failed",
        )


class LiveActionAdapter:
    """Live-capable boundary — DISABLED by default and never wired by the CLI.

    When disabled it returns a BLOCK/NOOP receipt and never calls the executor.
    The enabled path is an explicit future flag; production never sets it and
    never injects a working executor (the default is
    :class:`UnavailableSystemctlExecutor`, which raises), so no real
    ``systemctl`` restart can occur through this MVP.
    """

    is_live = True

    def __init__(self, executor: ServiceExecutor | None = None, *, enabled: bool = False) -> None:
        self._executor = executor if executor is not None else UnavailableSystemctlExecutor()
        self._enabled = bool(enabled)

    def consider(self, request: ActionRequest, *, now: float = 0.0) -> ActionReceipt:
        base = _base_fields(request)

        if not self._enabled:
            return ActionReceipt(
                **base, status=BLOCK, dry_run=True, fake=False, noop=True,
                applied=False, executed=False, attempts=0,
                noop_reason="live_adapter_disabled",
            )

        # Enabled path: intentionally guarded. Any executor failure (including the
        # default unavailable executor raising) fails closed to a BLOCK receipt.
        attempts = 0
        try:
            attempts = 1
            result = self._executor.restart_unit(request.target_unit)
        except Exception:  # fail closed: never surface a real service action
            return ActionReceipt(
                **base, status=BLOCK, dry_run=True, fake=False, noop=True,
                applied=False, executed=False, attempts=attempts,
                noop_reason="live_execution_unavailable",
            )
        if result.ok:
            return ActionReceipt(
                **base, status=GO, dry_run=False, fake=False, noop=False,
                applied=True, executed=True, attempts=attempts,
                noop_reason="", detail=short_text(result.detail),
            )
        return ActionReceipt(
            **base, status=BLOCK, dry_run=True, fake=False, noop=False,
            applied=False, executed=False, attempts=attempts,
            noop_reason="", detail="service_action_failed",
        )
