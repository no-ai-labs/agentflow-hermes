# Upstream Fit Audit: AgentFlow-Hermes vs. NousResearch/hermes-agent

Status: draft evidence audit. Written 2026-07-09 against a local read-only checkout of
`/home/duckran/.hermes/hermes-agent` (origin/main) and `gh`-fetched PR state from
`NousResearch/hermes-agent`. Not a proposal to merge; see §6 for the conclusion.

## 1. Hermes philosophy/rubric (source: hermes-agent `AGENTS.md`)

Four rules govern whether something belongs upstream:

- **Narrow waist, capability at the edges.** "The core is a narrow waist; capability lives
  at the edges. Every model tool we add is sent on every API call, so the bar for a new
  *core* tool is high. Most new capability should arrive as a CLI command + skill, a
  service-gated tool, or a plugin — not as core surface." The Footprint Ladder (AGENTS.md)
  ranks preference: extend existing code → CLI command + skill → service-gated tool
  (`check_fn`) → plugin → MCP server → new core tool (last resort).
- **Third-party products stay out of the core tree.** "Observability backends, vendor SaaS
  integrations, analytics dashboards, and similar 'someone else's product' plugins do NOT
  land under `plugins/` in this repo... Ship them as a standalone plugin repo users install
  into `~/.hermes/plugins/` (or via a pip entry point)... This is a coupling-and-maintenance
  decision, not a quality bar." This is explicit, load-bearing policy for AgentFlow's
  packaging decision (see §6).
- **No speculative hooks — but a stated external consumer counts.** "Hooks, callbacks, or
  extension points with no concrete consumer [are speculative]... A hook is NOT speculative
  if a contributor has a real, stated use case — even if the consumer ships separately."
  This is the opening AgentFlow needs: a hook PR can cite AgentFlow as the real consumer
  without AgentFlow itself living in-tree.
- **E2E validation and cache stability are hard requirements.** "For anything touching
  resolution chains, config propagation, security boundaries, remote backends, or
  file/network I/O, exercise the real path with real imports against a temp `HERMES_HOME`.
  Mocks hide integration bugs." And: "Preserve prompt caching, strict message role
  alternation..., and a system prompt that is byte-stable for the life of a conversation."
  Any upstream PR AgentFlow proposes must ship with E2E tests against a temp `HERMES_HOME`
  and must not touch cache-affecting code paths (e.g. `pre_llm_call` context injection).

## 2. Current Hermes surfaces relevant to AgentFlow

### 2.1 Kanban board (`website/docs/user-guide/features/kanban.md`)

- **Durable SQLite board.** Every task/board is `~/.hermes/kanban.db` (or per-board DB under
  `~/.hermes/kanban/boards/<slug>/`), rows for `tasks`, `task_links`, `task_comments`,
  `task_events`, `task_runs`, `task_attachments`, `kanban_notify_subs`.
- **Worker tools vs. CLI split.** Model-facing tools (`kanban_show`, `kanban_complete`,
  `kanban_block`, `kanban_create`, etc.) are gated on `HERMES_KANBAN_TASK` env or an
  orchestrator profile's toolset config, and are the surface dispatcher-spawned workers use.
  Humans/scripts/cron use the `hermes kanban ...` CLI. "Both surfaces route through the same
  `kanban_db` layer, so reads see a consistent view and writes can't drift." AgentFlow uses
  **only the CLI surface** (see §4.9), never the model-tool surface — consistent with being
  an external automation/orchestration layer, not a Hermes worker profile.
- **Parent-done dependency promotion.** The dispatcher promotes `todo → ready` when all
  parent links hit `done` (`task_links`); a `promoted` event is appended to `task_events`.
  This event kind exists in the DB log but — see §2.4 — **fires no plugin hook today**.
- **Gateway dispatcher.** A loop inside the gateway process (`kanban.dispatch_in_gateway:
  true`, default interval 60s) reclaims stale/crashed claims, promotes ready tasks, and
  spawns assigned profiles. One dispatcher per install sweeps all boards per tick; a
  cross-process advisory lock keeps it singleton.
- **Board isolation is absolute.** Each board is a separate SQLite DB with its own workspace
  dir; "Linking tasks across boards is not allowed." This maps directly onto AgentFlow's
  own `same_board_only` guard (§4.5) — the upstream system already enforces the same
  invariant AgentFlow additionally asserts client-side.

### 2.2 Plugins (`website/docs/user-guide/features/plugins.md`)

- Plugins register tools (`ctx.register_tool`), hooks (`ctx.register_hook`), slash commands
  (`ctx.register_command`), CLI subcommands (`ctx.register_cli_command`), and skills
  (`ctx.register_skill`); pip distribution via
  `[project.entry-points."hermes_agent.plugins"]`.
- **Opt-in by default.** "General plugins and user-installed backends are disabled by
  default... nothing with hooks or tools loads until you add the plugin's name to
  `plugins.enabled`." AgentFlow, as a general third-party plugin, requires this explicit
  opt-in from any Hermes operator who installs it — there is no bundled/exempt path.
- **No `ctx.data_dir` exists.** Verified by grep across the docs and code: plugins compose
  their own storage path via `get_hermes_home() / "my-provider"`, there is no dedicated
  accessor. (If any AgentFlow design doc assumed `ctx.data_dir`, correct that assumption.)
- **No RFC process is documented.** `AGENTS.md`'s rubric functions as the de facto design
  bar; there's no separate RFC doc referenced from `plugins.md` or `kanban.md`. (The PRs
  titled "RFC: ..." in §3 are informal design-doc PRs, not an institutional RFC track.)
- **The hooks table in `plugins.md` is non-exhaustive.** It lists 10 general hooks
  (`pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call`, session
  start/end/finalize/reset, `subagent_stop`, `pre_gateway_dispatch`) but omits the
  kanban-specific lifecycle hooks entirely — those are documented only in code
  (`hermes_cli/plugins.py` docstring).

### 2.3 `tools/kanban_tools.py` — model tool surface

Registers `kanban_show/list/complete/block/heartbeat/comment/create/link/unblock` via the
same `registry.register(name=..., toolset=..., schema=..., handler=..., check_fn=...)`
pattern a plugin uses for `ctx.register_tool`. Enforces per-worker task ownership
(`_enforce_worker_task_ownership`) so a dispatcher-spawned worker can only mutate its own
task id. Not directly relevant to AgentFlow, which never runs as a Hermes worker profile.

### 2.4 `hermes_cli/kanban_db.py` — kernel and hook firing points

This is the single source of truth all three surfaces (CLI, model tools, dashboard REST)
write through. The load-bearing fact for this audit:

- `_fire_kanban_lifecycle_hook` (lines ~140–162) fires **only three** lifecycle events —
  `kanban_task_claimed` (dispatcher process, pre-spawn), `kanban_task_completed` (worker
  process), `kanban_task_blocked` (worker or driving process) — always **after** the write
  transaction commits, fire-and-forget, with exceptions swallowed. Hooks cannot veto or
  delay a transition. Common kwargs: `task_id`, `board`, `assignee`, `run_id`,
  `profile_name` (+ `summary` for completed, `reason` for blocked).
- **`created` and `promoted` are DB event kinds with no matching hook.** `task_events` rows
  of kind `created`/`promoted` are written, but no `_fire_kanban_lifecycle_hook` call
  accompanies either. A plugin that wants to react the instant a task is created, or the
  instant dependency-promotion happens, has to poll `task_events` — same mechanism the
  built-in gateway notifier and dashboard WebSocket already use (cursor-based polling of
  the append-only event log). This is the sanctioned fallback AgentFlow already uses today.
- `VALID_HOOKS` in `hermes_cli/plugins.py` is a closed, enumerated set — a plugin cannot
  invent new hook names client-side. Any new kanban event hook requires an upstream PR.

### 2.5 `gateway/kanban_watchers.py` and `plugins/kanban/dashboard/plugin_api.py`

- `gateway/kanban_watchers.py` is entirely internal (underscore-prefixed mixin methods) —
  no plugin API surface of its own; it's the reference implementation of the
  poll-`task_events`-with-a-cursor pattern.
- `plugins/kanban/dashboard/plugin_api.py` is the bundled dashboard's REST/WS backend
  (2454 lines, not the ~700 the public doc claims — a doc/code drift worth noting, not an
  AgentFlow concern). Its docstring states plugin routes go through the same session-token
  auth middleware as core routes (`web_server.py` `_plugin_api_runtime_gate`) —
  contradicting `kanban.md`'s stale claim that `/api/plugins/` is unauthenticated by
  design. AgentFlow doesn't touch this REST surface directly, but if it ever builds a
  companion dashboard plugin, the code (not the currently-published doc) is ground truth.

## 3. PR evidence

All 21 referenced PRs were checked via `gh pr view --repo NousResearch/hermes-agent`.
**Every one of the 21 is currently OPEN — none merged, none closed** (checked 2026-07-09).
This matters directly: none of the plugin-hook/RFC groundwork below is landed capability
today; all of it is proposed and pending review.

| PR | Title | State | Relevance to AgentFlow |
|---|---|---|---|
| [#61510](https://github.com/NousResearch/hermes-agent/pull/61510) | fix(kanban): unpack goal judge wait result | OPEN | Goal-mode completion correctness; not AgentFlow-facing. |
| [#61412](https://github.com/NousResearch/hermes-agent/pull/61412) | fix: serialize Hermes updates and batch gateway restarts | OPEN | Ops hardening; not AgentFlow-facing. |
| **[#58548](https://github.com/NousResearch/hermes-agent/pull/58548)** | **RFC: Plugin observability hooks** (worker lifecycle, task mutation) | OPEN | **Directly on point.** Proposes `kanban_worker_spawned`, `kanban_worker_exited`, `kanban_worker_stale_claim`, `kanban_task_updated` hooks so plugins react to events instead of polling. PR body explicitly cites a third-party plugin's needs (design doc at `docs/rfcs/plugin-observability-hooks.md`). |
| **[#58549](https://github.com/NousResearch/hermes-agent/pull/58549)** | **RFC: Plugin infrastructure improvements** (`api_backend`, deps, `ctx.data_dir`) | OPEN | **Directly on point.** Proposes opt-in `api_backend` in `plugin.yaml`, `requires_hermes`/`requires_plugins`/`requires_packages` version declarations, and a plugin-owned `ctx.data_dir`. Confirms `ctx.data_dir` does not exist today (§2.2) and that this is a known, proposed gap — not overlooked. |
| **[#58541](https://github.com/NousResearch/hermes-agent/pull/58541)** | **feat: kanban lifecycle hooks** (`pre_complete`, `unblocked`, `created`) | OPEN | **Directly on point.** Adds `kanban_task_created`, `kanban_task_unblocked` (observer, post-commit) and `kanban_pre_complete` (governance hook, can veto with `{"action":"block",...}`). Would close the `created` hook gap identified in §2.4. PR body cites the kanban-advanced plugin as concrete consumer for each hook, references umbrella issue #35986. |
| [#56066](https://github.com/NousResearch/hermes-agent/pull/56066) | feat(kanban): `kanban_dispatch_tick` plugin hook | OPEN | Adds a hook firing on every `dispatch_once()` exit (locked/skipped/idle) with per-tick counts — deliberately hook-only, no built-in telemetry sink, citing AGENTS.md's "no third-party observability backends in-tree" policy. Useful precedent for how a dispatch-visibility PR should be scoped. |
| [#54871](https://github.com/NousResearch/hermes-agent/pull/54871) | feat(kanban): `inject_as_turn` notification flag | OPEN | Adds a DB column + CLI flag so terminal-state notifications can trigger an active agent turn instead of a silent `adapter.send()`. Relevant precedent if AgentFlow ever wants Hermes-side ACK delivery to trigger a live turn rather than a passive message. |
| [#52424](https://github.com/NousResearch/hermes-agent/pull/52424) | feat(kanban): direct Claude Code worker lane | OPEN | Adds a `claude-code` assignee lane to dispatch. Not AgentFlow-facing (AgentFlow doesn't run as a worker profile). |
| [#58137](https://github.com/NousResearch/hermes-agent/pull/58137) | feat(desktop): Kanban board UI over the existing plugin API | OPEN | Confirms the dashboard REST surface (§2.5) is being built on as-is with zero backend changes — evidence that the existing `hermes kanban ... --json` / REST surface is considered stable enough to build a full desktop UI on. |
| [#59812](https://github.com/NousResearch/hermes-agent/pull/59812) | fix: pause kanban dispatcher during gateway drain | OPEN | Dispatcher hardening; not AgentFlow-facing. |
| [#59937](https://github.com/NousResearch/hermes-agent/pull/59937) | fix(kanban): preserve completion artifact evidence | OPEN | Touches `hermes_cli/kanban_db.py`, `tools/kanban_tools.py`, `gateway/kanban_watchers.py` — durability fix for claimed-artifact paths. Relevant background if AgentFlow ever consumes completion artifacts, but not a dependency today. |
| [#61229](https://github.com/NousResearch/hermes-agent/pull/61229) | fix(kanban): honor `HERMES_KANBAN_GOAL_MAX_TURNS` | OPEN | Goal-mode turn budget bug; not AgentFlow-facing. |
| [#61230](https://github.com/NousResearch/hermes-agent/pull/61230) | fix(kanban_tools): coerce unknown `kanban_create` assignee to `default` | OPEN | Fixes a stuck-forever-in-`ready` failure mode for bad assignee names. Relevant: AgentFlow's `RealKanbanGraphAdapter` (§4.9) passes `--assignee` on every `kanban create` call — this fix reduces the blast radius of a typo'd assignee on AgentFlow's side too. |
| [#61295](https://github.com/NousResearch/hermes-agent/pull/61295) | feat(kanban): safe CDP tab pruning after completion | OPEN | Browser-tab hygiene; not AgentFlow-facing. |
| [#61516](https://github.com/NousResearch/hermes-agent/pull/61516) | feat(cron): null scheduler provider for HA standby | OPEN | Not AgentFlow-facing (AgentFlow's own cron/bridge logic is client-side). |
| [#61525](https://github.com/NousResearch/hermes-agent/pull/61525) | fix(cron): non-dict schedule no longer freezes scheduler | OPEN | Cron robustness; not AgentFlow-facing. |
| [#61581](https://github.com/NousResearch/hermes-agent/pull/61581) | fix(cron): malformed `next_run_at` no longer freezes scheduler | OPEN | Same family as 61525; not AgentFlow-facing. |
| [#59775](https://github.com/NousResearch/hermes-agent/pull/59775) | fix: lazy-discover plugins in `invoke_hook`/`invoke_middleware` | OPEN | Root-causes a bug where **user-registered hooks silently never fired in the gateway** because `PluginManager` started undiscovered. If unmerged, any AgentFlow-adjacent hook subscription running in gateway context could silently no-op today — worth flagging as a correctness dependency for any future hook-based AgentFlow integration, not just a nice-to-have. |
| [#60653](https://github.com/NousResearch/hermes-agent/pull/60653) | fix(kanban): read-only `immutable=1` integrity_check probe | OPEN | Fixes nightly SQLite index corruption from a WAL checkpoint race. Not AgentFlow-facing directly, but relevant: AgentFlow's CLI shell-outs read via `hermes kanban show/list --json`, which depend on the same DB not being corrupted. |
| [#57063](https://github.com/NousResearch/hermes-agent/pull/57063) | fix(toolsets): platform plugins silently get zero tools; config-set list-as-string bug | OPEN | General plugin-system robustness; not AgentFlow-facing. |
| [#49593](https://github.com/NousResearch/hermes-agent/pull/49593) | feat(gateway): active-wake operator receipts | OPEN | Adds durable ACK/active-wake receipt tracking (`ack_active_wake`, `ack_operator_receipt`) for kanban terminal ACKs. Conceptually parallel to AgentFlow's own receipts/idempotency ledger (§4.4) and ACK block parser (§4.7) — evidence that upstream is independently converging on a similar "durable receipt for every ACK-relevant transition" pattern. Draft, explicitly superseding three smaller PRs pending a maintainer decision on the split — a cautionary precedent for how NOT to structure a large PR (split into small reviewable pieces).|

Two PRs are the ones to actually track for a future AgentFlow-facing upstream contribution:
**#58541** (kanban lifecycle hooks including `created`) and **#58548**/**#58549** (the two
RFCs). If/when these land, the `created`/`promoted` polling workaround in AgentFlow's
kanban bridge (§2.4, §4.9) becomes unnecessary, and `ctx.data_dir` becomes available for
any future AgentFlow plugin-side state.

## 4. AgentFlow component classification

Legend: **A** external plugin/repo only · **B** upstreamable generic Hermes primitive ·
**C** upstream docs/tutorial contribution · **D** needs upstream API/hook/extension point
first · **E** user/local policy, not upstream.

| Component | Files | Class | Why |
|---|---|---|---|
| CLI + thin plugin-adapter split | `src/agentflow_hermes/cli.py`, `plugins/hermes-agentflow/__init__.py`, `docs/plugin-architecture.md` | **B**/**C** | The pattern — engine package owns all state/logic, plugin adapter is a thin CLI-argv translator with a hard tool-count cap, "no core monkeypatching" — is exactly the Footprint Ladder's "CLI command + skill" / "plugin" rungs done well. Worth writing up as a docs contribution (a "how to build a Hermes-integrating CLI+plugin pair" tutorial), not as code to merge. |
| Roadmap GO autopromoter (`propose_roadmap_promotion`/`apply_roadmap_promotion`) | `roadmap.py`, `roadmap_cli.py` | **A**/**D** | Deeply coupled to AgentFlow's own review-verdict/marker vocabulary (`Roadmap-Transition:`, `Next-Slice:`) and template presets. Not portable as-is. The one upstream blocker: it depends entirely on the CLI shell-out adapter (§4.9) because there is no typed Hermes Kanban client — see #58549. |
| `apply_mode` config + `--apply` CLI double-gate | `roadmap_config.py:37`, `release_action.py:60`, `roadmap_cli.py:45`, `release_cli.py:54` | **B** (strong) | Fully generic: two independent booleans (repo config + CLI flag) must both be true before any mutating adapter/runner is even *constructed* (not just before it's called). Domain-agnostic safety primitive; the repo's own `docs/plugin-architecture.md` design intent is the right shape for a short upstream "safe automation trigger" pattern doc. |
| Receipts / idempotency ledger | `store.py` (`operator_receipts`, `idempotency_keys` tables), `roadmap.py` in-memory ledgers, `release_action.py` JSON ledger | **B** (pattern) / **A**/**E** (schema) | Recurring shape across 4+ subsystems: `sha256`-derived stable key → duplicate-lookup before any mutating call → receipt recorded even on refuse/noop paths, never storing raw bodies. The *pattern* is upstreamable as guidance; the SQLite schema is AgentFlow-owned. Notably parallel to upstream's own PR #49593 (active-wake operator receipts) — evidence the idea has independent traction upstream already. |
| `same_board_only` | `roadmap_config.py:38`, `roadmap_cli.py:254,316` | **A**/**E** | Narrow, AgentFlow-specific safety guard. Redundant with (but not obviated by) the fact that Hermes boards are already hard-isolated per §2.1 — this is defense in depth on the client side, not something upstream needs to provide. |
| Template presets (`impl-review-fanin`, `research-loop`, `shaman-loop`) | `roadmap_templates.py` | **B** (engine) / **E** (content) | The role-validation/template-resolution engine (`resolve_template`, `_validate_resolved`) is generic; the three named presets encode this deployment's own Discord channel lanes (`#hermes-main`, `#research`, `#shaman`) and have no meaning outside it. Stays local. |
| ACK block parsing | `ack.py` | **A** (this format) / **B** (idiom) | The `[JOB ACK] key: value` format is AgentFlow's own worker→controller protocol, distinct from Hermes's board state and from the `Roadmap-Transition` markers. The underlying idiom — "parse explicit `Key: value` markers, fail-closed and deadletter on absence/malformed" — recurs 4+ times in this codebase and is a reasonable pattern to describe in docs, not to merge as code. |
| Remediation resolver / graph proposals | `remediation.py`, `graph_creator.py`, `bridges/kanban.py` | **A**/**D** | The "graph" is a fixed linear fix→review→final template, not a general DAG solver — domain-specific to AgentFlow's own review verdict taxonomy. The refusal taxonomy (ambiguous blockers / missing provenance / wrong verdict) is a reusable idea worth citing in a maintainer pitch, but the code itself is not portable. Depends on the same CLI-shell-out adapter as the autopromoter. |
| Real kanban adapter (`RealKanbanGraphAdapter`, `fetch_task_via_cli`, `resolve_kanban_board_client`) | `graph_creator.py:216-310`, `roadmap_cli.py:72-125`, `loop_cli.py:289-300` | **A** (glue) → reveals **D** | **This is the single most important finding of the audit.** AgentFlow has zero typed/importable dependency on hermes-agent internals — the entire integration surface is `hermes kanban {show,list,create} --json` subprocess calls, explicitly documented in code: "There is no importable Hermes board client to resolve; the actual Hermes surface is the `hermes kanban ... create ... --json` CLI." This is maximally decoupled (good — no version-coupling risk) but also means AgentFlow gets zero benefit from any hook/event system until it either (a) adopts the #58541 hooks once merged, or (b) polls `task_events` directly (already the sanctioned fallback, §2.4). |
| GitHub release trigger, fail-closed gates | `release_action.py`, `release_cli.py` | **B** (strong) | Near-generic "parse structured directive from review text → allowlist/approval/idempotency/live-duplicate-check gates → shell out via injectable runner" pipeline. Doesn't touch Hermes Kanban APIs at all (just git/gh) — the cleanest candidate of the whole set for an upstream "safe automation trigger" pattern writeup, parameterized by directive-marker names and injected CLI commands. |

## 5. Minimal upstream PR candidates

Ranked by leverage-to-size ratio; none propose merging AgentFlow itself.

1. **Support/track #58541** (kanban `created`/`unblocked`/`pre_complete` hooks). This is
   already an open, well-scoped PR with a concrete third-party consumer cited in its body.
   AgentFlow's contribution here is evidence, not new code: a short comment or companion
   note on the issue describing AgentFlow's own `created`/`promoted` polling workaround
   (§2.4, §4.9) as a second concrete consumer strengthens the "not speculative" case per
   AGENTS.md's own rubric.
2. **Support/track #58549** (`ctx.data_dir`, `api_backend`, version deps RFC). Same posture
   — cite AgentFlow's current workaround (`get_hermes_home() / "agentflow-hermes"`
   composition) as a second data point for why `ctx.data_dir` is worth landing.
3. **New, narrow: confirm and possibly help land #59775** (lazy-discover plugin hooks in
   gateway). If this genuinely fixes "user hooks silently never fire in gateway context,"
   it's a correctness dependency for *any* future AgentFlow hook subscription — worth
   independently verifying against a temp `HERMES_HOME` (per AGENTS.md's E2E rule) before
   AgentFlow designs anything hook-based on top of it.
4. **New, small, AgentFlow-authored: a docs/tutorial PR** ("Board-driven continuation: how
   an external orchestrator polls `task_events` and drives `hermes kanban` safely") —
   category C. This is low-risk, doesn't touch code, and demonstrates the sanctioned
   `--idempotency-key`-aware, JSON-parsing, fail-closed CLI-shell-out pattern AgentFlow
   already uses in `RealKanbanGraphAdapter` — useful for other plugin authors and it costs
   AgentFlow nothing to write since the pattern already exists and is tested.
5. **Not recommended as a PR right now:** anything from the `apply_mode`/idempotency-ledger
   pattern (classified B). It's a good pattern but there's no single obvious *place* to
   land it upstream (it's not a hook, not a tool, not a doc) — better surfaced via the
   maintainer pitch (§6) as a design precedent to reference, not code to submit.

## 6. Maintainer pitch outline

- **Problem.** AgentFlow-Hermes is a Discord-facing autopromotion/remediation/release
  automation layer sitting entirely outside hermes-agent, integrating only through
  `hermes kanban ... --json` CLI shell-outs (no typed API) and DB event polling (no
  lifecycle hooks for `created`/`promoted`). It works today but every read/write pays a
  subprocess-spawn + JSON-parse tax, and there's no way to react to task creation in real
  time short of polling.
- **Philosophy alignment.** AgentFlow was built CLI-first, plugin-second, with a hard cap
  on its own model-tool surface (≤8 tools) and an explicit "no core monkeypatching"
  invariant — i.e., it already follows the Footprint Ladder's "plugin, not core tool" rung
  without being told to. The pitch isn't "let us into core," it's "here's independent
  validation that your Footprint Ladder and hook-gating rules work as intended for a real,
  non-trivial external consumer."
- **Evidence from the standalone plugin.** Cite: (a) the `apply_mode` + `--apply` double
  gate as a working safety pattern for automation with real teeth (git tag/push/gh release,
  Kanban task creation) that has run without an unintended mutation; (b) the idempotency
  ledger pattern converging independently with upstream's own #49593 receipt work; (c) the
  CLI-shell-out adapter as a concrete data point that `--json` output stability matters to
  external consumers (useful if hermes-agent is considering CLI output stability
  guarantees).
- **Proposed upstream path.** Not a merge of AgentFlow. Instead: (1) back the existing
  #58541/#58548/#58549 RFCs/PRs with AgentFlow as a second cited consumer; (2) contribute a
  docs/tutorial PR on the polling pattern; (3) once #58541 lands, follow up with a small,
  separate PR (from AgentFlow, reviewed independently) that exercises the new
  `kanban_task_created` hook end-to-end against a temp `HERMES_HOME`, as the kind of E2E
  proof AGENTS.md requires before any hook is trusted.
- **Questions for maintainers.** Is #58541's scope (created/unblocked/pre_complete) final,
  or is `promoted` also in scope? Is there interest in an `hermes kanban` CLI output-schema
  stability guarantee (semver-style) given at least two external consumers (AgentFlow,
  the desktop Kanban UI's REST layer) now depend on JSON shape? Should #59775 land before
  any hook-dependent third-party plugin work is recommended to the community, given it's a
  correctness fix for hook delivery itself?

## 7. Risks and maintenance burden

- **Merging AgentFlow's code upstream would violate hermes-agent's own stated policy**
  (§1: "third-party products... do NOT land under `plugins/` in this repo... places an
  ongoing maintenance burden on us to keep them working against a fast-moving core, for a
  backend we don't own"). This isn't a judgment on AgentFlow's quality — it's explicit,
  general policy independent of any one plugin.
- **hermes-agent's kanban/plugin surface is still moving.** All 21 evidence PRs are open;
  three (#58541, #58548, #58549) are the exact primitives AgentFlow would want and none
  have landed. Building AgentFlow's core logic on any of them today would mean building on
  an unstable foundation and needing to track upstream review churn.
- **AgentFlow's CLI-shell-out integration is currently the *safer* choice, not just the
  available one.** It has zero import-time coupling to hermes-agent internals, so
  hermes-agent's internal refactors (e.g., the ongoing "god-file decomposition" evident in
  `gateway/kanban_watchers.py`'s own docstring) can't break AgentFlow silently — only
  `hermes kanban ... --json` output-shape changes can, and those are the one surface
  hermes-agent already has external consumers (the desktop UI) depending on.
- **Conclusion: AgentFlow should remain a standalone plugin/CLI first.** The upstream pitch
  should not ask to merge AgentFlow wholesale. The best upstream candidates are generic
  extension surfaces, bugs, and docs — kanban lifecycle/dispatch hooks with a concrete
  AgentFlow consumer (#58541 et al.), durable origin/ACK/injection semantics if not already
  landed (#49593), plugin `data_dir`/deps/`api_backend` support (#58549), and a tutorial
  doc for board-driven continuation. AgentFlow's own user/channel policy templates
  (`research-loop`, `shaman-loop`) and release automation (`release_action.py`) stay
  external and local — they encode this deployment's own policy, not a Hermes primitive.
