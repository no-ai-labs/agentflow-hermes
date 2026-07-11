# M26 — Global needs_input rollout

Extends the M25 needs-input continuation engine from a single-board vertical
slice to every board in the canonical catalog, driven by one shared,
board-aware scan loop rather than per-channel scanner scripts.

## Goal

Any board enrolled in the registry (`config/boards.yaml`) automatically receives
`needs_input` continuation — owner-anchor creation, active-wake subscription,
structured owner receipt, and exactly one resume/action task — with no
per-channel Python branch and no per-board cron.

Currently enrolled:

| Board | Channel | Default endpoint |
|-------|---------|------------------|
| agentflow-hermes | #hermes-main | `discord:#hermes-main:1497895797579190357` |
| warroom-os | #research | `discord:#research:1499390151393284106` |
| oracle-lab | #shaman | `discord:#shaman:1500539609413849200` |

## Architecture

```
config/boards.yaml            declarative catalog + route data (one place)
        │
        ▼
ingest_all_boards()           one board-aware scan loop over enabled boards
        │  per board
        ├─ cursor_exists? no → seed to current max event (no replay)
        │                      (unless an explicit migration cursor is given)
        └─ ingest_board_once() → parse_outcome_envelope → owner-input handler
                │
        LiveBoardEventSource   read-only over the real per-board kanban.db
                │              (task_events ⋈ tasks ⋈ task_runs)
        RealBoardAdapter       durable outbox → gated hermes CLI mutations
```

### Cursor identity and no-replay seeding

Cursor identity is `(board, db_identity)` keyed on `task_events.id`. A board
seen for the first time has **no** cursor row (distinct from a cursor of 0);
the scan loop seeds it to the board's current max event so historical events
are never replayed. An explicit `migration_cursors[board]` overrides the seed
when a controlled backfill is required.

### Outcome authority

`parse_outcome_envelope` prefers structured run metadata `agentflow_outcome`;
explicit summary markers (`Verdict`, `Outcome-Kind`, `Continuation-Contract`,
operator-input prose) are the compatibility fallback and can never claim
`confidence="structured"`. Vague prose routes to `UNKNOWN` and never mutates.

### Generic vs domain contracts

An explicit `continuation_kind=needs_input` with no `contract_ref` resolves to
the versioned `generic.owner-input.v1` contract. A domain contract such as
`warroom.g421.exposure-resolution.v1` supplies its own `contract_ref` and thus
overrides the generic behavior. The generic contract declares no artifacts: its
resume path materializes exactly one action/resume task carrying a sanitized
receipt reference. Owner-input BLOCK never creates a code-fix task.

### Return-endpoint resolution

Generic and declarative: a source task's own typed notify endpoint
(`kanban_notify_subs`) wins; otherwise the board's `default_endpoint` from the
registry. No per-board behavior branch exists in code.

## Watchdog runtime

`scripts/agentflow_needs_input_watchdog.py` is the single, registry-driven
no-agent runtime. Dry-run by default (in-memory adapter, no board write);
`--apply` switches to the real gated CLI adapter. Stdout carries only material
owner-input/GO/BLOCK creation; a cadence with nothing new is silent, so it is
safe on a tight cron.

Existing runtimes are untouched and continue to work independently: the GO
roadmap autopromoter and the M24B oracle actionable code-BLOCK remediation
watchdog keep their own cursor state; this watchdog defaults to `needs_input`
only and does not double-process their events.

## Tests

- `tests/test_live_board_event_source.py` — production sqlite read path against
  a real-shaped temp kanban.db (terminal events, structured metadata, cursor
  bounds, typed-endpoint-over-default resolution, missing-db safety).
- `tests/test_global_needs_input_rollout.py` — registry load, first-sight
  seeding without replay, post-seed processing on all boards, disabled-board
  skip, migration-cursor override, additive future enrollment, generic-contract
  fallback, default-endpoint fallback, `handle_kinds` restriction.
- `tests/test_needs_input_watchdog.py` — silent on no new events, empty-registry
  BLOCK, main() smoke.
- Existing `tests/test_g421_real_adapter_canary.py`, `test_owner_input_*`,
  `test_outcome.py`, `test_continuation_*`, and the M24B oracle remediation
  suite continue to pass unchanged.

## Real-board canary status

Running `--apply` against the three shared production boards creates real
owner-input cards on shared systems (Warroom/#research, Oracle/#shaman,
agentflow-hermes/#hermes-main). Per the same posture as the M25 G4.21 canary,
this outward-facing mutation was **deliberately not executed** without explicit
operator authorization. The live read path is proven: a dry-run cadence reads
all three real board DBs read-only and seeds cursors to their current max event
ids with zero instances, zero outbox rows, and zero board mutations. Code and
tests are ready for a controlled `--apply` canary when authorized.
