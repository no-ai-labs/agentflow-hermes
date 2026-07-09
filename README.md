# AgentFlow for Hermes (v0.2.0)

AgentFlow-Hermes is a Hermes add-on/CLI that turns board `GO`/`BLOCK` signals into durable, multi-agent task graphs — with ACK receipts, review gates, and automatic roadmap continuation. It watches Kanban events; it does not scrape Discord channel history.

## 한국어 요약

AgentFlow-Hermes는 Hermes Agent 위에서 동작하는 **작업 흐름 제어 플러그인 + CLI**입니다. 사람이 Discord/Kanban 완료 메시지를 보고 다음 작업을 손으로 이어 붙이던 과정을, Kanban 보드 이벤트와 설정 파일을 기준으로 자동화합니다.

핵심 목적은 간단합니다:

- `GO`가 뜬 작업에서 다음 구현/리뷰/fan-in 그래프를 안정적으로 생성합니다.
- `BLOCK`/리뷰 결과/최종 ACK를 durable하게 남겨서 재시작이나 context compaction 뒤에도 흐름이 끊기지 않게 합니다.
- 같은 이벤트를 여러 번 처리해도 중복 작업을 만들지 않도록 receipts/idempotency ledger를 사용합니다.
- `#research`, `#shaman`, `#hermes-main`처럼 채널/보드마다 다른 작업 템플릿을 씁니다.
- 기본은 항상 dry-run/request-only이며, 실제 보드 쓰기나 GitHub release 같은 side effect는 명시적인 config gate와 CLI `--apply`가 둘 다 있어야 실행됩니다.

즉 AgentFlow는 “채팅 기록을 읽는 봇”이 아니라, **Kanban 보드를 source of truth로 삼아 다음 agent workflow를 이어 주는 control-plane**입니다.

### 한국어 빠른 사용 흐름

1. Hermes 런타임 환경에 패키지를 설치하고 플러그인을 켭니다.
2. `agentflow-hermes roadmap init-config`로 보드별 roadmap config를 만듭니다.
3. `roadmap promote`로 특정 final `GO` task를 request-only로 smoke합니다.
4. 안전하면 `apply_mode: true` + `--apply`로 실제 다음 Kanban graph 생성을 허용합니다.
5. 여러 채널/보드는 `roadmap register-watchdog`로 registry에 등록해 watchdog이 주기적으로 처리하게 합니다.

예시 템플릿:

- `research-loop`: `scout -> evidence -> scorecard -> review -> brief`
- `shaman-loop`: `design -> impl -> browser_e2e -> review -> fanin`
- 기본값: `impl -> review -> fanin`

자세한 명령어와 안전 경계는 아래 영어 섹션에 함께 정리되어 있습니다.

## What is AgentFlow-Hermes?

A small Python CLI plus a Hermes plugin/toolset that:

- Builds and continues multi-agent task graphs (impl → review → fan-in, and channel-specific variants) from a single source Kanban task.
- Requires an explicit ACK (`[JOB ACK]` block) before a job is considered done, and returns a final ACK to the origin lane.
- Watches for a final `GO` on a board task and, on request, promotes the next slice — it never reads Discord channel history to infer state.
- Is dry-run/request-only by default everywhere: no board write, no live message send, no systemctl, no money side effects, unless you explicitly arm each gate.

## Why it exists

Before AgentFlow, a human had to notice a `GO`/`BLOCK` message on a board task and manually create the next slice's tasks — implementation, review, fan-in — by hand, every time, for every channel. That doesn't survive process restarts, context compaction, or someone being offline.

AgentFlow replaces that manual step with durable, board-driven continuation:

- The board (not a chat transcript or an agent's memory) is the source of truth for "what's the next slice."
- A receipts ledger makes promotion idempotent — re-running the same watch/promote command over the same task creates zero duplicate tasks.
- Work survives restarts and context compaction because state lives in the config + receipts file, not in an agent's context window.
- The final ACK is returned to the task's own `origin`/`return_to` lane, not wherever the promotion happened to run.

## Core capabilities

- **CLI/plugin split**: `agentflow-hermes` is a standalone, testable engine CLI; the Hermes plugin is a thin adapter that exposes the same operations as Hermes tools (`agentflow_enqueue`, `agentflow_status`, etc.).
- **`init` / `doctor` / `status`**: bootstrap local state and check environment health before you rely on the CLI.
- **Roadmap GO autopromoter** (`roadmap promote` / `roadmap watch`): request-only by default; board writes require both `apply_mode: true` in the config *and* `--apply` on the command line. A receipts ledger prevents duplicate task creation across repeated runs.
- **Real Kanban adapter**: promotion creates the next slice's graph on the *same* board as the source task (`same_board_only`) — there is no cross-board write path.
- **Watchdog registry**: `roadmap register-watchdog` / `unregister-watchdog` track which configs a cron/watchdog process should poll, across any number of boards/channels.
- **Channel template presets**: pick a task-graph shape per channel — legacy `impl-review-fanin`, `research-loop`, or `shaman-loop` (see Examples below).
- **Safety defaults everywhere**: no live `send_message`, no Discord channel scraping, no `systemctl` writes, no money side effects; writes are gated behind config flags and CLI flags together, and dry-run/request-only is always the default path.
- **GitHub release trigger (M20)**: a bounded, dry-run-first trigger (merged in commit `85c3ee8`) that can turn a final reviewed `GO` summary into a tag + push + `gh release create`. It is not wired into any default-on automation — see below.

## Quick start

Install the engine package from the `v0.2.0` tag into the same Python environment that runs Hermes/gateway, then install/enable the plugin:

```bash
# Inside the Hermes runtime environment, not an isolated uv tool env:
uv pip install 'agentflow-hermes @ git+https://github.com/no-ai-labs/agentflow-hermes.git@v0.2.0'
hermes plugins install no-ai-labs/agentflow-hermes#plugins/hermes-agentflow
hermes plugins enable agentflow
agentflow-hermes init
agentflow-hermes doctor
hermes gateway restart
```

`uv tool install` is useful for isolated CLI commands, but it does not make `agentflow_hermes` importable by the Hermes plugin interpreter. If the engine package is missing, the plugin degrades to `agentflow_doctor` guidance instead of owning business logic itself.

For local CLI development:

```bash
uv run agentflow-hermes init
uv run agentflow-hermes doctor
uv run agentflow-hermes status
uv run agentflow-hermes enqueue --title "Review PR" --target "discord:#review" --origin-return "discord:#hermes-main"
```

Scaffold a roadmap config for a board/channel (request-only by default — `apply_mode: false`):

```bash
agentflow-hermes roadmap init-config \
  --output agentflow-roadmap.yaml \
  --board agentflow-hermes --origin 'Discord Devhub / #hermes-main' \
  --transition 'm16->m17.impl_review_fanin' --from m16 --to m17
```

Smoke it in request-only mode (no board write, no `--apply`):

```bash
agentflow-hermes roadmap promote --config agentflow-roadmap.yaml --task <some-final-go-task-id>
```

Register the config so a cron/watchdog process picks it up:

```bash
agentflow-hermes roadmap register-watchdog \
  --config agentflow-roadmap.yaml \
  --registry ~/.hermes/agentflow/roadmap-watchdog-configs.json
```

## Examples: channel template presets

Presets shape the generated task graph per channel. Pass `--template-preset` to `roadmap init-config`:

```bash
# #research: research-loop -> scout / evidence / scorecard / review / brief
agentflow-hermes roadmap init-config --output research-roadmap.yaml \
  --board research --origin 'Discord Devhub / #research' \
  --template-preset research-loop \
  --transition 'r1->r2.impl_review_fanin' --from r1 --to r2

# #shaman: shaman-loop -> design / impl / browser_e2e / review / fanin
agentflow-hermes roadmap init-config --output shaman-roadmap.yaml \
  --board shaman --origin 'Discord Devhub / #shaman' \
  --template-preset shaman-loop \
  --transition 's1->s2.impl_review_fanin' --from s1 --to s2 \
  --impl-assignee ccsupervisor --review-assignee ccreviewer

# #hermes-main: default impl / review / fanin (no --template-preset needed)
agentflow-hermes roadmap init-config --output agentflow-roadmap.yaml \
  --board agentflow-hermes --origin 'Discord Devhub / #hermes-main' \
  --transition 'm16->m17.impl_review_fanin' --from m16 --to m17
```

After scaffolding, smoke each config with `roadmap promote` (request-only, no `--apply`) before arming writes.

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

## Operator notes / production boundaries

- Active-wake/notify subscription handling belongs to Hermes/Kanban itself — AgentFlow only consumes board events (`GO`/`BLOCK`) that Hermes/Kanban already surfaces.
- No live `send_message` dispatch by default.
- No Discord channel scraping — the only trigger is board state via `roadmap watch`/`roadmap promote`.
- No `systemctl` writes — watchdog registration only reads/writes local registry files; it never creates cron units.
- No money side effects anywhere in this CLI.

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

## Safety defaults

- No live `send_message` dispatch by default
- No Discord channel scraping — only board `GO`/`BLOCK` events drive continuation
- No monkeypatching Hermes core modules
- Job payloads store metadata and payload refs, not raw private transcripts or secrets
- ACKs use explicit `[JOB ACK]` blocks
- Writes (board promotion, release actions) require a config-level flag *and* a CLI flag together — never one alone

## Why a plugin + CLI?

The plugin makes AgentFlow feel native inside Hermes (`agentflow_enqueue`, `agentflow_status`, etc.). The CLI keeps the control-plane engine reusable and testable outside Hermes.
