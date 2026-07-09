# M24A Oracle #shaman ACK and auto-remediation gap RCA

Task: `t_65dba018`
Origin/return_to: Discord Devhub / `#hermes-main`; issue origin Oracle `#shaman` (`1500539609413849200`)
Scope: investigation/RCA only. No Discord live-send activation and no real auto-remediation writes were performed.

## Executive summary

Verdict: GO-for-review (RCA complete; activation still needs a separate reviewed card).

The missed Oracle/#shaman ACK was not a Discord live-send bug. It was a chain of missing subscription edges plus split/incorrect AgentFlow state surfaces:

1. The relevant Oracle reviewer/fix tasks lived in the per-board DB `~/.hermes/kanban/boards/oracle-lab/kanban.db`, but the auto-remediation watchdog scanned only `~/.hermes/kanban.db`.
2. The first terminal reviewer BLOCK tasks (`t_4ce892da`, `t_0b249fcf`) had no `kanban_notify_subs` rows, so the gateway had no wake/notify edge to fire.
3. Operator-main jobs were queued in an older AgentFlow ledger (`~/.hermes/agentflow/agentflow.sqlite`) but no current consumer was attached to that DB/target, and the packaged `agentflow-hermes` CLI uses a different default DB (`~/.agentflow/agentflow.db`).
4. Even if pointed at `oracle-lab`, the current auto-remediation scanner would not have safely materialized these exact W5R-4 BLOCKs: `t_4ce892da` lacks an extractable `Next action`; `t_0b249fcf` parses the next action as not actionable.
5. The watchdog has a global numeric `last_seen_event_id=5564`; Oracle events were `3426`/`3446`. A future board switch without board-scoped state initialization would skip historical Oracle events or risk cross-board replay mistakes.

## Evidence inspected

### Oracle Kanban tasks/events/runs

Command used:

```bash
python3 - <<'PY'
import sqlite3, json
DB='/home/duckran/.hermes/kanban/boards/oracle-lab/kanban.db'
ids=['t_4ce892da','t_12351e65','t_0b249fcf','t_bb37ba68','t_a93938dd']
# queried tasks, task_runs, task_comments, task_events, kanban_notify_subs, ack tables
PY
```

Key rows:

| Task | Status | Terminal event | Finding |
| --- | --- | --- | --- |
| `t_4ce892da` | done | `task_events.id=3426`, `completed`, run `286` | Reviewer verdict BLOCK: proxy forwarded forged `X-User-Hash` when `ORACLE_WEB_ENABLE_DEV_AUTH` disabled. |
| `t_12351e65` | done | `task_events.id=3439`, run `287` | Fix GO-for-review; changed `oracle-web/lib/api-proxy.ts` and tests. |
| `t_0b249fcf` | done | `task_events.id=3446`, run `288` | Reviewer verdict BLOCK: mixed `ORACLE_WEB_ENABLE_DEV_AUTH=false` + `NEXT_PUBLIC_ORACLE_ENABLE_DEV_AUTH=true` still enabled server-side proxy dev identity forwarding. |
| `t_bb37ba68` | running during audit | comment `311` at event `3455` | Fix handoff was posted; review child remained gated by parent state at the time of this task snapshot. |
| `t_a93938dd` | todo during audit | created event `3448` | Review child for the mixed-config fix. |

Important subscription evidence:

```text
kanban_notify_subs for t_4ce892da: none
kanban_notify_subs for t_12351e65: none
kanban_notify_subs for t_0b249fcf: none
kanban_notify_subs for t_bb37ba68: rows added later by operator for #shaman and #hermes-main
kanban_notify_subs for t_a93938dd: rows added later by operator for #shaman and #hermes-main
```

The query after operator repair showed only these relevant recent rows:

```json
{"task_id":"t_a93938dd","platform":"discord","chat_id":"1500539609413849200","thread_id":"","notifier_profile":"default","trigger_agent":0,"created_at":1783626205,"last_event_id":0}
{"task_id":"t_a93938dd","platform":"discord","chat_id":"1497895797579190357","thread_id":"","notifier_profile":"default","trigger_agent":0,"created_at":1783626205,"last_event_id":0}
```

Gateway log then confirmed the repaired edge worked for the later task:

```text
2026-07-09 19:45:39,601 INFO gateway.run: kanban notifier: woke agent for t_bb37ba68 on discord/1500539609413849200 profile=default events={'completed'}
2026-07-09 19:45:40,262 INFO gateway.run: kanban notifier: woke agent for t_bb37ba68 on discord/1497895797579190357 profile=default events={'completed'}
```

No equivalent gateway wake lines existed for `t_4ce892da` or `t_0b249fcf` because no subscription edge existed.

## Root cause chain

### A. Origin ACK/active-wake route gap

Root cause: Oracle work requested AgentFlow origin-return/ACK relay in prose, but no machine subscription/ACK edge was attached to the first terminal cards.

Evidence:

- `t_4ce892da` and `t_0b249fcf` bodies said final verdict should be relayed through AgentFlow origin-return/ACK ledger, not raw Kanban notify-subscribe.
- Their `kanban_notify_subs` rows were absent.
- `ack_task_verdict` had no recent relevant rows for those W5R-4 tasks in `oracle-lab`.
- Gateway only woke after operator manually added subscriptions for later cards.

Interpretation: the notifier is edge-driven. Human-readable `Origin/return_to` text is not enough for gateway wake. The initial graph had an origin-return intention but did not materialize a notify/ACK subscription edge for the terminal reviewer task.

### B. Operator-main queued-job consumer gap

Root cause: AgentFlow has queued `operator-main` jobs in one DB, while the current packaged CLI/runtime reads another DB and there is no active `operator-main` consumer state.

Evidence command:

```bash
python3 - <<'PY'
import sqlite3, json
DB='/home/duckran/.hermes/agentflow/agentflow.sqlite'
# queried jobs, job_events, origin_return, delivery_attempts, ack_ledger, agent_state
PY
PYTHONPATH=/home/duckran/dev/agentflow-hermes python3 -m agentflow_hermes.cli doctor
PYTHONPATH=/home/duckran/dev/agentflow-hermes python3 -m agentflow_hermes.cli status --json --limit 10
```

Findings:

- `~/.hermes/agentflow/agentflow.sqlite` contains queued jobs:
  - `job_cmd_000005`: `kind=implementation_review_graph`, `target_agent=operator-main`, status `queued`, correlation `oracle-lab:W5R-4:t_3a5757b7:t_4ce892da`, created from `oracle-lab:t_3a5757b7:created`.
  - `job_cmd_000004`: `kind=origin_ack_relay`, `target_agent=operator-main`, status `queued`, correlation `oracle-lab:W5R-3:t_48fa1ca6:t_e2d0571e`.
- `origin_return`, `delivery_attempts`, and `ack_ledger` tables in that DB were empty.
- `agent_state` had no `operator-main` row; only old canary/local agents were present.
- Packaged `agentflow-hermes` reported a different DB and policy path:
  - DB: `/home/duckran/.agentflow/agentflow.db`
  - Policy: `/home/duckran/.agentflow/policy.json`
  - Its `status` showed only old canary jobs, not `job_cmd_000004`/`job_cmd_000005`.

Interpretation: operator-main was queued but not consumed because the producer/ledger and active packaged runtime are not pointed at the same durable store, and there is no running consumer/dispatcher for `target_agent=operator-main` in the queued-job DB. This explains “queued but no active wake/operator relay.”

### C. Auto-remediation watchdog gap

Root cause: the cron exists and runs, but it scans the wrong board DB and its state/gating are not board-safe for Oracle.

Evidence:

`/home/duckran/.hermes/scripts/agentflow_auto_remediation_watchdog.py`:

```python
STATE = Path("/home/duckran/.hermes/state/agentflow_auto_remediation_watchdog.json")
KANBAN_DB = "/home/duckran/.hermes/kanban.db"
...
sources = read_kanban_block_events_from_sqlite(
    KANBAN_DB,
    limit=50,
    default_origin_ref="discord:#hermes-main",
    default_return_to_ref="discord:#hermes-main:1497895797579190357",
)
```

Cron config:

```json
{
  "id": "aa7adad4350b",
  "name": "AgentFlow reviewer BLOCK auto-remediation watchdog",
  "script": "agentflow_auto_remediation_watchdog.py",
  "schedule_display": "every 5m",
  "enabled": true,
  "state": "scheduled",
  "last_status": "ok",
  "last_output": null,
  "no_agent": true
}
```

State:

```json
{"last_seen_event_id": 5564}
```

Dry-run scanner evidence:

```bash
PYTHONPATH=/home/duckran/dev/agentflow python3 - <<'PY'
from agentflow.kanban_auto_remediation import read_kanban_block_events_from_sqlite, scan_sources, GatedKanbanSubprocessWriter, APPROVE_REAL_KANBAN_WRITE_MARKER
# Compared ~/.hermes/kanban.db vs ~/.hermes/kanban/boards/oracle-lab/kanban.db
PY
```

Results:

- Default DB `~/.hermes/kanban.db`: scanner found current `kanban:*` BLOCK events, including event id `5564`, and would apply remediation specs.
- Oracle DB `~/.hermes/kanban/boards/oracle-lab/kanban.db`: scanner found Oracle BLOCKs but classified W5R-4 as non-apply:
  - `oracle-lab:t_0b249fcf:kanban-event-3446` -> proposal, `blocked_reasons=['next_action_not_actionable']`
  - `oracle-lab:t_4ce892da:kanban-event-3426` -> proposal, `blocked_reasons=['missing_next_action']`

Interpretation:

- The watchdog never saw Oracle/#shaman W5R-4 because it scanned `~/.hermes/kanban.db` only.
- It was silent (`last_output=null`) because no new actionable events in its scanned DB required a report, not because Oracle had been handled.
- Its global numeric `last_seen_event_id` is unsafe across boards. If the DB were changed to `oracle-lab`, events `3426` and `3446` are lower than `5564`, so they would be skipped unless the state is board-scoped or initialized intentionally. Conversely, resetting too far back could replay old historical Oracle BLOCKs.
- The current parsing/safety classifier would have produced NEED_MORE proposals for these exact W5R-4 reviewer BLOCKs, not fix/review cards, because the canonical summaries were not shaped for bounded actionable extraction.

### D. Real-write gates and why not to activate broadly

`/home/duckran/dev/agentflow/agentflow/kanban_auto_remediation.py` is fail-closed at the writer boundary:

```python
APPROVE_REAL_KANBAN_WRITE_MARKER = "APPROVE_KANBAN_AUTO_REMEDIATION_REAL_WRITE"
APPROVE_REAL_KANBAN_WRITE_ENV = "AGENTFLOW_KANBAN_AUTO_REMEDIATION_ALLOW_REAL_WRITE"
...
def _is_fully_approved(self) -> bool:
    return (
        self.marker == APPROVE_REAL_KANBAN_WRITE_MARKER
        and self.env.get(APPROVE_REAL_KANBAN_WRITE_ENV) == "1"
        and self.allow_real_write_once
    )
```

The watchdog sets the env and `allow_real_write_once=True`, so if a source is classified `applied`, the adapter can create real Kanban cards.

The adapter `/home/duckran/.hermes/scripts/kanban_auto_remediation_adapter.py` then runs:

```python
cmd = [
  "hermes", "kanban", "create", title,
  "--json", "--assignee", assignee,
  "--workspace", workspace,
  "--priority", "120",
  "--created-by", "agentflow-auto-remediation",
  "--origin-platform", "discord",
  "--origin-chat-id", "1497895797579190357",
  "--idempotency-key", idem,
  "--body", body,
]
```

Risks:

- No explicit `--board`, so board targeting depends on the process environment/default board.
- Origin chat is hardcoded to `#hermes-main` (`1497895797579190357`), not source `#shaman`.
- Redacted `workspace_ref:*` falls back to `/home/duckran/dev/agentflow`, which is wrong for Oracle product remediation (`/home/duckran/oracle-lab`) unless explicitly preserved and allowlisted.
- The dirty `/home/duckran/dev/agentflow` worktree already has 13 modified auto-remediation/ledger files, so direct mutation there would risk clobbering in-progress work.

## Recommendation

### A. Immediate ACK route repair

Already-started operator repair is directionally correct: attach explicit notify/ACK subscriptions to the active fix/review path, not rely on prose.

Recommended next card:

- Type: implementation + review
- Scope: Oracle board subscription/origin-return edge materialization only
- Acceptance:
  1. New Oracle implementation/review graph creation stores machine-readable origin/return fields for `#shaman` and `#hermes-main`.
  2. Terminal reviewer verdict produces a durable ACK/verdict row or notifier subscription edge before the run completes.
  3. Gateway wake test confirms both target channels receive exactly one safe canonical wake, no raw transcript.
  4. No global Discord live-send/default route is enabled.

### B. Queued-job consumer/wake repair

Recommended next card:

- Decide and document one canonical AgentFlow DB for operator-main:
  - either migrate `~/.hermes/agentflow/agentflow.sqlite` into packaged `~/.agentflow/agentflow.db`, or configure the packaged runtime to use the existing `AGENTFLOW_HOME=/home/duckran/.hermes/agentflow` surface.
- Add a read-only status/health check:
  - queued jobs by target_agent;
  - stale queued/claimed jobs;
  - `agent_state` row existence for `operator-main`;
  - empty `ack_ledger` while jobs are queued should be visible as BLOCK/NEED_MORE.
- Start with dry-run consumer only. It may render the operator relay prompt/wake artifact, but must not send Discord live messages.

### C. Bounded auto-remediate activation plan

Do not turn on broad real writes. Create a separate reviewed activation card with this canary shape:

1. Make the watchdog board-aware:
   - configured allowlist: `oracle-lab` only for this canary;
   - explicit DB path `/home/duckran/.hermes/kanban/boards/oracle-lab/kanban.db`;
   - state key per board, e.g. `last_seen_event_id_by_board.oracle-lab`.
2. Initialize state to current max event id at activation time, not historical zero. This prevents replay storm.
3. Dry-run first:
   - record what sources would be `applied` vs `proposal`;
   - require output only on non-empty material changes.
4. Apply only allowlisted classes:
   - assignee must be `ccreviewer`;
   - verdict must be authoritative `BLOCK`;
   - workspace must be exactly `/home/duckran/oracle-lab` or a safe `workspace_ref` resolved by an audited map;
   - source board must be `oracle-lab`;
   - next action must begin with safe bounded implementation verbs and pass destructive/out-of-scope filters;
   - idempotency key includes board + source task + event id.
5. Adapter must create cards on the same board explicitly (`--board oracle-lab` or board-aware API/tool equivalent) and must not hardcode #hermes-main as the only origin.
6. Canary one fresh synthetic/review BLOCK after activation; verify exactly one fix card and one linked review card. Then pause and review before expanding.

### D. Safety gates/allowlist

Keep these gates fail-closed:

- No `send_message` live/default route.
- No replay of historical Oracle events.
- No workspace fallback to `/home/duckran/dev/agentflow` for Oracle tasks.
- No raw task body/comment/transcript persistence in ACK/relay ledgers.
- No auto-fix for summaries missing `Next action` or containing vague “operator should review/relay/decide” action.
- No writes to `/home/duckran/dev/agentflow` dirty files during this audit.

### E. What not to do

- Do not “fix” this by enabling broad Discord live-send.
- Do not reset `last_seen_event_id` to zero on the existing watchdog.
- Do not point the existing real-write watchdog at `oracle-lab` without board-scoped state, explicit board create, dry-run preview, and one fresh canary.
- Do not assume `Origin/return_to:` prose creates a gateway notification edge.
- Do not consume the old operator-main queued jobs with the packaged CLI until the DB split is resolved.

## Recommended next graph

1. `ACK route repair for Oracle board terminal verdicts` -> assignee `ccsupervisor`, reviewer `ccreviewer`.
2. `AgentFlow operator-main store/consumer alignment` -> assignee appropriate AgentFlow/Hermes maintainer; review required.
3. `Board-aware auto-remediation watchdog canary for oracle-lab` -> assignee `ccsupervisor`; review child `ccreviewer`; starts dry-run only.
4. `Reviewer summary contract hardening` -> require `Next action:` in canonical BLOCK summaries when auto-remediation is desired; otherwise classifier intentionally produces proposal/NEED_MORE.

## Final RCA verdict

Verdict: GO-for-review
Root cause: missing machine notification/subscription edges for first Oracle W5R-4 terminal tasks, plus auto-remediation watchdog scanning the wrong DB and operator-main jobs stranded in a non-consumed AgentFlow store.
Queued-job finding: `job_cmd_000005`/`job_cmd_000004` are queued in `~/.hermes/agentflow/agentflow.sqlite`, while current packaged `agentflow-hermes` status reads `~/.agentflow/agentflow.db`; no `operator-main` consumer/agent_state row was present.
Auto-remediation watchdog finding: cron `aa7adad4350b` is alive and silent, but scans only `~/.hermes/kanban.db`; `last_seen_event_id=5564` is global and not board-safe; Oracle W5R-4 sources would classify as proposal, not applied, under current parser.
Immediate operator action needed: keep manual subscriptions on the active Oracle review path; create reviewed repair cards for ACK edge materialization, AgentFlow DB/consumer alignment, and board-aware dry-run auto-remediation canary.
