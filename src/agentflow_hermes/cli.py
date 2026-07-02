from __future__ import annotations

import argparse
import json
from typing import Sequence

from .ack import AckError, parse_ack_block, validate_ack
from .bridges.cron import ingest_cron_output, scan_cron_output
from .bridges.kanban import load_fixture, resolve_blocked_remediation
from .live.gateway import FakeGateway
from .live.policy import LivePolicy, load_policy, policy_path, save_policy
from .live.sanitize import short_text
from .loop_cli import add_loop_cli_args, run_loop_evaluate
from .store import AgentFlowStore, render_dispatch_prompt


def _dump(data: dict, **kwargs) -> str:
    return json.dumps(data, ensure_ascii=False, **kwargs)


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
    sub.add_parser("doctor")

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

    loop = sub.add_parser("loop")
    loop_sub = loop.add_subparsers(dest="loop_cmd", required=True)
    loop_evaluate = loop_sub.add_parser("evaluate")
    add_loop_cli_args(loop_evaluate)

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
        print(_dump({
            "success": True,
            "db": str(store.path),
            "mode": "dry-run-first",
            "schema_version": version,
            "policy": policy.as_dict(),
            "policy_path": str(policy_path()),
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
            print(_dump({
                "success": True,
                "policy": policy.as_dict(),
                "policy_path": str(policy_path()),
                "degraded": degraded,
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
    if args.cmd == "loop" and args.loop_cmd == "evaluate":
        rc, report = run_loop_evaluate(args)
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
