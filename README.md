# AgentFlow for Hermes (v0.2.0)

AgentFlow-Hermes turns a `GO`/`BLOCK` signal on a Kanban task into the next slice's task graph (implement → review → fan-in, or a channel-specific variant) — automatically, durably, and without reading chat history.

**In one sentence:** you keep saying `GO` on a board task, AgentFlow keeps handing the next slice to a worker + reviewer for you.

By default every command is dry-run/request-only: nothing is written to a board, nothing is sent live, nothing is released, until you explicitly arm two separate switches (a config flag *and* a CLI flag).

## 한국어 요약

AgentFlow-Hermes는 Kanban 보드 위 `GO`/`BLOCK` 신호를 보고 다음 작업(구현 → 리뷰 → fan-in)을 자동으로 이어 붙이는 CLI + Hermes 플러그인입니다. 채팅 기록을 읽지 않고, 보드 상태만을 기준으로 동작합니다. 기본은 항상 dry-run/request-only이며, 실제 쓰기는 config의 `apply_mode: true`와 CLI의 `--apply`가 **둘 다** 있어야 실행됩니다.

## Use cases

| I want to... | Use this | Notes |
| --- | --- | --- |
| Have a worker implement something and a reviewer check it | `roadmap init-config` with the default preset, then `roadmap promote` | Default template is `impl -> review -> fanin` |
| Have a final `GO` create the next slice automatically | `roadmap promote` (manual) or `roadmap watch` + `register-watchdog` (polled) | Reads board state only, never chat history |
| Do this in another channel/board | `roadmap init-config --board <name> --origin '<lane>'` | One config per board; no cross-board writes (`same_board_only`) |
| Get a research-style evidence workflow | `roadmap init-config --template-preset research-loop` | Graph shape: `scout -> evidence -> scorecard -> review -> brief` |
| Publish a release only after an explicit reviewed GO | `release github --summary-file <file> --config <release.json>` | Fails closed without `Verdict: GO` + `Release-Approved: true` |
| Test any of this safely, no side effects | Run any command above without `--apply` | Request-only by default; `--apply` alone is a no-op without the matching config flag |

## Quick start: zero to dry-run smoke

```bash
# 1. Install into the same Python environment that runs Hermes/gateway
uv pip install 'agentflow-hermes @ git+https://github.com/no-ai-labs/agentflow-hermes.git@v0.2.0'
hermes plugins install no-ai-labs/agentflow-hermes#plugins/hermes-agentflow
hermes plugins enable agentflow

# 2. Sanity-check the environment
agentflow-hermes init
agentflow-hermes doctor

# 3. Scaffold a request-only config for your board/channel
agentflow-hermes roadmap init-config \
  --output agentflow-roadmap.yaml \
  --board my-board --origin 'Discord Devhub / #my-channel' \
  --transition 'm1->m2.impl_review_fanin' --from m1 --to m2

# 4. Dry-run smoke: no board write, no --apply
agentflow-hermes roadmap promote --config agentflow-roadmap.yaml --task <some-final-go-task-id>
```

Step 4 is safe to run as many times as you like — it proposes a plan and stops. Nothing is written until the config has `apply_mode: true` *and* you add `--apply`.

## What AgentFlow does *not* do

- It does not scrape or read Discord channel history — the only input is board state (`GO`/`BLOCK`) that Hermes/Kanban already surfaces.
- It does not send live messages by default — no `send_message` dispatch unless explicitly armed.
- It does not write to a board unless `apply_mode: true` in the config *and* `--apply` on the CLI are both present.
- It does not write across boards — promotion only creates tasks on the same board as the source task.
- It does not touch `systemctl` or create cron units — watchdog registration only reads/writes a local JSON registry file.
- It does not create a GitHub release unless a release config explicitly enables it and the summary carries an explicit reviewed `Verdict: GO` + `Release-Approved: true`.

## Core capabilities

- **CLI/plugin split**: `agentflow-hermes` is a standalone, testable engine CLI; the Hermes plugin is a thin adapter exposing the same operations as Hermes tools (`agentflow_enqueue`, `agentflow_status`, etc.).
- **`init` / `doctor` / `status`**: bootstrap local state and check environment health before you rely on the CLI.
- **Roadmap GO autopromoter** (`roadmap promote` / `roadmap watch`): request-only by default; board writes require both `apply_mode: true` in the config *and* `--apply` on the command line. A receipts ledger prevents duplicate task creation across repeated runs.
- **Watchdog registry**: `roadmap register-watchdog` / `unregister-watchdog` track which configs a cron/watchdog process should poll, across any number of boards/channels.
- **Channel template presets**: pick a task-graph shape per channel — default `impl-review-fanin`, `research-loop`, or `shaman-loop`.
- **GitHub release trigger (M20)**: a bounded, dry-run-first trigger that can turn a final reviewed `GO` summary into a tag + push + `gh release create`. Not wired into any default-on automation — see below.

## Examples

Local CLI development loop:

```bash
uv run agentflow-hermes init
uv run agentflow-hermes doctor
uv run agentflow-hermes status
uv run agentflow-hermes enqueue --title "Review PR" --target "discord:#review" --origin-return "discord:#hermes-main"
```

Channel template presets (`--template-preset` on `roadmap init-config`):

```bash
# Research-style evidence workflow: scout / evidence / scorecard / review / brief
agentflow-hermes roadmap init-config --output research-roadmap.yaml \
  --board research --origin 'Discord Devhub / #research' \
  --template-preset research-loop \
  --transition 'r1->r2.impl_review_fanin' --from r1 --to r2

# Design/impl/e2e/review workflow
agentflow-hermes roadmap init-config --output shaman-roadmap.yaml \
  --board shaman --origin 'Discord Devhub / #shaman' \
  --template-preset shaman-loop \
  --transition 's1->s2.impl_review_fanin' --from s1 --to s2 \
  --impl-assignee ccsupervisor --review-assignee ccreviewer

# Default worker + reviewer workflow (no --template-preset needed)
agentflow-hermes roadmap init-config --output agentflow-roadmap.yaml \
  --board agentflow-hermes --origin 'Discord Devhub / #hermes-main' \
  --transition 'm16->m17.impl_review_fanin' --from m16 --to m17
```

After scaffolding, smoke each config with `roadmap promote` (request-only, no `--apply`) before arming writes with `apply_mode: true` + `--apply`.

Register a config so a cron/watchdog process picks it up automatically:

```bash
agentflow-hermes roadmap register-watchdog \
  --config agentflow-roadmap.yaml \
  --registry ~/.hermes/agentflow/roadmap-watchdog-configs.json
```

### GitHub release trigger (M20) — bounded, not default automation

`agentflow-hermes release github` is a request-only-by-default trigger for turning a final reviewed `GO` summary into a `git tag` + `git push` + `gh release create`. It is **not** wired into any live default-on path — you must supply a config with `release_actions_enabled: true` and `apply_mode: true`, *and* pass `--apply`, before anything runs.

```bash
# Dry-run (default): reads the summary, evaluates the gates, proposes a plan.
# No --config at all => release actions are disabled => always a noop.
agentflow-hermes release github --summary-file final-go.txt --config release.json

# Apply requires BOTH config.apply_mode: true AND --apply on the CLI.
agentflow-hermes release github --summary-file final-go.txt --config release.json \
  --apply --receipts-file .agentflow-release-receipts.json
```

Safety gates (all fail closed): `release_actions_enabled` master switch, explicit `Verdict: GO` + required markers, an `allowed_actions` allowlist, explicit `Release-Approved: true`, version-pattern validation, two-layer duplicate protection (local receipts ledger + live `git tag`/`gh release view` check), and no receipt written on partial failure. See `src/agentflow_hermes/release_action.py` for the full gate list.

## Terminology glossary

| Term | Meaning |
| --- | --- |
| **Source final GO task** | The board task carrying an explicit `Verdict: GO` and continuation markers that AgentFlow reads to decide the next slice. |
| **Transition** | A named config entry (e.g. `m16->m17.impl_review_fanin`) mapping a `from` slice to a `to` slice, with its template preset and policy refs. |
| **Next slice** | The `to` slice a transition promotes into — the identifier for the upcoming unit of work. |
| **`apply_mode`** | The config-level kill switch for board writes. Must be `true` in the config *and* paired with `--apply` on the CLI before anything is written; either one alone is a no-op. |
| **Receipts file** | A local JSON ledger keyed by idempotency key; re-running `promote`/`watch` over the same task creates zero duplicate tasks. |
| **Watchdog registry** | A JSON file (`roadmap register-watchdog`/`unregister-watchdog`) listing which configs a cron/watchdog process should poll, across boards/channels. |
| **Final ACK** | The `[JOB ACK]` block a completed graph reports back to the task's own `origin`/`return_to` lane, closing the loop. |

## Why it exists

Before AgentFlow, a human had to notice a `GO`/`BLOCK` message on a board task and manually create the next slice's tasks — implementation, review, fan-in — by hand, every time, for every channel. That doesn't survive process restarts, context compaction, or someone being offline.

AgentFlow replaces that manual step with durable, board-driven continuation:

- The board (not a chat transcript or an agent's memory) is the source of truth for "what's the next slice."
- A receipts ledger makes promotion idempotent — re-running the same watch/promote command over the same task creates zero duplicate tasks.
- Work survives restarts and context compaction because state lives in the config + receipts file, not in an agent's context window.
- The final ACK is returned to the task's own `origin`/`return_to` lane, not wherever the promotion happened to run.

## Safety defaults

- No live `send_message` dispatch by default
- No Discord channel scraping — only board `GO`/`BLOCK` events drive continuation
- No monkeypatching Hermes core modules
- Job payloads store metadata and payload refs, not raw private transcripts or secrets
- ACKs use explicit `[JOB ACK]` blocks
- Writes (board promotion, release actions) require a config-level flag *and* a CLI flag together — never one alone

## Why a plugin + CLI?

The plugin makes AgentFlow feel native inside Hermes (`agentflow_enqueue`, `agentflow_status`, etc.). The CLI keeps the control-plane engine reusable and testable outside Hermes.

`uv tool install` is useful for isolated CLI commands, but it does not make `agentflow_hermes` importable by the Hermes plugin interpreter. If the engine package is missing, the plugin degrades to `agentflow_doctor` guidance instead of owning business logic itself.
