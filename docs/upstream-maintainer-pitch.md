# AgentFlow-Hermes upstream maintainer pitch

Status: maintainer-facing draft. Do not post verbatim until reviewed.
Origin/return_to: Discord Devhub / #hermes-main
Prepared: 2026-07-09

Current upstream PR snapshot (live `gh pr view`, checked 2026-07-09T18:17Z-18:19Z):

| PR | State | Draft | Mergeable | Updated | Maintainer-facing use |
|---|---|---:|---|---|---|
| [#58541](https://github.com/NousResearch/hermes-agent/pull/58541) | OPEN | false | MERGEABLE | 2026-07-08T06:29:56Z | lifecycle hooks: created/unblocked/pre_complete |
| [#58548](https://github.com/NousResearch/hermes-agent/pull/58548) | OPEN | false | MERGEABLE | 2026-07-04T23:11:48Z | observability hooks: worker/task update events |
| [#58549](https://github.com/NousResearch/hermes-agent/pull/58549) | OPEN | false | MERGEABLE | 2026-07-04T23:11:45Z | plugin infra: api_backend/deps/ctx.data_dir |
| [#59775](https://github.com/NousResearch/hermes-agent/pull/59775) | OPEN | false | MERGEABLE | 2026-07-06T17:54:49Z | hook/middleware lazy discovery correctness |

Treat mergeability as point-in-time metadata, not approval or CI status. `gh` returned an empty
status-check rollup for #59775 during the M23A verification.

## 1. Short problem statement

AgentFlow-Hermes is a board-driven continuation layer that sits outside `hermes-agent` and
uses Hermes Kanban as the durable source of truth. It currently integrates through
`hermes kanban ... --json` subprocess calls and, where event timing matters, the same
append-only `task_events` polling pattern used by Hermes' own gateway/dashboard code.

That shape works today and is intentionally low-coupling, but it exposes a few concrete
extension-surface gaps for third-party automation:

- there is no hook for task creation or dependency promotion, so external orchestrators poll
  for changes instead of reacting to lifecycle events;
- plugin hooks that run in gateway context need reliable cold-start delivery before any
  hook-based integration can be recommended;
- plugin authors need clearer state/dependency conventions (`ctx.data_dir`, package/version
  metadata, and documented CLI/event-polling patterns) to avoid bespoke sidecars and scattered
  files.

## 2. Philosophy alignment

This pitch is not a request to merge AgentFlow wholesale. The audit conclusion is the opposite:
AgentFlow should remain a standalone plugin/CLI because it encodes deployment-specific Discord,
review, release, and lane policy.

The useful upstream signal is that AgentFlow independently validates Hermes' existing design
rubric:

- **Board-owned state:** Hermes Kanban remains the source of truth. AgentFlow derives graph
  actions from board rows, parent links, run outcomes, and explicit ACK/verdict text; it does not
  keep a competing task state machine.
- **Narrow core, capability at the edges:** AgentFlow uses a CLI-first engine plus a thin plugin
  adapter, capped model-tool surface, and no core monkeypatching. This matches the Footprint
  Ladder's "CLI command + skill / standalone plugin" path.
- **Fail-closed automation:** mutating operations require explicit apply gates, idempotency keys,
  allowlists, and durable receipts. Those are local policy choices, but they are good docs examples
  for other external orchestrators.
- **Concrete consumer, not speculative hooks:** AgentFlow gives the existing hook/RFC PRs a second
  real consumer beside `kanban-advanced`, with specific pain points and tested workarounds.

## 3. Why AgentFlow remains standalone

Do not propose moving AgentFlow into `hermes-agent` core or bundled `plugins/`.

Reasons:

1. It is a third-party product/integration with deployment-specific policy. Hermes' own guidance
   says that kind of code should ship as a standalone plugin repo or pip entry point, not as a
   maintenance burden in core.
2. Its most valuable pieces are patterns, not reusable core modules: double apply gates,
   idempotency ledgers, explicit ACK markers, review-block remediation graphs, and docs around
   board-driven continuation.
3. The current CLI/polling integration is safe because it avoids import-time coupling to Hermes
   internals. Upstream should stabilize and document the generic extension surfaces rather than
   absorb AgentFlow's implementation.

## 4. Requested upstream path

Recommended path, in order:

1. **Support #59775 first as a correctness fix for hook delivery.** AgentFlow's current path is not
   blocked by it, but any future gateway-context hook subscription is unsafe while module-level
   `invoke_hook()`/`invoke_middleware()` can silently no-op before plugin discovery.
2. **Support/land #58541 (`kanban_task_created`, `kanban_task_unblocked`, `kanban_pre_complete`).**
   AgentFlow has concrete created/promotion polling pain today; this PR closes much of that gap
   without forcing AgentFlow into core.
3. **Support #58548 (worker lifecycle/task mutation observability RFC).** These hooks map directly
   to token-burning polling loops and stale-claim/reactive-supervision use cases.
4. **Support #58549 (plugin infrastructure RFC).** `ctx.data_dir`, package/version declarations,
   and opt-in API backends reduce boilerplate and give plugin authors a sanctioned storage/lifecycle
   path.
5. **Contribute a narrow docs/tutorial PR** showing how a standalone orchestrator safely drives
   Hermes Kanban via `hermes kanban ... --json`, idempotency keys, and `task_events` polling today,
   with a follow-up note showing how the pattern simplifies once lifecycle hooks land.

## 5. Exact maintainer questions

1. For #58541, is `kanban_task_created` enough for external orchestrators, or would maintainers be
   open to a separate post-commit `kanban_task_promoted` hook for dependency-driven `todo → ready`
   transitions?
2. Should #59775 or an equivalent lazy-discovery fix be treated as a prerequisite before docs
   recommend hook-based third-party gateway integrations?
3. Do maintainers want a lightweight stability expectation for `hermes kanban ... --json` output,
   since standalone tools like AgentFlow currently treat that CLI shape as the safest public
   integration surface?
4. For #58549, is `ctx.data_dir` intended only for plugin package state, or should it also be the
   documented place for external engines invoked by a thin plugin adapter to keep durable receipts?
5. Would a docs/tutorial PR about board-driven continuation be welcome as external-plugin guidance,
   or should that live outside the upstream docs until #58541/#58548/#58549 settle?

## 6. Draft public GitHub comments

### PR #59775 — lazy-discover plugin hooks/middleware

Draft comment:

> I have a second external-consumer data point for this fix from AgentFlow-Hermes
> (standalone repo: https://github.com/no-ai-labs/agentflow-hermes).
>
> AgentFlow's current production path intentionally stays on `hermes kanban ... --json` plus
> board/event polling, so it is not blocked by this PR today. But the upstream fit audit found
> that any future AgentFlow hook-based integration running in gateway context would depend on this
> exact behavior: module-level `invoke_hook()` / `invoke_middleware()` must discover user plugins on
> cold start, or registered hooks can silently no-op while appearing installed.
>
> We independently reproduced that shape against PR #59775 in a temp clone: origin/main no-oped a
> user hook/middleware with `_discovered=False`, while the PR head lazily discovered and fired both.
> Reproduction notes are public here:
> https://github.com/no-ai-labs/agentflow-hermes/blob/main/docs/upstream-pr-59775-verification.md
>
> Concrete ask: I would support landing #59775 (or an equivalent lazy-discovery fix) before the docs
> recommend gateway-context hook integrations for third-party plugins. It is a correctness dependency
> for hook consumers, separate from whether any one plugin is merged upstream.

### PR #58541 — kanban lifecycle hooks

Draft comment:

> AgentFlow-Hermes gives this PR another concrete consumer besides `kanban-advanced`:
> https://github.com/no-ai-labs/agentflow-hermes
>
> AgentFlow is intentionally a standalone CLI/plugin adapter, not something we think should merge
> into Hermes core. It uses Hermes Kanban as board-owned state and currently reacts to lifecycle
> changes by shelling out to `hermes kanban ... --json` and polling/deriving state from board events.
> That is safe and decoupled, but task creation and dependency-driven readiness are exactly where
> polling is least satisfying: external orchestrators want to create follow-up cards, wait for parent
> completion, and ACK promotion without burning cron ticks or missing a short-lived transition.
>
> The audit is here:
> https://github.com/no-ai-labs/agentflow-hermes/blob/main/docs/upstream-fit-audit.md
>
> `kanban_task_created` and `kanban_task_unblocked` would reduce AgentFlow's polling surface while
> preserving Hermes as the source of truth. One question: would maintainers consider a separate
> observer-only `kanban_task_promoted` hook for dependency-driven `todo -> ready` promotion, or is
> the intent that external plugins continue to observe promotion via the append-only `task_events`
> table / polling pattern?

### PR #58548 — plugin observability hooks RFC

Draft comment:

> This RFC matches a real pain point we hit in AgentFlow-Hermes:
> https://github.com/no-ai-labs/agentflow-hermes
>
> AgentFlow is a standalone board-driven orchestrator. It does not import Hermes internals; it uses
> `hermes kanban ... --json` and treats the Kanban board as owned state. That choice is deliberate
> and aligns with the "narrow core, capability at the edges" model, but it means worker-exit,
> stale-claim, and task-update visibility currently come from polling and comparing board/run state.
>
> The upstream fit audit calls this out as a good place for generic Hermes primitives rather than
> AgentFlow-specific code:
> https://github.com/no-ai-labs/agentflow-hermes/blob/main/docs/upstream-fit-audit.md
>
> I would support the RFC direction if the hooks stay observer-only, post-commit, and best-effort,
> with no third-party telemetry backend bundled into core. That gives external orchestrators lower
> latency and less token/cron waste without changing the board-owned-state contract.

### PR #58549 — plugin infrastructure improvements RFC

Draft comment:

> AgentFlow-Hermes has a related standalone-plugin data point for this RFC:
> https://github.com/no-ai-labs/agentflow-hermes
>
> The AgentFlow engine deliberately lives outside Hermes and exposes only a thin adapter/plugin
> surface. That keeps deployment-specific orchestration policy out of core, but it also means the
> plugin needs boring, reliable lifecycle primitives: where to store durable receipts/idempotency
> ledgers, how to declare compatible Hermes/plugin/package versions, and how to avoid bespoke API
> sidecars when a plugin needs an authenticated backend surface.
>
> Audit notes:
> https://github.com/no-ai-labs/agentflow-hermes/blob/main/docs/upstream-fit-audit.md
>
> `ctx.data_dir` is the most immediately useful part for us: AgentFlow currently composes its own
> storage under the Hermes home because there is no documented plugin-owned data directory. A
> sanctioned data dir plus dependency metadata would make standalone plugins easier to install,
> reason about, and clean up without implying their code belongs in Hermes core.

## 7. Optional docs/tutorial PR outline

Title:

> docs: add board-driven continuation guide for external orchestrators

Body:

> Add a narrow tutorial for building a standalone Hermes-adjacent orchestrator without merging it
> into core. The guide covers:
>
> - using `hermes kanban ... --json` as the stable human/script integration surface;
> - treating the Kanban board as the durable source of truth;
> - using idempotency keys and explicit apply gates before mutating board state;
> - polling `task_events` with a cursor today, and mapping the same pattern to lifecycle hooks once
>   #58541/#58548-style hooks land;
> - keeping deployment-specific policy in the external plugin/CLI repo.
>
> This is docs-only and uses AgentFlow-Hermes as an example consumer, not as code proposed for
> upstream inclusion.

## 8. Recommended posting order

1. #59775 first: correctness dependency for hook delivery; cite independent regression evidence.
2. #58541 second: lifecycle hooks; ask the promoted-event scope question.
3. #58548 third: observability RFC; frame as lower-latency replacement for polling, not telemetry.
4. #58549 fourth: plugin infrastructure; focus on `ctx.data_dir` and dependency declarations.
5. After maintainer response, open or draft the docs/tutorial PR if welcome.
