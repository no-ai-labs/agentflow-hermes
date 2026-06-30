from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .live.gateway import DeliveryResult, FakeGateway, GatewayUnavailable, HermesGateway
from .live.policy import LivePolicy, load_policy
from .live.sanitize import policy_snapshot, safe_body_for_delivery, safe_durable_ref, safe_event_payload, safe_job_field
from .live.throttle import check_throttle, consecutive_failures, record_failure, set_degraded
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

        safe_title, _ = safe_job_field(title, field="title", max_len=240)
        safe_body, _ = safe_job_field(body, field="body", max_len=4000)
        safe_target, _ = safe_job_field(target, field="target", max_len=240)
        safe_origin_return, _ = safe_job_field(origin_return, field="origin_return", max_len=240)
        safe_dedupe_key, _ = safe_job_field(dedupe_key, field="dedupe_key", max_len=240)
        safe_correlation_id, _ = safe_job_field(correlation_id, field="correlation_id", max_len=240)
        safe_causation_id, _ = safe_job_field(causation_id, field="causation_id", max_len=240)
        safe_source_kind, _ = safe_job_field(source_kind, field="source_kind", max_len=120)
        safe_source_id, _ = safe_job_field(source_id, field="source_id", max_len=240)
        safe_source_ref, source_ref_redacted = safe_durable_ref(source_ref, field="source_ref", source_hash=source_hash)
        safe_source_hash, _ = safe_job_field(source_hash, field="source_hash", max_len=128)

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
                    safe_title,
                    safe_body,
                    safe_target,
                    safe_origin_return,
                    safe_dedupe_key,
                    JobStatus.QUEUED.value,
                    now,
                    now,
                    safe_correlation_id,
                    safe_causation_id,
                    safe_source_kind,
                    safe_source_id,
                    safe_source_ref,
                    safe_source_hash,
                    0,
                ),
            )
            self.record_event(
                job_id,
                "enqueued",
                con=con,
                payload=safe_event_payload({
                    "title": safe_title,
                    "body": safe_body,
                    "target": safe_target,
                    "origin_return": safe_origin_return,
                    "correlation_id": safe_correlation_id,
                    "causation_id": safe_causation_id,
                    "source_kind": safe_source_kind,
                    "source_id": safe_source_id,
                    "source_ref": safe_source_ref,
                    "source_hash": safe_source_hash,
                    "dedupe_key": safe_dedupe_key,
                    "source_ref_redacted": source_ref_redacted,
                }),
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
        payload = safe_event_payload(dict(payload or {}))
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

    def record_receipt(
        self,
        *,
        job_id: str = "",
        channel: str,
        phase: str,
        target: str = "",
        idempotency_key: str = "",
        policy: LivePolicy | None = None,
        delivery_ref: str = "",
        reason: str = "",
        con: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Write an operator receipt inside the current or a new transaction."""
        snapshot = policy_snapshot(policy) if policy else "{}"
        now = time.time()

        def _run(c: sqlite3.Connection) -> dict[str, Any]:
            c.execute(
                """
                insert into operator_receipts(
                    job_id, channel, phase, target, idempotency_key,
                    policy_snapshot_json, delivery_ref, reason, created_at
                ) values(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, channel, phase, target, idempotency_key, snapshot, delivery_ref, reason, now),
            )
            return {
                "success": True,
                "job_id": job_id,
                "channel": channel,
                "phase": phase,
                "target": target,
                "reason": reason,
                "created_at": now,
            }

        if con is not None:
            return _run(con)
        self.init()
        with self.connect() as con:
            return _run(con)

    def _make_idempotency_key(self, *, channel: str, job_id: str, target: str, correlation_id: str) -> str:
        payload = f"{channel}:{job_id}:{target}:{correlation_id}".encode("utf-8")
        return sha256(payload).hexdigest()[:24]

    def dispatch_live(
        self,
        job_id: str,
        *,
        gateway: HermesGateway | None = None,
        policy: LivePolicy | None = None,
        live: bool = False,
    ) -> dict[str, Any]:
        """Gated live dispatch. Fail-closed, canary-only, receipt-first.

        Requires both a policy/config enablement and per-call ``live=True``.
        The gateway is injected; if ``None``, it is resolved from the plugin
        context (which is not available in tests/CLI, so those callers must
        supply one).
        """
        self.init()
        now = time.time()
        policy = policy if policy is not None else load_policy()

        with self.connect() as con:
            row = con.execute("select * from jobs where id=?", (job_id,)).fetchone()
            if row is None:
                return {"success": False, "error": "unknown_job", "job_id": job_id, "mode": "dry-run"}
            job = dict(row)
            target = job.get("target") or ""
            correlation_id = job.get("correlation_id") or job_id

            idempotency_key = self._make_idempotency_key(
                channel="live_dispatch", job_id=job_id, target=target, correlation_id=correlation_id
            )

            # If no live opt-in, behave exactly like dispatch_dry_run.
            if not live:
                result = self.dispatch_dry_run(job_id)
                result["mode"] = "dry-run"
                return result

            # Kill switch is evaluated first, before any receipt.
            if policy.kill_switch:
                self.record_receipt(
                    job_id=job_id,
                    channel="live_dispatch",
                    phase="refused",
                    target=target,
                    idempotency_key=idempotency_key,
                    policy=policy,
                    reason="kill_switch",
                    con=con,
                )
                return {"success": False, "error": "kill_switch", "job_id": job_id, "mode": "dry-run"}

            # Gate check.
            if not policy.live_dispatch_enabled:
                self.record_receipt(
                    job_id=job_id,
                    channel="live_dispatch",
                    phase="refused",
                    target=target,
                    idempotency_key=idempotency_key,
                    policy=policy,
                    reason="live_dispatch_disabled",
                    con=con,
                )
                return {"success": False, "error": "live_dispatch_disabled", "job_id": job_id, "mode": "dry-run"}

            # M6 canary-only: target must be in both allowed and canary lists.
            if target not in policy.allowed_targets or target not in policy.canary_targets:
                self.record_receipt(
                    job_id=job_id,
                    channel="live_dispatch",
                    phase="refused",
                    target=target,
                    idempotency_key=idempotency_key,
                    policy=policy,
                    reason="target_not_allowed",
                    con=con,
                )
                return {"success": False, "error": "target_not_allowed", "job_id": job_id, "mode": "dry-run"}

            # Idempotency / duplicate check.
            existing_key = con.execute(
                "select delivery_ref from idempotency_keys where key=?", (idempotency_key,)
            ).fetchone()
            if existing_key is not None:
                self.record_receipt(
                    job_id=job_id,
                    channel="live_dispatch",
                    phase="refused",
                    target=target,
                    idempotency_key=idempotency_key,
                    policy=policy,
                    delivery_ref=existing_key["delivery_ref"],
                    reason="duplicate",
                    con=con,
                )
                return {
                    "success": True,
                    "applied": False,
                    "duplicate": True,
                    "job_id": job_id,
                    "delivery_ref": existing_key["delivery_ref"],
                    "mode": "dry-run",
                }

            # Job-level guard.
            if job.get("live_delivered_at") is not None or job.get("live_delivery_ref"):
                self.record_receipt(
                    job_id=job_id,
                    channel="live_dispatch",
                    phase="refused",
                    target=target,
                    idempotency_key=idempotency_key,
                    policy=policy,
                    reason="duplicate",
                    con=con,
                )
                return {
                    "success": True,
                    "applied": False,
                    "duplicate": True,
                    "job_id": job_id,
                    "mode": "dry-run",
                }

            # Throttle / circuit breaker.
            allowed, throttle_reason, _state = check_throttle(con, policy, target, now=now)
            if not allowed:
                self.record_receipt(
                    job_id=job_id,
                    channel="live_dispatch",
                    phase="refused",
                    target=target,
                    idempotency_key=idempotency_key,
                    policy=policy,
                    reason=throttle_reason,
                    con=con,
                )
                return {"success": False, "error": throttle_reason, "job_id": job_id, "mode": "dry-run"}

            # Gateway resolution.
            try:
                if gateway is None:
                    # CLI/test path: no injected gateway means unavailable.
                    raise GatewayUnavailable("no gateway injected")
            except GatewayUnavailable as exc:
                self.record_receipt(
                    job_id=job_id,
                    channel="live_dispatch",
                    phase="refused",
                    target=target,
                    idempotency_key=idempotency_key,
                    policy=policy,
                    reason="gateway_unavailable",
                    con=con,
                )
                return {"success": False, "error": "gateway_unavailable", "detail": str(exc), "job_id": job_id, "mode": "dry-run"}

            # Write attempt receipt before external effect.
            self.record_receipt(
                job_id=job_id,
                channel="live_dispatch",
                phase="attempt",
                target=target,
                idempotency_key=idempotency_key,
                policy=policy,
                con=con,
            )

            body = safe_body_for_delivery(
                job.get("body") or "",
                job_id=job_id,
                source_ref=job.get("source_ref") or "",
                source_hash=job.get("source_hash") or "",
            )

            try:
                delivery = gateway.send_message(target=target, body=body, idempotency_key=idempotency_key)
            except Exception as exc:
                record_failure(con, target=target, now=now)
                if consecutive_failures(con, now=now) >= 3:
                    set_degraded(con, True)
                self.record_receipt(
                    job_id=job_id,
                    channel="live_dispatch",
                    phase="failed",
                    target=target,
                    idempotency_key=idempotency_key,
                    policy=policy,
                    reason="gateway_failure",
                    con=con,
                )
                return {"success": False, "error": "gateway_failure", "detail": str(exc), "job_id": job_id, "mode": "dry-run"}

            if not delivery.success:
                record_failure(con, target=target, now=now)
                if consecutive_failures(con, now=now) >= 3:
                    set_degraded(con, True)
                self.record_receipt(
                    job_id=job_id,
                    channel="live_dispatch",
                    phase="failed",
                    target=target,
                    idempotency_key=idempotency_key,
                    policy=policy,
                    delivery_ref=delivery.receipt_ref,
                    reason="delivery_failed",
                    con=con,
                )
                return {"success": False, "error": "delivery_failed", "delivery_ref": delivery.receipt_ref, "job_id": job_id, "mode": "dry-run"}

            # Success path: claim idempotency, update job, write applied receipt, emit passive_delivery event.
            con.execute(
                "insert into idempotency_keys(key, job_id, channel, target, delivery_ref, created_at) values(?, ?, ?, ?, ?, ?)",
                (idempotency_key, job_id, "live_dispatch", target, delivery.receipt_ref, now),
            )
            con.execute(
                "update jobs set status=?, updated_at=?, live_delivered_at=?, live_delivery_ref=? where id=?",
                (JobStatus.DISPATCHED.value, now, now, delivery.receipt_ref, job_id),
            )
            self.record_receipt(
                job_id=job_id,
                channel="live_dispatch",
                phase="applied",
                target=target,
                idempotency_key=idempotency_key,
                policy=policy,
                delivery_ref=delivery.receipt_ref,
                reason="delivered",
                con=con,
            )
            self.record_event(
                job_id,
                "passive_delivery",
                payload={
                    "channel": "live_dispatch",
                    "delivery_ref": delivery.receipt_ref,
                    "target": target,
                    "mode": "live",
                },
                con=con,
                prev_status=job.get("status") or JobStatus.QUEUED.value,
                new_status=JobStatus.DISPATCHED.value,
            )
            return {
                "success": True,
                "applied": True,
                "job_id": job_id,
                "delivery_ref": delivery.receipt_ref,
                "mode": "live",
            }


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
