from __future__ import annotations

SCHEMA_VERSION = 2

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

STEPS = [(1, SQL_V1), (2, SQL_V2)]


def migrate(con) -> int:
    """Apply pending migrations in order and return final schema version."""
    version = con.execute("pragma user_version").fetchone()[0]
    for target, sql in STEPS:
        if version < target:
            con.executescript(sql)
            con.execute(f"pragma user_version = {target}")
            version = target
    return version
