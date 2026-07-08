"""Repo-config roadmap GO autopromoter watchdog CLI (M17).

Thin orchestration only: builds a `LoopEvent`/`LoopPolicy`/registry from a repo
config file and a fetched board task, then delegates to the existing
`evaluate_loop_event` (M14/M15/M16 path). This module never creates graph
candidates or calls a board writer itself.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .graph_creator import RealKanbanGraphAdapter
from .live.sanitize import sanitize_string, short_text
from .loop_cli import resolve_kanban_board_client
from .loop_supervisor import (
    InMemoryLoopLedger,
    LoopEvent,
    LoopPolicy,
    build_loop_report,
    evaluate_loop_event,
)
from .roadmap import InMemoryRoadmapApplyLedger, InMemoryRoadmapPromotionLedger
from .roadmap_config import RepoRoadmapConfig, build_registry, load_repo_roadmap_config


def add_roadmap_promote_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--apply", action="store_true", default=False)
    parser.add_argument("--receipts-file", default="")


def add_roadmap_watch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true", default=False)
    parser.add_argument("--apply", action="store_true", default=False)
    parser.add_argument("--receipts-file", default="")


def config_to_loop_policy(config: RepoRoadmapConfig, *, apply: bool) -> LoopPolicy:
    apply_armed = apply and config.apply_mode
    return LoopPolicy(
        active_mode="apply" if apply else "request_only",
        apply_enabled=apply,
        kill_switch=config.kill_switch,
        require_origin_match=config.require_origin_match,
        require_policy_resolution=config.require_policy_resolution,
        expected_origin=config.expected_origin,
        expected_return_to=config.expected_return_to,
        roadmap_auto_continue=True,
        roadmap_allowlisted_transitions=config.allowed_transitions,
        roadmap_max_chain_depth=config.max_chain_depth,
        roadmap_max_promotions_per_roadmap=config.max_promotions_per_roadmap,
        roadmap_promote_cooldown_seconds=config.promote_cooldown_seconds,
        roadmap_require_review_edge=config.require_review_edge,
        roadmap_require_ack_edge=config.require_ack_edge,
        roadmap_require_trusted_assignee=config.require_trusted_assignee,
        roadmap_trusted_assignees=config.trusted_assignees,
        roadmap_require_policy_resolution=config.require_policy_resolution,
        roadmap_apply_enabled=apply_armed,
        roadmap_impl_assignee=config.impl_assignee,
        roadmap_review_assignee=config.review_assignee,
        roadmap_ack_trigger_agent=config.ack_trigger_agent,
        roadmap_board_adapter_mode="real",
    )


def fetch_task_via_cli(runner: Any, board: str, task_id: str, *, hermes_bin: str = "hermes") -> dict[str, Any]:
    argv = [hermes_bin, "kanban", "--board", board, "show", short_text(task_id), "--json"]
    try:
        returncode, stdout, _stderr = runner(argv)
    except Exception:
        return {"success": False, "error": "cli_runner_error"}
    if returncode != 0:
        return {"success": False, "error": "cli_runner_failed"}
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"success": False, "error": "cli_invalid_json"}
    if not isinstance(data, dict):
        return {"success": False, "error": "cli_invalid_json"}
    nested_task = data.get("task")
    task = nested_task if isinstance(nested_task, dict) else data
    if not isinstance(task, dict):
        return {"success": False, "error": "cli_invalid_json"}
    top_runs = data.get("runs")
    runs = top_runs if isinstance(top_runs, list) else task.get("runs")
    if not isinstance(runs, list):
        runs = []
    return {"success": True, "task": task, "runs": runs}


def list_final_tasks_via_cli(runner: Any, board: str, *, hermes_bin: str = "hermes") -> list[str]:
    # Hermes Kanban has durable statuses such as done/blocked/ready, but no
    # first-class "final" status or verdict filter on `list`. Scan completed
    # tasks from the same configured board, then fetch each task and let the
    # existing evaluate_loop_event/roadmap gates reject non-GO or ineligible
    # summaries. Do not invent unsupported CLI flags here.
    argv = [hermes_bin, "kanban", "--board", board, "list", "--status", "done", "--json"]
    try:
        returncode, stdout, _stderr = runner(argv)
    except Exception:
        return []
    if returncode != 0:
        return []
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    items = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    task_ids: list[str] = []
    for item in items:
        if isinstance(item, dict):
            task_id = str(item.get("id") or item.get("task_id") or "")
        else:
            task_id = str(item)
        if task_id:
            task_ids.append(task_id)
    return task_ids


def _task_str(task: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = task.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


_COMPLETED_RUN_STATUSES = {"completed", "done", "success"}


def _latest_completed_run_summary(runs: list[Any]) -> str:
    candidates: list[tuple[float | None, int, str]] = []
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            continue
        status = str(run.get("status") or "").lower()
        outcome = str(run.get("outcome") or "").lower()
        if status not in _COMPLETED_RUN_STATUSES and outcome not in _COMPLETED_RUN_STATUSES:
            continue
        summary = run.get("summary")
        if not isinstance(summary, str) or not summary:
            continue
        timestamp: float | None = None
        for key in ("ended_at", "completed_at", "started_at", "id"):
            value = run.get(key)
            if isinstance(value, (int, float)):
                timestamp = float(value)
                break
        candidates.append((timestamp, index, summary))
    if not candidates:
        return ""
    timestamped = [candidate for candidate in candidates if candidate[0] is not None]
    if timestamped:
        return max(timestamped, key=lambda candidate: candidate[0] or 0.0)[2]
    # Hermes `show --json` commonly returns the newest run at runs[0]. When no
    # timestamps are present, preserve that order instead of reversing it.
    return candidates[0][2]


def loop_event_from_task(
    task: dict[str, Any], config: RepoRoadmapConfig, *, event_id: str, runs: list[Any] | None = None
) -> LoopEvent:
    task_id = _task_str(task, "id", "task_id")
    summary = (
        _task_str(task, "result", "summary")
        or _latest_completed_run_summary(runs or [])
        or _task_str(task, "body", "title")
    )
    origin = _task_str(task, "origin") or config.expected_origin
    return_to = _task_str(task, "return_to") or config.expected_return_to
    subscription_status = _task_str(task, "subscription_status") or "unverified"
    policy_resolution_ref = _task_str(task, "policy_resolution_ref")
    assignee = _task_str(task, "assignee")
    return LoopEvent(
        event_id=event_id,
        source_graph_id=task_id or event_id,
        source_task_id=task_id,
        summary=summary,
        origin=origin,
        return_to=return_to,
        subscription_status=subscription_status,
        policy_resolution_ref=policy_resolution_ref,
        source_final_id=task_id,
        source_assignee=assignee,
        occurred_at=0.0,
    )


def _seed_apply_ledger(ledger: InMemoryRoadmapApplyLedger, path: str) -> None:
    p = Path(path)
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    for key, receipt in data.items():
        if isinstance(receipt, dict):
            ledger.applied[str(key)] = receipt


def _persist_apply_ledger(ledger: InMemoryRoadmapApplyLedger, path: str) -> None:
    Path(path).write_text(json.dumps(ledger.applied, ensure_ascii=False), encoding="utf-8")


def _load_config(config_path: str) -> tuple[RepoRoadmapConfig | None, dict[str, Any] | None]:
    try:
        return load_repo_roadmap_config(config_path), None
    except (OSError, ValueError) as exc:
        return None, {"success": False, "error": "malformed_config", "detail": sanitize_string(str(exc))}


def _make_adapter(config: RepoRoadmapConfig, runner: Any, apply: bool, *, source_task_id: str) -> RealKanbanGraphAdapter | None:
    if not (apply and config.apply_mode):
        return None
    return RealKanbanGraphAdapter(runner, board=config.board, source_task_id=source_task_id)


def run_roadmap_promote(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Fetch one final task and evaluate it through the existing loop path.

    Never creates a graph unless the config kill switch is off (`enabled`),
    `config.apply_mode` is true, and `--apply` is passed — both gates must be
    open. Same-board only: the board used for fetch and create is always
    `config.board`.
    """

    config, error = _load_config(args.config)
    if error is not None:
        return 2, error
    assert config is not None
    if not config.enabled:
        return 0, {"success": False, "action": "noop", "reason": "config_disabled", "applied": False, "created_task_ids": []}
    if not config.same_board_only or not config.board:
        return 2, {"success": False, "error": "same_board_required", "created_task_ids": []}

    runner = resolve_kanban_board_client()
    if runner is None:
        return 2, {"success": False, "error": "no_board_client"}

    fetched = fetch_task_via_cli(runner, config.board, args.task)
    if not fetched.get("success"):
        return 2, {"success": False, "error": fetched.get("error", "task_fetch_failed")}

    event = loop_event_from_task(
        fetched["task"], config, event_id=f"roadmap-promote:{args.task}", runs=fetched.get("runs")
    )
    policy = config_to_loop_policy(config, apply=args.apply)
    registry = build_registry(config)
    ledger = InMemoryLoopLedger()
    roadmap_ledger = InMemoryRoadmapPromotionLedger()
    apply_ledger = InMemoryRoadmapApplyLedger()
    receipts_path = args.receipts_file or ""
    if receipts_path:
        _seed_apply_ledger(apply_ledger, receipts_path)

    adapter = _make_adapter(config, runner, args.apply, source_task_id=args.task)

    try:
        decision = evaluate_loop_event(
            event,
            ledger,
            policy,
            adapter=adapter,
            roadmap_registry=registry,
            roadmap_ledger=roadmap_ledger,
            roadmap_apply_ledger=apply_ledger,
        )
        report = build_loop_report(decision)
    except Exception as exc:  # fail closed: never leak a raw traceback to CLI output
        return 2, {"success": False, "error": "evaluation_failed", "detail": sanitize_string(str(exc))}

    if receipts_path:
        _persist_apply_ledger(apply_ledger, receipts_path)
    return 0, report


def run_roadmap_watch(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """Scan the configured board for final GO tasks and promote each once.

    Requires `--once`: this slice never loops/polls/restarts by itself. A
    repeated `--once` run over the same tasks with the same `--receipts-file`
    creates 0 new tasks, because the apply ledger dedups by idempotency key
    before any adapter create is attempted.
    """

    if not args.once:
        return 2, {"success": False, "error": "continuous_watch_not_supported", "detail": "use --once"}

    config, error = _load_config(args.config)
    if error is not None:
        return 2, error
    assert config is not None
    if not config.enabled:
        return 0, {"success": False, "action": "noop", "reason": "config_disabled", "results": []}
    if not config.same_board_only or not config.board:
        return 2, {"success": False, "error": "same_board_required", "results": []}

    runner = resolve_kanban_board_client()
    if runner is None:
        return 2, {"success": False, "error": "no_board_client"}

    task_ids = list_final_tasks_via_cli(runner, config.board)
    policy = config_to_loop_policy(config, apply=args.apply)
    registry = build_registry(config)
    ledger = InMemoryLoopLedger()
    roadmap_ledger = InMemoryRoadmapPromotionLedger()
    apply_ledger = InMemoryRoadmapApplyLedger()
    receipts_path = args.receipts_file or ""
    if receipts_path:
        _seed_apply_ledger(apply_ledger, receipts_path)

    results: list[dict[str, Any]] = []
    created_task_ids: list[str] = []
    for task_id in task_ids:
        fetched = fetch_task_via_cli(runner, config.board, task_id)
        if not fetched.get("success"):
            results.append({"task": task_id, "success": False, "error": fetched.get("error", "task_fetch_failed")})
            continue
        event = loop_event_from_task(
            fetched["task"], config, event_id=f"roadmap-watch:{task_id}", runs=fetched.get("runs")
        )
        adapter = _make_adapter(config, runner, args.apply, source_task_id=task_id)
        try:
            decision = evaluate_loop_event(
                event,
                ledger,
                policy,
                adapter=adapter,
                roadmap_registry=registry,
                roadmap_ledger=roadmap_ledger,
                roadmap_apply_ledger=apply_ledger,
            )
            report = build_loop_report(decision)
        except Exception as exc:  # fail closed: never leak a raw traceback to CLI output
            results.append({"task": task_id, "success": False, "error": "evaluation_failed", "detail": sanitize_string(str(exc))})
            continue
        roadmap = (report.get("receipt") or {}).get("decision_payload", {}).get("roadmap_autopromote") or {}
        created_task_ids.extend(roadmap.get("created_task_ids") or [])
        results.append({"task": task_id, **report})

    if receipts_path:
        _persist_apply_ledger(apply_ledger, receipts_path)

    return 0, {
        "success": True,
        "scanned": len(task_ids),
        "created_task_ids": created_task_ids,
        "results": results,
    }
