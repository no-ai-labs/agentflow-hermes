from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from typing import Sequence

from .ack import AckError, parse_ack_block, validate_ack
from .bridges.cron import ingest_cron_output, scan_cron_output
from .bridges.kanban import load_fixture, resolve_blocked_remediation
from .continuation_cli import (
    add_continuation_cli_args,
    run_continuation_doctor,
    run_continuation_ingest,
    run_continuation_list,
    run_continuation_migrate_store,
    run_continuation_retry,
    run_continuation_show,
    run_continuation_submit,
)
from .live.gateway import FakeGateway
from .live.policy import LivePolicy, load_policy, policy_path, save_policy
from .live.sanitize import short_text
from .loop_cli import add_loop_cli_args, run_loop_evaluate
from .maintenance.installer import install_runner, render_install_plan
from .release_cli import add_release_github_args, run_release_github
from .roadmap_cli import add_roadmap_promote_args, add_roadmap_watch_args, run_roadmap_promote, run_roadmap_watch
from .roadmap_register import (
    add_roadmap_init_config_args,
    add_roadmap_register_args,
    add_roadmap_unregister_args,
    run_roadmap_init_config,
    run_roadmap_register,
    run_roadmap_unregister,
)
from .maintenance.runner import run_runner_evaluate
from .maintenance.trust import create_trust_grant, inspect_trust_grants, revoke_trust_grant
from .maintenance.units import UnitRenderError
from .store import AgentFlowStore, render_dispatch_prompt
from .continuation_store import ContinuationStore


def _default_agentflowd_unit_dir() -> Path:
    xdg_config_home = Path.home() / ".config"
    return xdg_config_home / "systemd" / "user"


def _read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return ""


def _extract_exec_args(unit_text: str) -> list[str]:
    for line in unit_text.splitlines():
        if line.startswith("ExecStart="):
            try:
                return shlex.split(line.split("=", 1)[1])
            except ValueError:
                return []
    return []


def _arg_value(argv: list[str], flag: str) -> str:
    try:
        idx = argv.index(flag)
    except ValueError:
        return ""
    next_idx = idx + 1
    return argv[next_idx] if next_idx < len(argv) else ""


def _agentflowd_service_status(unit_dir: Path | None = None) -> dict:
    from .service_install import RECONCILE_SERVICE_NAME, RECONCILE_TIMER_NAME, SERVICE_NAME

    root = unit_dir or _default_agentflowd_unit_dir()
    service_path = root / SERVICE_NAME
    reconcile_path = root / RECONCILE_SERVICE_NAME
    timer_path = root / RECONCILE_TIMER_NAME
    service_text = _read_text_if_exists(service_path)
    reconcile_text = _read_text_if_exists(reconcile_path)
    service_args = _extract_exec_args(service_text)
    reconcile_args = _extract_exec_args(reconcile_text)
    service_apply = "--apply" in service_args
    reconcile_apply = "--apply" in reconcile_args
    db = _arg_value(service_args, "--db") or _arg_value(reconcile_args, "--db")
    boards_root = _arg_value(service_args, "--boards-root") or _arg_value(reconcile_args, "--boards-root")
    return {
        "unit_dir": str(root),
        "installed": {
            SERVICE_NAME: service_path.exists(),
            RECONCILE_SERVICE_NAME: reconcile_path.exists(),
            RECONCILE_TIMER_NAME: timer_path.exists(),
        },
        "fully_installed": service_path.exists() and reconcile_path.exists() and timer_path.exists(),
        "service_apply": service_apply,
        "reconcile_apply": reconcile_apply,
        "apply": service_apply and reconcile_apply,
        "db": db,
        "boards_root": boards_root,
    }


def _continuation_runtime_status(args: argparse.Namespace) -> dict:
    from .daemon import AgentflowDaemon, DaemonConfig, default_boards_root

    service = _agentflowd_service_status(Path(args.agentflowd_unit_dir) if getattr(args, "agentflowd_unit_dir", "") else None)
    boards_root = Path(getattr(args, "boards_root", "") or service.get("boards_root") or default_boards_root())
    continuation_db = getattr(args, "continuation_db", "") or service.get("db") or ""
    store = ContinuationStore(Path(continuation_db)) if continuation_db else ContinuationStore.canonical()
    config = DaemonConfig(
        store=store,
        boards_root=boards_root,
        overrides_path=Path(args.overrides) if getattr(args, "overrides", "") else None,
        contracts_dir=Path(args.contracts_dir) if getattr(args, "contracts_dir", "") else None,
        apply=bool(service.get("apply")),
    )
    report = AgentflowDaemon(config).runtime_report()
    runtime = dict(report["continuation_runtime"])
    runtime["service"] = service
    runtime["direct_dispatch_policy"] = report["direct_dispatch_policy"]
    return runtime


def _dump(data: dict, **kwargs) -> str:
    return json.dumps(data, ensure_ascii=False, **kwargs)


def _add_status_surface_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--boards-root", default="", help="Override board discovery root for read-only status.")
    parser.add_argument("--continuation-db", default="", help="Override canonical continuation DB for read-only status.")
    parser.add_argument("--overrides", default="", help="Optional board registry override file.")
    parser.add_argument("--contracts-dir", default="", help="Optional contracts directory.")
    parser.add_argument("--agentflowd-unit-dir", default="", help="Override systemd user unit dir for service/apply inspection.")


def _add_autopilot_args(parser: argparse.ArgumentParser) -> None:
    """M27 zero-ceremony autopilot inspection commands (plan 12.2). All of
    these are read-only against the canonical continuation store except
    ``reconcile``, which runs one real (dry-run-by-default) recovery pass.
    The system must work with agentflowd alone; these exist for debugging."""
    sub = parser.add_subparsers(dest="autopilot_cmd", required=True)

    status_p = sub.add_parser("status")
    status_p.add_argument("--db", default="")

    waiting_p = sub.add_parser("waiting")
    waiting_p.add_argument("--db", default="")

    explain_p = sub.add_parser("explain")
    explain_p.add_argument("ref")
    explain_p.add_argument("--db", default="")

    sub.add_parser("policies").add_argument("--db", default="")

    reconcile_p = sub.add_parser("reconcile")
    reconcile_p.add_argument("--db", default="")
    reconcile_p.add_argument("--boards-root", default="")
    reconcile_p.add_argument("--overrides", default="")
    reconcile_p.add_argument("--contracts-dir", default="")
    reconcile_p.add_argument("--apply", action="store_true")


def _autopilot_store(args: argparse.Namespace) -> ContinuationStore:
    return ContinuationStore(Path(args.db)) if args.db else ContinuationStore.canonical()


def run_autopilot_status(args: argparse.Namespace) -> tuple[int, dict]:
    from .continuation_store import legacy_residue_report

    store = _autopilot_store(args)
    by_board: dict[str, dict[str, int]] = {}
    for inst in store.list_instances():
        board = inst["board"] or "unknown"
        bucket = by_board.setdefault(board, {"events": 0, "h0_auto": 0, "h1_asked": 0, "waiting": 0})
        bucket["events"] += 1
        if inst["state"] == "resumed":
            bucket["h0_auto"] += 1
        elif inst["state"] == "waiting_owner":
            bucket["waiting"] += 1
            bucket["h1_asked"] += 1
    outbox_pending = len([o for o in store.list_outbox() if o["state"] == "pending"])
    return 0, {
        "success": True,
        "boards": by_board,
        "outbox_pending": outbox_pending,
        "db": str(store.path),
        "legacy_residue": legacy_residue_report(),
    }


def run_autopilot_waiting(args: argparse.Namespace) -> tuple[int, dict]:
    from .interaction import InteractionInbox, compose_question

    store = _autopilot_store(args)
    inbox = InteractionInbox(store=store)
    open_states = ("collecting", "asked", "needs_clarification")
    waiting = [
        {"case_id": c.id, "endpoint": c.origin_endpoint, "state": c.state, "question": compose_question(c)}
        for c in inbox.list_cases()
        if c.state in open_states
    ]
    return 0, {"success": True, "waiting": waiting}


def run_autopilot_explain(args: argparse.Namespace) -> tuple[int, dict]:
    from .interaction import InteractionInbox

    store = _autopilot_store(args)
    ref = args.ref
    if ref.startswith("ic_"):
        inbox = InteractionInbox(store=store)
        case = inbox.get_case(ref)
        if case is None:
            return 2, {"success": False, "error": "unknown_case"}
        return 0, {
            "success": True,
            "case": {
                "id": case.id,
                "endpoint": case.origin_endpoint,
                "continuation_ids": list(case.continuation_ids),
                "state": case.state,
                "question_count": case.question_count,
            },
        }
    try:
        instance_id = int(ref)
    except ValueError:
        return 2, {"success": False, "error": "invalid_ref"}
    instance = store.get_instance(instance_id)
    if instance is None:
        return 2, {"success": False, "error": "unknown_instance"}
    return 0, {
        "success": True,
        "instance": instance,
        "steps": store.list_steps(instance_id),
        "events": store.list_events(instance_id),
        "satisfactions": store.list_requirement_satisfactions(instance_id),
        "receipts": store.list_owner_receipts(instance_id),
    }


def run_autopilot_policies(args: argparse.Namespace) -> tuple[int, dict]:
    store = _autopilot_store(args)
    return 0, {"success": True, "policies": store.list_standing_policies()}


def run_autopilot_reconcile(args: argparse.Namespace) -> tuple[int, dict]:
    from .daemon import AgentflowDaemon, DaemonConfig, default_boards_root

    store = _autopilot_store(args)
    config = DaemonConfig(
        store=store,
        boards_root=Path(args.boards_root) if args.boards_root else default_boards_root(),
        overrides_path=Path(args.overrides) if args.overrides else None,
        contracts_dir=Path(args.contracts_dir) if args.contracts_dir else None,
        apply=args.apply,
    )
    report = AgentflowDaemon(config).reconcile()
    return 0, report


def _add_cron_ingest_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ref", required=True)
    parser.add_argument("--hash", required=True)
    parser.add_argument("--marker-text", default="")
    parser.add_argument("--source", default="cron")
    parser.add_argument("--correlation-id", default="")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--target", default="")
    parser.add_argument("--origin-return", default="")
    parser.add_argument("--title", default="")


def _add_cron_scan_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--ref", default="")
    parser.add_argument("--hash", default="")
    parser.add_argument("--source", default="cron")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--target", default="")
    parser.add_argument("--origin-return", default="")
    parser.add_argument("--title", default="")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentflow-hermes")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    doctor = sub.add_parser("doctor")
    _add_status_surface_args(doctor)

    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--title", required=True)
    enqueue.add_argument("--body", default="")
    enqueue.add_argument("--target", default="")
    enqueue.add_argument("--origin-return", default="")
    enqueue.add_argument("--dedupe-key", default="")
    enqueue.add_argument("--correlation-id", default="")
    enqueue.add_argument("--causation-id", default="")
    enqueue.add_argument("--source-kind", default="manual")
    enqueue.add_argument("--source-id", default="")
    enqueue.add_argument("--source-ref", default="")
    enqueue.add_argument("--source-hash", default="")

    status = sub.add_parser("status")
    status.add_argument("--limit", type=int, default=20)
    status.add_argument("--json", action="store_true")

    dispatch = sub.add_parser("dispatch-dry-run")
    dispatch.add_argument("job_id")

    live = sub.add_parser("live")
    live_sub = live.add_subparsers(dest="live_cmd", required=True)
    live_status = live_sub.add_parser("status")
    _add_status_surface_args(live_status)
    live_enable = live_sub.add_parser("enable")
    live_enable.add_argument("--dispatch", action="store_true", help="enable live dispatch (operator-only)")
    live_disable = live_sub.add_parser("disable")
    live_disable.add_argument("--dispatch", action="store_true", help="disable live dispatch and set kill switch")
    live_canary = live_sub.add_parser("canary")
    live_canary.add_argument("--target", required=True)
    live_canary.add_argument("--live", action="store_true", default=False)
    live_canary.add_argument("--gateway", default="fake", choices=["fake"])

    dispatch_live = sub.add_parser("dispatch")
    dispatch_live.add_argument("--job-id", required=True)
    dispatch_live.add_argument("--live", action="store_true", default=False)

    ack = sub.add_parser("ack")
    ack_sub = ack.add_subparsers(dest="ack_cmd", required=True)
    ingest = ack_sub.add_parser("ingest")
    ingest.add_argument("--text", required=True)

    # Backward-compatible command: agentflow-hermes cron ingest ...
    cron = sub.add_parser("cron")
    cron_sub = cron.add_subparsers(dest="cron_cmd", required=True)
    _add_cron_ingest_args(cron_sub.add_parser("ingest"))

    # M2 bridge namespace: agentflow-hermes bridge cron scan/ingest ...
    bridge = sub.add_parser("bridge")
    bridge_sub = bridge.add_subparsers(dest="bridge_cmd", required=True)
    bridge_cron = bridge_sub.add_parser("cron")
    bridge_cron_sub = bridge_cron.add_subparsers(dest="bridge_cron_cmd", required=True)
    _add_cron_scan_args(bridge_cron_sub.add_parser("scan"))
    _add_cron_ingest_args(bridge_cron_sub.add_parser("ingest"))

    continuation = sub.add_parser("continuation")
    continuation_sub = continuation.add_subparsers(dest="continuation_cmd", required=True)
    add_continuation_cli_args(continuation_sub)

    loop = sub.add_parser("loop")
    loop_sub = loop.add_subparsers(dest="loop_cmd", required=True)
    loop_evaluate = loop_sub.add_parser("evaluate")
    add_loop_cli_args(loop_evaluate)

    # M17 repo-config roadmap GO autopromoter watchdog.
    roadmap = sub.add_parser("roadmap")
    roadmap_sub = roadmap.add_subparsers(dest="roadmap_cmd", required=True)
    roadmap_promote = roadmap_sub.add_parser("promote")
    add_roadmap_promote_args(roadmap_promote)
    roadmap_watch = roadmap_sub.add_parser("watch")
    add_roadmap_watch_args(roadmap_watch)
    # M18 scaffold + registry UX (config/register only; no live cron changes).
    roadmap_init = roadmap_sub.add_parser("init-config")
    add_roadmap_init_config_args(roadmap_init)
    roadmap_register = roadmap_sub.add_parser("register-watchdog")
    add_roadmap_register_args(roadmap_register)
    roadmap_unregister = roadmap_sub.add_parser("unregister-watchdog")
    add_roadmap_unregister_args(roadmap_unregister)

    # M20 bounded GitHub release publish trigger after review GO.
    release = sub.add_parser("release")
    release_sub = release.add_subparsers(dest="release_cmd", required=True)
    release_github = release_sub.add_parser("github")
    add_release_github_args(release_github)

    # M10 external maintenance runner (proposal/dry-run only from the CLI).
    maintenance = sub.add_parser("maintenance")
    maintenance_sub = maintenance.add_subparsers(dest="maintenance_cmd", required=True)
    maintenance_runner = maintenance_sub.add_parser("runner")
    maintenance_runner_sub = maintenance_runner.add_subparsers(dest="maintenance_runner_cmd", required=True)
    runner_evaluate = maintenance_runner_sub.add_parser("evaluate")
    runner_evaluate.add_argument("--input-file", required=True, help="JSON runner policy/config fixture")

    # M11 installer UX: render/print (default) or write to an explicit dir.
    # Never calls systemctl; never writes outside --unit-dir.
    maintenance_install = maintenance_sub.add_parser("install-runner")
    maintenance_install.add_argument("--config-file", required=True)
    maintenance_install.add_argument("--unit-dir", default="")
    maintenance_install.add_argument("--write-files", action="store_true", default=False)

    maintenance_render = maintenance_sub.add_parser("render-units")
    maintenance_render.add_argument("--config-file", required=True)
    maintenance_render.add_argument("--unit-dir", default="")

    trust_grant = maintenance_sub.add_parser("trust-grant")
    trust_grant.add_argument("--config-file", required=True)
    trust_grant.add_argument("--gateway", required=True)
    trust_grant.add_argument("--expires-at", required=True, type=float)
    trust_grant.add_argument("--comment", default="operator CLI trust grant")
    trust_grant.add_argument("--write", action="store_true", default=False)

    trust_inspect = maintenance_sub.add_parser("trust-inspect")
    trust_inspect.add_argument("--config-file", required=True)

    trust_revoke = maintenance_sub.add_parser("trust-revoke")
    trust_revoke.add_argument("--config-file", required=True)
    trust_revoke.add_argument("--gateway", required=True)
    trust_revoke.add_argument("--write", action="store_true", default=False)

    # M27 zero-ceremony autopilot inspection/control commands.
    autopilot = sub.add_parser("autopilot")
    _add_autopilot_args(autopilot)

    bridge_kanban = bridge_sub.add_parser("kanban")
    bridge_kanban_sub = bridge_kanban.add_subparsers(dest="bridge_kanban_cmd", required=True)
    resolve_blocked = bridge_kanban_sub.add_parser("resolve-blocked")
    resolve_blocked.add_argument("--blocked-card", required=True)
    resolve_blocked.add_argument("--remediation-review", required=True)
    resolve_blocked.add_argument("--dry-run", action="store_true", default=True)
    resolve_blocked.add_argument("--input-file", required=True)

    args = parser.parse_args(argv)
    store = AgentFlowStore.default()

    if args.cmd == "init":
        store.init()
        with store.connect() as con:
            version = store._schema_version(con)
        print(_dump({"success": True, "db": str(store.path), "schema_version": version}))
        return 0
    if args.cmd == "doctor":
        store.init()
        with store.connect() as con:
            version = store._schema_version(con)
        policy = load_policy()
        continuation_runtime = _continuation_runtime_status(args)
        direct_dispatch_policy = {
            "scope": "legacy_canary_only",
            "live_dispatch_enabled": policy.live_dispatch_enabled,
            "active_wake_enabled": policy.active_wake_enabled,
            "kanban_apply_enabled": policy.kanban_apply_enabled,
            "allowed_targets": list(policy.allowed_targets),
            "canary_targets": list(policy.canary_targets),
            "kill_switch": policy.kill_switch,
        }
        print(_dump({
            "success": True,
            "db": str(store.path),
            "mode": "dry-run-first",
            "schema_version": version,
            "policy": policy.as_dict(),
            "policy_path": str(policy_path()),
            "direct_dispatch_policy": direct_dispatch_policy,
            "continuation_runtime": continuation_runtime,
            "boards": continuation_runtime["boards"],
            "warnings": continuation_runtime["warnings"],
        }))
        return 0
    if args.cmd == "enqueue":
        print(_dump(store.enqueue(
            title=args.title,
            body=args.body,
            target=args.target,
            origin_return=args.origin_return,
            dedupe_key=args.dedupe_key,
            correlation_id=args.correlation_id,
            causation_id=args.causation_id,
            source_kind=args.source_kind,
            source_id=args.source_id,
            source_ref=args.source_ref,
            source_hash=args.source_hash,
        )))
        return 0
    if args.cmd == "status":
        jobs = store.list_jobs(limit=args.limit)
        if args.json:
            print(_dump({"success": True, "jobs": jobs}, indent=2))
        else:
            for job in jobs:
                print(f"{job['id']} {job['status']} {job['title']} -> {job['target']}")
        return 0
    if args.cmd == "dispatch-dry-run":
        job = store.get_job(args.job_id)
        if not job:
            print(_dump({"success": False, "error": f"unknown job_id: {args.job_id}"}))
            return 2
        store.dispatch_dry_run(args.job_id)
        job = store.get_job(args.job_id) or job
        print(render_dispatch_prompt(job))
        return 0
    if args.cmd == "dispatch":
        job = store.get_job(args.job_id)
        if not job:
            print(_dump({"success": False, "error": f"unknown job_id: {args.job_id}"}))
            return 2
        if not args.live:
            result = store.dispatch_dry_run(args.job_id)
            job = store.get_job(args.job_id) or job
            print(_dump({**result, "prompt": render_dispatch_prompt(job), "mode": "dry-run"}))
            return 0
        # Live dispatch requires an injected public gateway. The sidecar CLI has
        # no real gateway in M6, so it must fail closed instead of pretending a
        # fake delivery happened. The fake gateway is reserved for canary smoke.
        result = store.dispatch_live(args.job_id, gateway=None, live=True)
        print(_dump(result))
        return 0 if result.get("success") else 2
    if args.cmd == "live":
        if args.live_cmd == "status":
            policy = load_policy()
            store.init()
            with store.connect() as con:
                degraded_row = con.execute("select value from agentflow_meta where key='degraded'").fetchone()
            degraded = degraded_row is not None and degraded_row["value"] == "1"
            continuation_runtime = _continuation_runtime_status(args)
            direct_dispatch_policy = {
                "scope": "legacy_canary_only",
                "live_dispatch_enabled": policy.live_dispatch_enabled,
                "active_wake_enabled": policy.active_wake_enabled,
                "kanban_apply_enabled": policy.kanban_apply_enabled,
                "allowed_targets": list(policy.allowed_targets),
                "canary_targets": list(policy.canary_targets),
                "kill_switch": policy.kill_switch,
            }
            print(_dump({
                "success": True,
                "policy": policy.as_dict(),
                "policy_path": str(policy_path()),
                "degraded": degraded,
                "direct_dispatch_policy": direct_dispatch_policy,
                "continuation_runtime": continuation_runtime,
                "boards": continuation_runtime["boards"],
                "warnings": continuation_runtime["warnings"],
            }))
            return 0
        if args.live_cmd == "enable":
            policy = load_policy()
            if args.dispatch:
                policy = LivePolicy(
                    live_dispatch_enabled=True,
                    active_wake_enabled=policy.active_wake_enabled,
                    kanban_apply_enabled=policy.kanban_apply_enabled,
                    allowed_targets=policy.allowed_targets,
                    canary_targets=policy.canary_targets,
                    max_sends_per_min=policy.max_sends_per_min,
                    max_sends_per_target_per_hour=policy.max_sends_per_target_per_hour,
                    kill_switch=False,
                )
            save_policy(policy)
            print(_dump({"success": True, "policy": policy.as_dict(), "policy_path": str(policy_path())}))
            return 0
        if args.live_cmd == "disable":
            policy = load_policy()
            new_policy = LivePolicy(
                live_dispatch_enabled=False if args.dispatch else policy.live_dispatch_enabled,
                active_wake_enabled=policy.active_wake_enabled,
                kanban_apply_enabled=policy.kanban_apply_enabled,
                allowed_targets=policy.allowed_targets,
                canary_targets=policy.canary_targets,
                max_sends_per_min=policy.max_sends_per_min,
                max_sends_per_target_per_hour=policy.max_sends_per_target_per_hour,
                kill_switch=True,
            )
            save_policy(new_policy)
            print(_dump({"success": True, "policy": new_policy.as_dict(), "policy_path": str(policy_path())}))
            return 0
        if args.live_cmd == "canary":
            policy = load_policy()
            store.init()
            target = short_text(args.target)
            # Canary requires an explicit allowlist entry. To run a canary the
            # operator must have configured the target in policy.json. The
            # default fake gateway lets tests/smokes exercise the path without
            # reaching Hermes core.
            gateway = FakeGateway()
            if args.live:
                if not policy.live_dispatch_enabled:
                    print(_dump({"success": False, "error": "live_dispatch_disabled", "target": target}))
                    return 2
                if target not in policy.allowed_targets or target not in policy.canary_targets:
                    print(_dump({"success": False, "error": "target_not_allowed", "target": target}))
                    return 2
            # Enqueue a synthetic canary job so dispatch_live has a job row.
            created = store.enqueue(
                title=f"live canary to {target}",
                body="Synthetic canary message.",
                target=target,
                source_kind="canary",
            )
            result = store.dispatch_live(
                created["job_id"],
                gateway=gateway,
                live=args.live,
            )
            print(_dump({**result, "target": target, "gateway_calls": len(gateway.calls)}))
            return 0 if result.get("success") else 2
        raise AssertionError(args.live_cmd)
    if args.cmd == "ack" and args.ack_cmd == "ingest":
        try:
            fields = parse_ack_block(args.text)
            ack_payload = validate_ack(fields)
        except AckError as exc:
            if exc.deadletter:
                payload = dict(exc.payload or {})
                job_id = str(payload.get("job_id") or "")
                store.deadletter(reason=exc.reason, job_id=job_id, payload=payload)
            print(_dump({"success": False, "error": exc.reason}))
            return 2
        result = store.ack(job_id=ack_payload.job_id, status=ack_payload.status, summary=ack_payload.summary, payload=ack_payload.raw_fields)
        if not result.get("success"):
            print(_dump(result))
            return 2
        print(_dump(result))
        return 0
    if args.cmd == "cron" and args.cron_cmd == "ingest":
        result = ingest_cron_output(
            store,
            source_ref=args.ref,
            source_hash=args.hash,
            marker_text=args.marker_text,
            source=args.source,
            correlation_id=args.correlation_id,
            target=args.target,
            origin_return=args.origin_return,
            title=args.title,
            job_id=args.job_id,
            run_id=args.run_id,
        )
        print(_dump(result))
        return 0
    if args.cmd == "bridge" and args.bridge_cmd == "cron":
        if args.bridge_cron_cmd == "scan":
            result = scan_cron_output(
                store,
                output_file=args.output_file,
                source_ref=args.ref,
                source_hash=args.hash,
                source=args.source,
                job_id=args.job_id,
                run_id=args.run_id,
                target=args.target,
                origin_return=args.origin_return,
                title=args.title,
                dry_run=args.dry_run,
            )
        elif args.bridge_cron_cmd == "ingest":
            result = ingest_cron_output(
                store,
                source_ref=args.ref,
                source_hash=args.hash,
                marker_text=args.marker_text,
                source=args.source,
                correlation_id=args.correlation_id,
                target=args.target,
                origin_return=args.origin_return,
                title=args.title,
                job_id=args.job_id,
                run_id=args.run_id,
            )
        else:
            raise AssertionError(args.bridge_cron_cmd)
        print(_dump(result))
        return 0
    if args.cmd == "continuation":
        handlers = {
            "ingest": run_continuation_ingest,
            "list": run_continuation_list,
            "show": run_continuation_show,
            "submit": run_continuation_submit,
            "retry": run_continuation_retry,
            "doctor": run_continuation_doctor,
            "migrate-store": run_continuation_migrate_store,
        }
        rc, report = handlers[args.continuation_cmd](args)
        print(_dump(report))
        return rc
    if args.cmd == "loop" and args.loop_cmd == "evaluate":
        rc, report = run_loop_evaluate(args)
        print(_dump(report))
        return rc
    if args.cmd == "roadmap" and args.roadmap_cmd == "promote":
        rc, report = run_roadmap_promote(args)
        print(_dump(report))
        return rc
    if args.cmd == "roadmap" and args.roadmap_cmd == "watch":
        rc, report = run_roadmap_watch(args)
        print(_dump(report))
        return rc
    if args.cmd == "roadmap" and args.roadmap_cmd == "init-config":
        rc, report = run_roadmap_init_config(args)
        print(_dump(report))
        return rc
    if args.cmd == "roadmap" and args.roadmap_cmd == "register-watchdog":
        rc, report = run_roadmap_register(args)
        print(_dump(report))
        return rc
    if args.cmd == "roadmap" and args.roadmap_cmd == "unregister-watchdog":
        rc, report = run_roadmap_unregister(args)
        print(_dump(report))
        return rc
    if args.cmd == "release" and args.release_cmd == "github":
        rc, report = run_release_github(args)
        print(_dump(report))
        return rc
    if args.cmd == "maintenance" and args.maintenance_cmd == "runner" and args.maintenance_runner_cmd == "evaluate":
        rc, report = run_runner_evaluate(args)
        print(_dump(report))
        return rc
    if args.cmd == "maintenance" and args.maintenance_cmd == "render-units":
        try:
            plan = render_install_plan(args.config_file)
        except UnitRenderError as exc:
            print(_dump({"success": False, "error": str(exc)}))
            return 2
        print(_dump(plan))
        return 0
    if args.cmd == "maintenance" and args.maintenance_cmd == "trust-grant":
        result = create_trust_grant(
            args.config_file,
            gateway_unit=args.gateway,
            expires_at=args.expires_at,
            provenance=args.comment,
            write=args.write,
        )
        print(_dump(result))
        return 0 if result.get("success") else 2
    if args.cmd == "maintenance" and args.maintenance_cmd == "trust-inspect":
        result = inspect_trust_grants(args.config_file)
        print(_dump(result))
        return 0 if result.get("success") else 2
    if args.cmd == "maintenance" and args.maintenance_cmd == "trust-revoke":
        result = revoke_trust_grant(args.config_file, gateway_unit=args.gateway, write=args.write)
        print(_dump(result))
        return 0 if result.get("success") else 2
    if args.cmd == "maintenance" and args.maintenance_cmd == "install-runner":
        try:
            result = install_runner(
                args.config_file,
                unit_dir=args.unit_dir or None,
                write_files=args.write_files,
            )
        except UnitRenderError as exc:
            print(_dump({"success": False, "error": str(exc)}))
            return 2
        print(_dump(result))
        return 0
    if args.cmd == "autopilot":
        autopilot_handlers = {
            "status": run_autopilot_status,
            "waiting": run_autopilot_waiting,
            "explain": run_autopilot_explain,
            "policies": run_autopilot_policies,
            "reconcile": run_autopilot_reconcile,
        }
        rc, report = autopilot_handlers[args.autopilot_cmd](args)
        print(_dump(report))
        return rc
    if args.cmd == "bridge" and args.bridge_cmd == "kanban":
        if args.bridge_kanban_cmd == "resolve-blocked":
            fixture = load_fixture(args.input_file)
            cards = fixture.get("cards") or []
            blocked_card = next((c for c in cards if str(c.get("id") or "") == args.blocked_card), None)
            remediation_review = next(
                (c for c in cards if str(c.get("id") or "") == args.remediation_review), None
            )
            result = resolve_blocked_remediation(
                blocked_card,
                remediation_review,
                dry_run=args.dry_run,
            )
            print(_dump(result, indent=2))
            return 0 if result.get("success") else 2
        raise AssertionError(args.bridge_kanban_cmd)
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
