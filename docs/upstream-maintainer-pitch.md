# AgentFlow-Hermes upstream maintainer pitch

Status: maintainer-facing draft. Do not post verbatim until reviewed.
Origin/return_to: Discord Devhub / #hermes-main
Prepared: 2026-07-09
Refreshed: 2026-07-15 (kanban task `t_a5d8415e`)

Current upstream PR snapshot (live `gh pr view`, checked 2026-07-15T09:14Z-09:15Z UTC):

| PR | State | Draft | Mergeable | Updated | Maintainer-facing use |
|---|---|---:|---|---|---|
| [#58541](https://github.com/NousResearch/hermes-agent/pull/58541) | OPEN | false | **CONFLICTING** (was MERGEABLE 2026-07-09) | 2026-07-08T06:29:56Z | lifecycle hooks: created/unblocked/pre_complete |
| [#58548](https://github.com/NousResearch/hermes-agent/pull/58548) | OPEN | false | MERGEABLE | 2026-07-04T23:11:48Z | observability hooks RFC: worker/task update events |
| [#58549](https://github.com/NousResearch/hermes-agent/pull/58549) | OPEN | false | MERGEABLE | 2026-07-04T23:11:45Z | plugin infra RFC — **title/body vs. diff mismatch, see below** |
| [#59775](https://github.com/NousResearch/hermes-agent/pull/59775) | OPEN | false | MERGEABLE | 2026-07-06T17:54:49Z | hook/middleware lazy discovery correctness |

Treat mergeability as point-in-time metadata, not approval or CI status. `gh` returned an empty
status-check rollup for #59775 during the M23A verification. #58541 flipped from MERGEABLE to
CONFLICTING between the 2026-07-09 and 2026-07-15 snapshots with no new commits on the PR itself
(`headRefOid` unchanged at `7114febfe07d1bb20a05822ba5bc4659c951ac1f`) — base-branch drift, not a
PR-side regression. Re-check before posting.

**2026-07-15T09:39Z re-check confirmation:** a second live pass (mergeable/mergeStateStatus,
changed files, diff contents, comment counts, URL reachability) reconfirmed every fact in this
refresh with no drift since the 09:14–09:15Z snapshot above. Added granularity from that pass:
`mergeStateStatus` is `BLOCKED` for #59775/#58548/#58549 (no reviews/required checks yet, consistent
with "no comments/reviews" above) and `DIRTY` for #58541 (needs a rebase against base, consistent
with its `CONFLICTING` mergeable state). This does not change any draft comment content or the
`safe_to_post` conclusions below — it is additional point-in-time metadata, not a blocker.

### 2026-07-15 refresh: what changed since the 2026-07-09 draft

- **#58541** — two review comments landed (2026-07-06, 2026-07-08). The reviewer's testing-checklist
  question and the "does this overlap with #42692's tool-layer block hook" question are both fully
  answered in-thread: the author ticked all six checklist items (12 passing tests, including explicit
  `pre_complete` allow/block/audit/error-non-fatal cases) and documented the tool-layer-vs-DB-layer
  hook distinction directly in `plugins.py`. **Do not re-ask either of those in the posted comment.**
  The dependency-driven `todo -> ready` promotion question from the original draft is still open —
  confirmed by reading the PR diff: it adds `kanban_task_created`, `kanban_task_unblocked`, and
  `kanban_pre_complete` only, no `kanban_task_promoted` or equivalent. That question is still fair to
  ask narrowly.
- **#58549** — the PR title/body describe `api_backend`, `requires_hermes`/`requires_plugins`/
  `requires_packages`, and `ctx.data_dir`, matching the original draft's framing. **The actual diff
  does not match:** `gh pr view ... --json files` shows the only changed file is
  `docs/rfcs/plugin-context-injection-hooks.md`, and `gh pr diff 58549` confirms that file is a
  system-prompt/environment-hints injection design (`ctx.register_system_prompt_section()`, a
  `build_environment_hints` hook) — a `grep` for `data_dir`, `api_backend`, `requires_hermes`,
  `requires_plugins`, and `requires_packages` across the diff returns zero matches. This looks like a
  stale PR description carried over from a branch rename/rebase, not a content problem with the
  proposal actually attached to #58549. The original draft comment's specific claims about
  `ctx.data_dir` do not describe what is currently attached to this PR and have been removed; the
  posted comment now asks maintainers to confirm which RFC #58549 is instead of asserting content that
  isn't there.
- **#58548** — diff verified against its title/body: the four hooks described
  (`kanban_worker_spawned`, `kanban_worker_exited`, `kanban_worker_stale_claim`,
  `kanban_task_updated`) are all present in `docs/rfcs/plugin-observability-hooks.md`. No mismatch, no
  new comments. Draft comment carried forward with an AgentFlow-facts refresh (below).
- **#59775** — no new comments, no diff changes, `headRefOid` unchanged. Draft comment carried forward
  unchanged in substance.
- **AgentFlow-side facts refreshed** in every draft comment below: AgentFlow now ships `agentflowd`, a
  standalone event-driven continuation daemon (real Linux inotify on each board's `kanban.db` +
  WAL/journal/shm, timed poll as fallback) that reconciles through a durable per-board cursor and
  outbox in its own SQLite store, has rolled out live across three boards
  (`agentflow-hermes`/`warroom-os`/`oracle-lab`), and resolves ambiguous continuations by asking the
  board owner one plain-language question instead of a fixed marker grammar. Mutations back to Hermes
  Kanban still go only through `hermes kanban ... --json` under the existing fail-closed
  config-flag-plus-CLI-flag apply gate. None of this changes §3 (AgentFlow stays standalone); it
  changes how the "currently polls" framing in the original draft comments should be worded — AgentFlow
  now watches board DB file changes directly instead of relying purely on a cron-style poll loop, which
  makes the "no `created`/`promoted` hook" gap more concrete, not less.

## 1. Short problem statement

AgentFlow-Hermes is a board-driven continuation layer that sits outside `hermes-agent` and
uses Hermes Kanban as the durable source of truth. It mutates the board only through
`hermes kanban ... --json` subprocess calls, while `agentflowd` now reacts to live board DB
changes with an inotify-first watcher and durable per-board cursor/outbox in AgentFlow's own
SQLite control-plane store.

That shape works today and is intentionally low-coupling, but it exposes a few concrete
extension-surface gaps for third-party automation:

- there is no hook for task creation or dependency promotion, so external orchestrators still
  have to diff board/event state instead of receiving committed lifecycle events;
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
3. The current CLI plus board-DB-event integration is safe because it avoids import-time coupling
   to Hermes internals. Upstream should stabilize and document the generic extension surfaces
   rather than absorb AgentFlow's implementation.

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
4. **Flag the #58549 title/body-vs-diff mismatch, then support the plugin infrastructure RFC once
   confirmed.** `ctx.data_dir`, package/version declarations, and opt-in API backends would still
   reduce boilerplate and give plugin authors a sanctioned storage/lifecycle path — but the PR as it
   currently stands does not contain that proposal (see refresh note above), so the ask here is
   "which RFC is this," not "land this as described."
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
4. For #58549: the PR title/body describe `api_backend`, version/dependency declarations, and
   `ctx.data_dir`, but the current diff only contains `docs/rfcs/plugin-context-injection-hooks.md`
   (a system-prompt/environment-hints proposal). Is the infrastructure RFC (`ctx.data_dir` etc.)
   tracked on a different open PR, or did #58549 pick up the wrong branch?
5. Would a docs/tutorial PR about board-driven continuation be welcome as external-plugin guidance,
   or should that live outside the upstream docs until #58541/#58548/#58549 settle?

## 6. Draft public GitHub comments

### PR #59775 — lazy-discover plugin hooks/middleware

Draft comment:

> I have a second external-consumer data point for this fix from AgentFlow-Hermes
> (standalone repo: https://github.com/no-ai-labs/agentflow-hermes).
>
> AgentFlow's current production path is `hermes kanban ... --json` for mutations plus its own
> event-driven daemon (`agentflowd`) reading board DB changes directly, so it is not blocked by
> this PR today. But the upstream fit audit found that any future AgentFlow hook-based integration
> running in gateway context would depend on this exact behavior: module-level `invoke_hook()` /
> `invoke_middleware()` must discover user plugins on cold start, or registered hooks can silently
> no-op while appearing installed.
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
> into Hermes core. It uses Hermes Kanban as board-owned state; mutations go through
> `hermes kanban ... --json`, and its own daemon (`agentflowd`) watches each board's `kanban.db`
> directly (inotify-driven, durable per-board cursor) for everything else. That is safe and
> decoupled, but task creation and dependency-driven readiness are exactly where that still means
> diffing DB state ourselves instead of reacting to a committed event: external orchestrators want
> to create follow-up cards, wait for parent completion, and ACK promotion without a poll/diff step.
>
> The audit is here:
> https://github.com/no-ai-labs/agentflow-hermes/blob/main/docs/upstream-fit-audit.md
>
> `kanban_task_created` and `kanban_task_unblocked` would remove that diffing step for two of the
> three transitions we care about. One question the PR doesn't currently cover: would maintainers
> consider a separate observer-only `kanban_task_promoted` hook for dependency-driven
> `todo -> ready` promotion, or is the intent that external plugins continue to observe promotion via
> the append-only `task_events` table?

### PR #58548 — plugin observability hooks RFC

Draft comment:

> This RFC matches a real pain point we hit in AgentFlow-Hermes:
> https://github.com/no-ai-labs/agentflow-hermes
>
> AgentFlow is a standalone board-driven orchestrator. It does not import Hermes internals; it uses
> `hermes kanban ... --json` for mutations and treats the Kanban board as owned state. That choice
> is deliberate and aligns with the "narrow core, capability at the edges" model, but it means
> worker-exit, stale-claim, and task-update visibility currently come from our own daemon
> (`agentflowd`) diffing board DB state on every wake rather than reacting to a committed event.
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

> Quick heads-up rather than a design comment: the title/description here describe `api_backend`,
> version/dependency declarations, and `ctx.data_dir`, but the diff currently attached to this PR is
> only `docs/rfcs/plugin-context-injection-hooks.md` — a system-prompt/environment-hints proposal
> (`ctx.register_system_prompt_section()`, a `build_environment_hints` hook). I don't see
> `api_backend`, `requires_hermes`, `requires_plugins`, `requires_packages`, or `data_dir` anywhere
> in the current diff, so it looks like the description may be stale from a branch rename/rebase.
>
> We (AgentFlow-Hermes, a standalone Hermes plugin/CLI: https://github.com/no-ai-labs/agentflow-hermes)
> do have a concrete data point for the plugin-infrastructure proposal as described in the title —
> we currently compose our own storage under the Hermes home because there's no documented
> plugin-owned data directory — but we didn't want to comment on `ctx.data_dir` specifics against a
> PR whose diff doesn't contain that proposal. Is the infrastructure RFC tracked on a different open
> PR we should look at instead, or is this PR mid-update?

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
2. #58541 second: lifecycle hooks; ask only the still-open promoted-event scope question (testing
   checklist and tool-layer/#42692 relationship are already answered in-thread — do not re-ask).
3. #58548 third: observability RFC; frame as lower-latency replacement for polling, not telemetry.
4. #58549 fourth: flag the title/body-vs-diff mismatch and ask which RFC this PR is, rather than
   commenting on `ctx.data_dir` design as if it were in the current diff.
5. After maintainer response, open or draft the docs/tutorial PR if welcome.

## 9. Posting packet (2026-07-15 refresh)

Link check: all four target PR URLs and all three cited AgentFlow-Hermes doc/repo URLs returned
HTTP 200 via `urllib.request.urlopen` at both 2026-07-15T09:15Z and the 09:39Z re-check; no
non-200 responses observed. The
checked URLs were:

- https://github.com/NousResearch/hermes-agent/pull/59775
- https://github.com/NousResearch/hermes-agent/pull/58541
- https://github.com/NousResearch/hermes-agent/pull/58548
- https://github.com/NousResearch/hermes-agent/pull/58549
- https://github.com/no-ai-labs/agentflow-hermes
- https://github.com/no-ai-labs/agentflow-hermes/blob/main/docs/upstream-fit-audit.md
- https://github.com/no-ai-labs/agentflow-hermes/blob/main/docs/upstream-pr-59775-verification.md

| # | Target URL | safe_to_post | Notes |
|---|---|---|---|
| 1 | https://github.com/NousResearch/hermes-agent/pull/59775 | **true** | No new PR comments since 2026-07-09; content re-verified against current diff/body, unchanged. |
| 2 | https://github.com/NousResearch/hermes-agent/pull/58541 | **true** | Comment revised to drop nothing already asked (testing checklist / #42692 relationship were never part of our draft) and to reflect `agentflowd`; the one open question (dependency-promotion hook) is confirmed still unanswered by re-reading the current diff. Note for the poster: PR flipped to `mergeable: CONFLICTING` against base since 2026-07-09 — does not block commenting, worth a private mention to the reviewer if relevant. |
| 3 | https://github.com/NousResearch/hermes-agent/pull/58548 | **true** | Diff verified to match title/body (all four named hooks present in `docs/rfcs/plugin-observability-hooks.md`); comment refreshed with `agentflowd` framing only. |
| 4 | https://github.com/NousResearch/hermes-agent/pull/58549 | **true, but scope narrowed** | Original draft asserted `ctx.data_dir` specifics that are not present in the current diff (only `docs/rfcs/plugin-context-injection-hooks.md`, a context-injection proposal, is attached). Rewritten to report the mismatch and ask which PR/branch actually carries the infrastructure RFC, rather than reviewing content that isn't there. Safe to post as a factual heads-up; would not be safe to post the original `ctx.data_dir`-specific draft. |

Global `safe_to_post`: **true** for all four comments as rewritten above, conditional on: (a) a
human reviewer re-running the link check and `gh pr view`/`gh pr diff` snapshot immediately before
posting, since PR state (especially #58541's mergeability and #58549's attached diff) can change
between this refresh and actual posting; (b) no posting until the Hermes supervisor has reviewed
this file, per the task instruction not to post automatically.
