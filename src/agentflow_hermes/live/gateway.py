from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class GatewayUnavailable(Exception):
    """Raised when no public Hermes send capability is discoverable."""


@dataclass(frozen=True)
class DeliveryResult:
    success: bool
    receipt_ref: str
    target: str
    detail: str = ""
    delivered: bool = False


class HermesGateway(Protocol):
    def send_message(self, *, target: str, body: str, idempotency_key: str) -> DeliveryResult: ...


class FakeGateway:
    """In-memory gateway for tests and smoke canaries. Never reaches Hermes core."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.results: dict[str, DeliveryResult] = {}
        self.unavailable = False

    def send_message(self, *, target: str, body: str, idempotency_key: str) -> DeliveryResult:
        if self.unavailable:
            raise GatewayUnavailable("fake gateway forced unavailable")
        self.calls.append({"target": target, "body": body, "idempotency_key": idempotency_key})
        result = self.results.get(target, DeliveryResult(success=True, receipt_ref=f"fake:{idempotency_key}", target=target, delivered=True))
        return result

    def set_result(self, target: str, result: DeliveryResult) -> None:
        self.results[target] = result


def resolve_gateway(ctx: Any | None = None) -> HermesGateway:
    """Discover a public Hermes send capability via feature detection.

    Only public plugin APIs are inspected (e.g. ``ctx.send_message`` or a
    documented ``send_message`` tool). If none is present, raise
    ``GatewayUnavailable`` so the caller degrades to dry-run.
    """
    if ctx is None:
        raise GatewayUnavailable("no plugin context provided")

    # Direct capability on the context object.
    direct = getattr(ctx, "send_message", None)
    if callable(direct):
        return _DirectGateway(direct)

    # Tool lookup.
    tools = getattr(ctx, "tools", None)
    if isinstance(tools, dict) and callable(tools.get("send_message")):
        return _ToolGateway(tools["send_message"])
    if hasattr(tools, "get"):
        try:
            tool = tools.get("send_message")
            if callable(tool):
                return _ToolGateway(tool)
        except Exception:
            pass

    raise GatewayUnavailable("no public send_message capability found")


class _DirectGateway:
    def __init__(self, send: Any) -> None:
        self._send = send

    def send_message(self, *, target: str, body: str, idempotency_key: str) -> DeliveryResult:
        result = self._send(target=target, body=body, idempotency_key=idempotency_key)
        if isinstance(result, DeliveryResult):
            return result
        if isinstance(result, dict):
            return DeliveryResult(
                success=bool(result.get("success", False)),
                receipt_ref=str(result.get("receipt_ref") or ""),
                target=target,
                detail=str(result.get("detail") or "")[:240],
                delivered=bool(result.get("delivered", False)),
            )
        raise GatewayUnavailable("send_message returned an unsupported shape")


class _ToolGateway:
    def __init__(self, tool: Any) -> None:
        self._tool = tool

    def send_message(self, *, target: str, body: str, idempotency_key: str) -> DeliveryResult:
        result = self._tool(target=target, body=body, idempotency_key=idempotency_key)
        if isinstance(result, DeliveryResult):
            return result
        if isinstance(result, dict):
            return DeliveryResult(
                success=bool(result.get("success", False)),
                receipt_ref=str(result.get("receipt_ref") or ""),
                target=target,
                detail=str(result.get("detail") or "")[:240],
                delivered=bool(result.get("delivered", False)),
            )
        raise GatewayUnavailable("send_message tool returned an unsupported shape")
