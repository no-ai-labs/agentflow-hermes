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
  stage's own semantic GO, never on lifecycle `done`. All external board
  mutations made by the handler (`create_task`, owner-anchor `subscribe`, and
  owner-anchor completion) are first persisted as durable `board_outbox` rows;
  `continuation retry` replays pending rows by operation.
- `board_adapter.py` — `FakeBoardAdapter` / `RealBoardAdapter` (injectable CLI
  runner, same pattern as `RealKanbanGraphAdapter`) implementing
  create/block/subscribe/comment/complete-anchor, each idempotent by key.
  Real subprocess mode pins `HERMES_KANBAN_BOARD`/`HERMES_KANBAN_DB` to the
  explicit `--board` so a worker's inherited board environment cannot silently
  redirect `--board warroom-os` mutations to another board. `discord:#research`
  is normalized to the numeric Devhub #research channel id
  `1499390151393284106`, and successful numeric Discord subscriptions repair
  durable `kanban_notify_subs` + `ack_subscription` + `ack_active_wake` rows in
  the target board DB without sending Discord messages.
  **`RealBoardAdapter`'s argv shapes were verified against the installed
  `hermes` CLI's own `--help` output and `hermes_cli/kanban.py` source**
  (initial fix used invented flags — corrected after supervisor review, see
  "Corrections" below): `create --initial-status blocked` (no
  `--blocked-reason`/origin flags — folded into `--body` instead), `block
  <task_id> [reason] --kind needs_input`, `notify-subscribe <task_id>
  --platform P --chat-id C` (not `subscribe`), `comment <task_id> <text>`,
  `complete <task_id> --summary S --metadata JSON` (not `--receipt-ref`).
  Only `create`/`show`/`list` support `--json` on this CLI version; the
  adapter treats `block`/`comment`/`complete`/`notify-subscribe` as
  plain-text, exit-code-only commands.
- `board_events.py` + `continuation_engine.py` — board-scoped
  `(board, db_identity)` cursors (never global), structured-metadata-first
  ingestion, routing GO to the existing `propose_next_slice_graph`, code BLOCK
  to the existing `propose_remediation_graph`, and `needs_input` to
  `OwnerInputHandler`. Unknown/malformed outcomes never mutate anything.
- `continuation_cli.py` wired into `cli.py` as `agentflow-hermes continuation
  ingest|list|show|submit|retry|doctor` (`--adapter-mode fake|real` on
  `ingest`/`submit`/`retry`; see "Real-board canary readiness" below before
  ever passing `--adapter-mode real --board warroom-os`).
- G4.21 vertical canary, two layers:
  - `tests/test_g421_vertical_canary.py` — fresh synthetic `needs_input` event
    end-to-end through packet-rerun using `FakeBoardAdapter` (in-memory only;
    proves the continuation state machine and lazy-gating logic).
  - `tests/test_g421_real_adapter_canary.py` — the same end-to-end loop
    against `RealBoardAdapter` with a **mocked CLI runner** that returns
    CLI-shaped stdout for the verified argv patterns above, asserting exactly
    one owner-anchor `create --initial-status blocked` call, one
    `notify-subscribe --platform discord --chat-id 1499390151393284106` call, zero
    downstream `create` calls before the owner receipt, and exactly 4 total
    `create` calls (anchor + materialization + review + packet-rerun) across
    the full loop with zero duplicates on repeat ingest. It does **not**
    invoke a real subprocess or touch any real board database.

### Real-board canary execution note

`RealBoardAdapter` is unit-tested with mocked CLI runners so ordinary pytest
never mutates live boards. The supervised replacement canary for this slice is
run explicitly against `warroom-os` with disposable synthetic task IDs, numeric
#research ACK rows, durable outbox evidence, and cleanup/archival captured in
the Kanban handoff. Do not infer real G4.21 owner proof from that canary; it is
a controlled fixture only.

## Corrections applied after supervisor review

The first pass of `RealBoardAdapter` used non-existent CLI commands/flags
(`hermes kanban subscribe`, `create --status`/`--blocked-reason`,
`block --reason`/`--json`, `comment --body --json`, `complete --receipt-ref`)
and tests that asserted those invented shapes instead of the real ones. Fixed
by reading the installed CLI's own `--help` output and
`hermes_cli/kanban.py` source directly (see the `board_adapter.py` module
docstring for the verified shapes), rewriting `RealBoardAdapter` against them,
rewriting `tests/test_board_adapter.py` to assert the real argv, and adding
`tests/test_g421_real_adapter_canary.py` as a mocked-runner end-to-end proof
that the corrected adapter actually drives the vertical loop. No claim is
made that `FakeBoardAdapter` alone satisfies the real-board canary
requirement — see "Real-board canary readiness" above.

**Second correction — unstable board idempotency keys.** `OwnerInputHandler`
originally built board mutation idempotency keys as `owner_anchor:{instance_id}`,
`materialize:{instance_id}`, `review:{instance_id}`, `packet_rerun:{instance_id}`,
using the local SQLite row id. Two independent fresh stores (two canary runs,
or a temp store vs. the canonical store) commonly both assign row id `1` to
their first instance, so on a real shared board this could make unrelated
continuations collide on the same `--idempotency-key`. Fixed by deriving
these keys from the instance's durable, source-scoped
`idempotency_key` column (`continuation:{board}:{source_task_id}:{source_event_id}:{contract_ref}`,
already unique-indexed in `continuation_store.py`) via a sha256 digest
(`_stable_digest` in `continuations/owner_input.py`), never the row id.
`tests/test_owner_input_continuation.py::test_board_idempotency_keys_are_stable_and_differ_across_fresh_stores_sharing_row_id`
proves two fresh stores that both land on instance id `1` for different
source events now get different keys; it fails against the pre-fix code
(verified by re-running it with the fix stashed).

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
