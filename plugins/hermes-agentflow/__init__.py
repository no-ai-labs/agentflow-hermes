from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_cli(args: list[str]) -> dict:
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "src"
    code = "import sys; from agentflow_hermes.cli import main; raise SystemExit(main(sys.argv[1:]))"
    proc = subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=str(repo_root),
        env={**__import__("os").environ, "PYTHONPATH": str(src)},
        text=True,
        capture_output=True,
        timeout=30,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return {"success": False, "error": (proc.stderr or out or f"exit {proc.returncode}").strip()}
    try:
        return json.loads(out)
    except Exception:
        return {"success": True, "output": out}


AGENTFLOW_ENQUEUE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agentflow_enqueue",
        "description": "Queue a durable AgentFlow handoff job. Dry-run/supervisor dispatch happens separately.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "target": {"type": "string"},
                "origin_return": {"type": "string"},
                "dedupe_key": {"type": "string"},
            },
            "required": ["title"],
        },
    },
}

AGENTFLOW_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agentflow_status",
        "description": "List recent AgentFlow jobs from the local dry-run store.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}},
    },
}

AGENTFLOW_DISPATCH_DRY_RUN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agentflow_dispatch_dry_run",
        "description": "Render the dispatch prompt for a queued AgentFlow job without sending it anywhere.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    },
}

AGENTFLOW_ACK_INGEST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agentflow_ack_ingest",
        "description": "Ingest a [JOB ACK] block and update the local AgentFlow job state.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
}

AGENTFLOW_DOCTOR_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agentflow_doctor",
        "description": "Check the local AgentFlow store and dry-run mode.",
        "parameters": {"type": "object", "properties": {}},
    },
}

AGENTFLOW_BRIDGE_CRON_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agentflow_bridge_cron",
        "description": "Dry-run ingest a cron material-event ref/hash/marker into AgentFlow. No live active wake or send side effects.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "hash": {"type": "string"},
                "marker_text": {"type": "string"},
                "job_id": {"type": "string"},
                "run_id": {"type": "string"},
                "target": {"type": "string"},
                "origin_return": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["ref", "hash"],
        },
    },
}


def _handle_enqueue(args: dict) -> str:
    result = _run_cli([
        "enqueue",
        "--title", str(args.get("title") or ""),
        "--body", str(args.get("body") or ""),
        "--target", str(args.get("target") or ""),
        "--origin-return", str(args.get("origin_return") or ""),
        "--dedupe-key", str(args.get("dedupe_key") or ""),
    ])
    return json.dumps(result, ensure_ascii=False)


def _handle_status(args: dict) -> str:
    result = _run_cli(["status", "--json", "--limit", str(args.get("limit") or 20)])
    return json.dumps(result, ensure_ascii=False)


def _handle_dispatch_dry_run(args: dict) -> str:
    result = _run_cli(["dispatch-dry-run", str(args.get("job_id") or "")])
    return json.dumps(result, ensure_ascii=False)


def _handle_ack_ingest(args: dict) -> str:
    result = _run_cli(["ack", "ingest", "--text", str(args.get("text") or "")])
    return json.dumps(result, ensure_ascii=False)


def _handle_doctor(args: dict) -> str:
    result = _run_cli(["doctor"])
    return json.dumps(result, ensure_ascii=False)


def _handle_bridge_cron(args: dict) -> str:
    result = _run_cli([
        "bridge", "cron", "ingest",
        "--ref", str(args.get("ref") or ""),
        "--hash", str(args.get("hash") or ""),
        "--marker-text", str(args.get("marker_text") or ""),
        "--job-id", str(args.get("job_id") or ""),
        "--run-id", str(args.get("run_id") or ""),
        "--target", str(args.get("target") or ""),
        "--origin-return", str(args.get("origin_return") or ""),
        "--title", str(args.get("title") or ""),
    ])
    return json.dumps(result, ensure_ascii=False)


def register(ctx) -> None:
    ctx.register_tool("agentflow_enqueue", "agentflow", AGENTFLOW_ENQUEUE_SCHEMA, _handle_enqueue, emoji="🧭")
    ctx.register_tool("agentflow_status", "agentflow", AGENTFLOW_STATUS_SCHEMA, _handle_status, emoji="📋")
    ctx.register_tool("agentflow_dispatch_dry_run", "agentflow", AGENTFLOW_DISPATCH_DRY_RUN_SCHEMA, _handle_dispatch_dry_run, emoji="🧪")
    ctx.register_tool("agentflow_ack_ingest", "agentflow", AGENTFLOW_ACK_INGEST_SCHEMA, _handle_ack_ingest, emoji="✅")
    ctx.register_tool("agentflow_doctor", "agentflow", AGENTFLOW_DOCTOR_SCHEMA, _handle_doctor, emoji="🩺")
    ctx.register_tool("agentflow_bridge_cron", "agentflow", AGENTFLOW_BRIDGE_CRON_SCHEMA, _handle_bridge_cron, emoji="⏱️")
