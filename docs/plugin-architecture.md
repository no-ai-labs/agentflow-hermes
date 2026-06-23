# AgentFlow Hermes plugin architecture

Kanban task: `t_a4373be3`
Origin/return_to: Discord Devhub / #hermes-main
Branch/worktree: `af/m1-m2-store-ack-cron` at `/home/duckran/dev/worktrees/agentflow-hermes-m1-m2`
Authoritative design route: `claude-openrouter-opus` via OpenRouter model `anthropic/claude-opus-4.8`
Design verdict: GO, with packaging hardening required before claiming the plugin is installable by normal users.

This document extends `docs/m1-m2-design.md`. It does not supersede the M1/M2 store, ACK, or cron-bridge design.

## 1. Current inspection summary

| Area | Current state | Design verdict |
| --- | --- | --- |
| Store, migrations, event ledger | `AgentFlowStore`, SQLite migrations, job events, deadletter rows | Keep as engine-owned control plane |
| State machine / ACK semantics | `JobStatus`, final-state guard, duplicate/illegal ACK handling, multiline ACK parser | Keep; ACK verdicts remain separate from delivery receipts |
| Cron bridge | Ref/hash ingestion and marker parsing under `agentflow_hermes.bridges.cron`, compatibility shim in `cron_bridge.py` | Continue M2 as-is; review should verify dry-run/no-raw-output invariants |
| Plugin toolset | Thin Hermes toolset in `plugins/hermes-agentflow` | Keep surface compact, but harden packaging/install path |
| Blocked remediation gap | t_18032353 stayed blocked after remediation review GO | Solve with a narrow dry-run resolver, not broad auto-unblock |

Two packaging issues are load-bearing for the product goal:

1. Python floor mismatch: `pyproject.toml` says `requires-python = ">=3.10"`, while `StrEnum` requires Python 3.11. Either change `JobStatus` to `class JobStatus(str, Enum)` or bump the floor to `>=3.11`. Prefer `str, Enum` if Python 3.10 support matters.
2. Plugin layout assumption: the current plugin adapter shells out with `PYTHONPATH=<repo>/src` derived from `Path(__file__).resolve().parents[2]`. That only works when the plugin lives inside a full checkout. A real install such as `hermes plugins install no-ai-labs/agentflow-hermes#plugins/hermes-agentflow` may install only the plugin subtree, so the adapter must not depend on repo-relative `src/`.

These issues do not invalidate the M2 cron bridge direction, but they must become acceptance-gated packaging work before final product GO.

## 2. Architecture boundary

AgentFlow should remain two artifacts with one source of business logic:

```text
agentflow_hermes Python package
  store.py, migrations.py, states.py, ack.py, bridges/*, cli.py
  owns state, migrations, bridge logic, ACK validation, dry-run safety gates

plugins/hermes-agentflow Hermes plugin adapter
  plugin.yaml, after-install.md, __init__.py
  registers Hermes tools and translates tool args to engine calls
  owns no durable state and no business logic
```

Hard invariants:

- No Hermes core monkeypatching.
- The plugin registers tools through the Hermes plugin context only.
- Engine code is reusable from the sidecar CLI and from the plugin adapter.
- No live `send_message`, live `active_wake`, or live unblock path is added until a later explicit rollout gate.
- Stored job bodies/events use refs, hashes, summaries, and bounded metadata; they must not store raw private transcripts, secrets, or absolute private payloads.

## 3. Plugin install and packaging decision

Decision: require the `agentflow-hermes` Python package to be importable, and make the plugin adapter call the engine in-process.

Rejected for now: vendoring a copy of the engine under `plugins/hermes-agentflow/_vendor`. Vendoring would fork the control-plane safety guards and create two places to patch state-machine or ACK bugs.

Implementation direction:

- `after-install.md` should instruct users to install the engine package first, for example:

```bash
uv pip install 'agentflow-hermes @ git+https://github.com/no-ai-labs/agentflow-hermes'
# or the equivalent pip command in the Hermes runtime environment
hermes plugins enable agentflow
agentflow-hermes init
hermes gateway restart
```

- The plugin adapter should import `agentflow_hermes` directly and call engine functions in-process instead of spawning `python -c` with repo-relative `PYTHONPATH`.
- If import fails during plugin registration, the adapter should degrade to an `agentflow_doctor` tool that returns an actionable package-not-installed message, rather than crashing plugin load silently.
- `agentflow-hermes doctor` should report at least: schema version, package version, dry-run mode, DB path, and whether the gateway/plugin interpreter can import the engine.

## 4. Toolset surface and schema budget

Keep the `agentflow` toolset small and dry-run-oriented. Target: no more than 8 tools through M4.

Recommended tool surface:

- `agentflow_doctor`: install/store/dry-run health.
- `agentflow_enqueue`: queue a durable handoff using metadata/refs.
- `agentflow_status`: list recent local AgentFlow jobs.
- `agentflow_dispatch_dry_run`: render dispatch prompt only.
- `agentflow_ack_ingest`: ingest `[JOB ACK]` blocks and apply guarded state transition.
- `agentflow_bridge_cron`: ingest pre-sanitized cron bridge metadata/ref/hash/marker, dry-run only.
- Future `agentflow_resolve_blocked_dry_run`: emit one guarded unblock candidate; no mutation.

Do not expose `bridge cron scan --output-file` as a plugin tool. File scanning is appropriate for the CLI/operator path, but a model-callable plugin tool that reads arbitrary local files expands the data-exfiltration surface. The plugin should accept only refs, hashes, job/run IDs, targets, and compact marker text.

## 5. Control-plane channel separation

Keep these channels separate in schema, event kinds, and mental model:

- `task_verdict`: what a worker or reviewer decided (`succeeded`, `failed`, `waiting_review`, `waiting_user`) and the guarded ACK ledger transition.
- `passive_delivery`: a normal report/notification receipt, not proof that the task succeeded.
- `active_wake`: material-event wake metadata, disabled for live dispatch in this phase.
- `operator_receipt`: audit record that an operator-facing bridge or resolver proposed/refused/applied an action.

A delivery receipt must not be treated as a task verdict, and a task verdict must not imply a live wake/send happened.

## 6. Cron bridge boundary

M2 should continue with the current dry-run bridge shape:

- Input is source ref/hash plus bounded marker metadata.
- Raw cron output may be scanned by CLI, but is not stored in the AgentFlow DB.
- `HERMES_ACTIVE_WAKE {...}` is parsed only as material-event metadata; live wake remains disabled.
- Stable dedupe key shape should remain human-auditable, e.g. `cron:<job_id>:<run_id-or-output_hash>:<target>`.
- Duplicate material events should not enqueue duplicate jobs.
- Dispatch remains `dispatch-dry-run`; no live send path.

M2 acceptance should include empty/no-change, material, active-wake metadata, duplicate, and raw-output/secret/path leakage tests.

## 7. Future Kanban bridge boundary

Design the future Kanban bridge after the cron bridge, but do not implement it before packaging is hardened:

- Module: `agentflow_hermes.bridges.kanban`.
- Input: card ref/id, revision/hash, compact verdict/marker metadata.
- Storage: refs/hashes/summary only; do not copy raw card bodies or private thread text into AgentFlow jobs.
- Dedupe key: `kanban:<card_id>:<rev-or-hash>:<target>`.
- Default action: dry-run enqueue or dry-run resolver candidate.
- No Hermes core monkeypatch and no direct broad board mutation.

## 8. Guarded blocked-remediation resolver

The observed gap is real: a downstream card blocked by a semantic review BLOCK does not automatically resume when a later remediation review returns GO. That is correct for the general Kanban system, because broad auto-unblock of arbitrary blocked cards is unsafe. AgentFlow should provide a narrow candidate resolver.

Resolver design:

- CLI shape: `agentflow-hermes bridge kanban resolve-blocked --card <blocked-card-id> --verdict <remediation-review-id> --dry-run` or equivalent.
- Plugin shape, if exposed: `agentflow_resolve_blocked_dry_run` with required card/verdict refs only.
- Default dry-run only: emit an unblock/dispatch candidate with evidence; do not mutate board state.
- Future apply mode must be a separate explicit rollout/approval gate.

Required guards:

1. Single-card scope: resolver takes one blocked card and one remediation verdict/ref; it never scans and unblocks all blocked cards.
2. Explicit remediation shape: the blocked task/comment/body or metadata names the prior blocker and remediation path.
3. Semantic GO: referenced remediation review is a terminal semantic `Verdict: GO`, not merely status `done`.
4. Provenance match: remediation verdict correlates to the blocked card or the blocker it remediates; unrelated GO reviews cannot unlock unrelated cards.
5. Receipt-first audit: dry-run emits structured evidence; future apply must record an operator receipt before mutation.
6. Ref/hash storage: resolver receipts store IDs, refs, hashes, verdict summaries, and reasons, not raw private card transcripts.

Unsafe cases must refuse closed: missing verdict, BLOCK/NEED_MORE verdict, unrelated remediation, ambiguous multiple blockers, no explicit card id, or broad/unscoped scan request.

## 9. Phased implementation plan

### M2: cron bridge dry-run

Disposition: continue as-is and release to review `t_a4f7669a` after implementation finishes.

Acceptance:

- `uv run pytest -q` passes.
- CLI smoke with synthetic cron output fixture passes.
- Empty/no-change/no marker does not enqueue.
- Material marker enqueues one job.
- Duplicate ref/hash returns duplicate/no second job.
- Active-wake marker becomes metadata only, with live wake disabled.
- No raw output, secret-like payload, or absolute private payload is stored.
- Plugin schema remains compact and dry-run only.

### M3: plugin packaging hardening (new recommended card)

Purpose: make the plugin actually installable/operable via the documented Hermes plugin UX.

Acceptance:

- Resolve Python floor mismatch (`str, Enum` or honest `>=3.11`).
- Plugin adapter removes repo-layout `parents[2]`/`PYTHONPATH` assumption.
- Plugin uses in-process engine imports/calls.
- Missing engine package degrades to actionable `agentflow_doctor` result, not plugin load crash.
- `after-install.md` includes engine install, enable, init, restart steps.
- `doctor` reports package/import/schema/dry-run health.
- Existing tests still pass.
- Add plugin adapter tests for importable and missing-package paths.
- Add guard check for no Hermes core monkeypatch and no repo-layout assumption.

### M4: guarded blocked-remediation resolver (re-scoped M2.5)

Purpose: address the t_18032353/t_60d7a850 class without broad auto-unblock.

Acceptance:

- Fixture matching original review BLOCK + remediation review GO + downstream blocked card returns one candidate in dry-run.
- Dry-run does not mutate board state.
- BLOCK/NEED_MORE/missing remediation verdict refuses closed.
- Unrelated GO verdict refuses closed by provenance guard.
- Broad/no-card scan requests fail.
- Receipt/evidence uses refs/metadata only.
- If a plugin tool is added, it remains dry-run only and compact.

### M5: Kanban bridge

Purpose: generalize dry-run card/ref ingestion after the resolver and packaging are proven.

Acceptance mirrors cron bridge: ref/hash ingestion, no raw transcript storage, dedupe, dry-run dispatch prompt, compact plugin surface if exposed.

### Rollout gate: live delivery/apply

Separate explicit approval card only. This is where live `send_message`, `active_wake`, or live unblock/apply can be considered. Until then, all bridges and resolvers remain dry-run.

## 10. Task graph recommendations

- `t_18032353` (M2 cron bridge): continue as-is; do not supersede.
- `t_a4f7669a` (M2 review): keep after M2.
- New M3 implementation + review: insert before product-final GO to fix packaging/installability.
- `t_a3245020` (M2.5): re-scope to M4 guarded blocked-remediation resolver, with this design's resolver guards and tests.
- `t_f728373f`: review the re-scoped M4 resolver.
- `t_695c95b3` fan-in: include M2 review, M3 review, M4 review, M1 fix review, and this Opus design review before terminal GO.

Suggested dependency sketch:

```text
t_a4373be3 Opus design -> t_cb099523 design review
M2 t_18032353 -> t_a4f7669a review
M3 packaging hardening -> M3 review
M4/t_a3245020 guarded resolver -> t_f728373f review
fan-in t_695c95b3 waits for design review + M2 review + M3 review + M4 review + M1 remediation review
```

## 11. Risks and residual blockers

- Packaging is not fully product-ready until M3 fixes the Python floor and plugin layout assumptions.
- The resolver is the highest-risk surface for accidental broad board mutation; keep it single-card and dry-run until a later explicit apply gate.
- Marker trust remains bounded by allowlists, summaries, and ref/hash storage. Continue length caps and secret stripping.
- SQLite feature assumptions should be surfaced by doctor if the gateway runtime has an old SQLite.

Final architecture verdict: GO for the design direction and for allowing M2 to continue; NEEDS FOLLOW-UP for packaging hardening before final plugin/product GO.
