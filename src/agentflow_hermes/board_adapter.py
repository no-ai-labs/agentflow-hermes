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
(not a ``#name`` placeholder), ``RealBoardAdapter.subscribe`` ensures the
board's own Kanban DB actually reflects a wake-capable subscription (M30D):

- If the installed ``hermes`` CLI's own ``notify-subscribe --help`` output
  advertises ``--delivery-mode`` (probed once per adapter instance, never
  invented), ``subscribe`` calls ``notify-subscribe`` with ``--delivery-mode
  notify+wake --chat-type ...`` and then verifies the authoritative
  ``kanban_notify_subs`` row landed with ``delivery_mode='notify+wake'``.
  That verified canonical row is ACK/wake success on its own -- new/global
  boards need not have the legacy ``ack_subscription``/``ack_active_wake``
  tables at all.
- Otherwise it falls back to idempotently repairing the legacy
  ``kanban_notify_subs``/``ack_subscription``/``ack_active_wake`` rows,
  mirroring ``kanban_auto_remediation_adapter.py``'s
  ``_ensure_durable_ack_rows`` for the M24B oracle canary.

Either way, a nested ACK/verification failure is fatal to the whole
``subscribe`` call: a bare ``kanban_notify_subs`` row alone is not enough for
the gateway's active-wake path to fire.
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
    # Devhub #shaman / #hermes-main lanes used by the AgentFlow loops. These
    # mirror scripts/kanban_auto_remediation_adapter.py's default notify set.
    "shaman": "1500539609413849200",
    "hermes-main": "1497895797579190357",
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


def _sanitized_cli_error(
    *, exc: BaseException | None = None, returncode: int | None = None, stderr: str = ""
) -> str:
    """Map CLI runner failures to bounded durable classes.

    AgentFlow stores this string in its retry outbox. Keep it low-cardinality
    and free of raw command lines, DB paths, exception reprs, or route text.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        return "cli_runner_timeout"
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return "cli_runner_db_locked"
    msg = (stderr or "").lower()
    if "database is locked" in msg or "database table is locked" in msg or "sqlite_busy" in msg:
        return "cli_runner_db_locked"
    if "timed out" in msg or "timeout" in msg:
        return "cli_runner_timeout"
    if returncode is not None:
        return "cli_runner_failed"
    return "cli_runner_error"


class FakeBoardAdapter:
    """In-memory idempotent adapter for tests. Never mutates external state."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}  # idempotency_key -> task
        self.subscriptions: list[tuple[str, str]] = []
        self.scheduled_origin_wakes: list[tuple[str, str]] = []
        self.satisfied_origin_wakes: set[tuple[str, str]] = set()
        self.consumer_acks: list[tuple[str, str, str]] = []
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

    def origin_wake_satisfied(self, task_id: str, endpoint: str) -> dict[str, Any]:
        if (task_id, endpoint) in self.satisfied_origin_wakes:
            return {"success": True, "source": "fake_existing_origin_wake"}
        return {"success": False, "error": "origin_wake_not_satisfied"}

    def schedule_origin_wake(self, task_id: str, endpoint: str) -> dict[str, Any]:
        pair = (task_id, endpoint)
        if pair not in self.scheduled_origin_wakes:
            self.scheduled_origin_wakes.append(pair)
        self.satisfied_origin_wakes.add(pair)
        return {"success": True, "scheduled": True}

    def consumer_ack_satisfied(self, task_id: str, endpoint: str, status: str) -> dict[str, Any]:
        if (task_id, endpoint, status) in self.consumer_acks:
            return {"success": True, "consumer_ack_status": status}
        return {"success": False, "error": "consumer_ack_missing"}

    def record_consumer_ack(self, task_id: str, endpoint: str, status: str) -> dict[str, Any]:
        pair = (task_id, endpoint, status)
        if pair not in self.consumer_acks:
            self.consumer_acks.append(pair)
        return {"success": True, "consumer_ack_status": status}

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
        self._delivery_mode_supported: bool | None = None

    def _supports_delivery_mode(self) -> bool:
        """Detect whether the installed `hermes` CLI's notify-subscribe
        supports canonical `--delivery-mode` by checking its own --help
        output (never invents CLI capability). Cached per adapter instance
        so repeat subscribe() calls don't re-probe."""
        if self._delivery_mode_supported is not None:
            return self._delivery_mode_supported
        argv = [self.hermes_bin, "kanban", "notify-subscribe", "--help"]
        try:
            returncode, stdout, _stderr = self.runner(argv)
        except Exception:
            self._delivery_mode_supported = False
            return False
        self._delivery_mode_supported = returncode == 0 and "--delivery-mode" in (stdout or "")
        return self._delivery_mode_supported

    def _verify_canonical_notify_sub(
        self,
        task_id: str,
        *,
        platform: str,
        chat_id: str,
        thread_id: str = "",
        chat_type: str = "",
        user_id: str = "",
    ) -> dict[str, Any]:
        """Verify the authoritative canonical `kanban_notify_subs` row for
        this subscription has `delivery_mode='notify+wake'` in the target
        board's own Kanban DB. This is ACK/wake success on its own -- unlike
        the legacy path, it does not require the legacy
        ack_subscription/ack_active_wake tables to exist (M30D: new/global
        boards may not have them)."""
        try:
            conn = sqlite3.connect(str(self.board_db_path))
        except Exception:
            return {"success": False, "error": "canonical_db_connect_failed"}
        try:
            if not _table_exists(conn, "kanban_notify_subs"):
                return {"success": False, "error": "canonical_schema_missing"}
            cols = _table_columns(conn, "kanban_notify_subs")
            if "delivery_mode" not in cols:
                return {"success": False, "error": "canonical_schema_missing"}
            row = conn.execute(
                "SELECT delivery_mode, chat_type, user_id FROM kanban_notify_subs "
                "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
                (task_id, platform, chat_id, thread_id),
            ).fetchone()
            if row is None:
                receipt = self._verify_existing_notify_wake_receipt(
                    conn,
                    task_id,
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    chat_type=chat_type,
                    user_id=user_id,
                )
                if receipt.get("success"):
                    return receipt
                return {"success": False, "error": "canonical_row_missing", "receipt": receipt}
            row_mode, row_chat_type, row_user_id = row
            if row_mode != "notify+wake":
                return {"success": False, "error": "canonical_delivery_mode_mismatch"}
            if chat_type and "chat_type" in cols and row_chat_type and row_chat_type != chat_type:
                return {"success": False, "error": "canonical_chat_type_mismatch"}
            if user_id and "user_id" in cols and row_user_id and row_user_id != user_id:
                return {"success": False, "error": "canonical_user_id_mismatch"}
            return {"success": True}
        except Exception:
            return {"success": False, "error": "canonical_verify_failed"}
        finally:
            conn.close()

    def _verify_existing_notify_wake_receipt(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        *,
        platform: str,
        chat_id: str,
        thread_id: str = "",
        chat_type: str = "",
        user_id: str = "",
    ) -> dict[str, Any]:
        """Recognize an already-delivered one-shot notify+wake cleanup.

        A global board can delete the source subscription row after a one-shot
        delivery. That is still an authoritative return edge only when a typed
        origin row and a durable passive-delivery + active-wake receipt exist.
        Consumer ACK is not inferred here; semantic_refusal records its own ACK.
        """
        if not (_table_exists(conn, "kanban_task_origin") and _table_exists(conn, "kanban_notify_receipts")):
            return {"success": False, "error": "receipt_schema_missing"}
        origin_cols = _table_columns(conn, "kanban_task_origin")
        receipt_cols = _table_columns(conn, "kanban_notify_receipts")
        required_origin = {"task_id", "platform", "chat_id", "thread_id"}
        required_receipt = {"task_id", "platform", "chat_id", "thread_id", "notify_delivery_status", "active_wake_status"}
        if not required_origin.issubset(origin_cols) or not required_receipt.issubset(receipt_cols):
            return {"success": False, "error": "receipt_schema_missing"}
        origin_chat_type_expr = "chat_type" if "chat_type" in origin_cols else "''"
        origin_user_id_expr = "user_id" if "user_id" in origin_cols else "''"
        origin = conn.execute(
            f"SELECT {origin_chat_type_expr}, {origin_user_id_expr} FROM kanban_task_origin WHERE task_id=? AND platform=? AND chat_id=? AND thread_id=?",
            (task_id, platform, chat_id, thread_id),
        ).fetchone()
        if origin is None:
            return {"success": False, "error": "typed_origin_missing"}
        origin_chat_type, origin_user_id = origin
        if chat_type and origin_chat_type and origin_chat_type not in (chat_type, "group"):
            return {"success": False, "error": "typed_origin_chat_type_mismatch"}
        if user_id and origin_user_id and origin_user_id != user_id:
            return {"success": False, "error": "typed_origin_user_id_mismatch"}
        receipt = conn.execute(
            """
            SELECT notify_delivery_status, active_wake_status, consumer_ack_status
            FROM kanban_notify_receipts
            WHERE task_id=? AND platform=? AND chat_id=? AND thread_id=?
            """,
            (task_id, platform, chat_id, thread_id),
        ).fetchone()
        if receipt is None:
            return {"success": False, "error": "notify_wake_receipt_missing"}
        notify_status, wake_status, consumer_ack_status = receipt
        if notify_status != "delivered" or wake_status not in {"accepted", "started", "completed"}:
            return {"success": False, "error": "notify_wake_receipt_not_accepted"}
        return {
            "success": True,
            "source": "existing_notify_wake_receipt",
            "consumer_ack_status": consumer_ack_status or "",
        }

    def origin_wake_satisfied(self, task_id: str, endpoint: str) -> dict[str, Any]:
        """Check durable origin+wake receipts before trying one-shot subscribe.

        This is the terminal-source race boundary: a gateway may already have
        consumed and deleted ``kanban_notify_subs`` after successfully waking
        the origin. That durable receipt is sufficient for semantic-refusal ACK
        materialization; no new ephemeral subscription row is required.
        """
        origin_flags = _map_origin_to_flags(endpoint)
        if not origin_flags:
            return {"success": False, "error": "unparseable_origin_endpoint"}
        if origin_flags["platform"] == "discord":
            chat_key = origin_flags["chat_id"].lstrip("#")
            origin_flags["chat_id"] = _DISCORD_CHANNEL_ALIASES.get(chat_key, origin_flags["chat_id"])
            if not origin_flags["chat_id"].isdigit():
                return {"success": False, "error": "discord_chat_id_not_numeric"}
        try:
            conn = sqlite3.connect(str(self.board_db_path))
        except Exception:
            return {"success": False, "error": "canonical_db_connect_failed"}
        try:
            delivered = self._verify_existing_notify_wake_receipt(
                conn,
                task_id,
                platform=origin_flags["platform"],
                chat_id=origin_flags["chat_id"],
                thread_id=origin_flags.get("thread_id", ""),
                chat_type="thread" if origin_flags.get("thread_id") else "channel",
                user_id=origin_flags.get("user_id", ""),
            )
            if delivered.get("success"):
                return delivered
            return self._verify_existing_origin_wake_receipt(
                conn,
                task_id,
                platform=origin_flags["platform"],
                chat_id=origin_flags["chat_id"],
                thread_id=origin_flags.get("thread_id", ""),
                chat_type="thread" if origin_flags.get("thread_id") else "channel",
                user_id=origin_flags.get("user_id", ""),
            )
        finally:
            conn.close()

    def _verify_existing_origin_wake_receipt(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        *,
        platform: str,
        chat_id: str,
        thread_id: str = "",
        chat_type: str = "",
        user_id: str = "",
    ) -> dict[str, Any]:
        """Recognize accepted typed wake-origin receipts without passive notify."""
        if not (_table_exists(conn, "kanban_task_origin") and _table_exists(conn, "kanban_notify_receipts")):
            return {"success": False, "error": "receipt_schema_missing"}
        origin_cols = _table_columns(conn, "kanban_task_origin")
        receipt_cols = _table_columns(conn, "kanban_notify_receipts")
        required_origin = {"task_id", "platform", "chat_id", "thread_id"}
        required_receipt = {"task_id", "platform", "chat_id", "thread_id", "active_wake_status"}
        if not required_origin.issubset(origin_cols) or not required_receipt.issubset(receipt_cols):
            return {"success": False, "error": "receipt_schema_missing"}
        origin_chat_type_expr = "chat_type" if "chat_type" in origin_cols else "''"
        origin_user_id_expr = "user_id" if "user_id" in origin_cols else "''"
        origin = conn.execute(
            f"SELECT {origin_chat_type_expr}, {origin_user_id_expr} FROM kanban_task_origin WHERE task_id=? AND platform=? AND chat_id=? AND thread_id=?",
            (task_id, platform, chat_id, thread_id),
        ).fetchone()
        if origin is None:
            return {"success": False, "error": "typed_origin_missing"}
        origin_chat_type, origin_user_id = origin
        if chat_type and origin_chat_type and origin_chat_type not in (chat_type, "group"):
            return {"success": False, "error": "typed_origin_chat_type_mismatch"}
        if user_id and origin_user_id and origin_user_id != user_id:
            return {"success": False, "error": "typed_origin_user_id_mismatch"}
        receipt = conn.execute(
            """
            SELECT active_wake_status, consumer_ack_status
            FROM kanban_notify_receipts
            WHERE task_id=? AND platform=? AND chat_id=? AND thread_id=?
            """,
            (task_id, platform, chat_id, thread_id),
        ).fetchone()
        if receipt is None:
            return {"success": False, "error": "origin_wake_receipt_missing"}
        wake_status, consumer_ack_status = receipt
        if wake_status not in {"accepted", "started", "completed"}:
            return {"success": False, "error": "origin_wake_not_accepted"}
        return {
            "success": True,
            "source": "existing_origin_wake_receipt",
            "consumer_ack_status": consumer_ack_status or "",
        }

    def consumer_ack_satisfied(self, task_id: str, endpoint: str, status: str) -> dict[str, Any]:
        """Verify a durable Hermes consumer ACK receipt for the task origin."""
        origin_flags = _map_origin_to_flags(endpoint)
        if not origin_flags:
            return {"success": False, "error": "unparseable_origin_endpoint"}
        if origin_flags["platform"] == "discord":
            chat_key = origin_flags["chat_id"].lstrip("#")
            origin_flags["chat_id"] = _DISCORD_CHANNEL_ALIASES.get(chat_key, origin_flags["chat_id"])
            if not origin_flags["chat_id"].isdigit():
                return {"success": False, "error": "discord_chat_id_not_numeric"}
        try:
            conn = sqlite3.connect(str(self.board_db_path))
        except Exception:
            return {"success": False, "error": "canonical_db_connect_failed"}
        try:
            if not (_table_exists(conn, "kanban_task_origin") and _table_exists(conn, "kanban_notify_receipts")):
                return {"success": False, "error": "receipt_schema_missing"}
            row = conn.execute(
                """
                SELECT r.consumer_ack_status
                  FROM kanban_task_origin o
                  JOIN kanban_notify_receipts r
                    ON r.task_id=o.task_id AND r.platform=o.platform
                   AND r.chat_id=o.chat_id AND r.thread_id=o.thread_id
                 WHERE o.task_id=? AND o.platform=? AND o.chat_id=? AND o.thread_id=?
                """,
                (task_id, origin_flags["platform"], origin_flags["chat_id"], origin_flags.get("thread_id", "")),
            ).fetchone()
            if row is None:
                return {"success": False, "error": "consumer_ack_missing"}
            actual = row[0] or ""
            if actual != status:
                return {"success": False, "error": "consumer_ack_status_mismatch", "consumer_ack_status": actual}
            return {"success": True, "consumer_ack_status": actual}
        except Exception:
            return {"success": False, "error": "consumer_ack_verify_failed"}
        finally:
            conn.close()

    def schedule_origin_wake(self, task_id: str, endpoint: str) -> dict[str, Any]:
        """Schedule a durable origin wake through Hermes CLI, fail-closed.

        This generic edge records ``active_wake_status=scheduled`` against the
        durable ``kanban_task_origin`` binding. Gateway watchers own the actual
        active wake; AgentFlow never sends directly and never writes Hermes DB
        tables itself.
        """
        origin_flags = _map_origin_to_flags(endpoint)
        argv = [self.hermes_bin, "kanban", "--board", self.board, "wake-origin", task_id]
        if origin_flags:
            argv += ["--platform", origin_flags["platform"], "--chat-id", origin_flags["chat_id"]]
            if origin_flags.get("thread_id"):
                argv += ["--thread-id", origin_flags["thread_id"]]
        argv += ["--json"]
        return self._run_json(argv)

    def record_consumer_ack(self, task_id: str, endpoint: str, status: str) -> dict[str, Any]:
        """Record a typed semantic ACK via Hermes' public CLI, never direct DB."""
        origin_flags = _map_origin_to_flags(endpoint)
        argv = [self.hermes_bin, "kanban", "--board", self.board, "consumer-ack-origin", task_id, "--status", status]
        if origin_flags:
            if origin_flags["platform"] == "discord":
                chat_key = origin_flags["chat_id"].lstrip("#")
                origin_flags["chat_id"] = _DISCORD_CHANNEL_ALIASES.get(chat_key, origin_flags["chat_id"])
            argv += ["--platform", origin_flags["platform"], "--chat-id", origin_flags["chat_id"]]
            if origin_flags.get("thread_id"):
                argv += ["--thread-id", origin_flags["thread_id"]]
        argv += ["--json"]
        result = self._run_json(argv)
        if not result.get("success"):
            return result
        verified = self.consumer_ack_satisfied(task_id, endpoint, status)
        if not verified.get("success"):
            return {"success": False, "error": verified.get("error", "consumer_ack_verify_failed"), "ack": verified}
        return {**result, "ack": verified}

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
            returncode, _stdout, stderr = self.runner(argv)
        except Exception as exc:
            return {"success": False, "error": _sanitized_cli_error(exc=exc)}
        stderr_error = _sanitized_cli_error(stderr=stderr)
        if stderr_error in {"cli_runner_db_locked", "cli_runner_timeout"}:
            return {"success": False, "error": stderr_error}
        if returncode != 0:
            return {"success": False, "error": _sanitized_cli_error(returncode=returncode, stderr=stderr)}
        return {"success": True}

    def _run_json(self, argv: list[str]) -> dict[str, Any]:
        """Run a subcommand that supports ``--json`` (only `create`/`show`/`list`)."""
        try:
            returncode, stdout, stderr = self.runner(argv)
        except Exception as exc:
            return {"success": False, "error": _sanitized_cli_error(exc=exc)}
        stderr_error = _sanitized_cli_error(stderr=stderr)
        if stderr_error in {"cli_runner_db_locked", "cli_runner_timeout"}:
            return {"success": False, "error": stderr_error}
        if returncode != 0:
            return {"success": False, "error": _sanitized_cli_error(returncode=returncode, stderr=stderr)}
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
        parent_task_id = short_text(str(intent.get("parent_task_id") or ""))
        if parent_task_id:
            # Link a generated review card to its fix card so the code-fix
            # graph is a real parent/child graph on the board, not two
            # unrelated cards. The Hermes ``create`` surface accepts --parent
            # (same flag RealKanbanGraphAdapter uses for roadmap graphs).
            argv += ["--parent", parent_task_id]
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
            if not origin_flags["chat_id"].isdigit():
                return {"success": False, "error": "discord_chat_id_not_numeric"}

        # Only probe/use the canonical delivery-mode CLI shape when ACK
        # repair is actually wired (real adapter with a real board DB); pure
        # argv-shape unit tests (fake runner, no board_db_path) keep their
        # existing plain notify-subscribe behavior untouched.
        use_canonical = self._repair_ack_rows and self._supports_delivery_mode()
        chat_type = "thread" if origin_flags.get("thread_id") else "channel"
        chat_id = origin_flags["chat_id"]

        if use_canonical:
            existing = self._verify_canonical_notify_sub(
                task_id,
                platform=origin_flags["platform"],
                chat_id=chat_id,
                thread_id=origin_flags.get("thread_id", ""),
                chat_type=chat_type,
                user_id=origin_flags.get("user_id", ""),
            )
            if existing.get("success"):
                return {"success": True, "ack": existing, "duplicate": True}

        argv = [
            self.hermes_bin, "kanban", "--board", self.board, "notify-subscribe", task_id,
            "--platform", origin_flags["platform"], "--chat-id", origin_flags["chat_id"],
        ]
        if origin_flags.get("thread_id"):
            argv += ["--thread-id", origin_flags["thread_id"]]
        if origin_flags.get("user_id"):
            argv += ["--user-id", origin_flags["user_id"]]
        if use_canonical:
            argv += ["--chat-type", chat_type, "--delivery-mode", "notify+wake"]

        result = self._run(argv)
        if not (result.get("success") and self._repair_ack_rows and origin_flags["platform"] == "discord" and chat_id.isdigit()):
            return result

        if use_canonical:
            ack = self._verify_canonical_notify_sub(
                task_id,
                platform="discord",
                chat_id=chat_id,
                thread_id=origin_flags.get("thread_id", ""),
                chat_type=chat_type,
                user_id=origin_flags.get("user_id", ""),
            )
            if not ack.get("success"):
                # A CLI exit-0 alone isn't proof the canonical row landed as
                # notify+wake -- verify against the authoritative board DB
                # and fail closed if it didn't (M30D).
                return {"success": False, "error": ack.get("error", "canonical_verification_failed"), "ack": ack}
            return {**result, "ack": ack}

        ack = self._ensure_durable_ack_rows(
            task_id, platform="discord", chat_id=chat_id, thread_id=origin_flags.get("thread_id", "")
        )
        if not ack.get("success"):
            # notify-subscribe plus ACK repair is one semantic operation:
            # a bare kanban_notify_subs row with no durable
            # ack_subscription/ack_active_wake repair leaves the
            # gateway's active-wake path with nothing to act on, so this
            # must never surface as a top-level success (M30C).
            return {"success": False, "error": ack.get("error", "ack_ensure_failed"), "ack": ack}
        return {**result, "ack": ack}

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
