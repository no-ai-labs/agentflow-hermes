# AgentFlow Hermes M1/M2 implementation design

Origin/return_to: Discord Devhub / #hermes-main
Kanban task: t_e2cd51af
Authoritative consultation route: `claude-openrouter-opus` via OpenRouter `anthropic/claude-opus-4.8`
Verdict: GO

## Current M0 skeleton verified

- `src/agentflow_hermes/store.py`
  - `AgentFlowStore.default/connect/init/enqueue/list_jobs/get_job/ack`
  - `render_dispatch_prompt`
  - Current schema is flat `jobs` + `job_events`, initialized through one inline `executescript`.
  - Current `ack()` blindly updates `jobs.status`; no enum, transition guard, duplicate handling, schema version, or deadletter path.
- `src/agentflow_hermes/cli.py`
  - Commands: `init`, `doctor`, `enqueue`, `status`, `dispatch-dry-run`, `ack ingest`.
  - `parse_ack_block()` extracts fields from `[JOB ACK]` but does not validate status or transitions.
- `tests/test_store.py`
  - Happy-path coverage for enqueue, dispatch prompt, ACK update, status JSON shape.
- `plugins/hermes-agentflow/plugin.yaml` and `plugins/hermes-agentflow/after-install.md`
  - Plugin install UX exists and should remain compatible.
- `README.md`
  - Documents dry-run-first AgentFlow add-on/plugin + sidecar CLI boundary.

## Non-negotiable product and safety boundaries

- AgentFlow remains a Hermes add-on/plugin plus sidecar CLI. Do not monkeypatch Hermes core.
- Dry-run only by default. Do not add live `send_message`, active wake, or live dispatch paths in M1/M2.
- Durable state must store refs, hashes, metadata, and summaries only. Do not store raw private transcripts, secrets, or absolute private payloads in job bodies/events/deadletter rows.
- Preserve existing plugin install UX and existing CLI commands/output shapes where possible; changes should be additive.

## M1 design: schema, migrations, source metadata, correlation IDs, ledger, final guards

### Files to create or edit

- Create `src/agentflow_hermes/migrations.py`
  - Own ordered schema migrations and SQLite `PRAGMA user_version` handling.
- Create `src/agentflow_hermes/states.py`
  - Own `JobStatus`, `FINAL_STATES`, and `ALLOWED_TRANSITIONS`.
- Edit `src/agentflow_hermes/store.py`
  - Replace inline one-shot schema with migration driver.
  - Extend `enqueue()` with source/correlation fields.
  - Add `record_event()`, `deadletter()`, and transition-guarded `ack()`.
  - Extend `render_dispatch_prompt()` with source provenance fields while keeping ACK format compatible.
- Edit `tests/test_store.py`, or split into new test modules:
  - `tests/test_migrations.py`
  - `tests/test_ack.py`
  - `tests/test_cron_bridge.py`

### Migration approach

Use SQLite `PRAGMA user_version` as the schema version source of truth.

Implementation shape:

- `SCHEMA_VERSION = 2`
- `migrations.py` exports ordered steps, for example `STEPS = [(1, SQL_V1), (2, SQL_V2)]`.
- `AgentFlowStore.init()` opens a transaction, reads `PRAGMA user_version`, applies any pending migration steps in order, and sets `PRAGMA user_version = <version>` after each successful step.
- Fresh databases apply v1 base schema then v2 additive schema.
- Existing M0/v1 databases upgrade via additive `ALTER TABLE ADD COLUMN`, new table creation, and new indexes. No destructive rewrites.

### Target schema additions

`jobs` existing columns remain:

- `id text primary key`
- `title text not null`
- `body text not null default ''`
- `target text not null default ''`
- `origin_return text not null default ''`
- `dedupe_key text not null default ''`
- `status text not null default 'queued'`
- `created_at real not null`
- `updated_at real not null`

Add columns:

- `correlation_id text not null default ''`
  - Set to explicit value when provided, otherwise default to job id after insert/update.
- `source_kind text not null default 'manual'`
  - Initial values: `manual`, `cron`, `discord` if later needed.
- `source_ref text not null default ''`
  - External reference to source artifact, not raw source content.
- `source_hash text not null default ''`
  - Hash of source content or marker payload for dedupe and provenance.
- `attempt integer not null default 0`
- `final_at real null`
  - Set once when entering a final state.

`job_events` existing columns remain:

- `id integer primary key autoincrement`
- `job_id text not null`
- `kind text not null`
- `payload_json text not null default '{}'`
- `created_at real not null`

Add columns:

- `seq integer not null default 0`
  - Per-job monotonic event sequence.
- `prev_status text not null default ''`
- `new_status text not null default ''`

Create `deadletter`:

- `id integer primary key autoincrement`
- `job_id text not null default ''`
- `reason text not null`
- `raw_ref text not null default ''`
  - Ref/hash only; do not store raw offending text.
- `payload_json text not null default '{}'`
  - Metadata only.
- `created_at real not null`

### Indexes and constraints

Keep:

- `idx_jobs_status_updated on jobs(status, updated_at)`
- `idx_events_job_id on job_events(job_id, id)`

Add:

- `idx_jobs_correlation on jobs(correlation_id)`
- `idx_jobs_source on jobs(source_kind, source_hash)`
- `idx_deadletter_job on deadletter(job_id, id)`
- `uniq_events_job_seq unique(job_id, seq)`
- `uniq_jobs_source_hash unique(source_hash) where source_hash != ''`
- Optional: `idx_jobs_dedupe on jobs(dedupe_key) where dedupe_key != ''`

SQLite partial indexes are acceptable. If compatibility becomes an issue, keep uniqueness in application logic plus a normal index, but prefer the DB hard guard.

### Event ledger rules

Add `AgentFlowStore.record_event(job_id, kind, payload=None, prev_status='', new_status='')`:

- Computes `seq = coalesce(max(seq), 0) + 1` for the job inside the same transaction as the mutation.
- Stores metadata-only JSON.
- For state transitions, records both `prev_status` and `new_status`.
- Event kinds to use in M1/M2:
  - `enqueued`
  - `dispatched_dry_run`
  - `ack_applied`
  - `duplicate_ack`
  - `ack_rejected`
  - `deadlettered`
  - `cron_ingested`
  - `cron_duplicate`
  - `cron_noise`

### State machine

Create `src/agentflow_hermes/states.py`:

```python
from enum import StrEnum

class JobStatus(StrEnum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    WAITING_REVIEW = "waiting_review"
    WAITING_USER = "waiting_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

FINAL_STATES = {JobStatus.SUCCEEDED, JobStatus.FAILED}

ALLOWED_TRANSITIONS = {
    JobStatus.QUEUED: {JobStatus.DISPATCHED, JobStatus.FAILED},
    JobStatus.DISPATCHED: {JobStatus.WAITING_REVIEW, JobStatus.WAITING_USER, JobStatus.SUCCEEDED, JobStatus.FAILED},
    JobStatus.WAITING_REVIEW: {JobStatus.DISPATCHED, JobStatus.SUCCEEDED, JobStatus.FAILED},
    JobStatus.WAITING_USER: {JobStatus.DISPATCHED, JobStatus.SUCCEEDED, JobStatus.FAILED},
    JobStatus.SUCCEEDED: set(),
    JobStatus.FAILED: set(),
}
```

Transition behavior:

- Same status as current:
  - Treat as duplicate/idempotent ACK.
  - No `jobs` update.
  - Record `duplicate_ack` event.
  - Return `success: true, applied: false, duplicate: true`.
- Current state in `FINAL_STATES` and new status differs:
  - Reject as `already_final`.
  - No `jobs` update.
  - Record `ack_rejected` and deadletter metadata.
  - CLI exits non-zero.
- New status not in `ALLOWED_TRANSITIONS[current]`:
  - Reject as `illegal_transition`.
  - No `jobs` update.
  - Record `ack_rejected` and deadletter metadata.
  - CLI exits non-zero.
- Legal transition:
  - Update `status`, `updated_at`, and `final_at` if entering final state.
  - Record `ack_applied` with `prev_status` and `new_status`.
  - Return `success: true, applied: true`.

## M1 design: ACK parser and validator

### Files/functions

- Create `src/agentflow_hermes/ack.py`
  - `parse_ack_block(text: str) -> dict[str, str]`
  - `validate_ack(fields: Mapping[str, str]) -> AckPayload`
  - `AckError(reason: str, deadletter: bool = True)`
- Edit `src/agentflow_hermes/cli.py`
  - Import from `ack.py` instead of owning parser logic inline.
  - Keep existing CLI command `agentflow-hermes ack ingest --text ...`.
- Edit `src/agentflow_hermes/store.py`
  - `ack()` takes validated status and performs DB transition rules.

### ACK format

Keep current prompt-compatible format:

```text
[JOB ACK]
job_id: job_...
status: succeeded|failed|waiting_review|waiting_user
summary: <short result>
artifacts:
- <files/links/tests>
blockers: <none or exact blocker>
```

Allowed ACK statuses:

- `succeeded`
- `failed`
- `waiting_review`
- `waiting_user`

Note: `dispatched` is an internal lifecycle state, not an ACK status emitted by workers.

### Invalid, duplicate, and deadletter cases

- Missing `[JOB ACK]` block:
  - CLI prints JSON error, exits 2.
  - If a source ref/hash is available in future ingest paths, create deadletter reason `malformed`.
- Missing `job_id`:
  - Error reason `missing_job_id`, exit 2.
- Missing `status`:
  - Error reason `missing_status`, exit 2.
- Unknown status:
  - Error reason `invalid_status`, deadletter, no job mutation, exit 2.
- Unknown job id:
  - Store returns `success: false, error: unknown_job`, deadletter with `job_id` field if present, exit 2.
- Duplicate ACK for same current status:
  - Idempotent no-op, `success: true, applied: false`, record event, exit 0.
- ACK after final state or illegal transition:
  - `success: false`, `reason: already_final` or `illegal_transition`, record rejected event and deadletter metadata, exit 2.

## M2 design: cron bridge dry-run

### Files and CLI commands

Create `src/agentflow_hermes/cron_bridge.py`:

- `classify_markers(text: str) -> list[CronMarker]`
- `make_dedupe_key(source_kind: str, correlation_id: str, source_hash: str) -> str`
- `ingest_cron_output(store: AgentFlowStore, *, source_ref: str, source_hash: str, marker_text: str = '', source: str = 'cron', correlation_id: str = '', target: str = '', origin_return: str = '') -> dict`

Edit `src/agentflow_hermes/cli.py`:

- Add command group `cron`.
- Add `cron ingest`:
  - `--ref` required
  - `--hash` required
  - `--marker-text` optional, small marker excerpt only
  - `--source` default `cron`
  - `--correlation-id` optional
  - `--target` optional
  - `--origin-return` optional
  - `--title` optional; default from marker summary

### Cron bridge data flow

1. External Hermes cron/supervisor job produces an output artifact and passes AgentFlow a ref plus content hash.
2. AgentFlow receives only source ref/hash and optional small marker line, not the raw transcript.
3. `classify_markers()` parses marker lines.
4. If marker is `kind=noise` or no material marker exists:
   - Do not enqueue a job.
   - Record `cron_noise` metadata if there is a correlation/job context; otherwise return a structured dry-run result.
5. If marker is `kind=material`:
   - Compute or validate `source_hash`.
   - Compute dedupe key.
   - Attempt enqueue with `source_kind='cron'`, `source_ref`, `source_hash`, `correlation_id`, `dedupe_key`.
   - DB unique guard on `source_hash` prevents duplicates.
   - Record `cron_ingested` event.
6. If same hash arrives again:
   - Return `success: true, applied: false, duplicate: true`.
   - Record `cron_duplicate` where practical.
7. `dispatch-dry-run <job_id>` renders the dispatch prompt only. No live send.

### Marker format

Use single-line markers that can be safely extracted from cron output:

```text
[AF-CRON] kind=<material|noise> ref=<ref> hash=<sha256-or-prefix> summary=<short human summary>
```

Rules:

- `kind=material` is eligible to enqueue.
- `kind=noise` is explicitly ignored.
- Missing marker defaults to no enqueue.
- `summary` should be short and safe for a job title/body summary.
- `ref` must identify where an authorized operator/tool can retrieve source material; AgentFlow stores the ref but not the raw payload.

### Dedupe key

```python
sha256(f"{source_kind}:{correlation_id}:{source_hash}".encode()).hexdigest()[:16]
```

- Store in `jobs.dedupe_key` for cheap lookup and operator readability.
- DB hard guard is `uniq_jobs_source_hash`.

### Dispatch dry-run prompt extension

Extend `render_dispatch_prompt(job)` additively:

```text
You are working an AgentFlow job. Return an explicit [JOB ACK] block when done.

[JOB]
job_id: ...
correlation_id: ...
source_kind: cron
source_ref: ...
source_hash: ...
target: ...
origin_return: ...
title: ...

<body or safe summary/ref>

[JOB ACK FORMAT]
[JOB ACK]
job_id: ...
status: succeeded|failed|waiting_review|waiting_user
summary: <short result>
artifacts:
- <files/links/tests>
blockers: <none or exact blocker>
```

Keep old fields present so M0 callers remain compatible.

## Tests and smokes for implementation card

Add tests:

- Migration tests:
  - Fresh DB initializes at v2.
  - M0/v1 DB upgrades without losing existing jobs/events.
  - New columns and indexes exist.
- State/ACK tests:
  - Legal transitions for queued/dispatched/waiting states.
  - Illegal transition rejection.
  - Final-state guard rejects mutation after `succeeded` or `failed`.
  - Duplicate ACK is idempotent and records `duplicate_ack`.
  - Invalid status creates deadletter/no mutation.
  - Unknown job creates deadletter/no mutation.
  - Malformed ACK returns exit 2.
- Event ledger tests:
  - `seq` is monotonic per job.
  - Transition events include `prev_status` and `new_status`.
- Cron bridge tests:
  - Material marker enqueues one job with source fields.
  - Same hash ingested twice returns duplicate/no second job.
  - Noise marker does not enqueue.
  - No marker does not enqueue.
  - Dry-run prompt includes source provenance.
- CLI smoke tests:
  - `uv run agentflow-hermes init`
  - `uv run agentflow-hermes enqueue --title ...`
  - `uv run agentflow-hermes dispatch-dry-run <job_id>`
  - `uv run agentflow-hermes ack ingest --text ...`
  - `uv run agentflow-hermes status --json`
  - `uv run agentflow-hermes cron ingest --ref ... --hash ... --marker-text ...`

## Implementation checklist for next ccsupervisor card

1. Add `states.py` with status enum, final states, transition matrix, and helper normalization.
2. Add `migrations.py` with v1/v2 schema and `PRAGMA user_version` migration driver.
3. Refactor `store.py` init to run migrations.
4. Extend `store.enqueue()` with `correlation_id`, `source_kind`, `source_ref`, `source_hash`, and dedupe support.
5. Add `record_event()` and use it for enqueue, ACK apply/reject/duplicate, cron ingest/duplicate/noise, and deadletter events.
6. Add `deadletter()` with ref/hash-only storage.
7. Move ACK parsing/validation into `ack.py`; keep CLI command compatible.
8. Make `store.ack()` transition-guarded and final-state safe.
9. Add `cron_bridge.py` with marker classification, dedupe key generation, and ref/hash-only ingestion.
10. Add `cli.py cron ingest` while preserving existing commands.
11. Extend `render_dispatch_prompt()` with source provenance and keep `[JOB ACK FORMAT]` unchanged.
12. Add tests listed above.
13. Run `uv run pytest` plus CLI smoke commands.

## Risks and assumptions

- Non-blocking assumption: the caller supplies cron output as ref/hash and, optionally, a small safe marker excerpt. AgentFlow should not ingest/store raw cron transcripts.
- SQLite partial unique indexes should be supported in the target environment; if not, keep a normal index plus application-level duplicate guard.
- `correlation_id` default needs care: for new jobs, set it to the generated job id if not provided.
- Existing M0 ACK tests should continue to pass with updated expected response shape.

## Final verdict

Verdict: GO

The design is implementable as the next card. It is additive, migration-safe, keeps AgentFlow inside plugin/sidecar boundaries, preserves dry-run-only behavior, avoids raw payload storage, and names concrete files/functions/tests for M1/M2 implementation.
