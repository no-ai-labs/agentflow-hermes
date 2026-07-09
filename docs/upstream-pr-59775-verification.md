# Upstream Verification: NousResearch/hermes-agent PR #59775

- Kanban task: `t_7f4e6377` (M23A — verify Hermes PR #59775 hook delivery relevance)
- Origin/return_to: Discord Devhub / #hermes-main
- Project: AgentFlow-Hermes upstream path M23A

## Summary

PR [#59775](https://github.com/NousResearch/hermes-agent/pull/59775) fixes a real bug where `invoke_hook()` and `invoke_middleware()` in `hermes_cli/plugins.py` could silently no-op in a cold-start gateway process because plugins were never discovered before dispatch. Independent regression testing against a temp clone confirms the bug exists on `main` and is fixed by the PR. AgentFlow should treat this PR as a correctness dependency for any hook-based integration that runs in gateway context, but it does not block the current CLI/polling AgentFlow path.

## PR status / checks

- URL: https://github.com/NousResearch/hermes-agent/pull/59775
- State: **OPEN**, draft: false, mergeable: **UNKNOWN**, reviewDecision: empty
- Title: `fix: lazy-discover plugins in invoke_hook / invoke_middleware so user hooks fire in gateway context`
- Head: `feat/lazy-discover-plugin-hooks` → base: `main`; author: `magicbluesmoke`
- Status check rollup from `gh`: empty (`checks: []`) — no GitHub checks reported by this query
- Commits: `a49a58b...`, `e6d74e5...`, final `4fc01ffbc8c8c7ca3e9092ec578fabc548f48cfd`
- Files changed: `hermes_cli/kanban_db.py` (+7/-1), `hermes_cli/plugins.py` (+18/-2), `tests/hermes_cli/test_plugins.py` (+77/-0)

## Bug / fix claim

- An earlier commit patched the symptom directly, adding an explicit `discover_plugins()` call inside `_fire_kanban_lifecycle_hook`.
- The final state generalizes the fix in `hermes_cli/plugins.py`:
  - `invoke_hook()` now fetches `pm = get_plugin_manager()`, lazily calls `pm.discover_and_load()` when `getattr(pm, '_discovered', True)` is false, then dispatches `pm.invoke_hook(...)`.
  - `invoke_middleware()` gets the same lazy-discover guard before `pm.invoke_middleware(...)`.
- New tests in `tests/hermes_cli/test_plugins.py` cover the module-level `invoke_hook` and `invoke_middleware` cold-start paths (i.e., calling them without any prior explicit discovery call).

## Independent verification

Performed in a temp clone, no live Hermes checkout was mutated:

- Temp clone: `/home/duckran/tmp/hermes-pr59775-verify`, with `origin/main` and PR ref `pull/59775/head:pr-59775` fetched from GitHub.
- Regression script: created a temp `HERMES_HOME`, enabled a user plugin `lazy_probe`, then called module-level `hermes_cli.plugins.invoke_hook('kanban_task_claimed', ...)` and `invoke_middleware('llm_request', ...)` **without** explicitly calling `discover_plugins()` first — simulating a cold-start gateway process.

Results:

| SHA | before discovered | after discovered | hook_result | middleware_result | hook fired | middleware fired |
|---|---|---|---|---|---|---|
| `main` @ `3a1a3c7e67` | False | False | `[]` | `[]` | No | No |
| PR @ `4fc01ffbc8` | False | True | `['hook-ok']` | `[{'middleware': 'ok'}]` | Yes | Yes |

This confirms the bug on `main` (hooks/middleware silently absent on cold start) and confirms the fix on the PR head.

- PR-added targeted tests were also run in the temp clone:
  - Command: `uv run pytest tests/hermes_cli/test_plugins.py -q -k 'module_level_invoke_hook_lazily_discovers_plugins or module_level_invoke_middleware_lazily_discovers' -o 'addopts='`
  - Result: `2 passed, 100 deselected in 0.56s`

## Conclusion for AgentFlow

Treat PR #59775 as a **real correctness dependency/blocker** for any hook-based AgentFlow integration that runs in a Hermes gateway context, until it (or an equivalent fix) merges. Without it, `invoke_hook`/`invoke_middleware` can silently no-op on cold start, meaning AgentFlow-registered hooks/middleware would appear to be installed but never actually fire — a hard-to-detect integration failure mode.

This does **not** block AgentFlow's current CLI/polling integration path, which does not rely on gateway-context hook dispatch.

## Residual risks

- No GitHub status checks were reported by `gh` for this PR (`checks: []`) — CI signal, if any exists, was not observed by this verification.
- No full Hermes test suite was run against the PR; only the two PR-added targeted tests were executed.
- No live gateway end-to-end or process-restart test was performed, by design (verification was scoped to a temp clone, no live Hermes mutation).
- Hook coverage in this verification is limited to the existing hook/event names used in the regression script (`kanban_task_claimed`, `llm_request`); other hook/event names were not exercised.
