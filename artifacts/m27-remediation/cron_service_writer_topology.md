# M27 Final Remediation — Cron/Service Writer Topology Audit

Task: t_0af40006 (M27 final remediation: side-effect-free dry-run and legacy incident quarantine)
Captured: 2026-07-13T05:17:00Z

## Single live writer (canonical store agentflow-daemon.sqlite)

```
-- agentflowd.service (primary, event-driven, --apply) --
ExecStart=python3 /home/duckran/dev/agentflow-hermes/scripts/agentflowd.py run --boards-root /home/duckran/.hermes/kanban/boards --db /home/duckran/.hermes/agentflow/agentflow-daemon.sqlite --apply
agentflowd.service active: active

-- agentflow-reconcile.service (quiet recovery, same --apply --db) --
ExecStart=python3 /home/duckran/dev/agentflow-hermes/scripts/agentflowd.py reconcile --boards-root /home/duckran/.hermes/kanban/boards --db /home/duckran/.hermes/agentflow/agentflow-daemon.sqlite --apply

-- agentflow-reconcile.timer (5m) --
OnBootSec=5min
OnUnitActiveSec=5min
Unit=agentflow-reconcile.service
agentflow-reconcile.timer active: active
```

Both systemd units target the SAME canonical DB (`/home/duckran/.hermes/agentflow/agentflow-daemon.sqlite`) with `--apply`, so there is exactly one writer topology and no split-brain between the run daemon and the reconcile timer.

## Retired competing writer (cron aa7adad4350b)

```
id        : aa7adad4350b
name      : AgentFlow global needs_input continuation watchdog
script    : agentflow_needs_input_watchdog.py
state     : paused
enabled   : False
paused_at : 2026-07-13T04:52:04.098963+00:00
reason    : RETIRED (M27 final remediation t_0af40006): superseded by live agentflowd.service (--apply --db agentflow-daemon.sqlite) + agentflow-reconcile.timer. Kept paused/disabled; the pre-fix dry-run of this watchdog leaked incident rows into the legacy continuation store (since quarantined). Do not re-enable.
```

The retired cron ran `agentflow_needs_input_watchdog.py` in dry-run against the LEGACY store
(`~/.hermes/state/agentflow_needs_input_continuations.sqlite`). Before the fix its dry-run leaked
durable rows there. It is now paused + disabled + annotated RETIRED, and the watchdog dry-run is
strictly side-effect-free, so even an accidental re-run cannot leak.

## No competing writer to the legacy store

- cron aa7adad4350b: paused/disabled/retired (above).
- agentflowd.service + reconcile.timer: write only the canonical `agentflow-daemon.sqlite`, never the legacy path.
- Legacy store is now quiescent; incident rows 6/7/8 + outbox 17-22 quarantined (see quarantine receipt).
