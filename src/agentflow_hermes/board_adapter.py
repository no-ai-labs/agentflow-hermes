"""Real/fake board adapter primitives for the needs-input continuation vertical.

Owner continuation needs create + block + subscribe + comment + lazy
downstream creation, not a single `create_graph` call, so this is a separate,
smaller surface from ``graph_creator.py``'s remediation-graph adapters.

``RealBoardAdapter``'s argv shapes are verified against the installed
``hermes`` CLI's own ``--help`` output and ``hermes_cli/kanban.py`` source
(not invented):

- ``create`` supports ``--initial-status {blocked,running}`` and ``--json``,
  but has no ``--blocked-reason``/origin flags — the awaiting-owner reason and
  required-field checklist are folded into the task body instead.
- ``block`` is positional ``block <task_id> [reason ...] [--kind ...]`` and
  prints plain text, not JSON.
- ``comment`` is positional ``comment <task_id> <text...>`` and prints plain
  text.
- ``complete`` has ``--summary``/``--metadata`` (JSON dict), not
  ``--receipt-ref``, and prints plain text.
- there is no ``hermes kanban subscribe``; the real command is
  ``notify-subscribe <task_id> --platform P --chat-id C [--thread-id ..]
  [--user-id ..]``, and it also prints plain text, not JSON.

Only ``create``/``show``/``list`` support ``--json`` on this CLI version; the
other subcommands must be treated as successful purely by exit code.

``_default_cli_runner`` pins ``HERMES_KANBAN_BOARD``/``HERMES_KANBAN_DB`` in
the subprocess environment to the board named by the ``--board`` argv flag,
the same fix already applied in ``scripts/kanban_auto_remediation_adapter.py``
after real cards were observed landing on the wrong board: Kanban workers
often inherit ``HERMES_KANBAN_DB`` for whatever board they're currently
running against, and that env var can silently outrank an explicit
``--board`` flag on this CLI version. Without pinning it, ``--board
warroom-os`` is not sufficient by itself to guarantee a mutation lands on
``warroom-os``.

After a successful real ``notify-subscribe`` to a numeric Discord channel id
(not a ``#name`` placeholder), ``RealBoardAdapter.subscribe`` also
idempotently repairs the durable ``kanban_notify_subs`` /
``ack_subscription`` / ``ack_active_wake`` rows in that board's own Kanban
DB, mirroring ``kanban_auto_remediation_adapter.py``'s
``_ensure_durable_ack_rows`` for the M24B oracle canary. ``notify-subscribe``
alone only writes ``kanban_notify_subs``; the gateway's active-wake path also
needs the ``ack_subscription``/``ack_active_wake`` rows to fire.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .graph_creator import _map_origin_to_flags
from .live.sanitize import short_text

BoardCliRunner = Callable[[list[str]], tuple[int, str, str]]

_ACK_TABLES = ("kanban_notify_subs", "ack_subscription", "ack_active_wake")
_DISCORD_CHANNEL_ALIASES = {
    # Devhub #research. Keep the real ACK path on the numeric channel id;
    # chat_id='research' is only a human label and does not wake Discord.
    "research": "1499390151393284106",
}


def default_board_kanban_db_path(board: str) -> Path:
    inherited_db = os.environ.get("HERMES_KANBAN_DB")
    if inherited_db:
        inherited = Path(inherited_db)
        # Worker profiles set HERMES_HOME to the profile dir, but Kanban boards
        # live in the shared board root. If the current run already points at a
        # per-board DB, derive the sibling board path from that known-good root.
        if inherited.name == "kanban.db" and inherited.parent.parent.name == "boards":
            return inherited.parent.parent / board / "kanban.db"
    return Path.home() / ".hermes" / "kanban" / "boards" / board / "kanban.db"


def _default_cli_runner(argv: list[str]) -> tuple[int, str, str]:
    env = dict(os.environ)
    if "--board" in argv:
        idx = argv.index("--board")
        if idx + 1 < len(argv):
            board = argv[idx + 1]
            env["HERMES_KANBAN_BOARD"] = board
            env["HERMES_KANBAN_DB"] = str(default_board_kanban_db_path(board))
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}


class FakeBoardAdapter:
    """In-memory idempotent adapter for tests. Never mutates external state."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}  # idempotency_key -> task
        self.subscriptions: list[tuple[str, str]] = []
        self.blocked: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self.completed: list[tuple[str, str]] = []

    def create_task(self, intent: dict[str, Any]) -> dict[str, Any]:
        key = str(intent.get("idempotency_key") or "")
        existing = self.tasks.get(key)
        if existing is not None:
            return {"success": True, "task_id": existing["task_id"], "duplicate": True}
        task_id = "task:" + hashlib.sha256(key.encode()).hexdigest()[:12]
        self.tasks[key] = {"task_id": task_id, **intent}
        return {"success": True, "task_id": task_id, "duplicate": False}

    def block_task(self, task_id: str, reason: str, *, kind: str = "needs_input") -> dict[str, Any]:
        pair = (task_id, reason)
        if pair not in self.blocked:
            self.blocked.append(pair)
        return {"success": True}

    def subscribe(self, task_id: str, endpoint: str) -> dict[str, Any]:
        pair = (task_id, endpoint)
        if pair not in self.subscriptions:
            self.subscriptions.append(pair)
        return {"success": True}

    def comment(self, task_id: str, body: str) -> dict[str, Any]:
        pair = (task_id, body)
        if pair not in self.comments:
            self.comments.append(pair)
        return {"success": True}

    def complete_owner_anchor(self, task_id: str, *, receipt_ref: str) -> dict[str, Any]:
        pair = (task_id, receipt_ref)
        if pair not in self.completed:
            self.completed.append(pair)
        return {"success": True}


class RealBoardAdapter:
    """Apply owner-continuation board operations via the real `hermes` CLI.

    Only constructed by explicit apply-mode wiring in the continuation engine.
    Idempotency is enforced twice: server-side via `--idempotency-key` on
    `create` (per the same contract as `RealKanbanGraphAdapter`) and locally
    via a key->task_id cache, so a duplicate local call never re-invokes the
    CLI.
    """

    def __init__(
        self,
        *,
        runner: BoardCliRunner | None = None,
        board: str = "",
        hermes_bin: str = "hermes",
        board_db_path: Path | str | None = None,
    ) -> None:
        self.runner = runner or _default_cli_runner
        self.board = short_text(board)
        self.hermes_bin = hermes_bin or "hermes"
        self.board_db_path = Path(board_db_path) if board_db_path else default_board_kanban_db_path(self.board)
        self._repair_ack_rows = runner is None or board_db_path is not None
        self._key_to_task_id: dict[str, str] = {}

    def _ensure_durable_ack_rows(
        self, task_id: str, *, platform: str, chat_id: str, thread_id: str = ""
    ) -> dict[str, Any]:
        """Idempotently repair the durable notify/ACK rows for `task_id` in
        this board's own Kanban DB. Never sends anything to Discord; it only
        writes local sqlite rows so the gateway's active-wake path has real
        state to act on. Safe to call repeatedly (ON CONFLICT/dedupe by key)."""
        try:
            conn = sqlite3.connect(str(self.board_db_path))
        except Exception:
            return {"success": False, "error": "ack_db_connect_failed"}
        try:
            if not all(_table_exists(conn, t) for t in _ACK_TABLES):
                return {"success": False, "error": "ack_schema_missing"}

            notify_cols = _table_columns(conn, "kanban_notify_subs")
            now = int(time.time())
            notifier_profile = "default"

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

            correlation_id = f"owner-anchor:{task_id}:{platform}:{chat_id}"
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
            return {"success": True}
        except Exception:
            return {"success": False, "error": "ack_ensure_failed"}
        finally:
            conn.close()

    def _run(self, argv: list[str]) -> dict[str, Any]:
        """Run a subcommand that prints plain text (block/comment/complete/
        notify-subscribe on this CLI version). Success is exit-code only."""
        try:
            returncode, _stdout, _stderr = self.runner(argv)
        except Exception:
            return {"success": False, "error": "cli_runner_error"}
        if returncode != 0:
            return {"success": False, "error": "cli_runner_failed"}
        return {"success": True}

    def _run_json(self, argv: list[str]) -> dict[str, Any]:
        """Run a subcommand that supports ``--json`` (only `create`/`show`/`list`)."""
        try:
            returncode, stdout, _stderr = self.runner(argv)
        except Exception:
            return {"success": False, "error": "cli_runner_error"}
        if returncode != 0:
            return {"success": False, "error": "cli_runner_failed"}
        try:
            result = json.loads(stdout)
        except (json.JSONDecodeError, ValueError, TypeError):
            return {"success": False, "error": "cli_invalid_json"}
        if not isinstance(result, dict):
            return {"success": False, "error": "cli_invalid_json"}
        return {"success": True, **result}

    def _blocked_anchor_body(self, intent: dict[str, Any]) -> str:
        body = str(intent.get("body") or "")
        reason = str(intent.get("blocked_reason") or "")
        checklist = intent.get("required_owner_fields") or []
        extra_lines = [f"Awaiting owner input: {reason}" if reason else "Awaiting owner input."]
        if checklist:
            extra_lines.append("Required owner fields:")
            extra_lines.extend(f"- {name}" for name in checklist)
        extra = "\n".join(extra_lines)
        return f"{body}\n\n{extra}".strip() if body else extra

    def create_task(self, intent: dict[str, Any]) -> dict[str, Any]:
        key = str(intent.get("idempotency_key") or "")
        cached = self._key_to_task_id.get(key)
        if cached:
            return {"success": True, "task_id": cached}

        title = str(intent.get("title") or "")
        is_blocked = str(intent.get("status") or "") == "blocked"
        body = self._blocked_anchor_body(intent) if is_blocked else str(intent.get("body") or "")

        argv = [self.hermes_bin, "kanban", "--board", self.board, "create", title]
        if body:
            argv += ["--body", body]
        assignee = short_text(str(intent.get("assignee") or ""))
        if assignee:
            argv += ["--assignee", assignee]
        if key:
            argv += ["--idempotency-key", key]
        if is_blocked:
            argv += ["--initial-status", "blocked"]
        argv += ["--json"]

        result = self._run_json(argv)
        if not result.get("success"):
            return {"success": False, "error": result.get("error", "cli_error")}
        task_id = short_text(str(result.get("id") or result.get("task_id") or ""))
        if not task_id:
            return {"success": False, "error": "cli_missing_task_id"}
        if key:
            self._key_to_task_id[key] = task_id
        return {"success": True, "task_id": task_id}

    def block_task(self, task_id: str, reason: str, *, kind: str = "needs_input") -> dict[str, Any]:
        argv = [self.hermes_bin, "kanban", "--board", self.board, "block", task_id]
        if reason:
            argv.append(reason)
        if kind:
            argv += ["--kind", kind]
        return self._run(argv)

    def subscribe(self, task_id: str, endpoint: str) -> dict[str, Any]:
        origin_flags = _map_origin_to_flags(endpoint)
        if not origin_flags:
            return {"success": False, "error": "unparseable_origin_endpoint"}
        if origin_flags["platform"] == "discord":
            chat_key = origin_flags["chat_id"].lstrip("#")
            origin_flags["chat_id"] = _DISCORD_CHANNEL_ALIASES.get(chat_key, origin_flags["chat_id"])
        argv = [
            self.hermes_bin, "kanban", "--board", self.board, "notify-subscribe", task_id,
            "--platform", origin_flags["platform"], "--chat-id", origin_flags["chat_id"],
        ]
        if origin_flags.get("thread_id"):
            argv += ["--thread-id", origin_flags["thread_id"]]
        if origin_flags.get("user_id"):
            argv += ["--user-id", origin_flags["user_id"]]
        result = self._run(argv)
        chat_id = origin_flags["chat_id"]
        if result.get("success") and self._repair_ack_rows and origin_flags["platform"] == "discord" and chat_id.isdigit():
            ack = self._ensure_durable_ack_rows(
                task_id, platform="discord", chat_id=chat_id, thread_id=origin_flags.get("thread_id", "")
            )
            result = {**result, "ack": ack}
        return result

    def comment(self, task_id: str, body: str) -> dict[str, Any]:
        argv = [self.hermes_bin, "kanban", "--board", self.board, "comment", task_id, body]
        return self._run(argv)

    def complete_owner_anchor(self, task_id: str, *, receipt_ref: str) -> dict[str, Any]:
        argv = [
            self.hermes_bin, "kanban", "--board", self.board, "complete", task_id,
            "--summary", f"owner_receipt:{receipt_ref}",
            "--metadata", json.dumps({"receipt_ref": receipt_ref}),
        ]
        return self._run(argv)
