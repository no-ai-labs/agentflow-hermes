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
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Any, Callable

from .graph_creator import _map_origin_to_flags
from .live.sanitize import short_text

BoardCliRunner = Callable[[list[str]], tuple[int, str, str]]


def _default_cli_runner(argv: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    return proc.returncode, proc.stdout, proc.stderr


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
    ) -> None:
        self.runner = runner or _default_cli_runner
        self.board = short_text(board)
        self.hermes_bin = hermes_bin or "hermes"
        self._key_to_task_id: dict[str, str] = {}

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
        argv = [
            self.hermes_bin, "kanban", "--board", self.board, "notify-subscribe", task_id,
            "--platform", origin_flags["platform"], "--chat-id", origin_flags["chat_id"],
        ]
        if origin_flags.get("thread_id"):
            argv += ["--thread-id", origin_flags["thread_id"]]
        if origin_flags.get("user_id"):
            argv += ["--user-id", origin_flags["user_id"]]
        return self._run(argv)

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
