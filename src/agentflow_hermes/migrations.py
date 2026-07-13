from __future__ import annotations

SCHEMA_VERSION = 5

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

SQL_V5 = """
-- Needs-Input Continuation Engine: durable continuation ledger. Refs, hashes,
-- and short sanitized identifiers only; never raw transcripts or secrets.
create table if not exists continuation_instances (
    id integer primary key autoincrement,
    board text not null,
    source_task_id text not null,
    source_event_id text not null,
    source_graph_id text not null default '',
    contract_ref text not null default '',
    verdict text not null default '',
    continuation_kind text not null default '',
    state text not null default 'detected',
    origin_ref text not null default '',
    return_to_ref text not null default '',
    workspace_ref text not null default '',
    idempotency_key text not null,
    created_at real not null,
    updated_at real not null
);

create table if not exists continuation_steps (
    id integer primary key autoincrement,
    continuation_id integer not null,
    step_kind text not null,
    state text not null default 'pending',
    board_task_id text not null default '',
    parent_step_id integer null,
    idempotency_key text not null,
    created_at real not null,
    updated_at real not null,
    foreign key(continuation_id) references continuation_instances(id)
);

create table if not exists owner_input_receipts (
    id integer primary key autoincrement,
    continuation_id integer not null,
    version integer not null,
    owner_ref text not null default '',
    fields_json text not null default '{}',
    source_ref text not null default '',
    created_at real not null,
    supersedes_receipt_id integer null,
    foreign key(continuation_id) references continuation_instances(id)
);

create table if not exists continuation_events (
    id integer primary key autoincrement,
    continuation_id integer not null,
    seq integer not null,
    kind text not null,
    payload_json text not null default '{}',
    created_at real not null,
    foreign key(continuation_id) references continuation_instances(id)
);

create table if not exists board_cursors (
    board text not null,
    db_identity text not null,
    last_event_id integer not null default 0,
    updated_at real not null,
    primary key(board, db_identity)
);

create table if not exists board_outbox (
    id integer primary key autoincrement,
    continuation_id integer not null,
    step_id text not null default '',
    operation text not null,
    payload_json text not null default '{}',
    idempotency_key text not null,
    state text not null default 'pending',
    board_task_id text not null default '',
    attempts integer not null default 0,
    created_at real not null,
    updated_at real not null,
    foreign key(continuation_id) references continuation_instances(id)
);

create unique index if not exists uniq_continuation_source_tuple
    on continuation_instances(board, source_task_id, source_event_id, contract_ref);
create index if not exists idx_continuation_state on continuation_instances(state, updated_at);
create unique index if not exists uniq_continuation_steps_key on continuation_steps(continuation_id, idempotency_key);
create index if not exists idx_continuation_steps_continuation on continuation_steps(continuation_id, step_kind);
create unique index if not exists uniq_owner_receipt_version on owner_input_receipts(continuation_id, version);
create unique index if not exists uniq_continuation_events_seq on continuation_events(continuation_id, seq);
create unique index if not exists uniq_board_outbox_key on board_outbox(idempotency_key);
create index if not exists idx_board_outbox_continuation on board_outbox(continuation_id, state);
"""

STEPS = [(1, SQL_V1), (2, SQL_V2), (3, SQL_V3), (4, SQL_V4), (5, SQL_V5)]

# Portable continuation-ledger tables introduced by SQL_V5. This migration
# chain is shared by both ``AgentFlowStore`` (jobs.db) and
# ``ContinuationStore`` (agentflow-control-plane.sqlite) — SCHEMA_VERSION cannot be bumped
# per-store, so control-plane store consolidation (plan section 10:
# ``agentflow-hermes continuation migrate-store``) copies exactly these
# tables row-by-row between physical DB files rather than adding a new
# migration step here. See ``continuation_store.migrate_legacy_store``.
CONTINUATION_LEDGER_TABLES: tuple[str, ...] = (
    "continuation_instances",
    "continuation_steps",
    "owner_input_receipts",
    "continuation_events",
    "board_cursors",
    "board_outbox",
)


def migrate(con) -> int:
    """Apply pending migrations in order and return final schema version."""
    version = con.execute("pragma user_version").fetchone()[0]
    for target, sql in STEPS:
        if version < target:
            con.executescript(sql)
            con.execute(f"pragma user_version = {target}")
            version = target
    return version
