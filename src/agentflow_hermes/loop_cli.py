from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .graph_creator import FakeKanbanGraphAdapter
from .live.sanitize import sanitize_string, short_text
from .loop_supervisor import (
    InMemoryLoopLedger,
    LoopEvent,
    LoopPolicy,
    build_loop_report,
    evaluate_loop_event,
)

_EVENT_FIELDS = (
    "event_id",
    "source_graph_id",
    "source_task_id",
    "event_type",
    "verdict",
    "summary",
    "blocker_class",
    "origin",
    "return_to",
    "subscription_status",
    "policy_resolution_ref",
    "round_no",
    "occurred_at",
    "source_final_id",
    "remediation_review_id",
)

_STR_EVENT_FIELDS = (
    "event_id",
    "source_graph_id",
    "source_task_id",
    "event_type",
    "verdict",
    "summary",
    "blocker_class",
    "origin",
    "return_to",
    "subscription_status",
    "policy_resolution_ref",
    "source_final_id",
    "remediation_review_id",
)
_DICT_OR_NONE_EVENT_FIELDS = ("old_final_card", "remediation_review_card")

_RECEIPT_STR_FIELDS = (
    "event_id",
    "source_graph_id",
    "source_task_id",
    "source_final_id",
    "blocker_class",
    "idempotency_key",
    "policy_resolution_ref",
    "origin_ref",
    "return_to_ref",
    "subscription_status",
    "reason",
    "decision",
    "mode",
)
_RECEIPT_INT_FIELDS = ("round_no", "same_blocker_count", "final_vn")
_RECEIPT_NUMERIC_FIELDS = ("created_at",)

_POLICY_FIELDS = (
    "active_mode",
    "apply_enabled",
    "kill_switch",
    "allowlisted_blockers",
    "max_rounds",
    "max_same_blocker",
    "max_auto_creates_per_run",
    "max_tasks_per_graph",
    "cooldown_seconds",
    "backoff_multiplier",
    "require_subscription_verified",
    "require_origin_match",
    "require_policy_resolution",
    "request_only_by_default",
    "expected_origin",
    "expected_return_to",
)


def add_loop_cli_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input-file", default="", help="fixture JSON with event/policy/ledger_receipts")

    parser.add_argument("--event-id", default=None)
    parser.add_argument("--source-graph-id", default=None)
    parser.add_argument("--source-task-id", default=None)
    parser.add_argument("--event-type", default=None)
    parser.add_argument("--verdict", default=None)
    parser.add_argument("--summary", default=None)
    parser.add_argument("--blocker-class", default=None)
    parser.add_argument("--origin", default=None)
    parser.add_argument("--return-to", default=None)
    parser.add_argument("--subscription-status", default=None)
    parser.add_argument("--policy-resolution-ref", default=None)
    parser.add_argument("--round-no", type=int, default=None)
    parser.add_argument("--occurred-at", type=float, default=None)
    parser.add_argument("--source-final-id", default=None)
    parser.add_argument("--remediation-review-id", default=None)

    parser.add_argument("--active-mode", default=None)
    parser.add_argument("--apply", action="store_true", default=False, help="shorthand for --active-mode apply; still requires --apply-enabled to open the adapter gate")
    parser.add_argument("--apply-enabled", dest="apply_enabled", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--kill-switch", dest="kill_switch", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--allowlisted-blockers", default=None, help="comma-separated blocker classes")
    parser.add_argument("--max-rounds", type=int, default=None)
    parser.add_argument("--max-same-blocker", type=int, default=None)
    parser.add_argument("--max-auto-creates-per-run", type=int, default=None)
    parser.add_argument("--max-tasks-per-graph", type=int, default=None)
    parser.add_argument("--cooldown-seconds", type=int, default=None)
    parser.add_argument("--backoff-multiplier", type=float, default=None)
    parser.add_argument("--require-subscription-verified", dest="require_subscription_verified", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--require-origin-match", dest="require_origin_match", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--require-policy-resolution", dest="require_policy_resolution", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--expected-origin", default=None)
    parser.add_argument("--expected-return-to", default=None)


def _load_fixture(path: str) -> dict[str, Any]:
    if path == "-":
        import sys

        text = sys.stdin.read()
    else:
        text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("fixture root must be a JSON object")
    return data


def _build_event_kwargs(args: argparse.Namespace, fixture_event: dict[str, Any]) -> dict[str, Any]:
    allowed = set(LoopEvent.__dataclass_fields__)
    kwargs = {k: v for k, v in fixture_event.items() if k in allowed}
    for field in _EVENT_FIELDS:
        value = getattr(args, field.replace("-", "_"), None)
        if value is not None:
            kwargs[field] = value
    for key in ("old_final_card", "remediation_review_card"):
        if key in fixture_event:
            kwargs[key] = fixture_event[key]
    kwargs.setdefault("event_id", "")
    kwargs.setdefault("source_graph_id", "")
    return kwargs


def _build_policy_kwargs(args: argparse.Namespace, fixture_policy: dict[str, Any]) -> dict[str, Any]:
    allowed = set(_POLICY_FIELDS)
    kwargs = {k: v for k, v in fixture_policy.items() if k in allowed}
    for field in _POLICY_FIELDS:
        value = getattr(args, field, None)
        if value is not None:
            kwargs[field] = value
    if getattr(args, "apply", False):
        kwargs["active_mode"] = "apply"
    blockers = kwargs.get("allowlisted_blockers")
    if isinstance(blockers, str):
        kwargs["allowlisted_blockers"] = tuple(b.strip() for b in blockers.split(",") if b.strip())
    elif isinstance(blockers, list):
        kwargs["allowlisted_blockers"] = tuple(blockers)
    return kwargs


def _validate_event_kwargs(kwargs: dict[str, Any]) -> str | None:
    """Return the offending field name if an event kwarg has a wrong/unsafe type, else None."""
    for field in _STR_EVENT_FIELDS:
        if field in kwargs and not isinstance(kwargs[field], str):
            return field
    if "round_no" in kwargs:
        value = kwargs["round_no"]
        if isinstance(value, bool) or not isinstance(value, int):
            return "round_no"
    if "occurred_at" in kwargs:
        value = kwargs["occurred_at"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "occurred_at"
    for field in _DICT_OR_NONE_EVENT_FIELDS:
        if field in kwargs and kwargs[field] is not None and not isinstance(kwargs[field], dict):
            return field
    return None


def _receipt_is_well_typed(receipt: dict[str, Any]) -> bool:
    for field in _RECEIPT_STR_FIELDS:
        if field in receipt and receipt[field] is not None and not isinstance(receipt[field], str):
            return False
    for field in _RECEIPT_INT_FIELDS:
        if field in receipt:
            value = receipt[field]
            if isinstance(value, bool) or not isinstance(value, int):
                return False
    for field in _RECEIPT_NUMERIC_FIELDS:
        if field in receipt:
            value = receipt[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
    return True


def _sanitize_ledger_receipts(raw_receipts: list[Any]) -> tuple[list[dict[str, Any]], int]:
    """Reject non-object or malformed-field receipts before ledger construction.

    Never lets a malformed receipt reach InMemoryLoopLedger/safe_event_payload,
    where non-dict or wrong-typed numeric/timestamp fields can raise or distort
    fail-closed round/cooldown accounting.
    """
    valid: list[dict[str, Any]] = []
    dropped = 0
    for receipt in raw_receipts:
        if not isinstance(receipt, dict) or not _receipt_is_well_typed(receipt):
            dropped += 1
            continue
        valid.append(receipt)
    return valid, dropped


def run_loop_evaluate(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Evaluate one loop event and return (exit_code, sanitized report dict).

    Default is always request-only / dry-run: apply only happens when the
    resolved policy has active_mode == "apply" AND apply_enabled is True, and
    even then only a FakeKanbanGraphAdapter is ever used — never a production
    board writer.
    """
    fixture: dict[str, Any] = {}
    if args.input_file:
        try:
            fixture = _load_fixture(args.input_file)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return 2, {"success": False, "error": "malformed_input", "detail": short_text(str(exc))}

    fixture_event = fixture.get("event") if isinstance(fixture.get("event"), dict) else {}
    fixture_policy = fixture.get("policy") if isinstance(fixture.get("policy"), dict) else {}
    fixture_receipts_raw = fixture.get("ledger_receipts") if isinstance(fixture.get("ledger_receipts"), list) else []

    event_kwargs = _build_event_kwargs(args, fixture_event)
    bad_field = _validate_event_kwargs(event_kwargs)
    if bad_field is not None:
        return 2, {"success": False, "error": "malformed_event", "detail": f"invalid_type:{short_text(bad_field)}"}
    try:
        event = LoopEvent(**event_kwargs)
    except TypeError:
        return 2, {"success": False, "error": "malformed_event"}

    policy_kwargs = _build_policy_kwargs(args, fixture_policy)
    try:
        policy: LoopPolicy = LoopPolicy(**policy_kwargs)
    except TypeError:
        policy = LoopPolicy(kill_switch=True)

    fixture_receipts, dropped_receipts = _sanitize_ledger_receipts(fixture_receipts_raw)
    if dropped_receipts:
        return 2, {"success": False, "error": "malformed_ledger_receipts", "detail": f"invalid_receipts:{dropped_receipts}"}
    ledger = InMemoryLoopLedger(receipts=fixture_receipts)

    apply_gate_open = policy.active_mode == "apply" and policy.apply_enabled is True
    adapter = FakeKanbanGraphAdapter() if apply_gate_open else None

    try:
        decision = evaluate_loop_event(event, ledger, policy, adapter=adapter)
        report = build_loop_report(decision)
    except Exception as exc:  # fail closed: never leak a raw traceback to CLI output
        return 2, {"success": False, "error": "evaluation_failed", "detail": sanitize_string(str(exc))}

    return 0, report
