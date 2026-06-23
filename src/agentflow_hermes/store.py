from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def init(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                create table if not exists jobs (
                    id text primary key,
                    title text not null,
                    body text not null default '',
                    target text not null default '',
                    origin_return text not null default '',
                    dedupe_key text not null default '',
                    status text not null default 'queued',
                    created_at real not null,
                    updated_at real not null
                );
                create table if not exists job_events (
                    id integer primary key autoincrement,
                    job_id text not null,
                    kind text not null,
                    payload_json text not null default '{}',
                    created_at real not null,
                    foreign key(job_id) references jobs(id)
                );
                create index if not exists idx_jobs_status_updated on jobs(status, updated_at);
                create index if not exists idx_events_job_id on job_events(job_id, id);
                """
            )

    def enqueue(self, *, title: str, body: str = "", target: str = "", origin_return: str = "", dedupe_key: str = "") -> dict[str, Any]:
        self.init()
        now = time.time()
        job_id = f"job_{int(now * 1000):x}"
        with self.connect() as con:
            con.execute(
                "insert into jobs(id,title,body,target,origin_return,dedupe_key,status,created_at,updated_at) values(?,?,?,?,?,?,?,?,?)",
                (job_id, title, body, target, origin_return, dedupe_key, "queued", now, now),
            )
            con.execute(
                "insert into job_events(job_id,kind,payload_json,created_at) values(?,?,?,?)",
                (job_id, "enqueued", json.dumps({"target": target, "origin_return": origin_return}, ensure_ascii=False), now),
            )
        return {"success": True, "job_id": job_id, "status": "queued"}

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

    def ack(self, *, job_id: str, status: str, summary: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.init()
        payload = dict(payload or {})
        payload.update({"status": status, "summary": summary})
        now = time.time()
        with self.connect() as con:
            cur = con.execute("update jobs set status=?, updated_at=? where id=?", (status, now, job_id))
            if cur.rowcount == 0:
                return {"success": False, "error": f"unknown job_id: {job_id}"}
            con.execute(
                "insert into job_events(job_id,kind,payload_json,created_at) values(?,?,?,?)",
                (job_id, "ack", json.dumps(payload, ensure_ascii=False), now),
            )
        return {"success": True, "job_id": job_id, "status": status}


def render_dispatch_prompt(job: dict[str, Any]) -> str:
    return f"""You are working an AgentFlow job. Return an explicit [JOB ACK] block when done.\n\n[JOB]\njob_id: {job['id']}\ntarget: {job.get('target') or ''}\norigin_return: {job.get('origin_return') or ''}\ntitle: {job.get('title') or ''}\n\n{job.get('body') or ''}\n\n[JOB ACK FORMAT]\n[JOB ACK]\njob_id: {job['id']}\nstatus: succeeded|failed|waiting_review|waiting_user\nsummary: <short result>\nartifacts:\n- <files/links/tests>\nblockers: <none or exact blocker>\n"""
