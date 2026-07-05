"""M13 durable maintenance-cycle receipts, cross-process idempotency, and the
degraded/deadletter failure path.

Reuses the committed sqlite3 + ``migrations.migrate`` pattern from
``store.py``. Only refs, reasons, and short sanitized identifiers are ever
persisted — never raw private paths, secrets, transcripts, or task bodies.

Cross-process idempotency: :func:`claim_cycle` atomically inserts one row per
``idempotency_key`` guarded by a unique index. A second claim attempt for the
same key — from a fresh runner instance, a duplicate oneshot invocation, or a
crash-recovery retry before a terminal status was written — always finds the
existing row and returns ``claimed=False`` with that prior row. No caller ever
performs a second action for the same key.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from agentflow_hermes.live.sanitize import safe_durable_ref, safe_event_payload, short_text
from agentflow_hermes.migrations import migrate

__all__ = [
    "build_cycle_key",
    "claim_cycle",
    "get_cycle",
    "update_cycle_status",
    "count_cycles_today",
    "last_applied_at",
    "set_degraded",
    "is_degraded",
    "write_deadletter",
    "day_bucket",
]


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30.0)
    con.row_factory = sqlite3.Row
    migrate(con)
    return con


def build_cycle_key(*, repo_id: str, target_unit: str, cycle_ref: str) -> str:
    """Stable cross-process idempotency key for one maintenance cycle attempt."""
    return safe_durable_ref(
        f"maint:cycle:{repo_id or 'default'}:{cycle_ref or 'nocycle'}:{target_unit or 'observe'}",
        field="idempotency_key",
    )[0]


def day_bucket(now: float) -> tuple[float, float]:
    """Return a stable ``[day_start, day_end)`` window covering *now*."""
    day_start = now - (now % 86400.0)
    return day_start, day_start + 86400.0


def _row_to_receipt(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return safe_event_payload({
        "idempotency_key": row["idempotency_key"],
        "status": row["status"],
        "reason": row["reason"],
        "target_unit": row["target_unit"],
        "repo_id": row["repo_id"],
        "dry_run": bool(row["dry_run"]),
        "fake": bool(row["fake"]),
        "source_ref": row["source_ref"],
        "policy_ref": row["policy_ref"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    })


def get_cycle(db_path: str, idempotency_key: str) -> dict[str, Any] | None:
    with _connect(db_path) as con:
        row = con.execute(
            "select * from maintenance_cycles where idempotency_key=?", (idempotency_key,)
        ).fetchone()
        return _row_to_receipt(row)


def claim_cycle(
    db_path: str,
    *,
    idempotency_key: str,
    target_unit: str,
    repo_id: str,
    reason: str,
    dry_run: bool,
    fake: bool,
    source_ref: str,
    policy_ref: str,
    now: float,
) -> tuple[bool, dict[str, Any]]:
    """Atomically claim one cycle attempt. Returns ``(claimed, row)``.

    ``claimed=False`` means a row for this key already existed (any status —
    ``attempt``, ``applied``, ``failed``, ``degraded``, or ``refused``); the
    prior row is returned and the caller must not perform a second action.
    """
    safe_ref, _ = safe_durable_ref(source_ref, field="source_ref")
    with _connect(db_path) as con:
        try:
            con.execute(
                """
                insert into maintenance_cycles(
                    idempotency_key, status, reason, target_unit, repo_id,
                    dry_run, fake, source_ref, policy_ref, created_at, updated_at
                ) values(?, 'attempt', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key, short_text(reason), short_text(target_unit),
                    short_text(repo_id), int(bool(dry_run)), int(bool(fake)),
                    safe_ref, short_text(policy_ref), now, now,
                ),
            )
        except sqlite3.IntegrityError:
            row = con.execute(
                "select * from maintenance_cycles where idempotency_key=?", (idempotency_key,)
            ).fetchone()
            return False, _row_to_receipt(row)
        row = con.execute(
            "select * from maintenance_cycles where idempotency_key=?", (idempotency_key,)
        ).fetchone()
        return True, _row_to_receipt(row)


def update_cycle_status(db_path: str, idempotency_key: str, *, status: str, reason: str) -> None:
    now = time.time()
    with _connect(db_path) as con:
        con.execute(
            "update maintenance_cycles set status=?, reason=?, updated_at=? where idempotency_key=?",
            (short_text(status, max_len=32), short_text(reason), now, idempotency_key),
        )


def count_cycles_today(db_path: str, *, repo_id: str, target_unit: str, day_start: float, day_end: float) -> int:
    with _connect(db_path) as con:
        row = con.execute(
            """
            select count(*) as n from maintenance_cycles
            where repo_id=? and target_unit=? and status='applied'
              and created_at >= ? and created_at < ?
            """,
            (short_text(repo_id), short_text(target_unit), day_start, day_end),
        ).fetchone()
    return int(row["n"]) if row else 0


def last_applied_at(db_path: str, *, repo_id: str, target_unit: str) -> float | None:
    with _connect(db_path) as con:
        row = con.execute(
            """
            select max(created_at) as ts from maintenance_cycles
            where repo_id=? and target_unit=? and status='applied'
            """,
            (short_text(repo_id), short_text(target_unit)),
        ).fetchone()
    ts = row["ts"] if row else None
    return float(ts) if ts is not None else None


def set_degraded(db_path: str, value: bool) -> None:
    now = time.time()
    with _connect(db_path) as con:
        con.execute(
            """
            insert into maintenance_state(key, value, updated_at) values('degraded', ?, ?)
            on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at
            """,
            ("1" if value else "0", now),
        )


def is_degraded(db_path: str) -> bool:
    with _connect(db_path) as con:
        row = con.execute("select value from maintenance_state where key='degraded'").fetchone()
    return bool(row) and row["value"] == "1"


def write_deadletter(db_path: str, *, reason: str, target_unit: str, idempotency_key: str, ref: str) -> None:
    now = time.time()
    safe_ref, _ = safe_durable_ref(ref, field="ref")
    with _connect(db_path) as con:
        con.execute(
            """
            insert into maintenance_deadletter(reason, target_unit, idempotency_key, ref, created_at)
            values(?, ?, ?, ?, ?)
            """,
            (short_text(reason), short_text(target_unit), short_text(idempotency_key), safe_ref, now),
        )
