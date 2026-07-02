# AgentFlow for Hermes installed

This plugin is only the Hermes tool adapter. The `agentflow-hermes` Python engine package must also be installed in the same Python environment used by Hermes.

## Install/upgrade the engine package

From a checkout:

```bash
# Run this inside the same Python environment that launches Hermes/gateway.
uv pip install -e /path/to/agentflow-hermes
```

For a GitHub install, use the repository URL/tag your deployment trusts from the Hermes runtime environment:

```bash
uv pip install 'agentflow-hermes @ git+https://github.com/no-ai-labs/agentflow-hermes.git'
```

Do not use `uv tool install` for the engine required by this plugin: tool installs create isolated command environments and do not make `agentflow_hermes` importable by the Hermes plugin interpreter.

## Enable the plugin and initialize the dry-run store

```bash
hermes plugins enable agentflow
agentflow-hermes init
agentflow-hermes doctor
hermes gateway restart
```

After restart, call the `agentflow_doctor` tool. A healthy install reports the engine as importable, the local schema version, and `mode: dry-run-first`.

## Safety defaults

AgentFlow M0/M3 is dry-run first. Use `agentflow_dispatch_dry_run` to render a supervisor handoff before any future live dispatch bridge. The plugin does not monkeypatch Hermes core, does not send live messages, and does not trigger `active_wake` dispatch.

## If doctor reports the engine is missing

Install the engine package in the Hermes runtime environment, keep the plugin enabled, then restart Hermes and run `agentflow_doctor` again. The plugin should degrade to this doctor guidance instead of crashing plugin load when the engine package is absent.

## Seamless maintenance (optional, operator CLI only)

This is a separate, external systemd **user** runner — not part of the Hermes gateway process or cgroup, and not reachable as a model-callable tool. It only ever invokes the existing `agentflow-hermes maintenance runner evaluate --input-file <config>` entrypoint, which is request-only/dry-run by construction.

```bash
# Render the unit/timer content and print it (default: no files written, no systemctl call).
agentflow-hermes maintenance render-units --config-file ~/.agentflow/maintenance.json

# Write a default request_only config plus unit files into an explicit directory you choose.
# Still never calls systemctl — you enable/start the units yourself once you've reviewed them.
agentflow-hermes maintenance install-runner \
  --config-file ~/.agentflow/maintenance.json \
  --unit-dir ~/.config/systemd/user \
  --write-files
```

Safety boundary:

- **Default is request_only.** The generated `maintenance.json` has `mode: "request_only"`, an empty `allowed_services` list, no `trust_grants`, and `requested_action: "observe"`. In this mode the runner can only ever produce a proposal — `service_cycle` is blocked before any allowlist/grant check runs.
- **No restart, no systemctl, ever, from these commands.** `install-runner`/`render-units` only render unit text and optionally write it to the directory you name; they never shell out to `systemctl` and never enable or start anything.
- **Actual guarded service cycles remain future/policy-gated.** Moving to `mode: "guarded_cycle"` requires an explicit, unit-scoped `trust-grant` (not part of this install path) naming the exact allowlisted service; without it the runner always refuses with the specific blocking gate.
