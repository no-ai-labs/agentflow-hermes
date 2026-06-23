from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .migrations import migrate
from .states import ALLOWED_TRANSITIONS, FINAL_STATES, JobStatus, normalize_status


def default_home() -> Path:
    return Path(os.environ.get("AGENTFLOW_HOME") or Path.home() / ".agentflow")


def default_db_path() -> Path:
    return default_home() / "agentflow.db"


@dataclass(frozen=True)
class AgentFlowStore:
    path: Path

    @classmethod
    def default(cls) -> "AgentFlowStore":
        return cls(default_db_path())

    def connect(self, *, timeout: float = 30.0) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=timeout)
        con.row_factory = sqlite3.Row
        return con

    def init(self) -> None:
        with self.connect() as con:
            migrate(con)

    def _schema_version(self, con: sqlite3.Connection) -> int:
        return con.execute("pragma user_version").fetchone()[0]

    def enqueue(
        self,
        *,
        title: str,
        body: str = "",
        target: str = "",
        origin_return: str = "",
        dedupe_key: str = "",
        correlation_id: str = "",
        causation_id: str = "",
        source_kind: str = "manual",
        source_id: str = "",
        source_ref: str = "",
        source_hash: str = "",
    ) -> dict[str, Any]:
        self.init()
        now = time.time()
        job_id = f"job_{int(now * 1000):x}"
        correlation_id = correlation_id or job_id
        with self.connect() as con:
            con.execute(
                """
                insert into jobs(
                    id, title, body, target, origin_return, dedupe_key,
                    status, created_at, updated_at,
                    correlation_id, causation_id, source_kind, source_id, source_ref, source_hash, attempt
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    title,
                    body,
                    target,
                    origin_return,
                    dedupe_key,
                    JobStatus.QUEUED.value,
                    now,
                    now,
                    correlation_id,
                    causation_id,
                    source_kind,
                    source_id,
                    source_ref,
                    source_hash,
                    0,
                ),
            )
            self.record_event(
                job_id,
                "enqueued",
                con=con,
                payload={
                    "target": target,
                    "origin_return": origin_return,
                    "correlation_id": correlation_id,
                    "causation_id": causation_id,
                    "source_kind": source_kind,
                    "source_id": source_id,
                    "source_ref": source_ref,
                    "source_hash": source_hash,
                    "dedupe_key": dedupe_key,
                },
            )
        return {"success": True, "job_id": job_id, "status": JobStatus.QUEUED.value}

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as con:
            rows = con.execute(
                "select * from jobs order by updated_at desc limit ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        self.init()
        with self.connect() as con:
            row = con.execute("select * from jobs where id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def get_job_by_source_hash(self, source_hash: str) -> dict[str, Any] | None:
        self.init()
        with self.connect() as con:
            row = con.execute(
                "select * from jobs where source_hash=? limit 1", (source_hash,)
            ).fetchone()
        return dict(row) if row else None

    def record_event(
        self,
        job_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        con: sqlite3.Connection | None = None,
        prev_status: str = "",
        new_status: str = "",
    ) -> dict[str, Any]:
        """Append a metadata-only event to the ledger.

        If *con* is provided the event is written inside the existing
        transaction; otherwise a new connection/transaction is used.
        """
        now = time.time()
        payload = dict(payload or {})
        payload["created_at"] = now

        def _run(c: sqlite3.Connection) -> dict[str, Any]:
            seq_row = c.execute(
                "select coalesce(max(seq), 0) from job_events where job_id=?",
                (job_id,),
            ).fetchone()
            seq = int(seq_row[0]) + 1
            c.execute(
                """
                insert into job_events(
                    job_id, kind, payload_json, created_at, seq, prev_status, new_status
                ) values(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    seq,
                    prev_status,
                    new_status,
                ),
            )
            return {"success": True, "job_id": job_id, "kind": kind, "seq": seq}

        if con is not None:
            return _run(con)

        self.init()
        with self.connect() as con:
            return _run(con)

    def deadletter(
        self,
        *,
        reason: str,
        job_id: str = "",
        raw_ref: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a deadletter row containing refs/hashes/metadata only."""
        self.init()
        now = time.time()
        with self.connect() as con:
            con.execute(
                "insert into deadletter(job_id, reason, raw_ref, payload_json, created_at) values(?, ?, ?, ?, ?)",
                (
                    job_id,
                    reason,
                    raw_ref,
                    json.dumps(dict(payload or {}), ensure_ascii=False),
                    now,
                ),
            )
        return {"success": True, "reason": reason, "job_id": job_id}

    def ack(
        self,
        *,
        job_id: str,
        status: str | JobStatus,
        summary: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply a transition-guarded ACK for *job_id*.

        Returns a dict describing the outcome. The store handles duplicate,
        final-state, and illegal-transition cases; callers decide CLI exit
        codes.
        """
        self.init()
        try:
            new_status = normalize_status(status.value if isinstance(status, JobStatus) else status)
        except ValueError:
            self.deadletter(reason="invalid_status", job_id=job_id, payload={"status": str(status), "summary": summary})
            return {"success": False, "error": "invalid_status", "reason": "invalid_status", "job_id": job_id}
        now = time.time()

        with self.connect() as con:
            row = con.execute(
                "select id, status, source_hash from jobs where id=?", (job_id,)
            ).fetchone()
            if row is None:
                meta = {
                    "status": new_status.value,
                    "summary": summary,
                    "source_hash": "",
                }
                self.record_event("", "ack_rejected", payload=meta, con=con)
                con.execute(
                    "insert into deadletter(job_id, reason, raw_ref, payload_json, created_at) values(?, ?, ?, ?, ?)",
                    (job_id, "unknown_job", "", json.dumps(meta, ensure_ascii=False), now),
                )
                return {"success": False, "error": "unknown_job", "job_id": job_id}

            current = normalize_status(row["status"])

            # Duplicate / idempotent ACK for the current status.
            if new_status == current:
                meta = {"status": new_status.value, "summary": summary}
                self.record_event(
                    job_id,
                    "duplicate_ack",
                    payload=meta,
                    con=con,
                    prev_status=current.value,
                    new_status=new_status.value,
                )
                return {
                    "success": True,
                    "applied": False,
                    "duplicate": True,
                    "job_id": job_id,
                    "status": new_status.value,
                }

            # Final-state guard.
            if current in FINAL_STATES:
                meta = {"status": new_status.value, "summary": summary}
                self.record_event(
                    job_id,
                    "ack_rejected",
                    payload=meta,
                    con=con,
                    prev_status=current.value,
                    new_status=new_status.value,
                )
                con.execute(
                    "insert into deadletter(job_id, reason, raw_ref, payload_json, created_at) values(?, ?, ?, ?, ?)",
                    (job_id, "already_final", row["source_hash"], json.dumps(meta, ensure_ascii=False), now),
                )
                return {
                    "success": False,
                    "error": "already_final",
                    "reason": "already_final",
                    "job_id": job_id,
                }

            # Illegal transition guard.
            if new_status not in ALLOWED_TRANSITIONS.get(current, set()):
                meta = {
                    "status": new_status.value,
                    "summary": summary,
                    "current": current.value,
                }
                self.record_event(
                    job_id,
                    "ack_rejected",
                    payload=meta,
                    con=con,
                    prev_status=current.value,
                    new_status=new_status.value,
                )
                con.execute(
                    "insert into deadletter(job_id, reason, raw_ref, payload_json, created_at) values(?, ?, ?, ?, ?)",
                    (job_id, "illegal_transition", row["source_hash"], json.dumps(meta, ensure_ascii=False), now),
                )
                return {
                    "success": False,
                    "error": "illegal_transition",
                    "reason": "illegal_transition",
                    "job_id": job_id,
                }

            # Legal transition.
            final_at = now if new_status in FINAL_STATES else None
            con.execute(
                "update jobs set status=?, updated_at=?, final_at=? where id=?",
                (new_status.value, now, final_at, job_id),
            )
            event_payload = dict(payload or {})
            event_payload.update({"summary": summary})
            self.record_event(
                job_id,
                "ack_applied",
                payload=event_payload,
                con=con,
                prev_status=current.value,
                new_status=new_status.value,
            )
            return {
                "success": True,
                "applied": True,
                "duplicate": False,
                "job_id": job_id,
                "status": new_status.value,
            }

    def dispatch_dry_run(self, job_id: str) -> dict[str, Any]:
        """Record that a job was rendered for dry-run dispatch.

        This remains dry-run only: it mutates local lifecycle state/ledger but
        does not send to Hermes, Discord, webhooks, or active wake.
        """
        self.init()
        now = time.time()
        with self.connect() as con:
            row = con.execute("select id, status from jobs where id=?", (job_id,)).fetchone()
            if row is None:
                return {"success": False, "error": "unknown_job", "job_id": job_id}
            current = normalize_status(row["status"])
            if current == JobStatus.QUEUED:
                con.execute(
                    "update jobs set status=?, updated_at=? where id=?",
                    (JobStatus.DISPATCHED.value, now, job_id),
                )
                self.record_event(
                    job_id,
                    "dispatched_dry_run",
                    payload={"mode": "dry-run"},
                    con=con,
                    prev_status=current.value,
                    new_status=JobStatus.DISPATCHED.value,
                )
                return {"success": True, "applied": True, "job_id": job_id, "status": JobStatus.DISPATCHED.value}
            self.record_event(
                job_id,
                "dispatched_dry_run",
                payload={"mode": "dry-run", "current_status": current.value},
                con=con,
                prev_status=current.value,
                new_status=current.value,
            )
            return {"success": True, "applied": False, "job_id": job_id, "status": current.value}


def render_dispatch_prompt(job: dict[str, Any]) -> str:
    return f"""You are working an AgentFlow job. Return an explicit [JOB ACK] block when done.

[JOB]
job_id: {job['id']}
correlation_id: {job.get('correlation_id') or job['id']}
causation_id: {job.get('causation_id') or ''}
source_kind: {job.get('source_kind') or 'manual'}
source_id: {job.get('source_id') or ''}
source_ref: {job.get('source_ref') or ''}
source_hash: {job.get('source_hash') or ''}
target: {job.get('target') or ''}
origin_return: {job.get('origin_return') or ''}
title: {job.get('title') or ''}

{job.get('body') or ''}

[JOB ACK FORMAT]
[JOB ACK]
job_id: {job['id']}
status: succeeded|failed|waiting_review|waiting_user
summary: <short result>
artifacts:
- <files/links/tests>
blockers: <none or exact blocker>
"""
