# AgentFlow for Hermes

Durable multi-agent handoffs, ACK receipts, and supervisor queues for [Hermes Agent](https://hermes-agent.nousresearch.com/docs).

This repository packages AgentFlow as a Hermes add-on: a small Python CLI plus a Hermes plugin/toolset. It is designed to work even when upstream Hermes primitives are still under review.

## Install concept

The Hermes plugin is a thin adapter. Install the `agentflow-hermes` engine package into the same Python environment that runs Hermes/gateway first, then install/enable the plugin:

```bash
# Inside the Hermes runtime environment, not an isolated uv tool env:
uv pip install 'agentflow-hermes @ git+https://github.com/no-ai-labs/agentflow-hermes.git'
hermes plugins install no-ai-labs/agentflow-hermes#plugins/hermes-agentflow
hermes plugins enable agentflow
agentflow-hermes init
agentflow-hermes doctor
hermes gateway restart
```

`uv tool install` is useful for isolated CLI commands, but it does not make `agentflow_hermes` importable by the Hermes plugin interpreter. If the engine package is missing, the plugin degrades to `agentflow_doctor` guidance instead of owning business logic itself.

For local CLI development:

```bash
uv run agentflow-hermes doctor
uv run agentflow-hermes init
uv run agentflow-hermes enqueue --title "Review PR" --target "discord:#review" --origin-return "discord:#hermes-main"
```

## Current status

M0 skeleton:

- SQLite-backed local job/event store
- CLI: `init`, `enqueue`, `status`, `ack ingest`, `dispatch-dry-run`, `doctor`
- Hermes plugin registering the `agentflow` toolset
- Dry-run first; live dispatch is intentionally out of scope for M0

## Roadmap GO autopromoter watchdog (M17)

`agentflow-hermes roadmap` drives the existing M15/M16 promotion/apply path from a
committed repo config file instead of a one-off fixture. It never reimplements
graph creation — it builds a `LoopEvent`/`LoopPolicy`/transition registry from the
config and a fetched board task, then calls the same `evaluate_loop_event` used by
`agentflow-hermes loop evaluate`.

```bash
# Promote one final GO task by id (request-only; no board write without --apply):
agentflow-hermes roadmap promote --config agentflow-roadmap.yaml --task t_final_123

# Same, with the board write armed (still requires apply_mode: true in the config):
agentflow-hermes roadmap promote --config agentflow-roadmap.yaml --task t_final_123 --apply

# Scan completed tasks on the configured board, let the existing GO gates reject
# ineligible rows, and promote eligible final GO tasks once. Keep receipts in a
# durable repo-local path for cron/watchdog idempotency across process runs:
agentflow-hermes roadmap watch --config agentflow-roadmap.yaml --once --apply --receipts-file .agentflow-roadmap-receipts.json
```

Enabling it for a repo:

1. Copy `agentflow-roadmap.yaml` (or write your own) and set `enabled: true`,
   `board`, `apply_mode`, `allowed_transitions`, `transitions`, and the default
   `impl_assignee`/`review_assignee`/`ack_trigger_agent`.
2. `enabled: false` is the kill switch — with it off, `roadmap promote`/`roadmap
   watch` perform no board read or write at all, even with `--apply`.
3. `board`/`same_board_only: true` means the same board is used for both reading
   the source task and creating the impl/review/fanin graph; there is no
   cross-board write path.
4. Repeated `promote`/`watch --once` runs over the same task(s) with the same
   `--receipts-file` create 0 new tasks — the apply ledger dedups by
   idempotency key before any adapter create is attempted. The real Kanban
   adapter also passes the same idempotency keys to `hermes kanban create`.

Post-update smoke (no live board write):

```bash
agentflow-hermes roadmap promote --config agentflow-roadmap.yaml --task <some-final-task-id>
```

## Safety defaults

- No live `send_message` dispatch by default
- No monkeypatching Hermes core modules
- Job payloads should store metadata and payload refs, not raw private transcripts or secrets
- ACKs use explicit `[JOB ACK]` blocks

## Why a plugin + CLI?

The plugin makes AgentFlow feel native inside Hermes (`agentflow_enqueue`, `agentflow_status`, etc.). The CLI keeps the control-plane engine reusable and testable outside Hermes.
