# AgentFlow for Hermes installed

Enable and restart Hermes:

```bash
hermes plugins enable agentflow
hermes gateway restart
```

Initialize the local AgentFlow store:

```bash
agentflow-hermes init
```

M0 is dry-run first. Use `agentflow_dispatch_dry_run` before any future live dispatch bridge.
