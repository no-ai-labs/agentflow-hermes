# AgentFlow Hermes live control-plane migration design

Kanban task: `t_2312b85a`
Origin/return_to: Discord Devhub / #hermes-main
Baseline: `main@033d38b` on branch `af/live-dispatch-control-plane`
Status: design artifact only — no production code changes in this task.

## Route evidence

- Consultation route: `claude-openrouter-opus`.
- Model: OpenRouter `anthropic/claude-opus-4.8` (Claude Opus 4.8).
- The runtime reports model id `anthropic/claude-opus-4.8` via the `claude-openrouter-opus` wrapper. Internal runtime model inspection is not fully self-verifiable from inside this process; the supervisor verified the wrapper route externally and supplied the wrapper/model name recorded here. No other model is claimed.
- This document continues the design line of `docs/m1-m2-design.md` and `docs/plugin-architecture.md`, both authored on the same route. It does not supersede their M1–M5 store/ACK/cron/kanban/packaging decisions; it extends them with an opt-in live phase.

## 1. Current architecture at baseline `033d38b`

AgentFlow is two artifacts with one source of business logic, exactly as `docs/plugin-architecture.md` §2 requires.

### Engine package `agentflow_hermes`

| Module | Responsibility at baseline |
| --- | --- |
| `store.py` | `AgentFlowStore` (SQLite). `enqueue`, `list_jobs`, `get_job`, `get_job_by_source_hash`, `record_event`, `deadletter`, transition-guarded `ack`, `dispatch_dry_run`, and `render_dispatch_prompt`. |
| `migrations.py` | `PRAGMA user_version` driver. `SCHEMA_VERSION = 2`, `STEPS = [(1, SQL_V1), (2, SQL_V2)]`. Additive-only. |
| `states.py` | `JobStatus(str, Enum)` (`queued/dispatched/waiting_review/waiting_user/succeeded/failed`), `FINAL_STATES`, `ALLOWED_TRANSITIONS`, `normalize_status`. |
| `ack.py` | `parse_ack_block` (multiline-aware), `validate_ack`, `AckPayload`, `AckError`. `dispatched` is rejected as an ACK status. |
| `bridges/cron.py` | Ref/hash/marker ingestion. `classify_markers` (legacy `[AF-CRON]` + `HERMES_ACTIVE_WAKE {json}`), `make_dedupe_key` (`cron:<job>:<run-or-hash>:<target>`), `ingest_cron_output`, `scan_cron_output`. Secret/private-path/absolute-ref scrubbing via `_contains_sensitive_text`, `_safe_source_ref`, `_sanitize_summary`. `scan_cron_output` refuses when `dry_run=False` (`live_dispatch_disabled`). |
| `bridges/kanban.py` | `resolve_blocked_remediation` single-card dry-run resolver with provenance/verdict/ambiguity guards. Refuses when `dry_run=False` (`live_mutation_disabled`). |
| `cron_bridge.py` | Compatibility shim re-exporting `bridges.cron`. |
| `cli.py` | `init`, `doctor`, `enqueue`, `status`, `dispatch-dry-run`, `ack ingest`, `cron ingest`, `bridge cron scan|ingest`, `bridge kanban resolve-blocked`. |

### Plugin adapter `plugins/hermes-agentflow`

- `__init__.py` registers six tools through `ctx.register_tool` only: `agentflow_enqueue`, `agentflow_status`, `agentflow_dispatch_dry_run`, `agentflow_ack_ingest`, `agentflow_doctor`, `agentflow_bridge_cron`.
- Engine is invoked **in-process** via `agentflow_hermes.cli.main` with stdout/stderr captured; no `python -c` and no repo-relative `PYTHONPATH` (`test_plugin_has_no_path_parents_repo_layout_assumption` guards this).
- Missing engine degrades to an actionable `agentflow_doctor` message instead of crashing plugin load.

### Current safety posture (the baseline we must preserve)

- **No live side effects exist anywhere.** Every dispatch path is dry-run: `dispatch_dry_run` mutates only local lifecycle/ledger; `scan_cron_output`/`resolve_blocked_remediation` hard-refuse `dry_run=False`.
- No Hermes core monkeypatch. The plugin only touches the Hermes plugin `ctx`.
- Durable rows store refs/hashes/summaries/bounded metadata. `bridges/cron.py` actively redacts secrets, bearer tokens, and absolute private paths before anything is persisted; `raw_output_stored: False` is asserted on cron events.
- Channel separation is already partly encoded: ACK verdicts (`ack_applied/duplicate_ack/ack_rejected`) are distinct from cron material-event metadata, and active-wake markers carry `live_wake_disabled: true`.

### Tests at baseline (must keep green)

`tests/test_store.py`, `test_ack.py`, `test_migrations.py`, `test_cron_bridge.py`, `test_kanban_bridge.py`, `test_plugin_adapter.py`. Notably `test_dry_run_required`, `test_scan_cron_output_*`, `test_active_wake_secret_and_private_path_summary_do_not_persist`, and `test_absolute_source_ref_is_replaced_before_job_events_and_status` are the dry-run/no-leak invariants the live migration must not weaken.

## 2. Live migration: dry-run-only → opt-in live control plane

### 2.0 Design stance

The migration is **additive and gated**, never a mode switch on existing code paths. Existing dry-run entry points keep their exact current behavior and remain the default. Live behavior is reachable only through new, separately named functions/commands/tools that:

1. require an explicit policy gate to be enabled, AND
2. require an explicit per-call `--live`/`live=true` opt-in, AND
3. validate the target against an allowlist, AND
4. pass idempotency + throttle checks, AND
5. write an operator receipt before and after any external effect.

If any of 1–5 is not satisfied, the path **fails closed** to dry-run semantics. There is no configuration in which live is the default.

### 2.1 Live dispatch through Hermes send_message / gateway (no core monkeypatch)

Add `src/agentflow_hermes/live/gateway.py` defining a thin capability-discovery boundary:

```python
class GatewayUnavailable(Exception): ...

@dataclass(frozen=True)
class DeliveryResult:
    success: bool
    receipt_ref: str        # opaque id/hash from Hermes, or local synthetic ref
    target: str
    detail: str = ""        # short, scrubbed
    delivered: bool = False

class HermesGateway(Protocol):
    def send_message(self, *, target: str, body: str, idempotency_key: str) -> DeliveryResult: ...
```

Resolution order, all via **public plugin/tool APIs only**:

1. If the plugin adapter is given a Hermes plugin context exposing a send/message capability (e.g. `ctx.send_message`, `ctx.tools["send_message"]`, or a documented gateway client), wrap it behind `HermesGateway`. The probe is feature-detection (`getattr`/capability lookup), never a private import or attribute patch.
2. If no capability is present, `resolve_gateway()` raises `GatewayUnavailable`, and every live path degrades to dry-run with `reason: "gateway_unavailable"`.

Hard constraints:

- The engine **never imports Hermes core**. The gateway object is injected by the plugin adapter (which legitimately holds `ctx`) or supplied in tests as a fake. The sidecar CLI has no gateway unless one is explicitly wired by an operator integration.
- No fallback to shelling out, writing to Hermes internal files, or reaching into gateway internals. If the public capability is absent, AgentFlow does not deliver. This satisfies "degrade safely; no direct private core hacks."
- Outbound `body` is the rendered dispatch prompt or a bounded notification, scrubbed through the existing `bridges/cron._contains_sensitive_text`/`_short_text` helpers (lifted to a shared `live/sanitize.py` if reused). No raw transcripts, secrets, or absolute private paths leave the process.

New store method `store.dispatch_live(...)` (see §2.4) orchestrates: gate check → allowlist check → idempotency claim → throttle check → operator receipt (attempt) → `gateway.send_message` → operator receipt (result) → lifecycle transition. It never bypasses the §1 transition guards.

### 2.2 Active wake semantics

"Active wake" = AgentFlow asking Hermes to proactively surface/resume a target conversation for a material event, as opposed to passive delivery of a report.

- Today `HERMES_ACTIVE_WAKE {json}` is parsed to **metadata only** with `live_wake_disabled: true`. The migration keeps that as the default and adds a separate, independently gated live path. Live wake is gated by its **own** policy flag distinct from live dispatch — enabling live send does NOT enable live wake.
- Live wake uses the same gateway capability discovery; if Hermes exposes a distinct wake/notify capability use it, otherwise active wake stays disabled (it must not silently fall back to a plain send and call it a wake).
- A wake request is recorded as an `active_wake` channel event (§2.9), never folded into `task_verdict` or `passive_delivery`. A wake firing is not proof of task success.
- Active wake is the highest-noise surface, so it is throttled the hardest (§2.7) and is excluded from the M6 canary entirely — it is introduced no earlier than M7 and only after live dispatch has soaked.

### 2.3 Operator receipt ledger

Add table `operator_receipts` (migration v3, §2.4) and `store.record_receipt(...)`. A receipt is the audit record that an operator-facing action was **proposed, refused, or applied** — distinct from a task verdict and from a delivery receipt.

```
operator_receipts(
  id integer primary key autoincrement,
  job_id text not null default '',
  channel text not null,            -- 'live_dispatch' | 'active_wake' | 'kanban_apply'
  phase text not null,              -- 'attempt' | 'applied' | 'refused' | 'failed'
  target text not null default '',
  idempotency_key text not null default '',
  policy_snapshot_json text not null default '{}',  -- which gates were on, who/what authorized
  delivery_ref text not null default '',            -- opaque gateway receipt ref/hash only
  reason text not null default '',
  created_at real not null
)
```

Rules:

- Every live attempt writes a `phase='attempt'` receipt **before** calling the gateway, and exactly one terminal receipt (`applied`/`failed`/`refused`) after. A live effect with no paired terminal receipt is a detectable invariant violation (smoke-tested).
- Receipts store `delivery_ref`/hashes/policy snapshots/short reasons only — never message bodies, transcripts, or secrets.
- Refusals (gate off, target not allowed, throttled, duplicate) write `phase='refused'` with a reason and **no gateway call**.

### 2.4 Policy / config gates and allowed targets

Add `src/agentflow_hermes/live/policy.py`. Resolution precedence (later overrides earlier), all fail-closed:

1. Built-in default: everything live is **off**.
2. Config file `~/.agentflow/policy.json` (path from `AGENTFLOW_HOME`), if present and well-formed.
3. Environment overrides (`AGENTFLOW_LIVE_DISPATCH`, `AGENTFLOW_LIVE_WAKE`, `AGENTFLOW_LIVE_KANBAN_APPLY`) accepting only the literal `"1"`/`"true"`.
4. Per-invocation `--live` flag / `live=true` tool arg. Absence ⇒ dry-run regardless of config.

`LivePolicy` shape:

```python
@dataclass(frozen=True)
class LivePolicy:
    live_dispatch_enabled: bool = False
    active_wake_enabled: bool = False
    kanban_apply_enabled: bool = False
    allowed_targets: tuple[str, ...] = ()      # exact-match allowlist; no globs in M6
    canary_targets: tuple[str, ...] = ()       # subset usable before full enablement
    max_sends_per_min: int = 3
    max_sends_per_target_per_hour: int = 10
    kill_switch: bool = False                  # true => hard-disable all live, ignore everything else
```

- A live action requires: the matching `*_enabled` flag true, `kill_switch` false, AND `target ∈ allowed_targets`. Any miss ⇒ refusal receipt + dry-run result.
- Allowlist is exact-string match in M6 (`discord:#hermes-canary`, etc.). No wildcards until a later hardening pass — a wildcard allowlist is an exfiltration/blast-radius risk.
- `doctor` reports the resolved effective policy (flags, allowlist size, kill-switch state, schema version) so operators can confirm posture before flipping anything.

Schema migration v3 (additive, follows the established `migrations.py` pattern):

```
SCHEMA_VERSION = 3
SQL_V3 = """
create table if not exists operator_receipts ( ... );        -- §2.3
create table if not exists idempotency_keys (                -- §2.5
    key text primary key,
    job_id text not null default '',
    channel text not null default '',
    target text not null default '',
    delivery_ref text not null default '',
    created_at real not null
);
alter table jobs add column live_delivered_at real null;
alter table jobs add column live_delivery_ref text not null default '';
create index if not exists idx_receipts_job on operator_receipts(job_id, id);
create index if not exists idx_receipts_channel on operator_receipts(channel, created_at);
"""
STEPS = [(1, SQL_V1), (2, SQL_V2), (3, SQL_V3)]
```

`test_v1_db_upgrades_without_data_loss` is extended to also cover v2→v3.

### 2.5 Idempotency, dedupe, no-storm throttles

- **Idempotency key** per live action: `sha256(f"{channel}:{job_id}:{target}:{correlation_id}".encode()).hexdigest()[:24]`. Claimed by `insert` into `idempotency_keys` inside the same transaction as the attempt receipt. A `UNIQUE` collision ⇒ treat as already-delivered: return the prior `delivery_ref`, write a `phase='refused', reason='duplicate'` receipt, **no second gateway call**. This reuses the cron bridge's proven dedupe philosophy (DB hard guard + readable key).
- **Job-level guard**: if `jobs.live_delivered_at` is already set for the same idempotency scope, short-circuit before the gateway.
- **No-storm throttles** (token-bucket in `live/throttle.py`, persisted counters keyed by minute/hour buckets in the receipts table queries):
  - global `max_sends_per_min`,
  - per-target `max_sends_per_target_per_hour`,
  - a circuit breaker: N consecutive gateway failures (default 3) flips an in-memory + persisted `degraded` state that forces dry-run until manually cleared, preventing retry storms.
- Throttle/breaker refusals are receipts, not exceptions, and always degrade to dry-run output.

### 2.6 Live canary / smoke plan and kill switch

- **Kill switch**: `policy.kill_switch=true` (config or `AGENTFLOW_KILL_SWITCH=1`) hard-disables every live path regardless of all other flags, evaluated first in every gate check. A CLI shortcut `agentflow-hermes live disable` flips it. This is the operator's single panic control.
- **Canary**: live is first exercised only against `canary_targets` (a bounded, dedicated test target/origin such as `discord:#hermes-canary` with origin `discord:#hermes-main`). The canary command sends one bounded synthetic message, asserts a `delivery_ref` came back, and asserts exactly one attempt + one terminal receipt. Dedupe/no-storm controls are active during canary so a retry can never become a storm into the canary channel.
- The canary never targets a real user/production channel, and the smoke fixture uses a fake gateway by default; a live canary against the real bounded target is an explicit operator step, not run in CI.

### 2.7 Throttle summary (no-storm posture)

| Control | Default | Scope |
| --- | --- | --- |
| `max_sends_per_min` | 3 | global, all live channels |
| `max_sends_per_target_per_hour` | 10 | per allowlisted target |
| active-wake sub-limit | 1/min, 5/hour | wake only, stricter than dispatch |
| circuit breaker | 3 consecutive failures ⇒ degrade | global, manual reset |
| idempotency window | persistent (DB unique) | per `(channel, job, target, correlation)` |

### 2.8 Stale final-fan-in supersession resolver (live-safe dry-run/apply candidate)

> The concrete `RemediationPlanner`/`StaleFinalResolver`, `GraphIntent` capture, `SubscriptionEnsurer`, and `VerdictParser` that build on this section are specified in `docs/ack-remediation-control-plane.md`.

Observed class: a terminal fan-in job (e.g. `t_695c95b3`) records a final verdict, but a **later** material event or remediation arrives that should supersede the stale final. The existing kanban resolver (`bridges/kanban.py`) and `FINAL_STATES` guard correctly refuse to mutate finals, so this needs a narrow, evidence-first resolver, mirroring the §8 blocked-remediation resolver in `docs/plugin-architecture.md`.

Add `src/agentflow_hermes/bridges/supersession.py`:

- `resolve_stale_final(stale_final, superseding_event, *, dry_run=True)` — single stale-final + single superseding ref. Never scans all finals.
- Guards (fail closed): stale job is genuinely in `FINAL_STATES`; superseding event is newer (`created_at`/seq) and carries explicit supersession intent + provenance correlation to the stale job; not ambiguous across multiple supersessors.
- **Dry-run default**: emits a structured supersession candidate (`action: "supersede_and_requeue"`, evidence, no mutations). This is the safe candidate the next stage reviews.
- **Apply mode** is opt-in, policy-gated (`kanban_apply_enabled` + `--live`), and is the only place allowed to create a *new* successor job (it never rewrites the immutable final; it requeues a fresh correlated job and writes an operator receipt). It uses the same gate/idempotency/receipt machinery as §2.1–2.5.
- CLI: `agentflow-hermes bridge supersession resolve --stale <id> --superseded-by <ref> --input-file <fixture> [--live]`. Plugin tool, if exposed: `agentflow_resolve_supersession_dry_run` (dry-run only, compact refs).

### 2.9 Channel separation (task_verdict / passive_delivery / active_wake / operator_receipt)

These remain four separate concepts in schema, event kinds, and API — never conflated:

| Channel | Storage | Event kinds | Meaning |
| --- | --- | --- | --- |
| `task_verdict` | `jobs.status` + `ack_*` ledger events | `ack_applied`, `duplicate_ack`, `ack_rejected` | What a worker/reviewer decided. Unchanged by live migration. |
| `passive_delivery` | new `passive_delivery` ledger events + `jobs.live_delivery_ref` | `delivery_attempted`, `delivery_succeeded`, `delivery_failed` | A report/notification was sent. NOT proof of task success. |
| `active_wake` | `active_wake` ledger events | `wake_requested`, `wake_dispatched`, `wake_disabled`, `wake_refused` | A proactive resume was requested/fired. Separately gated. |
| `operator_receipt` | `operator_receipts` table | n/a (own table) | Audit of proposed/refused/applied operator action. |

Invariant (smoke-tested): a `delivery_succeeded` event MUST NOT set or imply a `task_verdict`, and a terminal `task_verdict` MUST NOT imply any `passive_delivery`/`active_wake` event exists.

### 2.10 Plugin tool / API surface and CLI commands

Keep the toolset compact (the architecture doc's ≤8-through-M4 budget; live adds at most two, staying ≤8). New/changed tools are **dry-run by default**; live requires the explicit arg AND server-side policy.

CLI additions (existing commands unchanged):

- `agentflow-hermes live status` — print resolved policy + kill-switch + degraded state.
- `agentflow-hermes live enable|disable` — flip kill switch / channel flags in the config file (operator-only; never a plugin tool).
- `agentflow-hermes live canary --target <canary> [--live]` — bounded canary send.
- `agentflow-hermes dispatch --job-id <id> [--live]` — `--live` omitted ⇒ identical to today's `dispatch-dry-run`; `--live` ⇒ gated live dispatch.
- `agentflow-hermes bridge supersession resolve ...` (§2.8).

Plugin tool additions:

- Extend `agentflow_dispatch_dry_run` semantics by adding a **new** sibling tool `agentflow_dispatch` with optional `live: boolean` (default false). The existing dry-run tool is untouched.
- `agentflow_live_status` (read-only policy/health). No `enable/disable`, no kill-switch flip, and no allowlist editing is ever exposed as a model-callable tool — those are operator CLI only, matching the "do not expose file-scanning to the model" stance in `docs/plugin-architecture.md` §4.

## 3. Implementation sequencing (M6 / M7 / M8)

### M6 — Live foundation, gated, canary-only

Files: `live/policy.py`, `live/gateway.py`, `live/throttle.py`, `live/sanitize.py`, `migrations.py` v3, `store.dispatch_live`/`record_receipt`/idempotency, `cli.py` (`live status|enable|disable|canary`, `dispatch --live`), plugin `agentflow_dispatch` + `agentflow_live_status`.

Scope: live **passive dispatch** only, against `canary_targets` only, fully gated. No active wake. No apply. Kill switch operational. Default remains dry-run; all baseline tests green.

### M7 — Active wake + real allowlisted targets

Files: gateway wake-capability probe, `active_wake` events, stricter wake throttles, policy `active_wake_enabled`, expand `allowed_targets` beyond canary after M6 soak.

Scope: opt-in live active wake; live dispatch graduates from canary-only to allowlisted production targets. Still fully gated, kill-switch-respecting.

### M8 — Supersession resolver apply + hardening

Files: `bridges/supersession.py`, `cli.py` supersession command, optional dry-run plugin tool, circuit-breaker persistence, allowlist hardening.

Scope: stale-final supersession dry-run (always) and gated apply; operator-receipt-first requeue; consolidate no-storm/breaker controls. Apply mode behind `kanban_apply_enabled` + `--live`.

Each milestone ends with its own review card before the next begins; M7/M8 do not start until the prior milestone's canary/soak acceptance passes.

## 4. Acceptance tests and smokes per stage

### M6

- `test_policy_defaults_off` — fresh config ⇒ all live flags false, kill switch effectively blocks live.
- `test_dispatch_without_live_flag_is_dry_run` — `dispatch` w/o `--live` is byte-identical in effect to `dispatch-dry-run`.
- `test_live_refused_when_gate_off` / `_when_target_not_allowed` / `_when_kill_switch` — refusal receipt written, no gateway call (fake gateway asserts zero calls).
- `test_live_dispatch_happy_path_canary` — fake gateway returns `delivery_ref`; exactly one `attempt` + one `applied` receipt; `jobs.live_delivered_at` set; a `passive_delivery` event, NOT a `task_verdict`.
- `test_idempotent_live_send` — same idempotency key twice ⇒ one gateway call, second is `refused/duplicate` returning prior ref.
- `test_throttle_blocks_storm` — exceeding `max_sends_per_min` ⇒ refusal receipts, no extra gateway calls.
- `test_gateway_unavailable_degrades_to_dry_run`.
- `test_no_secret_or_private_path_in_receipts_or_events` (extends existing leak tests).
- `test_v2_to_v3_migration_no_data_loss`.
- Smoke: `live status`, `dispatch --job-id X` (dry-run), `live canary --target <canary>` against fake gateway, `doctor` shows policy.

### M7

- `test_active_wake_disabled_by_default` and `test_active_wake_separate_gate` (enabling dispatch does not enable wake).
- `test_wake_uses_wake_capability_or_disables` (no silent fallback to plain send).
- `test_wake_throttle_stricter_than_dispatch`.
- `test_allowlist_exact_match_only` (no wildcard match).
- `test_channel_separation_invariants` — wake event never implies verdict/delivery.

### M8

- `test_supersession_dry_run_emits_candidate_no_mutation`.
- `test_supersession_refuses_non_final` / `_stale_not_newer` / `_unrelated_provenance` / `_ambiguous_supersessors`.
- `test_supersession_apply_requeues_with_receipt_and_leaves_final_immutable`.
- `test_circuit_breaker_forces_dry_run_after_failures`.
- Smoke: `bridge supersession resolve` dry-run on fixture; gated apply on fixture with fake gateway.

## 5. Risks, non-goals, and review gates

### Risks

- **Accidental live default** — mitigated by two-key gating (config flag AND per-call `--live`), fail-closed precedence, and a kill switch evaluated first.
- **Storm into a real channel** — mitigated by canary-first rollout, exact-match allowlist, per-min/per-target throttles, idempotency, and circuit breaker.
- **Leakage in outbound bodies/receipts** — mitigated by reusing the proven cron-bridge sanitizers and asserting no-leak in receipts/events.
- **Gateway capability assumptions** — Hermes may not expose a public send/wake capability; the design degrades to dry-run rather than reaching into core, but live value then waits on upstream API availability.
- **Active wake noise** — highest-risk surface; deferred to M7 with the strictest throttles.

### Non-goals

- No Hermes core monkeypatch, no private imports, no shelling into gateway internals — ever.
- No removal/weakening of any existing dry-run path or its tests.
- No wildcard/glob target allowlists in M6–M8.
- No model-callable enable/disable, kill-switch, or allowlist-editing tools.
- No broad auto-supersession or auto-unblock; resolvers stay single-subject and evidence-first.
- No raw transcript/secret/absolute-path storage in DB, events, receipts, or prompts.

### Review gates

1. **M6 design + safety review** before any live code merges — confirm gate precedence, fail-closed behavior, kill switch, and that defaults are unchanged.
2. **M6 canary sign-off** — operator-run bounded canary against the dedicated test target, with receipts inspected, before M7 starts.
3. **M7 wake review** — confirm wake is independently gated and never falls back to plain send.
4. **M8 apply review** — confirm supersession apply leaves finals immutable, requeues with receipts, and respects all gates.
5. **Fan-in terminal gate** — live migration GO requires M6/M7/M8 reviews plus the baseline dry-run/no-leak test suite all green, with the kill switch demonstrated.

Verdict: GO for the staged, opt-in, fail-closed live control plane as specified. Live remains off by default at every layer until each milestone's review and canary gate is cleared.
