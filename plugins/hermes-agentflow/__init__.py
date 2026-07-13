from __future__ import annotations

import contextlib
import io
import json
import re
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


AGENTFLOW_INPUT_INBOX_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agentflow_input_inbox",
        "description": (
            "List the current origin session's unresolved owner interaction cases and render the "
            "one concise question for each (plan section 6/7.1). No task ids, contract_ref, or "
            "receipt syntax appear in the rendered question."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string", "description": "Origin session endpoint, e.g. discord:#channel."},
                "case_id": {"type": "string", "description": "Optional: only this case."},
            },
        },
    },
}

AGENTFLOW_SUBMIT_INPUT_TEXT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agentflow_submit_input_text",
        "description": (
            "Submit the current user's natural-language reply for an interaction case (plan section "
            "7.2). Compiles the reply into typed candidate fields, validates them, and applies/resumes "
            "on success. Raw text is never stored as the receipt — only a content hash/message ref. "
            "`endpoint` must be the current gateway session's own origin — the case is refused if it "
            "does not belong to that origin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "endpoint": {"type": "string", "description": "Current gateway session origin endpoint, e.g. discord:#channel."},
                "text": {"type": "string", "description": "Raw current user reply."},
                "owner_ref": {"type": "string"},
                "source_ref": {"type": "string"},
                "message_ref": {"type": "string"},
            },
            "required": ["case_id", "endpoint", "text"],
        },
    },
}

AGENTFLOW_INPUT_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agentflow_input_status",
        "description": (
            "Return the human-oriented state of an interaction case: waiting for you | resolved | "
            "resumed | failed retryable (plan section 7.3). `endpoint` must be the current gateway "
            "session's own origin — the case is refused if it does not belong to that origin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "endpoint": {"type": "string", "description": "Current gateway session origin endpoint, e.g. discord:#channel."},
            },
            "required": ["case_id", "endpoint"],
        },
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


def _handle_input_inbox(args: dict) -> str:
    bad = _ensure_engine()
    if bad is not None:
        return json.dumps(bad, ensure_ascii=False)
    from agentflow_hermes.continuation_store import ContinuationStore
    from agentflow_hermes.interaction import STATE_ANSWERED, STATE_APPLIED, InteractionInbox

    store = ContinuationStore.canonical()
    inbox = InteractionInbox(store=store)
    endpoint = str(args.get("endpoint") or "") or None
    case_id = str(args.get("case_id") or "")

    if case_id:
        case = inbox.get_case(case_id)
        # A case_id is caller-supplied and may name a case from another
        # origin lane (leaked case id, stale link, cross-board confusion).
        # Hard-bind to the current gateway session's own endpoint: a case
        # that does not belong to it must fail closed, exactly like the
        # multi-case listing path below already does via list_cases(endpoint=...).
        if case is not None and endpoint is not None and case.origin_endpoint != endpoint:
            case = None
        cases = (case,) if case is not None else ()
    else:
        cases = inbox.list_cases(endpoint=endpoint)

    unresolved = [c for c in cases if c.state not in (STATE_ANSWERED, STATE_APPLIED)]
    rendered = [_render_interaction_case(store, inbox, c) for c in unresolved]
    return json.dumps({"success": True, "cases": rendered}, ensure_ascii=False)


def _render_interaction_case(store, inbox, case) -> dict[str, Any]:
    from agentflow_hermes.interaction import STATE_COLLECTING, STATE_NEEDS_CLARIFICATION, classify_effort, compose_question

    if case.state in (STATE_COLLECTING, STATE_NEEDS_CLARIFICATION):
        # Asking (via this tool) is what closes the coalescing window in this
        # milestone (no long-lived agentflowd daemon yet — commit 6); this is
        # also the H0/H1/H2 question-count bump (plan "Human Effort Budget").
        case = inbox.mark_asked(case.id)
    question = compose_question(case, resolved_summary=_resolved_summary(store, case))
    return {
        "case_id": case.id,
        "endpoint": case.origin_endpoint,
        "state": case.state,
        "question": question,
        "effort": classify_effort(case.question_count),
        "continuation_ids": list(case.continuation_ids),
    }


def _resolved_summary(store, case) -> tuple[str, ...]:
    count = 0
    for continuation_id in case.continuation_ids:
        count += len(store.list_requirement_satisfactions(continuation_id))
    if count:
        return (f"{count} other value(s) already confirmed automatically",)
    return ()


_URL_RE = re.compile(r"https?://\S+")
_NUMBERED_ITEM_RE = re.compile(r"(\d+)\)?\s*[:.]?\s*([^,;]+)")


def _extract_single_value(text: str, field: dict[str, Any]) -> Any:
    """Lightweight, dependency-free natural-language field extractor (plan
    7.2). Not a general NLU model — a bounded set of deterministic rules
    sufficient to turn a plain reply into a typed candidate, which the
    contract still validates before anything is applied."""
    allowed = field.get("allowed_values") or []
    if allowed:
        lowered = text.lower()
        for candidate in allowed:
            if str(candidate).lower() in lowered:
                return candidate
        return text.strip()
    urls = _URL_RE.findall(text)
    if urls:
        return urls[0]
    return text.strip()


def _compile_reply_text(text: str, ordered_fields: list[dict[str, Any]]) -> dict[str, Any]:
    text = (text or "").strip()
    if not text or not ordered_fields:
        return {}
    if len(ordered_fields) == 1:
        return {ordered_fields[0]["name"]: _extract_single_value(text, ordered_fields[0])}

    matches = _NUMBERED_ITEM_RE.findall(text)
    result: dict[str, Any] = {}
    for num_str, chunk in matches:
        idx = int(num_str) - 1
        if 0 <= idx < len(ordered_fields):
            result[ordered_fields[idx]["name"]] = _extract_single_value(chunk.strip(), ordered_fields[idx])
    return result


def _handle_submit_input_text(args: dict) -> str:
    bad = _ensure_engine()
    if bad is not None:
        return json.dumps(bad, ensure_ascii=False)
    from agentflow_hermes.continuation_store import ContinuationStore
    from agentflow_hermes.continuations.owner_input import OwnerInputHandler
    from agentflow_hermes.input_contract import InputContract
    from agentflow_hermes.interaction import STATE_APPLIED, InteractionInbox, requirement_from_dict

    case_id = str(args.get("case_id") or "")
    endpoint = str(args.get("endpoint") or "")
    text = str(args.get("text") or "")
    owner_ref = str(args.get("owner_ref") or "")
    source_ref = str(args.get("source_ref") or "")
    message_ref = str(args.get("message_ref") or "") or f"reply:{case_id}"

    if not case_id or not text:
        return json.dumps({"success": False, "error": "case_id_and_text_required"}, ensure_ascii=False)
    if not endpoint:
        return json.dumps({"success": False, "error": "endpoint_required"}, ensure_ascii=False)

    store = ContinuationStore.canonical()
    inbox = InteractionInbox(store=store)
    case = inbox.get_case(case_id)
    if case is None:
        return json.dumps({"success": False, "error": "unknown_case"}, ensure_ascii=False)
    if case.origin_endpoint != endpoint:
        # Fail closed: a case from another origin lane must never be
        # transitioned or materialized just because its case_id is known.
        return json.dumps({"success": False, "error": "origin_mismatch"}, ensure_ascii=False)
    if case.state == STATE_APPLIED:
        return json.dumps({"success": True, "status": "resumed", "case_id": case_id, "note": "already applied"}, ensure_ascii=False)

    members = store.list_interaction_members(case_id)
    ordered_fields = [f for m in members for f in m["requirements"]]
    candidate = _compile_reply_text(text, ordered_fields)

    fields_by_continuation: dict[int, dict[str, Any]] = {}
    missing: list[str] = []
    for member in members:
        continuation_id = member["continuation_id"]
        requirements = tuple(requirement_from_dict(item) for item in member["requirements"])
        contract = InputContract.dynamic_owner_input(
            contract_ref="generic.owner-input.v1",
            owner_role="owner",
            resume_transition="",
            requirements=requirements,
        )
        candidate_subset = {name: candidate[name] for name in candidate if contract.field(name) is not None}
        clean, errors = contract.validate_owner_submission(candidate_subset)
        if errors:
            missing.extend(f"{continuation_id}:{e}" for e in errors)
            continue
        fields_by_continuation[continuation_id] = clean

    # Never store raw text — only the content hash/message ref plus the
    # already-validated typed compile result (plan 6/7.2).
    inbox.record_inbound_reply(case_id, message_ref=message_ref, raw_text=text, compile_result=candidate)

    if missing or len(fields_by_continuation) != len(members):
        inbox.mark_needs_clarification(case_id)
        return json.dumps(
            {
                "success": False,
                "status": "waiting for you",
                "case_id": case_id,
                "missing": missing or ["incomplete_reply"],
                "message": "One more concise reply is needed — not every field was recognized in that answer.",
            },
            ensure_ascii=False,
        )

    handler = OwnerInputHandler()
    resumed: list[int] = []
    for continuation_id, fields in fields_by_continuation.items():
        instance = store.get_instance(continuation_id)
        if instance is None:
            continue
        requirements = next(
            (tuple(requirement_from_dict(item) for item in m["requirements"]) for m in members if m["continuation_id"] == continuation_id),
            (),
        )
        contract = InputContract.dynamic_owner_input(
            contract_ref="generic.owner-input.v1",
            owner_role="owner",
            resume_transition="",
            requirements=requirements,
        )
        result = handler.on_receipt(
            instance,
            {"owner_ref": owner_ref, "fields": fields, "source_ref": source_ref},
            store=store,
            adapter=None,
            contract=contract,
        )
        if result.success:
            resumed.append(continuation_id)

    inbox.apply_fields(case_id, fields_by_continuation=fields_by_continuation)
    inbox.mark_applied(case_id)

    return json.dumps(
        {"success": True, "status": "resumed", "case_id": case_id, "resumed_continuation_ids": resumed},
        ensure_ascii=False,
    )


def _handle_input_status(args: dict) -> str:
    bad = _ensure_engine()
    if bad is not None:
        return json.dumps(bad, ensure_ascii=False)
    from agentflow_hermes.continuation_store import ContinuationState, ContinuationStore
    from agentflow_hermes.interaction import STATE_ANSWERED, STATE_APPLIED, InteractionInbox

    case_id = str(args.get("case_id") or "")
    endpoint = str(args.get("endpoint") or "")
    if not case_id:
        return json.dumps({"success": False, "error": "case_id_required"}, ensure_ascii=False)
    if not endpoint:
        return json.dumps({"success": False, "error": "endpoint_required"}, ensure_ascii=False)

    store = ContinuationStore.canonical()
    inbox = InteractionInbox(store=store)
    case = inbox.get_case(case_id)
    if case is None:
        return json.dumps({"success": False, "error": "unknown_case"}, ensure_ascii=False)
    if case.origin_endpoint != endpoint:
        # Fail closed: never leak another origin lane's case status.
        return json.dumps({"success": False, "error": "origin_mismatch"}, ensure_ascii=False)

    instances = [store.get_instance(cid) for cid in case.continuation_ids]
    instances = [i for i in instances if i is not None]
    failed = any(i["state"] == ContinuationState.FAILED_RETRYABLE.value for i in instances)

    if failed:
        status = "failed retryable"
    elif case.state == STATE_APPLIED:
        status = "resumed"
    elif case.state == STATE_ANSWERED:
        status = "resolved"
    else:
        status = "waiting for you"

    return json.dumps(
        {
            "success": True,
            "case_id": case_id,
            "status": status,
            "continuations": [{"id": i["id"], "state": i["state"]} for i in instances],
        },
        ensure_ascii=False,
    )


def register(ctx) -> None:
    ctx.register_tool("agentflow_enqueue", "agentflow", AGENTFLOW_ENQUEUE_SCHEMA, _handle_enqueue, emoji="🧭")
    ctx.register_tool("agentflow_status", "agentflow", AGENTFLOW_STATUS_SCHEMA, _handle_status, emoji="📋")
    ctx.register_tool("agentflow_dispatch_dry_run", "agentflow", AGENTFLOW_DISPATCH_DRY_RUN_SCHEMA, _handle_dispatch_dry_run, emoji="🧪")
    ctx.register_tool("agentflow_dispatch", "agentflow", AGENTFLOW_DISPATCH_SCHEMA, _handle_dispatch, emoji="🚀")
    ctx.register_tool("agentflow_ack_ingest", "agentflow", AGENTFLOW_ACK_INGEST_SCHEMA, _handle_ack_ingest, emoji="✅")
    ctx.register_tool("agentflow_doctor", "agentflow", AGENTFLOW_DOCTOR_SCHEMA, _handle_doctor, emoji="🩺")
    ctx.register_tool("agentflow_bridge_cron", "agentflow", AGENTFLOW_BRIDGE_CRON_SCHEMA, _handle_bridge_cron, emoji="⏱️")
    ctx.register_tool("agentflow_live_status", "agentflow", AGENTFLOW_LIVE_STATUS_SCHEMA, _handle_live_status, emoji="🛡️")
    ctx.register_tool("agentflow_input_inbox", "agentflow", AGENTFLOW_INPUT_INBOX_SCHEMA, _handle_input_inbox, emoji="📥")
    ctx.register_tool("agentflow_submit_input_text", "agentflow", AGENTFLOW_SUBMIT_INPUT_TEXT_SCHEMA, _handle_submit_input_text, emoji="💬")
    ctx.register_tool("agentflow_input_status", "agentflow", AGENTFLOW_INPUT_STATUS_SCHEMA, _handle_input_status, emoji="🔔")
