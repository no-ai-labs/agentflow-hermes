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

### Add a new board/channel in 3 commands

`roadmap init-config` scaffolds a committed config from flags, and
`roadmap register-watchdog` records its path in a JSON registry the existing
no-agent cron script iterates. The generated config defaults to `apply_mode:
false` (proposes, never writes a board) until you explicitly arm it with
`--apply-mode` or edit the file — and the cron script still needs `--apply` at
run time, so both gates stay closed by default.

```bash
# 1. Scaffold a config for the board (request-only by default):
agentflow-hermes roadmap init-config \
  --output ~/roadmaps/contextops-roadmap.yaml \
  --board contextops --origin 'Discord Devhub / #contextops' \
  --transition 'm1->m2.impl_review_fanin' --from m1 --to m2

# 2. Smoke the generated config in request-only mode (no board write):
agentflow-hermes roadmap promote \
  --config ~/roadmaps/contextops-roadmap.yaml --task <some-final-go-task-id>

# Optional: review/edit the file, then arm writes when ready by re-running
# step 1 with --apply-mode --force, or edit apply_mode: true.

# 3. Register it so the cron watchdog picks it up:
agentflow-hermes roadmap register-watchdog \
  --config ~/roadmaps/contextops-roadmap.yaml \
  --registry ~/.hermes/agentflow/roadmap-watchdog-configs.json
```

Register is idempotent — running it again reports `already_registered: true`
and adds no duplicate entry. The registry is plain JSON the cron script reads:

```json
{
  "version": 1,
  "configs": [
    {
      "name": "contextops",
      "config": "/home/you/roadmaps/contextops-roadmap.yaml",
      "workdir": "/home/you/roadmaps",
      "receipts_file": "/home/you/roadmaps/.agentflow-roadmap-receipts.json",
      "board": "contextops",
      "enabled": true
    }
  ]
}
```

Each entry keys on `config` (the resolved config path), matching the no-agent
cron script's `item["config"]` contract; `workdir`/`receipts_file`/`name` are
also read by the script, and `board`/`enabled` are extra metadata it ignores.
The cron script iterates `configs[]` and runs, per entry, `agentflow-hermes
roadmap watch --config <config> --once --apply --receipts-file <receipts_file>`
with `cwd=<workdir>`. A legacy registry that keyed entries on `path` is still
read and deduped correctly. `init-config`/`register-watchdog` never create cron
units, touch systemctl, or write another board — they only read/write local
files.

### Remove / disable a board/channel

```bash
# Stop the cron watchdog from picking a config up (leaves the file on disk):
agentflow-hermes roadmap unregister-watchdog \
  --config ~/roadmaps/contextops-roadmap.yaml \
  --registry ~/.hermes/agentflow/roadmap-watchdog-configs.json

# Or hard-disable in-place: set `enabled: false` in the config (the kill switch),
# after which promote/watch perform no board read or write at all.
```

### Board templates

Scaffold per board/channel. Only boards you explicitly name are enabled; no
board is registered or armed by default.

```bash
# #hermes-main (the primary devhub board):
agentflow-hermes roadmap init-config --output agentflow-roadmap.yaml \
  --board agentflow-hermes --origin 'Discord Devhub / #hermes-main' \
  --transition 'm16->m17.impl_review_fanin' --from m16 --to m17

# #contextops:
agentflow-hermes roadmap init-config --output contextops-roadmap.yaml \
  --board contextops --origin 'Discord Devhub / #contextops' \
  --transition 'm1->m2.impl_review_fanin' --from m1 --to m2

# #research:
agentflow-hermes roadmap init-config --output research-roadmap.yaml \
  --board research --origin 'Discord Devhub / #research' \
  --template-preset research-loop \
  --transition 'r1->r2.impl_review_fanin' --from r1 --to r2

# #oracle / #shaman-style advisory boards (review-heavy):
agentflow-hermes roadmap init-config --output oracle-roadmap.yaml \
  --board oracle --origin 'Discord Devhub / #oracle' \
  --template-preset shaman-loop \
  --transition 's1->s2.impl_review_fanin' --from s1 --to s2 \
  --impl-assignee ccsupervisor --review-assignee ccreviewer
```

Post-update smoke (no live board write):

```bash
agentflow-hermes roadmap promote --config agentflow-roadmap.yaml --task <some-final-task-id>
```

## GitHub release publish trigger (M20)

A bounded, dry-run-first trigger that turns a final reviewed `GO` summary into
a `git tag` + `git push` (tag) + `gh release create`. It never runs git/gh on
its own — see `src/agentflow_hermes/release_action.py`.

```bash
# Dry-run (default): reads the summary, evaluates the gates, proposes a plan.
# No config passed at all => release actions are disabled => always a noop.
agentflow-hermes release github --summary-file final-go.txt --config release.json

# Apply requires BOTH config.apply_mode: true AND --apply on the CLI.
agentflow-hermes release github --summary-file final-go.txt --config release.json \
  --apply --receipts-file .agentflow-release-receipts.json
```

The final GO summary must carry explicit markers — prose like "we should
probably release this" is never treated as a directive:

```
Verdict: GO
Release-Action: github-release
Release-Version: v1.2.3
Release-Approved: true
Release-Title: v1.2.3 release        # optional
Release-Notes: Bugfixes and docs.    # optional
Release-Target: main                 # optional, passed to `gh release create --target`
```

Safety gates, all fail closed:

1. `release_actions_enabled: false` (default) — no summary is ever acted on;
   this is the master kill switch in the release-action config.
2. `Verdict: GO` plus all four required markers must be present and explicit;
   `BLOCK`/`NEED_MORE`/prose-only directives always refuse.
3. `Release-Action` must be in the config's `allowed_actions` allowlist (e.g.
   `github-release`) — an unlisted action refuses (`unknown_action`).
4. `Release-Approved: true` must be present — anything else refuses
   (`missing_approval`).
5. `Release-Version` must match `allowed_versions` (an explicit list) or
   `allowed_version_pattern` (a regex) if configured, else the built-in
   `vX.Y.Z` pattern — otherwise refuses (`invalid_version`).
6. Even with all of the above, the CLI only *proposes* a plan
   (`mutations` describes the planned release action; nothing runs) unless `config.apply_mode: true` **and**
   `--apply` are both set.
7. Duplicate protection is two-layered: a local receipts-file ledger keyed by
   `release:<action>:<version>` short-circuits before touching git/gh at all,
   and — independently, right before any mutation — a live `git tag -l` /
   `gh release view` check refuses (`duplicate_tag` / `duplicate_release`) if
   the tag or release already exists upstream, even if the local ledger is
   missing or stale.
8. If `git tag`, `git push`, or `gh release create` fails partway through, the
   run refuses (`tag_failed` / `push_failed` / `release_create_failed`) and
   writes **no** receipt, so a retry can safely pick up where it left off.
9. The receipt written on success stores only `action`, `version`,
   `idempotency_key`, and `result` — never the raw summary body, notes, or any
   secret/token.

An example `release.json`:

```json
{
  "release_actions_enabled": true,
  "apply_mode": true,
  "allowed_actions": ["github-release"],
  "allowed_version_pattern": "^v\\d+\\.\\d+\\.\\d+$"
}
```

Tests (`tests/test_release_action.py`, `tests/test_release_cli.py`) exercise
every gate above against an injectable fake git/gh runner. No test ever
shells out to real git/gh, and this module is not wired into any live
default-on path — an operator must write a config with both flags on and pass
`--apply` explicitly.

## Safety defaults

- No live `send_message` dispatch by default
- No monkeypatching Hermes core modules
- Job payloads should store metadata and payload refs, not raw private transcripts or secrets
- ACKs use explicit `[JOB ACK]` blocks

## Why a plugin + CLI?

The plugin makes AgentFlow feel native inside Hermes (`agentflow_enqueue`, `agentflow_status`, etc.). The CLI keeps the control-plane engine reusable and testable outside Hermes.
