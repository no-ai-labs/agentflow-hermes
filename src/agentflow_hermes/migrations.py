from __future__ import annotations

SCHEMA_VERSION = 4

SQL_V1 = """
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

SQL_V2 = """
-- additive columns on jobs
alter table jobs add column correlation_id text not null default '';
alter table jobs add column causation_id text not null default '';
alter table jobs add column source_kind text not null default 'manual';
alter table jobs add column source_id text not null default '';
alter table jobs add column source_ref text not null default '';
alter table jobs add column source_hash text not null default '';
alter table jobs add column attempt integer not null default 0;
alter table jobs add column final_at real null;

-- additive columns on job_events
alter table job_events add column seq integer not null default 0;
alter table job_events add column prev_status text not null default '';
alter table job_events add column new_status text not null default '';

-- Backfill stable per-job sequence numbers for existing M0 events before the
-- unique ledger guard is created.
with ranked as (
    select id, row_number() over (partition by job_id order by id) as rn
    from job_events
)
update job_events
set seq = (select rn from ranked where ranked.id = job_events.id)
where seq = 0;

-- new deadletter table
-- Stores refs/hashes/metadata only; no raw private transcripts or secrets.
create table if not exists deadletter (
    id integer primary key autoincrement,
    job_id text not null default '',
    reason text not null,
    raw_ref text not null default '',
    payload_json text not null default '{}',
    created_at real not null
);

-- indexes
-- Note: SQLite does not allow adding a constraint via ALTER TABLE, so we guard
-- duplicates in application code and rely on these indexes for lookups.
create index if not exists idx_jobs_correlation on jobs(correlation_id);
create index if not exists idx_jobs_source on jobs(source_kind, source_hash);
create index if not exists idx_jobs_dedupe on jobs(dedupe_key) where dedupe_key != '';
create index if not exists idx_deadletter_job on deadletter(job_id, id);
create unique index if not exists uniq_events_job_seq on job_events(job_id, seq);
create unique index if not exists uniq_jobs_source_hash on jobs(source_hash) where source_hash != '';
"""

SQL_V3 = """
-- operator receipt ledger: audit of proposed/refused/applied operator actions
-- No message bodies, transcripts, or secrets are stored here.
create table if not exists operator_receipts (
    id integer primary key autoincrement,
    job_id text not null default '',
    channel text not null,
    phase text not null,
    target text not null default '',
    idempotency_key text not null default '',
    policy_snapshot_json text not null default '{}',
    delivery_ref text not null default '',
    reason text not null default '',
    created_at real not null
);

-- idempotency guard for live sends
-- UNIQUE collision => already delivered, no second gateway call.
create table if not exists idempotency_keys (
    key text primary key,
    job_id text not null default '',
    channel text not null default '',
    target text not null default '',
    delivery_ref text not null default '',
    created_at real not null
);

-- job-level live delivery tracking
alter table jobs add column live_delivered_at real null;
alter table jobs add column live_delivery_ref text not null default '';

-- tiny key-value store for circuit breaker / degraded state
create table if not exists agentflow_meta (
    key text primary key,
    value text not null default '',
    updated_at real not null
);

-- indexes
create index if not exists idx_receipts_job on operator_receipts(job_id, id);
create index if not exists idx_receipts_channel on operator_receipts(channel, created_at);
create index if not exists idx_receipts_idempotency on operator_receipts(idempotency_key);
create index if not exists idx_idempotency_keys on idempotency_keys(key);
"""

SQL_V4 = """
-- M13 durable maintenance-cycle receipts / idempotency claims / failure path.
-- Refs, reasons, and short sanitized identifiers only; never raw transcripts,
-- private paths, or secrets.
create table if not exists maintenance_cycles (
    id integer primary key autoincrement,
    idempotency_key text not null,
    status text not null default 'attempt',
    reason text not null default '',
    target_unit text not null default '',
    repo_id text not null default '',
    dry_run integer not null default 1,
    fake integer not null default 0,
    source_ref text not null default '',
    policy_ref text not null default '',
    created_at real not null,
    updated_at real not null
);

-- tiny key-value store for maintenance degraded / circuit-breaker state
create table if not exists maintenance_state (
    key text primary key,
    value text not null default '',
    updated_at real not null
);

-- refs-only deadletter/journal fallback for maintenance failure paths
create table if not exists maintenance_deadletter (
    id integer primary key autoincrement,
    reason text not null default '',
    target_unit text not null default '',
    idempotency_key text not null default '',
    ref text not null default '',
    created_at real not null
);

create unique index if not exists uniq_maintenance_cycles_key on maintenance_cycles(idempotency_key);
create index if not exists idx_maintenance_cycles_repo_unit on maintenance_cycles(repo_id, target_unit, status, created_at);
create index if not exists idx_maintenance_deadletter_target on maintenance_deadletter(target_unit, created_at);
"""

STEPS = [(1, SQL_V1), (2, SQL_V2), (3, SQL_V3), (4, SQL_V4)]


def migrate(con) -> int:
    """Apply pending migrations in order and return final schema version."""
    version = con.execute("pragma user_version").fetchone()[0]
    for target, sql in STEPS:
        if version < target:
            con.executescript(sql)
            con.execute(f"pragma user_version = {target}")
            version = target
    return version
