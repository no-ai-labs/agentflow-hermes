from __future__ import annotations

import argparse
import json
from typing import Sequence

from .ack import AckError, parse_ack_block, validate_ack
from .bridges.cron import ingest_cron_output, scan_cron_output
from .bridges.kanban import load_fixture, resolve_blocked_remediation
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
        print(_dump({"success": True, "db": str(store.path), "schema_version": 2}))
        return 0
    if args.cmd == "doctor":
        store.init()
        with store.connect() as con:
            version = store._schema_version(con)
        print(_dump({"success": True, "db": str(store.path), "mode": "dry-run-first", "schema_version": version}))
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
