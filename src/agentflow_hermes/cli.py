from __future__ import annotations

import argparse
import json
import re
from typing import Sequence

from .store import AgentFlowStore, render_dispatch_prompt

_ACK_RE = re.compile(r"\[JOB ACK\](?P<body>.*)", re.IGNORECASE | re.DOTALL)
_FIELD_RE = re.compile(r"^(?P<key>[a-zA-Z_][\w-]*):\s*(?P<value>.*)$")


def parse_ack_block(text: str) -> dict[str, str]:
    match = _ACK_RE.search(text or "")
    if not match:
        raise ValueError("missing [JOB ACK] block")
    fields: dict[str, str] = {}
    for raw in match.group("body").splitlines():
        m = _FIELD_RE.match(raw.strip())
        if m:
            fields[m.group("key").replace("-", "_").lower()] = m.group("value").strip()
    if not fields.get("job_id"):
        raise ValueError("ACK missing job_id")
    if not fields.get("status"):
        raise ValueError("ACK missing status")
    return fields


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

    status = sub.add_parser("status")
    status.add_argument("--limit", type=int, default=20)
    status.add_argument("--json", action="store_true")

    dispatch = sub.add_parser("dispatch-dry-run")
    dispatch.add_argument("job_id")

    ack = sub.add_parser("ack")
    ack_sub = ack.add_subparsers(dest="ack_cmd", required=True)
    ingest = ack_sub.add_parser("ingest")
    ingest.add_argument("--text", required=True)

    args = parser.parse_args(argv)
    store = AgentFlowStore.default()

    if args.cmd == "init":
        store.init()
        print(json.dumps({"success": True, "db": str(store.path)}, ensure_ascii=False))
        return 0
    if args.cmd == "doctor":
        store.init()
        print(json.dumps({"success": True, "db": str(store.path), "mode": "dry-run-first"}, ensure_ascii=False))
        return 0
    if args.cmd == "enqueue":
        print(json.dumps(store.enqueue(title=args.title, body=args.body, target=args.target, origin_return=args.origin_return, dedupe_key=args.dedupe_key), ensure_ascii=False))
        return 0
    if args.cmd == "status":
        jobs = store.list_jobs(limit=args.limit)
        if args.json:
            print(json.dumps({"success": True, "jobs": jobs}, ensure_ascii=False, indent=2))
        else:
            for job in jobs:
                print(f"{job['id']} {job['status']} {job['title']} -> {job['target']}")
        return 0
    if args.cmd == "dispatch-dry-run":
        job = store.get_job(args.job_id)
        if not job:
            print(json.dumps({"success": False, "error": f"unknown job_id: {args.job_id}"}, ensure_ascii=False))
            return 2
        print(render_dispatch_prompt(job))
        return 0
    if args.cmd == "ack" and args.ack_cmd == "ingest":
        try:
            fields = parse_ack_block(args.text)
        except ValueError as exc:
            print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False))
            return 2
        print(json.dumps(store.ack(job_id=fields["job_id"], status=fields["status"], summary=fields.get("summary", ""), payload=fields), ensure_ascii=False))
        return 0
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
