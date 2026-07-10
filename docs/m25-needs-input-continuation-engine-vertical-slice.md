# M25: Needs-Input Continuation Engine — First Vertical Slice

Implements the architecture in `docs/plans/2026-07-10-needs-input-continuation-engine-design.md`
through Task 8 of that plan's implementation section. This is Phase A/B of the
design's migration plan (additive; existing GO/code-fix paths unchanged).

## Delivered

- `outcome.py` — `Verdict`/`ContinuationKind`/`OutcomeEnvelope`, structured
  Kanban run-metadata authority first, explicit text-marker fallback second.
- `input_contract.py` / `continuation_config.py` / `contracts/warroom.g421.exposure-resolution.v1.yaml`
  — `FieldAuthority`-typed `InputContract`, owner-submission validation that
  refuses unknown fields, non-owner-authority fields, and missing required
  owner fields in one shot (no partial apply).
- `continuation_store.py` (+ `migrations.py` `SQL_V5`) — durable
  `continuation_instances/steps/owner_input_receipts/events/board_cursors/board_outbox`,
  a `ContinuationState` machine with legal-transition enforcement, append-only
  receipt versioning, idempotent steps/outbox, board-scoped cursors, and
  `doctor_store_selection` split-brain detection between the profile-scoped
  canonical store and the legacy `~/.agentflow/agentflow.db` fallback.
- `continuations/owner_input.py` + `continuation.py` — `OwnerInputHandler`
  implementing `needs_input -> WAITING_OWNER -> owner receipt ->
  MATERIALIZING -> (materialization GO) -> WAITING_REVIEW -> (review GO) ->
  RESUMABLE -> RESUMED`, with lazy per-stage task creation gated on each
  stage's own semantic GO, never on lifecycle `done`.
- `board_adapter.py` — `FakeBoardAdapter` / `RealBoardAdapter` (injectable CLI
  runner, same pattern as `RealKanbanGraphAdapter`) implementing
  create/block/subscribe/comment/complete-anchor, each idempotent by key.
- `board_events.py` + `continuation_engine.py` — board-scoped
  `(board, db_identity)` cursors (never global), structured-metadata-first
  ingestion, routing GO to the existing `propose_next_slice_graph`, code BLOCK
  to the existing `propose_remediation_graph`, and `needs_input` to
  `OwnerInputHandler`. Unknown/malformed outcomes never mutate anything.
- `continuation_cli.py` wired into `cli.py` as `agentflow-hermes continuation
  ingest|list|show|submit|retry|doctor`.
- G4.21 vertical canary (`tests/test_g421_vertical_canary.py`) exercising a
  fresh synthetic `needs_input` event end-to-end through packet-rerun using
  only `FakeBoardAdapter` (no subprocess, no live gateway).

## Explicit follow-ups (not hidden, not implemented here)

1. **Task 9 (full watchdog consolidation) is out of scope for this slice.**
   `scripts/agentflow_auto_remediation_watchdog.py` and the roadmap watchdog
   entrypoint still run their own cursors/ledgers; they are not yet pointed at
   `continuation_engine.ingest_board_once`. Migrating them is additive (Phase
   C/D of the design doc) and should land as its own reviewed slice so the
   existing M24B Oracle canary and roadmap-GO autopromoter behavior can be
   re-verified in isolation.
2. **`continuation ingest` reads a fixture file, not a live Hermes Kanban
   event stream.** There is no importable real-time Kanban event client
   available at runtime (the same constraint already documented on
   `RealKanbanGraphAdapter`). Wiring `BoardEventSource` to the real per-board
   Kanban DB is a follow-up once that DB's schema/API surface is confirmed.
3. **`ROADMAP_NEXT`/`CODE_FIX` are routed to the existing `propose_next_slice_graph`
   / `propose_remediation_graph` functions**, not converted into full
   `ContinuationHandler` objects behind the durable store. They remain
   request-only exactly as before; only `needs_input` is durable-continuation
   backed in this slice.
4. **`approval_required` and `external_wait` continuation kinds** are modeled
   in `outcome.py`'s enum but have no handler yet (P2 in the design doc).
5. **Timeout/reminder policy for `WAITING_OWNER` anchors** is not implemented.
