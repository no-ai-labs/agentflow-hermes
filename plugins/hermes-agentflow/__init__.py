from __future__ import annotations

import contextlib
import io
import json
from typing import Any, Callable


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
        "description": "Check the local AgentFlow store, dry-run mode, and engine package health.",
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

AGENTFLOW_DISPATCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agentflow_dispatch",
        "description": "Render or live-dispatch a queued AgentFlow job. Live requires explicit live=true and server-side policy enablement; default is dry-run.",
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "live": {"type": "boolean", "default": False},
            },
            "required": ["job_id"],
        },
    },
}

AGENTFLOW_LIVE_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agentflow_live_status",
        "description": "Read-only view of the effective AgentFlow live policy, kill-switch, and degraded state. No mutations.",
        "parameters": {"type": "object", "properties": {}},
    },
}


# Lazily load engine state so a missing/uninstalled package can degrade gracefully.
_engine_error: str | None = None
_run_cli: Callable[[list[str]], dict[str, Any]] | None = None


def _load_engine() -> None:
    global _engine_error, _run_cli
    if _run_cli is not None or _engine_error is not None:
        return
    try:
        from agentflow_hermes.cli import main as engine_main
    except Exception as exc:  # pragma: no cover - degraded path covered by import failure tests
        _engine_error = f"agentflow_hermes engine not importable: {exc}"
        return

    def run(args: list[str]) -> dict[str, Any]:
        out = io.StringIO()
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = engine_main(args)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else (0 if exc.code in (None, True) else 1)
        out_text = out.getvalue().strip()
        err_text = err.getvalue().strip()
        if rc != 0:
            return {"success": False, "error": err_text or out_text or f"exit {rc}"}
        try:
            return json.loads(out_text)
        except Exception:
            return {"success": True, "output": out_text}

    _run_cli = run


def _ensure_engine() -> dict[str, Any] | None:
    _load_engine()
    if _engine_error is not None:
        return {"success": False, "error": _engine_error}
    return None


def _handle_enqueue(args: dict) -> str:
    bad = _ensure_engine()
    if bad is not None:
        return json.dumps(bad, ensure_ascii=False)
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
    bad = _ensure_engine()
    if bad is not None:
        return json.dumps(bad, ensure_ascii=False)
    result = _run_cli(["status", "--json", "--limit", str(args.get("limit") or 20)])
    return json.dumps(result, ensure_ascii=False)


def _handle_dispatch_dry_run(args: dict) -> str:
    bad = _ensure_engine()
    if bad is not None:
        return json.dumps(bad, ensure_ascii=False)
    result = _run_cli(["dispatch-dry-run", str(args.get("job_id") or "")])
    return json.dumps(result, ensure_ascii=False)


def _handle_ack_ingest(args: dict) -> str:
    bad = _ensure_engine()
    if bad is not None:
        return json.dumps(bad, ensure_ascii=False)
    result = _run_cli(["ack", "ingest", "--text", str(args.get("text") or "")])
    return json.dumps(result, ensure_ascii=False)


def _handle_doctor(args: dict) -> str:
    bad = _ensure_engine()
    if bad is not None:
        return json.dumps({
            "success": False,
            "engine_importable": False,
            "mode": "dry-run-first",
            "error": bad["error"],
            "installation": "Install the agentflow-hermes engine package in the Hermes environment, enable the plugin, and restart Hermes.",
        }, ensure_ascii=False)
    result = _run_cli(["doctor"])
    result.setdefault("engine_importable", True)
    result.setdefault("mode", "dry-run-first")
    return json.dumps(result, ensure_ascii=False)


def _handle_bridge_cron(args: dict) -> str:
    bad = _ensure_engine()
    if bad is not None:
        return json.dumps(bad, ensure_ascii=False)
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


def _handle_dispatch(args: dict) -> str:
    bad = _ensure_engine()
    if bad is not None:
        return json.dumps(bad, ensure_ascii=False)
    live = bool(args.get("live", False))
    cmd = ["dispatch", "--job-id", str(args.get("job_id") or "")]
    if live:
        cmd.append("--live")
    result = _run_cli(cmd)
    return json.dumps(result, ensure_ascii=False)


def _handle_live_status(args: dict) -> str:
    bad = _ensure_engine()
    if bad is not None:
        return json.dumps(bad, ensure_ascii=False)
    result = _run_cli(["live", "status"])
    return json.dumps(result, ensure_ascii=False)


def register(ctx) -> None:
    ctx.register_tool("agentflow_enqueue", "agentflow", AGENTFLOW_ENQUEUE_SCHEMA, _handle_enqueue, emoji="🧭")
    ctx.register_tool("agentflow_status", "agentflow", AGENTFLOW_STATUS_SCHEMA, _handle_status, emoji="📋")
    ctx.register_tool("agentflow_dispatch_dry_run", "agentflow", AGENTFLOW_DISPATCH_DRY_RUN_SCHEMA, _handle_dispatch_dry_run, emoji="🧪")
    ctx.register_tool("agentflow_dispatch", "agentflow", AGENTFLOW_DISPATCH_SCHEMA, _handle_dispatch, emoji="🚀")
    ctx.register_tool("agentflow_ack_ingest", "agentflow", AGENTFLOW_ACK_INGEST_SCHEMA, _handle_ack_ingest, emoji="✅")
    ctx.register_tool("agentflow_doctor", "agentflow", AGENTFLOW_DOCTOR_SCHEMA, _handle_doctor, emoji="🩺")
    ctx.register_tool("agentflow_bridge_cron", "agentflow", AGENTFLOW_BRIDGE_CRON_SCHEMA, _handle_bridge_cron, emoji="⏱️")
    ctx.register_tool("agentflow_live_status", "agentflow", AGENTFLOW_LIVE_STATUS_SCHEMA, _handle_live_status, emoji="🛡️")
