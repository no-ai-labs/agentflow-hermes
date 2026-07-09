#!/usr/bin/env python3
"""Materialize one AgentFlow auto-remediation card spec into Hermes Kanban.

M24B canary scope: explicit oracle-lab board targeting plus durable
notify-subscribe rows for #shaman and #hermes-main. This adapter creates Kanban
cards only; it does not send Discord messages or enable any live-send route.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

STATE_PATH = Path(os.environ.get("HERMES_AUTO_REMEDIATION_MAP", "/home/duckran/.hermes/state/kanban_auto_remediation_map.json"))
BOARD = os.environ.get("HERMES_AUTO_REMEDIATION_BOARD", "oracle-lab")
NOTIFY_CHATS = os.environ.get("HERMES_AUTO_REMEDIATION_NOTIFY_CHATS", "discord:1500539609413849200,discord:1497895797579190357")
SAFE = "[REDACTED]"
RunFn = Callable[[list[str]], dict[str, Any]]


def _db_path(board: str) -> Path:
    override = os.environ.get("HERMES_AUTO_REMEDIATION_DB")
    if override:
        return Path(override)
    return Path(f"/home/duckran/.hermes/kanban/boards/{board}/kanban.db")


def _load(path: Path = STATE_PATH) -> dict[str, str]:
    try:
        data = json.loads(path.read_text())
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict[str, str], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2))
    tmp.replace(path)


def _run(args: list[str]) -> dict[str, Any]:
    env = dict(os.environ)
    # Kanban workers often inherit HERMES_KANBAN_DB for their current board; that
    # env override can beat --board in the CLI. Pin the subprocess to the canary
    # board DB so adapter writes cannot silently land on the supervisor board.
    env["HERMES_KANBAN_BOARD"] = BOARD
    env["HERMES_KANBAN_DB"] = f"/home/duckran/.hermes/kanban/boards/{BOARD}/kanban.db"
    proc = subprocess.run(args, text=True, capture_output=True, timeout=60, env=env)
    if proc.returncode != 0:
        return {"ok": False, "action": "command_failed", "returncode": proc.returncode, "stderr": SAFE}
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
            return parsed if isinstance(parsed, dict) else {"ok": True, "stdout": ""}
        except Exception:
            if "create" not in args:
                return {"ok": True, "stdout": ""}
            return {"ok": False, "action": "json_parse_failed", "stdout": SAFE}
    return {"ok": True}


def _task_id(result: dict[str, Any]) -> str:
    for key in ("id", "task_id"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    task = result.get("task")
    if isinstance(task, dict):
        value = task.get("id") or task.get("task_id")
        if isinstance(value, str) and value:
            return value
    return ""


def _notify_targets(raw: str = NOTIFY_CHATS) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            continue
        platform, chat_id = item.split(":", 1)
        platform = platform.strip()
        chat_id = chat_id.strip()
        if platform and chat_id and re.fullmatch(r"[A-Za-z0-9_-]+", platform):
            targets.append((platform, chat_id))
    return targets


def _workspace_from_spec(spec: dict[str, Any]) -> str:
    workspace_kind = str(spec.get("workspace_kind") or "dir")
    workspace_path = str(spec.get("workspace_path") or "/home/duckran/oracle-lab")
    if workspace_path.startswith("workspace_ref:"):
        workspace_path = "/home/duckran/oracle-lab"
    allowed = {"/home/duckran/oracle-lab", "/home/duckran/dev/agentflow-hermes"}
    if workspace_kind != "dir" or workspace_path not in allowed:
        workspace_kind = "dir"
        workspace_path = "/home/duckran/oracle-lab"
    return f"{workspace_kind}:{workspace_path}"


def _ensure_notify(task_id: str, *, board: str, run: RunFn = _run) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for platform, chat_id in _notify_targets():
        cmd = [
            "hermes", "kanban", "--board", board, "notify-subscribe", task_id,
            "--platform", platform,
            "--chat-id", chat_id,
            "--notifier-profile", "default",
        ]
        results.append(run(cmd))
    return results


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}


def _ensure_durable_ack_rows(task_id: str, *, board: str, db_path: Path) -> dict[str, Any]:
    """Create/repair durable notify + ACK rows for `task_id` so the gateway's
    active-wake path fires even though `hermes kanban notify-subscribe` only
    writes kanban_notify_subs rows. Idempotent: safe to call on every
    materialize (including deduped re-runs) without duplicating rows."""
    try:
        conn = sqlite3.connect(str(db_path))
    except Exception:
        return {"ok": False, "action": "ack_db_connect_failed"}
    try:
        if not (
            _table_exists(conn, "kanban_notify_subs")
            and _table_exists(conn, "ack_subscription")
            and _table_exists(conn, "ack_active_wake")
        ):
            return {"ok": False, "action": "ack_schema_missing"}

        notify_cols = _table_columns(conn, "kanban_notify_subs")
        now = int(time.time())
        thread_id = ""
        notifier_profile = "default"

        for platform, chat_id in _notify_targets():
            if "trigger_agent" in notify_cols:
                conn.execute(
                    """
                    INSERT INTO kanban_notify_subs
                        (task_id, platform, chat_id, thread_id, notifier_profile, trigger_agent, created_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(task_id, platform, chat_id, thread_id)
                    DO UPDATE SET trigger_agent = 1, notifier_profile = excluded.notifier_profile
                    """,
                    (task_id, platform, chat_id, thread_id, notifier_profile, now),
                )
            else:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO kanban_notify_subs
                        (task_id, platform, chat_id, thread_id, notifier_profile, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, platform, chat_id, thread_id, notifier_profile, now),
                )

            sub_row = conn.execute(
                """
                SELECT id FROM ack_subscription
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? AND notifier_profile = ?
                """,
                (task_id, platform, chat_id, thread_id, notifier_profile),
            ).fetchone()
            if sub_row:
                sub_id = sub_row[0]
                conn.execute(
                    "UPDATE ack_subscription SET active_wake_required = 1, desired_delivery_mode = ? WHERE id = ?",
                    ("passive+active_wake", sub_id),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO ack_subscription
                        (task_id, platform, chat_id, thread_id, notifier_profile,
                         desired_delivery_mode, active_wake_required, operator_receipt_required, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?)
                    """,
                    (task_id, platform, chat_id, thread_id, notifier_profile, "passive+active_wake", now),
                )
                sub_id = cur.lastrowid

            correlation_id = f"kanban-auto-remediation:{task_id}:{platform}:{chat_id}"
            wake_row = conn.execute(
                "SELECT id FROM ack_active_wake WHERE task_id = ? AND subscription_id = ? AND correlation_id = ?",
                (task_id, sub_id, correlation_id),
            ).fetchone()
            if not wake_row:
                conn.execute(
                    """
                    INSERT INTO ack_active_wake
                        (task_id, subscription_id, triggered_agent, correlation_id, status, created_at)
                    VALUES (?, ?, 0, ?, ?, ?)
                    """,
                    (task_id, sub_id, correlation_id, "pending", now),
                )

        conn.commit()
        return {"ok": True}
    except Exception:
        return {"ok": False, "action": "ack_ensure_failed"}
    finally:
        conn.close()


def materialize(spec: dict[str, Any], *, board: str = BOARD, state_path: Path = STATE_PATH, run: RunFn = _run, db_path: Path | None = None) -> tuple[int, dict[str, Any]]:
    if board != "oracle-lab":
        return 2, {"ok": False, "action": "unsupported_board", "board": board}
    idem = str(spec.get("idempotency_key") or "")
    if not idem.startswith("kanban-auto-remediation:"):
        return 2, {"ok": False, "action": "unsafe_idempotency_key"}
    resolved_db_path = db_path if db_path is not None else _db_path(board)

    mapping = _load(state_path)
    if idem in mapping:
        task_id = mapping[idem]
        notify = _ensure_notify(task_id, board=board, run=run)
        ack = _ensure_durable_ack_rows(task_id, board=board, db_path=resolved_db_path)
        if ack.get("ok") is False:
            return 1, {"ok": False, "action": "ack_ensure_failed", "id": task_id, "idempotency_key": idem, "notify": notify, "ack": ack}
        return 0, {"ok": True, "action": "deduped", "id": task_id, "idempotency_key": idem, "notify": notify}

    parent_arg: list[str] = []
    parent_idem = spec.get("parent_idempotency_key")
    if parent_idem:
        parent_id = mapping.get(str(parent_idem))
        if not parent_id:
            return 2, {"ok": False, "action": "missing_parent_mapping", "idempotency_key": idem}
        parent_arg = ["--parent", parent_id]

    title = str(spec.get("title") or "[auto-remediation]")[:180]
    body = str(spec.get("body") or "")[:4000]
    origin_ref = str(spec.get("origin_ref") or "")[:200]
    return_to_ref = str(spec.get("return_to_ref") or "")[:200]
    if origin_ref and "origin_ref:" not in body:
        body += f"\norigin_ref: {origin_ref}"
    if return_to_ref and "return_to_ref:" not in body:
        body += f"\nreturn_to_ref: {return_to_ref}"
    body += "\nnotify_subscribe: discord:1500539609413849200, discord:1497895797579190357"

    assignee = str(spec.get("assignee") or "ccsupervisor")
    cmd = [
        "hermes", "kanban", "--board", board, "create", title,
        "--json",
        "--assignee", assignee,
        "--workspace", _workspace_from_spec(spec),
        "--priority", "120",
        "--created-by", "agentflow-auto-remediation",
        "--idempotency-key", idem,
        "--body", body,
    ] + parent_arg
    result = run(cmd)
    if result.get("ok") is False and not _task_id(result):
        return 1, result
    task_id = _task_id(result)
    if not task_id:
        return 1, {"ok": False, "action": "missing_created_id", "idempotency_key": idem}
    mapping[idem] = task_id
    _save(mapping, state_path)
    notify = _ensure_notify(task_id, board=board, run=run)
    failed_notify = [n for n in notify if n.get("ok") is False]
    if failed_notify:
        return 1, {"ok": False, "action": "notify_subscribe_failed", "id": task_id, "idempotency_key": idem, "notify": notify}
    ack = _ensure_durable_ack_rows(task_id, board=board, db_path=resolved_db_path)
    if ack.get("ok") is False:
        return 1, {"ok": False, "action": "ack_ensure_failed", "id": task_id, "idempotency_key": idem, "notify": notify, "ack": ack}
    return 0, {"ok": True, "action": "created", "id": task_id, "idempotency_key": idem, "notify": notify}


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "action": "usage"}))
        return 2
    try:
        spec = json.loads(sys.argv[1])
    except Exception:
        print(json.dumps({"ok": False, "action": "invalid_json"}))
        return 2
    code, result = materialize(spec, board=BOARD, state_path=STATE_PATH)
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
