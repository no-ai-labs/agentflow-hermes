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
