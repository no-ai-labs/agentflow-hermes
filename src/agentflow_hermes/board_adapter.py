"""Real/fake board adapter primitives for the needs-input continuation vertical.

Owner continuation needs create + block + subscribe + comment + lazy
downstream creation, not a single `create_graph` call, so this is a separate,
smaller surface from ``graph_creator.py``'s remediation-graph adapters. It
follows the same fake/real split and idempotent-CLI-runner pattern already
established there (``RealKanbanGraphAdapter``).
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

    def block_task(self, task_id: str, reason: str) -> dict[str, Any]:
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
    Idempotency is enforced twice: server-side via `--idempotency-key` (per
    the same contract as `RealKanbanGraphAdapter`) and locally via a
    key->task_id cache, so a duplicate local call never re-invokes the CLI.
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

    def create_task(self, intent: dict[str, Any]) -> dict[str, Any]:
        key = str(intent.get("idempotency_key") or "")
        cached = self._key_to_task_id.get(key)
        if cached:
            return {"success": True, "task_id": cached}

        argv = [self.hermes_bin, "kanban", "--board", self.board, "create", str(intent.get("title") or "")]
        assignee = short_text(str(intent.get("assignee") or ""))
        if assignee:
            argv += ["--assignee", assignee]
        if key:
            argv += ["--idempotency-key", key]
        origin_flags = _map_origin_to_flags(str(intent.get("origin_ref") or ""), str(intent.get("return_to_ref") or ""))
        if origin_flags:
            argv += ["--origin-platform", origin_flags["platform"], "--origin-chat-id", origin_flags["chat_id"]]
        if intent.get("status"):
            argv += ["--status", str(intent["status"])]
        if intent.get("blocked_reason"):
            argv += ["--blocked-reason", str(intent["blocked_reason"])]
        argv += ["--json"]

        result = self._run(argv)
        if not result.get("success"):
            return {"success": False, "error": result.get("error", "cli_error")}
        task_id = short_text(str(result.get("task_id") or result.get("id") or ""))
        if not task_id:
            return {"success": False, "error": "cli_missing_task_id"}
        if key:
            self._key_to_task_id[key] = task_id
        return {"success": True, "task_id": task_id}

    def block_task(self, task_id: str, reason: str) -> dict[str, Any]:
        argv = [self.hermes_bin, "kanban", "--board", self.board, "block", task_id, "--reason", reason, "--json"]
        return self._run(argv)

    def subscribe(self, task_id: str, endpoint: str) -> dict[str, Any]:
        argv = [self.hermes_bin, "kanban", "--board", self.board, "subscribe", task_id]
        origin_flags = _map_origin_to_flags(endpoint)
        if origin_flags:
            argv += ["--origin-platform", origin_flags["platform"], "--origin-chat-id", origin_flags["chat_id"]]
        argv += ["--json"]
        return self._run(argv)

    def comment(self, task_id: str, body: str) -> dict[str, Any]:
        argv = [self.hermes_bin, "kanban", "--board", self.board, "comment", task_id, "--body", body, "--json"]
        return self._run(argv)

    def complete_owner_anchor(self, task_id: str, *, receipt_ref: str) -> dict[str, Any]:
        argv = [self.hermes_bin, "kanban", "--board", self.board, "complete", task_id, "--receipt-ref", receipt_ref, "--json"]
        return self._run(argv)
