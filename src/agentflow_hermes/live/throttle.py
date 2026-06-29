from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from .policy import LivePolicy


@dataclass(frozen=True)
class ThrottleState:
    minute_bucket: str
    hour_bucket: str
    sends_this_minute: int
    sends_this_target_hour: int
    degraded: bool


def check_throttle(
    con: sqlite3.Connection,
    policy: LivePolicy,
    target: str,
    *,
    now: float | None = None,
    force_degraded: bool = False,
) -> tuple[bool, str, ThrottleState]:
    """Return (allowed, reason, state). Persists nothing on its own."""
    now = now if now is not None else time.time()
    minute_bucket = _bucket(now, 60)
    hour_bucket = _bucket(now, 3600)

    state = ThrottleState(
        minute_bucket=minute_bucket,
        hour_bucket=hour_bucket,
        sends_this_minute=_count_receipts(con, "attempt", bucket=minute_bucket, width=60),
        sends_this_target_hour=_count_target_receipts(con, target, bucket=hour_bucket, width=3600),
        degraded=force_degraded or _is_degraded(con),
    )

    if state.degraded:
        return False, "circuit_breaker_degraded", state
    if state.sends_this_minute >= policy.max_sends_per_min:
        return False, "throttled_per_minute", state
    if state.sends_this_target_hour >= policy.max_sends_per_target_per_hour:
        return False, "throttled_per_target_hour", state
    return True, "", state


def record_attempt(con: sqlite3.Connection, *, channel: str, target: str, bucket: str, now: float) -> None:
    con.execute(
        "insert into operator_receipts(job_id, channel, phase, target, idempotency_key, policy_snapshot_json, delivery_ref, reason, created_at) values(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("", channel, "attempt", target, "", "{}", "", "", now),
    )


def record_failure(con: sqlite3.Connection, *, target: str, now: float) -> None:
    con.execute(
        "insert into operator_receipts(job_id, channel, phase, target, idempotency_key, policy_snapshot_json, delivery_ref, reason, created_at) values(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("", "live_dispatch", "failed", target, "", "{}", "", "gateway_failure", now),
    )


def consecutive_failures(con: sqlite3.Connection, *, window_minutes: int = 5, now: float | None = None) -> int:
    now = now if now is not None else time.time()
    cutoff = now - window_minutes * 60
    total_attempts = con.execute(
        "select count(*) from operator_receipts where channel='live_dispatch' and phase='attempt' and created_at >= ?",
        (cutoff,),
    ).fetchone()[0]
    total_applied = con.execute(
        "select count(*) from operator_receipts where channel='live_dispatch' and phase='applied' and created_at >= ?",
        (cutoff,),
    ).fetchone()[0]
    return max(0, total_attempts - total_applied)


def _count_receipts(con: sqlite3.Connection, phase: str, bucket: str, *, width: int) -> int:
    # bucket is a minute-granularity string; use created_at directly for simplicity.
    minute_start = _bucket_to_timestamp(bucket, width)
    row = con.execute(
        "select count(*) from operator_receipts where phase=? and created_at >= ? and created_at < ?",
        (phase, minute_start, minute_start + 60),
    ).fetchone()
    return int(row[0]) if row else 0


def _count_target_receipts(con: sqlite3.Connection, target: str, bucket: str, *, width: int) -> int:
    hour_start = _bucket_to_timestamp(bucket, width)
    row = con.execute(
        "select count(*) from operator_receipts where phase='attempt' and target=? and created_at >= ? and created_at < ?",
        (target, hour_start, hour_start + 3600),
    ).fetchone()
    return int(row[0]) if row else 0


def _is_degraded(con: sqlite3.Connection) -> bool:
    row = con.execute("select value from agentflow_meta where key='degraded'").fetchone()
    return row is not None and row[0] == "1"


def set_degraded(con: sqlite3.Connection, degraded: bool) -> None:
    con.execute(
        "insert into agentflow_meta(key, value, updated_at) values('degraded', ?, ?) on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at",
        ("1" if degraded else "0", time.time()),
    )


def _bucket(now: float, width: int) -> str:
    return str(int(now) // width)


def _bucket_to_timestamp(bucket: str, width: int) -> float:
    return float(bucket) * width
