from __future__ import annotations

import argparse
import json
from typing import Sequence

from .ack import AckError, parse_ack_block, validate_ack
from .cron_bridge import ingest_cron_output
from .store import AgentFlowStore, render_dispatch_prompt


def _dump(data: dict, **kwargs) -> str:
    return json.dumps(data, ensure_ascii=False, **kwargs)


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

    cron = sub.add_parser("cron")
    cron_sub = cron.add_subparsers(dest="cron_cmd", required=True)
    cron_ingest = cron_sub.add_parser("ingest")
    cron_ingest.add_argument("--ref", required=True)
    cron_ingest.add_argument("--hash", required=True)
    cron_ingest.add_argument("--marker-text", default="")
    cron_ingest.add_argument("--source", default="cron")
    cron_ingest.add_argument("--correlation-id", default="")
    cron_ingest.add_argument("--target", default="")
    cron_ingest.add_argument("--origin-return", default="")
    cron_ingest.add_argument("--title", default="")

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
        print(_dump(
            store.enqueue(
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
            )
        ))
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
        result = store.ack(
            job_id=ack_payload.job_id,
            status=ack_payload.status,
            summary=ack_payload.summary,
            payload=ack_payload.raw_fields,
        )
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
        )
        print(_dump(result))
        return 0
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
