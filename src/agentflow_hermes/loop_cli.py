from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .graph_creator import FakeKanbanGraphAdapter, RealKanbanGraphAdapter, _default_cli_runner
from .live.sanitize import sanitize_string, short_text
from .loop_supervisor import (
    InMemoryLoopLedger,
    LoopEvent,
    LoopPolicy,
    build_loop_report,
    evaluate_loop_event,
)
from .roadmap import InMemoryRoadmapApplyLedger, InMemoryRoadmapPromotionLedger, RoadmapTransition, RoadmapTransitionRegistry

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
    "roadmap_auto_continue",
    "roadmap_allowlisted_transitions",
    "roadmap_trusted_assignees",
    "roadmap_apply_enabled",
    "roadmap_impl_assignee",
    "roadmap_review_assignee",
    "roadmap_ack_trigger_agent",
    "roadmap_board_adapter_mode",
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

    parser.add_argument("--roadmap-auto-continue", dest="roadmap_auto_continue", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--roadmap-allowlisted-transitions", default=None, help="comma-separated roadmap transition ids")
    parser.add_argument("--roadmap-trusted-assignees", default=None, help="comma-separated trusted assignees")
    parser.add_argument("--roadmap-apply-enabled", dest="roadmap_apply_enabled", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--roadmap-impl-assignee", default=None)
    parser.add_argument("--roadmap-review-assignee", default=None)
    parser.add_argument("--roadmap-ack-trigger-agent", default=None)
    parser.add_argument("--roadmap-board-adapter-mode", choices=("fake", "real"), default=None)


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
    transitions = kwargs.get("roadmap_allowlisted_transitions")
    if isinstance(transitions, str):
        kwargs["roadmap_allowlisted_transitions"] = tuple(t.strip() for t in transitions.split(",") if t.strip())
    elif isinstance(transitions, list):
        kwargs["roadmap_allowlisted_transitions"] = tuple(transitions)
    assignees = kwargs.get("roadmap_trusted_assignees")
    if isinstance(assignees, str):
        kwargs["roadmap_trusted_assignees"] = tuple(a.strip() for a in assignees.split(",") if a.strip())
    elif isinstance(assignees, list):
        kwargs["roadmap_trusted_assignees"] = tuple(assignees)
    return kwargs


def _build_roadmap_registry(raw: Any) -> RoadmapTransitionRegistry | None:
    if not isinstance(raw, dict):
        return None
    transitions_raw = raw.get("transitions")
    if not isinstance(transitions_raw, dict):
        return None
    transitions: dict[str, RoadmapTransition] = {}
    try:
        for key, value in transitions_raw.items():
            if not isinstance(value, dict):
                return None
            transition = RoadmapTransition(
                transition_id=str(value.get("transition_id") or key),
                roadmap_id=str(value.get("roadmap_id") or ""),
                from_slice=str(value.get("from_slice") or ""),
                to_slice=str(value.get("to_slice") or ""),
                slice_template=tuple(str(x) for x in value.get("slice_template") or ()),
                policy_refs=tuple(str(x) for x in value.get("policy_refs") or ()),
                max_chain_depth=int(value.get("max_chain_depth", 3)),
                version=str(value.get("version") or ""),
            )
            transitions[key] = transition
        return RoadmapTransitionRegistry(
            version=str(raw.get("version") or "fixture"),
            source_ref=str(raw.get("source_ref") or "fixture"),
            transitions=transitions,
        )
    except (TypeError, ValueError):
        return None


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


def resolve_kanban_board_client() -> Any | None:
    """Return an injectable Hermes Kanban CLI runner when the `hermes` CLI is available.

    There is no importable Hermes board client to resolve; the actual Hermes
    surface is the `hermes kanban ... create ... --json` CLI. Agentflow has no
    hard dependency on Hermes internals, so this remains a soft adapter point:
    tests monkeypatch this function to inject a fake runner, and production
    deployments rely on an installed `hermes` binary on PATH.
    """
    if shutil.which("hermes") is None:
        return None
    return _default_cli_runner


def _select_loop_adapter(policy: LoopPolicy, event: LoopEvent) -> Any | None:
    apply_gate_open = policy.active_mode == "apply" and policy.apply_enabled is True
    roadmap_real_requested = policy.roadmap_board_adapter_mode == "real" and policy.roadmap_apply_enabled is True
    if not apply_gate_open:
        return None
    if roadmap_real_requested:
        runner = resolve_kanban_board_client()
        if runner is None:
            return None
        return RealKanbanGraphAdapter(
            runner,
            board=os.environ.get("HERMES_KANBAN_BOARD", ""),
            source_task_id=event.source_final_id or event.source_task_id,
            created_by=os.environ.get("HERMES_KANBAN_CREATED_BY", ""),
        )
    return FakeKanbanGraphAdapter()


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
        except OSError:
            # Never echo the raw path/errno text: it can leak private paths or
            # token-like filename fragments back to the CLI caller.
            return 2, {"success": False, "error": "malformed_input", "detail": "input_file_unreadable"}
        except (json.JSONDecodeError, ValueError):
            # JSON parse errors and fixture-root validation embed offending
            # source text; return a stable generic detail instead.
            return 2, {"success": False, "error": "malformed_input", "detail": "input_json_invalid"}

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

    adapter = _select_loop_adapter(policy, event)
    roadmap_registry = _build_roadmap_registry(fixture.get("roadmap_registry"))
    roadmap_ledger = InMemoryRoadmapPromotionLedger()
    roadmap_apply_ledger = InMemoryRoadmapApplyLedger()

    try:
        decision = evaluate_loop_event(
            event,
            ledger,
            policy,
            adapter=adapter,
            roadmap_registry=roadmap_registry,
            roadmap_ledger=roadmap_ledger,
            roadmap_apply_ledger=roadmap_apply_ledger,
        )
        report = build_loop_report(decision)
    except Exception as exc:  # fail closed: never leak a raw traceback to CLI output
        return 2, {"success": False, "error": "evaluation_failed", "detail": sanitize_string(str(exc))}

    return 0, report
