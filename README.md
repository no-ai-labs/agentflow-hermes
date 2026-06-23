# AgentFlow for Hermes

Durable multi-agent handoffs, ACK receipts, and supervisor queues for [Hermes Agent](https://hermes-agent.nousresearch.com/docs).

This repository packages AgentFlow as a Hermes add-on: a small Python CLI plus a Hermes plugin/toolset. It is designed to work even when upstream Hermes primitives are still under review.

## Install concept

```bash
hermes plugins install no-ai-labs/agentflow-hermes#plugins/hermes-agentflow
hermes plugins enable agentflow
hermes gateway restart
```

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

## Safety defaults

- No live `send_message` dispatch by default
- No monkeypatching Hermes core modules
- Job payloads should store metadata and payload refs, not raw private transcripts or secrets
- ACKs use explicit `[JOB ACK]` blocks

## Why a plugin + CLI?

The plugin makes AgentFlow feel native inside Hermes (`agentflow_enqueue`, `agentflow_status`, etc.). The CLI keeps the control-plane engine reusable and testable outside Hermes.
