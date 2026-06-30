# AgentFlow Hermes seamless maintenance: plugin control plane + external runner design

Kanban task: `t_cda43d0a` (Opus design gate)
Origin/return_to: Discord Devhub / #hermes-main
Baseline: `main@d7ccd3a` (M6 live gates, fail-closed policy, kill_switch hard-stop,
raw-durability sanitization, ACK/remediation design docs; engine installed into the
Hermes gateway venv).
Status: design artifact only — no production code changes in this task.

## Route evidence

- This artifact was produced under **native Claude Code `--model opus`** on the
  Anthropic Claude Code subscription route (model id `claude-opus-4-8`, Opus 4.8).
  It is the same native-Opus route as `docs/ack-remediation-control-plane.md`,
  not the OpenRouter/Kimi wrapper route used for `docs/m1-m2-design.md`,
  `docs/plugin-architecture.md`, and `docs/live-migration-design.md`.
- Runtime self-inspection of the route from inside the process is not fully
  self-verifiable; **external supervisor verification of the native `--model opus`
  route is a precondition of accepting this design's verdict**, as in
  `docs/ack-remediation-control-plane.md` §Route evidence.
- Supervisor route verification for this task: `type claude` resolved to the
  profile wrapper at `/home/duckran/.hermes/profiles/ccsupervisor/home/bin/claude`;
  `claude --version` returned `2.1.196 (Claude Code)`; `claude auth status --text`
  reported `Login method: Claude Max account`; and the design run was invoked as
  `claude --model opus ... --output-format json`, returning `session_id`
  `b3b635a6-9f04-4828-8ce2-66e4a76c7f71` with `modelUsage` key
  `claude-opus-4-8` and no OpenRouter/Kimi/Moonshot wrapper in the command path.
- This document **extends** the existing design line. It does not supersede any
  M1–M8 decision:
  - `docs/plugin-architecture.md` — two-artifact split (§2), ≤8 compact dry-run
    tool budget (§4), four-channel separation (§5), single-subject evidence-first
    resolvers (§8).
  - `docs/live-migration-design.md` — additive/gated/fail-closed live foundation,
    `LivePolicy`, `operator_receipts`, idempotency, throttle/circuit-breaker,
    kill switch (§2).
  - `docs/ack-remediation-control-plane.md` — `GraphIntent` (origin/return/
    subscription), `VerdictParser` semantic GO/BLOCK, `SubscriptionEnsurer`,
    operator-receipt ledger.

## 0. Problem statement and design stance

**Product requirement.** Make three capabilities *seamless* — "install only and it
works" — without becoming a dangerous automatic update daemon:

1. an **opportunistic upstream watcher** that notices when upstream AgentFlow is
   ahead of the Hermes checkout;
2. a **Kanban graph creator** that turns that observation into a reviewable sync
   request (origin / return_to / subscription), not a code change; and
3. a **gateway service-cycle runner** that can restart the Hermes gateway after a
   reviewed sync — but only when it is explicitly safe.

**Why a daemon is the wrong shape.** A single in-gateway process that fetches,
pulls, reinstalls, and restarts unattended is exactly the blast-radius/exfiltration
risk the project has refused since M0: it would (a) mutate git without review,
(b) restart the very process it runs inside (killing itself mid-cycle), and
(c) take a privileged OS action on a model-influenced trigger. This design instead
**splits observe-and-propose from act**, keeps the observer read-only, runs the
actor *outside* the gateway cgroup, and gates the actor behind the same
two-key/fail-closed machinery as the live control plane.

**Design stance (inherited, non-negotiable).** Additive and gated, never a mode
switch on existing paths. Every new capability is **observe/propose/dry-run by
default**. A real service cycle requires, in order: `kill_switch` off → mode is
`guarded_cycle` → a **trust grant** exists for the exact gateway unit → a request
graph exists with **reviewed sync GO** (and **runtime install GO** if a checkout
move is involved) → the target unit ∈ **service allowlist** → no active workers /
cc tmux → quiet-period / max-cycles-per-day satisfied → idempotency claim →
**fake-canary cycle passed** → receipt(attempt) → effect → post-smoke →
receipt(terminal). Any miss **fails closed** to request_only semantics.

## 1. Plugin control plane vs external systemd runner — the split

The single most important architectural decision: **the observer and the actor are
different artifacts in different trust domains, communicating only through the
durable AgentFlow store.**

```text
┌─────────────────────────── Hermes gateway process (in-cgroup) ───────────────────────────┐
│  plugins/hermes-agentflow  (control plane, UNPRIVILEGED, observe + propose only)          │
│    • agentflow_maintenance_status  (read-only)                                            │
│    • watcher proposal path is dry-run; NEVER cycles the gateway, NEVER mutates git        │
│    • reuses in-process engine import (no python -c, no repo-relative PYTHONPATH)           │
└───────────────────────────────────────────────────────────────────────────────────────────┘
            │ writes request graphs + reads status                ▲ ACK/receipts read back
            ▼  (AgentFlow SQLite store @ AGENTFLOW_HOME)           │
┌──────────────────────── External systemd USER units (OUT of gateway cgroup) ──────────────┐
│  agentflow-watch.timer  → agentflow-watch.service   (Type=oneshot, read-only git probe)   │
│  agentflow-runner.timer → agentflow-runner.service  (Type=oneshot, PRIVILEGED actor)      │
│    • in its own  agentflow-maintenance.slice  so restarting the gateway can't kill it     │
│    • the ONLY component allowed to `systemctl --user restart <allowlisted gateway unit>`  │
│    • the ONLY component allowed an ff-only checkout move, and only under runtime-install GO│
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

Engine subpackage (additive; mirrors `live/` and `bridges/` layout):

| New module | Responsibility |
| --- | --- |
| `maintenance/policy.py` | `MaintenancePolicy` (modes + gates) loaded from `AGENTFLOW_HOME/maintenance.json`, fail-closed like `live/policy.py`. |
| `maintenance/gitprobe.py` | **Read-only** git facts: `git fetch` (refs only), ahead/behind, dirty worktree, locally-carried commits, reviewed-SHA fast-forward eligibility. Never mutates. |
| `maintenance/activity.py` | Active-worker / cc-tmux / inflight-job detection (returns a bounded `ActivitySnapshot`). |
| `maintenance/watcher.py` | Orchestrates a watch run: probe → dedupe → create a sync **Kanban graph** (proposal) or an observe-only note. No git, no restart. |
| `maintenance/runner.py` | Consumes an eligible request, evaluates gates, runs the **fake-canary** then the real cycle plan via an injected executor, post-smoke, writes receipts + Kanban ACK, handles the gateway-cannot-return failure path. |
| `maintenance/executor.py` | `ServiceExecutor` Protocol + `SystemctlExecutor` (real) + `FakeServiceExecutor` (tests/canary). The boundary that does `systemctl --user`/pinned install/`git merge --ff-only`. |
| `maintenance/units.py` | Renders systemd user unit/timer templates and installs/uninstalls them (operator CLI only). |
| `maintenance/installer.py` | `install-runner` flow: init DB/policy → install units → enable request_only → doctor/smoke. |

**Trust-domain rules (hard):**

- The **plugin/in-gateway control plane has no privileged verb.** It cannot
  restart, install, or move git. The most it does is create a *proposal* row and
  read status — identical risk class to today's `agentflow_enqueue`.
- The **runner is never part of the gateway unit** and never imported by Hermes.
  It is a standalone systemd user oneshot in `agentflow-maintenance.slice`; it
  talks to the gateway only through `systemctl --user` (an OS interface), never
  through Hermes internals → **no Hermes core monkeypatch, no private import.**
- The two halves share **only the SQLite store**. The watcher writes request
  graphs; the runner claims and acts on them; both write to the
  `operator_receipts` ledger. This is the same in-process-engine reuse the M3
  packaging hardening already mandates.

## 2. Maintenance modes

A single enum drives everything; the default is the second-safest, and the only
dangerous mode is doubly opt-in (mode **and** trust grant).

| Mode | Watcher behavior | Runner behavior | Default? |
| --- | --- | --- | --- |
| `disabled` | no-op (timer fires, exits 0 immediately) | no-op | — |
| `observe_only` | probe + write an **observation note** to the ledger/Kanban (info card, no actionable request, no subscription edge) | no-op | — |
| `request_only` | probe + create/refresh a **sync request Kanban graph** with `GraphIntent{origin, return_to, wants_subscription}` | **never cycles**; surfaces the request for human/reviewer action only | **yes** |
| `guarded_cycle` | same as request_only | **may** execute the gated cycle plan when a request with reviewed GO exists and all §7 gates pass | — (requires trust grant) |

Resolution & fail-closed semantics (reusing the `live/policy._strict_bool`
pattern): an unknown / malformed `mode` value resolves to `request_only` (not
`guarded_cycle`); a malformed `maintenance_kill_switch` resolves to `true`
(hard-stop), exactly mirroring `kill_switch` malformed→True at
`live/policy.py:97`. `guarded_cycle` with no valid trust grant **downgrades to
request_only at evaluation time** and records a `refused/no_trust_grant` receipt.

## 3. First-run trust grant UX (guarded cycles without repeated approvals)

The goal: a guarded service cycle must not prompt the operator every time, yet must
never happen on a fresh install. Solution: a **one-time, scoped, durable trust
grant**, separate from the mode flag.

- **Default safe.** A fresh install is `request_only`; `guarded_cycle` is
  unreachable until *both* the mode is set and a trust grant exists.
- **One-time grant, operator-only CLI** (never a model-callable tool):

  ```bash
  agentflow-hermes maintenance trust-grant --gateway hermes-gateway.service --confirm
  ```

  Writes a trust record into `AGENTFLOW_HOME/maintenance.json`:

  ```json
  {
    "trust_grants": [
      { "gateway_unit": "hermes-gateway.service",
        "granted_at": 1750000000.0,
        "host_id": "<sha256(hostname+AGENTFLOW_HOME)[:16]>",
        "scope": "service_cycle",
        "max_cycles_per_day": 2 }
    ]
  }
  ```

  - **Scoped to one exact unit name** (no globs), bound to a `host_id` so a copied
    config can't silently authorize a cycle on a different host (the grant is
    treated invalid if `host_id` mismatches → fail closed).
  - `scope: "service_cycle"` authorizes restart only. A **separate**
    `runtime_install` scope is required before the runner may perform a pinned
    ff-only checkout move + reinstall (§6); absent that scope, the runner cycles
    only and leaves the checkout exactly as the reviewer pinned it.
  - `trust-revoke --gateway <unit>` removes it; `maintenance status` always prints
    grant presence/scope/age so posture is visible before anything fires.
- **No repeated ad-hoc approval.** Once granted, guarded cycles proceed under the
  §7 gates without re-prompting — the per-cycle safety comes from the gates, the
  *authorization* comes once from the grant. This is the deliberate trade: a single
  reviewed human decision replaces N ad-hoc approvals, while every individual cycle
  still has to pass mechanical gates and a fake canary.

## 4. Opportunistic upstream watcher (read-only; M9)

`maintenance/watcher.py::watch_once(store, *, config, probe=None, dry_run=True)`.
Runs from `agentflow-watch.service` (oneshot) on `agentflow-watch.timer`
(default `OnUnitActiveSec=15min`, jittered). **It never mutates git and never
restarts anything.** Steps:

1. **Fetch / ahead-behind (read-only).** `gitprobe.fetch_refs(repo)` runs
   `git -C <repo> fetch --quiet <remote>` (updates remote-tracking refs only — not
   the worktree), then computes `git rev-list --left-right --count
   <upstream>...HEAD` → `(behind, ahead)`. If `behind == 0`, nothing to propose.
2. **Dirty / locally-carried-commit detection.** `git status --porcelain`
   (dirty worktree) and `ahead > 0` (local commits not upstream = "carried"). A
   dirty or carried checkout makes a fast-forward unsafe → the proposal is marked
   `ff_eligible=false` and explicitly flagged "manual rebase required"; the runner
   will **refuse** to move such a checkout (ff-only, §6).
3. **Active worker / tmux check.** `activity.snapshot()` returns counts of inflight
   AgentFlow jobs (non-final rows), and presence of cc/worker tmux sessions
   (`tmux list-sessions` parsed for the configured worker prefixes, read-only).
   If activity is present, the proposal is created but stamped
   `cycle_safe_now=false` (a cycle would interrupt work).
4. **Dedupe against an existing sync graph.** Stable dedupe key
   `maint:sync:<repo_id>:<upstream_sha>` (reuses the cron-bridge dedupe philosophy:
   DB unique + human-auditable key). If an open sync request already exists for the
   same upstream SHA, the watcher **refreshes** it (updates ahead/behind/activity
   facts) instead of creating a second — no storm of duplicate cards.
5. **Create the Kanban sync graph (proposal only).** Via the existing
   `store.enqueue(...)` + kanban bridge, `source_kind="maintenance_sync"`, carrying
   a `GraphIntent` (`docs/ack-remediation-control-plane.md` §1):
   `origin_channel` and `return_target` = the configured Devhub channel
   (`discord:#hermes-main`), `wants_subscription=true`, `intent_kind="remediation"`.
   The card body stores **refs/hashes/short facts only** — `repo_id`,
   `upstream_sha`, `behind`, `ahead`, `dirty`, `ff_eligible`, `cycle_safe_now`,
   reviewed-GO placeholder. **No diffs, no commit messages, no absolute paths, no
   secrets** (reuse `live/sanitize.py` + cron sanitizers; absolute repo path is
   replaced by a `repo_id` hash exactly as `_safe_source_ref` does today).

Watcher safety invariants (smoke-tested): zero `systemctl`/`git checkout`/
`git merge`/`git pull`/`pip` calls on any watch path; `git fetch` is the only git
subprocess and it touches refs only; the created row contains no raw output.

## 5. External runner (privileged actor; M10)

`maintenance/runner.py::run_once(store, *, config, executor, live=False)`. Runs
from `agentflow-runner.service` (oneshot, `Type=oneshot`) on
`agentflow-runner.timer` (default `OnUnitActiveSec=5min`), **in
`agentflow-maintenance.slice`, outside the gateway cgroup** so restarting the
gateway cannot kill the runner mid-cycle. Flow:

1. **Mode/trust gate.** If mode ≠ `guarded_cycle` or no valid trust grant for the
   target unit → exit with a `refused` receipt, no action. (request_only and below
   never reach here.)
2. **Claim a request.** Find the single oldest open `maintenance_sync` request that
   carries a **reviewed sync GO** verdict (parsed via the shared `VerdictParser`,
   `confidence='explicit'`). Single-subject only — never batch-cycles. Claim it via
   an idempotency insert (`channel='service_cycle'`, key
   `maint:cycle:<repo_id>:<upstream_sha>:<unit>`); a duplicate claim returns the
   prior receipt and makes **no** second cycle.
3. **Evaluate §7 gates** (kill switch, allowlist, no-activity, quiet/max-per-day,
   etc.). Any miss → `refused` receipt + dry-run result, no OS effect.
4. **Fake-canary cycle.** Run the entire cycle plan once against
   `FakeServiceExecutor` and assert it returns a healthy post-smoke before touching
   the real executor. A canary failure aborts with `refused/canary_failed`.
5. **Receipt(attempt) → cycle plan (real) → post-smoke → receipt(terminal).** The
   cycle plan is a **fixed, allowlisted sequence**, not arbitrary commands:
   - *(optional, only with `runtime_install` trust scope + runtime-install GO on the
     request)* `git -C <repo> merge --ff-only <reviewed_sha>` then the **pinned**
     install command from config (e.g. `uv pip install -e <repo>`); if the move is
     not a clean fast-forward → abort, no restart.
   - `systemctl --user restart <allowlisted gateway unit>`.
   - **Post-smoke:** wait for the unit to become `active`, then run
     `agentflow-hermes doctor` + the existing live/fake canary against the gateway;
     assert engine-importable + schema-version + `mode: dry-run-first`.
6. **Ledger / Kanban ACK.** On success: write a terminal `applied` receipt and ACK
   the sync request graph (`[JOB ACK]` → `succeeded`) through the existing dry-run
   ACK path, satisfying the request's subscription edge. **No live external
   dispatch** — the ACK is a durable store transition, not a gateway send (live
   dispatch stays governed by `LivePolicy`, off by default).
7. **Failure path — gateway cannot come back.** If post-smoke fails or the unit
   does not return to `active` within the timeout: the runner does **not** retry in
   a loop (circuit-breaker: `set_degraded(True)`, reuse `live/throttle.py`). It
   writes a `failed` receipt and a **local fallback record** — to the AgentFlow
   `deadletter` table *and* `journalctl`/`systemd-cat` under the runner unit — so
   the failure is observable even though the gateway (and therefore the in-gateway
   plugin) is down. It then attempts a single `systemctl --user restart` rollback
   to the last-known-good only if a `runtime_install` move was performed (ff to the
   prior SHA); otherwise it leaves the unit alone and stops, surfacing the incident
   for an operator. All fallback records are refs/short-reasons only — no raw logs,
   secrets, or absolute private paths.

## 6. The only permitted mutations, and where they live

| Mutation | Who | Precondition | Constraint |
| --- | --- | --- | --- |
| `git fetch` (refs only) | watcher | any non-disabled mode | never touches worktree |
| `git merge --ff-only <sha>` | runner | `guarded_cycle` + `runtime_install` grant + runtime-install GO + `ff_eligible` | fast-forward only; abort on conflict/dirty/carried |
| pinned `uv pip install` | runner | same as above | exact command from config, no interpolation |
| `systemctl --user restart <unit>` | runner | `guarded_cycle` + `service_cycle` grant + GO + gates | exact unit ∈ allowlist |

The watcher's "no direct git mutation" requirement is absolute: a dirty/carried/
non-ff checkout is **never** moved automatically — it is flagged for manual
resolution. This is the line that keeps this from being an auto-update daemon.

## 7. Policy gates (exact)

`MaintenancePolicy` (fail-closed, `AGENTFLOW_HOME/maintenance.json`; defaults safe),
evaluated in this fixed order — first failure short-circuits to a refusal receipt:

```python
@dataclass(frozen=True)
class MaintenancePolicy:
    mode: str = "request_only"                 # malformed -> "request_only"
    maintenance_kill_switch: bool = False      # malformed -> True (hard stop)
    allowed_services: tuple[str, ...] = ()      # exact-match unit names, no globs
    repo_path: str = ""                         # the Hermes checkout to probe
    pinned_install_cmd: tuple[str, ...] = ()    # exact argv, no shell, no interpolation
    max_cycles_per_day: int = 2
    min_seconds_between_cycles: int = 1800
    quiet_hours: tuple[int, int] = ()           # e.g. (1, 6): only cycle 01:00–06:00 local
    require_no_active_workers: bool = True
    worker_tmux_prefixes: tuple[str, ...] = ("cc-", "worker-")
    require_reviewed_sync_go: bool = True
    require_runtime_install_go: bool = True     # for any checkout move
    canary_before_cycle: bool = True
```

1. **Kill switch first.** `maintenance_kill_switch` OR the existing live
   `kill_switch` (`AGENTFLOW_KILL_SWITCH=1` / malformed→True) hard-disables every
   cycle. `agentflow-hermes maintenance disable` flips it — the single panic
   control.
2. **Exact service allowlist.** Target unit must be a verbatim member of
   `allowed_services` (`hermes-gateway.service`). No wildcards (blast-radius/
   exfiltration risk, same stance as `live/policy` allowlist).
3. **No active workers / cc tmux / inflight jobs.** `require_no_active_workers` ⇒
   `activity.snapshot()` must show zero inflight non-final jobs and zero matching
   tmux sessions; otherwise refuse (a cycle would interrupt live work).
4. **Max cycles/day + min interval.** Count `service_cycle` `applied` receipts in
   the trailing 24h against `max_cycles_per_day`; enforce
   `min_seconds_between_cycles`. Reuses the receipt table as the throttle source,
   exactly like `live/throttle.py` minute/hour buckets.
5. **Quiet period.** If `quiet_hours` set, cycle only within the window (off-peak),
   using a host-local clock passed in by the runner (not `Date.now()` inside any
   resumable code path).
6. **Fake canary.** `canary_before_cycle` ⇒ a full `FakeServiceExecutor` dry cycle
   must pass before the real executor is touched.
7. **Reviewed sync GO / runtime install GO.** The request must carry an explicit
   `Verdict: GO` (sync), and — if the cycle moves the checkout — a second explicit
   runtime-install GO. `UNKNOWN`/`BLOCK`/`NEED_MORE` ⇒ refuse closed.
8. **No live external dispatch by default.** The watcher and runner never send a
   live message or active wake. Notifications back to Devhub ride the **existing**
   dry-run ACK / subscription edge; turning that into a live send is still governed
   by `LivePolicy.live_dispatch_enabled` (off by default) and is out of scope here.

Idempotency / dedupe / no-storm reuse the committed machinery: idempotency_keys
table + `_make_idempotency_key`, circuit breaker via `set_degraded`, watcher dedupe
key, and the per-day cycle cap. A flapping upstream or a stuck timer cannot produce
a storm of cards or cycles.

## 8. Packaging / plugin surfaces

**CLI (additive `maintenance` namespace; existing commands unchanged):**

- `agentflow-hermes maintenance install-runner [--mode request_only] [--repo <path>] [--gateway <unit>]`
  — the one-command seamless installer (§9 below): init DB/policy, write systemd
  user units+timers, enable `request_only`, run doctor/smoke. Operator-only.
- `agentflow-hermes maintenance uninstall-runner` / `disable` — stop+remove units,
  set `disabled`, optionally purge timers. The clean uninstall path.
- `agentflow-hermes maintenance status` — mode, gate values, trust-grant
  presence/scope/age, last watch facts, last cycle receipt, degraded flag.
- `agentflow-hermes maintenance set-mode <disabled|observe_only|request_only|guarded_cycle>`
  — operator-only.
- `agentflow-hermes maintenance trust-grant|trust-revoke --gateway <unit> [--scope service_cycle|runtime_install] --confirm`
  — operator-only (§3).
- `agentflow-hermes maintenance watch [--dry-run]` — exactly what
  `agentflow-watch.service` invokes; read-only.
- `agentflow-hermes maintenance run [--live]` — exactly what
  `agentflow-runner.service` invokes; `--live` gated, default uses
  `FakeServiceExecutor`.

**Plugin tools (model-callable; keep the ≤8-ish compact budget):** add **at most
one**, read-only:

- `agentflow_maintenance_status` — read-only view of mode, gates, trust-grant
  presence, last watch/cycle (refs/short facts only).

Everything privileged — `install-runner`, `set-mode`, `trust-grant`, `run --live`,
unit install/uninstall — is **operator CLI only, never a model-callable tool**,
matching `plugin-architecture.md` §4 and `live-migration-design.md` §2.10.

**Templates / scripts:** `maintenance/units.py` renders, into
`~/.config/systemd/user/`, four units from string templates (no f-string injection
of untrusted data; only the validated unit name and absolute `ExecStart` of the
installed `agentflow-hermes` console script are substituted):

```ini
# agentflow-watch.service  (Type=oneshot, read-only)
[Service]
Type=oneshot
Slice=agentflow-maintenance.slice
ExecStart=%h/.local/bin/agentflow-hermes maintenance watch --dry-run
# agentflow-watch.timer    OnBootSec=2min  OnUnitActiveSec=15min  (jitter)
# agentflow-runner.service (Type=oneshot, privileged actor, own slice)
ExecStart=%h/.local/bin/agentflow-hermes maintenance run
# agentflow-runner.timer   OnUnitActiveSec=5min
```

`agentflow-maintenance.slice` is deliberately distinct from the gateway's slice so a
gateway restart never reaps the runner.

**After-install docs:** extend `plugins/hermes-agentflow/after-install.md` with a
"Seamless maintenance (optional)" section: the one `install-runner` command, the
explanation that the default is `request_only` (proposals only, nothing restarts),
and the explicit two-step opt-in (`set-mode guarded_cycle` + `trust-grant`) needed
for auto-cycles, plus `disable`/`uninstall-runner`.

## 9. Installer UX — "install only and it works"

`agentflow-hermes maintenance install-runner` is the seam. It is **idempotent** and
performs, in order, with a single summary JSON at the end:

1. **Init DB/policy.** `store.init()` (additive migration, §10), and write a
   default `maintenance.json` with `mode="request_only"`, empty
   `allowed_services`/`trust_grants`, sensible throttles — **all safe defaults**.
2. **Install systemd user units/timer.** Render + `systemctl --user daemon-reload`
   + `enable --now` the watch timer (and the runner timer, which is harmless in
   `request_only` because the runner short-circuits before any effect).
3. **Enable request_only by default.** The watcher begins producing reviewable sync
   proposals immediately; nothing restarts, nothing mutates git → this is the
   "works on install" behavior that is still safe.
4. **Run doctor/smoke.** `agentflow-hermes doctor` + a `maintenance run` against
   `FakeServiceExecutor` to prove the runner path is wired and **refuses** (mode is
   request_only / no trust grant), printing the exact gate that blocked it. The
   installer fails loudly if doctor reports the engine non-importable (the M3
   packaging invariant) or if units fail to enable.

Net first-run result: upstream changes surface as Kanban sync cards with a
subscription back to #hermes-main, the operator reviews them, and **only** after an
explicit `set-mode guarded_cycle` + `trust-grant` does any restart ever happen.

## 10. Backward compatibility & schema

- **Additive subpackage + migration only.** New code lives under
  `maintenance/`; no existing module's behavior changes. The single required
  cross-module touch is additive: `cli.py` gains the `maintenance` subparser
  (existing subcommands untouched).
- **Schema:** add the **next free** migration step (v4 if the
  `ack-remediation` graph columns have not yet landed, else v5) introducing one
  table for cycle bookkeeping; reuse `operator_receipts` (new `channel` values
  `'service_cycle'`/`'watch'`) and `idempotency_keys` rather than forking. Follow
  the established `migrations.py` `PRAGMA user_version` additive pattern; extend
  `test_v*_to_v*_migration_no_data_loss`.

  ```sql
  create table if not exists maintenance_cycles (
    id integer primary key autoincrement,
    request_job_id text not null default '',
    repo_id text not null default '',
    upstream_sha text not null default '',
    unit text not null default '',
    phase text not null,            -- 'claimed'|'canary'|'applied'|'failed'|'refused'
    reason text not null default '',
    created_at real not null
  );
  ```

- **Works with installed AgentFlow 0.1.0 + the existing updated Hermes checkout.**
  Maintenance is entirely opt-in: with no units installed and no `maintenance.json`,
  the engine behaves exactly as today. The 0.1.0 engine already in the gateway venv
  keeps working; `install-runner` is what upgrades behavior, and it only adds.
- **No Hermes core monkeypatch.** The runner reaches the gateway via
  `systemctl --user` (OS), the watcher via `git` CLI on a configured path; neither
  imports Hermes. The plugin half stays within the existing in-process-engine
  adapter (no `python -c`, no repo-relative `PYTHONPATH`), preserving
  `test_plugin_has_no_path_parents_repo_layout_assumption`.

## 11. E2E test plan

All tests use a **temp `HERMES_HOME`/`AGENTFLOW_HOME`** (tmp_path env override) and
**injected fakes** — no real systemd, no real git remote, no real gateway.

- **Fake systemd executor.** `FakeServiceExecutor` records `restart`/`install`/
  `merge_ff` calls and returns scripted health; assert *zero* real-OS calls on every
  non-`guarded_cycle` path.
- **Fake gateway service-cycle.** Post-smoke runs against a fake `doctor`/canary
  returning healthy/unhealthy on demand → exercises both success and the
  gateway-cannot-return failure path (degraded set, deadletter + journal fallback
  written, no retry storm).
- **Watcher read-only proof.** `test_watch_never_mutates` — fake git probe; assert
  no `checkout`/`merge`/`pull`/`pip`/`systemctl` ever invoked; `fetch` touches refs
  only; dirty/carried checkout ⇒ `ff_eligible=false` and no auto-move.
- **Mode matrix.** `disabled`/`observe_only`/`request_only`/`guarded_cycle` each
  produce the §2 behavior; `guarded_cycle` without trust grant downgrades to
  request_only with a `refused/no_trust_grant` receipt.
- **Trust grant.** Grant scoped to one unit authorizes only that unit; `host_id`
  mismatch invalidates the grant (fail closed); `runtime_install` scope required
  before any ff-move.
- **Policy fail-closed on malformed values.** `mode="guarded"` (typo) → resolves
  `request_only`; malformed `maintenance_kill_switch` → `true`; non-bool
  `require_no_active_workers` → safe default. Mirrors the committed
  `live/policy._strict_bool` tests.
- **No raw secrets / private paths in the ledger.** Extend the existing leak tests
  (`test_active_wake_secret_and_private_path_summary_do_not_persist`,
  `test_absolute_source_ref_is_replaced_*`) to watch/cycle rows and receipts:
  absolute repo path → `repo_id` hash; no commit bodies, diffs, secrets, or raw
  smoke logs persisted.
- **Idempotency / dedupe / no-storm.** Same upstream SHA twice ⇒ one card refreshed,
  not two; same cycle claim twice ⇒ one cycle, second `refused/duplicate`;
  `max_cycles_per_day`/`min_seconds_between_cycles`/quiet-hours block extra cycles;
  circuit breaker forces refusal after consecutive failures.
- **Installer smoke.** `install-runner` is idempotent; produces request_only;
  doctor reports engine importable; `maintenance run` against the fake executor
  *refuses* with the exact blocking gate.
- **Existing suites stay green.** `test_store/ack/migrations/cron_bridge/
  kanban_bridge/plugin_adapter/live_dispatch/live_cli` plus the AgentFlow live
  migration tests must remain unchanged and passing. New:
  `test_maintenance_policy.py`, `test_watcher.py`, `test_runner.py`,
  `test_maintenance_cli.py`, `test_maintenance_units.py`,
  `test_v*_to_v*_migration_no_data_loss` extension.

## 12. Milestones

### M9 — Opportunistic upstream watcher (observe + propose; **no mutation**)

`maintenance/policy.py`, `maintenance/gitprobe.py`, `maintenance/activity.py`,
`maintenance/watcher.py`, migration step, `cli.py` `maintenance watch|status|
set-mode`, plugin `agentflow_maintenance_status`. Scope: read-only probe → dedupe →
sync Kanban graph with origin/return/subscription. No systemd, no restart, no git
mutation. Modes `disabled`/`observe_only`/`request_only` fully functional;
`guarded_cycle` parses but the runner does not exist yet (so it is inert).

### M10 — External systemd runner (gated service cycle)

`maintenance/executor.py`, `maintenance/runner.py`, `maintenance/units.py`, the
trust-grant store + `cli.py` `maintenance run|trust-grant|trust-revoke`, failure
fallback (deadletter + journal), circuit-breaker reuse. Scope: `guarded_cycle`
becomes operational behind trust grant + §7 gates + fake canary + post-smoke + ACK.
ff-only checkout move only under `runtime_install` scope.

### M11 — Installer UX + packaging surfaces + docs

`maintenance/installer.py`, `cli.py` `maintenance install-runner|uninstall-runner|
disable`, systemd templates, extended `after-install.md`, uninstall/disable path,
read-only plugin tool finalized. Scope: one-command seamless install that ends in
request_only with doctor/smoke green; clean uninstall.

Each milestone ends with its own review + (for M10/M11) a fake-executor canary
sign-off before the next begins; `guarded_cycle` is never demonstrated against a
real gateway in CI.

## 13. Acceptance criteria

- **M9:** watcher produces exactly one deduped sync card per upstream SHA with a
  subscription edge; dirty/carried/non-ff is flagged, never auto-moved; zero
  privileged/git-mutating calls; no raw secrets/paths in any row; malformed policy
  fails closed; existing suites green.
- **M10:** with mode=`guarded_cycle` + trust grant + reviewed GO + gates passing +
  fake canary green, the fake executor performs the cycle, post-smoke passes, a
  terminal `applied` receipt + Kanban ACK are written, idempotent on re-run; with
  any gate failing, a `refused` receipt is written and zero OS effects occur; the
  gateway-cannot-return path sets degraded, writes deadletter+journal, and does not
  retry-storm.
- **M11:** `install-runner` is idempotent, leaves the system in request_only,
  passes doctor/smoke, and `maintenance run` refuses with the exact gate; uninstall
  removes all units and sets `disabled`; `after-install.md` documents the two-step
  opt-in.
- **Cross-cutting:** the AgentFlow live-migration tests and all baseline suites
  remain green; no Hermes core monkeypatch; no raw secrets/private absolute paths/
  raw transcripts in any persistent ledger row.

## 14. Risks and mitigations

- **Accidental auto-update daemon.** Mitigated by the observe/act split, watcher
  read-only invariant, request_only default, double opt-in (mode + trust grant),
  ff-only-and-only-under-GO checkout moves, and a fake canary before every real
  cycle.
- **Runner killed by the restart it triggers.** Mitigated by running the runner as a
  oneshot in `agentflow-maintenance.slice`, outside the gateway cgroup.
- **Restart of the wrong/too many services.** Mitigated by the exact-match service
  allowlist (no globs) and host-bound, unit-scoped trust grant.
- **Cycle storm / flapping upstream.** Mitigated by watcher dedupe, max-cycles/day,
  min-interval, quiet hours, idempotency, and the shared circuit breaker.
- **Leakage of repo internals/secrets into the ledger.** Mitigated by reusing the
  proven cron/live sanitizers, `repo_id` hashing of absolute paths, and refs-only
  storage; leak tests extended to the new channels.
- **Gateway never returns after a cycle.** Mitigated by post-smoke verification,
  degraded/circuit-breaker (no retry loop), deadletter + journal fallback that does
  not depend on the gateway being up, and a single ff-rollback only when a
  runtime-install move was the cause.
- **systemd-less hosts / restricted environments.** `units.py` detects absent
  `systemctl --user` at install time and degrades to printing the unit files +
  instructions rather than failing the whole install; the watcher still runs via the
  timer-less `maintenance watch` invoked by any scheduler.

## 15. Exact implementation task graph

```text
M9 watcher
  t_m9a  maintenance/policy.py (MaintenancePolicy, modes, fail-closed _strict parse)   -> review t_m9a_r
  t_m9b  maintenance/gitprobe.py (read-only fetch/ahead-behind/dirty/carried/ff-elig)  -> review t_m9b_r
  t_m9c  maintenance/activity.py (inflight jobs + tmux snapshot)                        -> review t_m9c_r
  t_m9d  maintenance/watcher.py (probe->dedupe->sync Kanban graph + GraphIntent)        -> review t_m9d_r
  t_m9e  migration step + maintenance_cycles table + no-data-loss test                  -> review t_m9e_r
  t_m9f  cli.py: maintenance watch|status|set-mode ; plugin agentflow_maintenance_status-> review t_m9f_r
  t_m9g  tests: test_maintenance_policy/watcher + leak + dedupe + read-only invariants  -> review t_m9g_r

M10 external runner   (depends on M9)
  t_m10a maintenance/executor.py (ServiceExecutor + Systemctl/Fake)                     -> review t_m10a_r
  t_m10b trust-grant store + host_id binding + scope (service_cycle|runtime_install)    -> review t_m10b_r
  t_m10c maintenance/runner.py (claim->gates->canary->cycle->post-smoke->ACK->receipts) -> review t_m10c_r
  t_m10d failure path: degraded + deadletter + journal fallback + ff rollback           -> review t_m10d_r
  t_m10e cli.py: maintenance run|trust-grant|trust-revoke                               -> review t_m10e_r
  t_m10f tests: test_runner (fake executor/gateway), idempotency, no-storm, fail-closed -> review t_m10f_r

M11 installer UX      (depends on M10)
  t_m11a maintenance/units.py (systemd user unit/timer templates + slice + daemon-reload)-> review t_m11a_r
  t_m11b maintenance/installer.py (init->units->request_only->doctor/smoke, idempotent) -> review t_m11b_r
  t_m11c cli.py: maintenance install-runner|uninstall-runner|disable                    -> review t_m11c_r
  t_m11d after-install.md seamless-maintenance section + uninstall/disable docs         -> review t_m11d_r
  t_m11e tests: test_maintenance_cli/units + installer smoke + refuses-with-exact-gate  -> review t_m11e_r

fan-in t_m_fanin  waits on: M9 reviews + M10 reviews + M11 reviews
                  + baseline/live-migration suites green + kill switch demonstrated
                  + fake-executor guarded_cycle canary inspected (receipts + ACK)
```

## 16. Hard-constraint compliance check

- **No Hermes core monkeypatch / no private import.** Runner uses `systemctl --user`
  (OS); watcher uses `git` CLI on a configured path; plugin uses the existing
  in-process engine adapter. ✔
- **Additive, gated, fail-closed, dry-run/request-only default.** request_only is the
  install default; guarded_cycle needs mode + trust grant + GO + gates + canary;
  malformed policy/mode fails closed; kill switch first. ✔
- **No raw secrets / private absolute paths / raw transcripts** in any persistent
  ledger (cards, receipts, cycle rows, deadletter, journal refs). Absolute paths →
  `repo_id` hash; refs/short facts only; reuse proven sanitizers. ✔
- **No watcher git mutation; no auto-update daemon.** Watcher fetches refs only;
  the only worktree mutation is a gated, ff-only, GO-pinned move performed by the
  external runner, never by the watcher. ✔
- **Backward compatible** with installed AgentFlow 0.1.0 and the existing Hermes
  checkout; maintenance is fully opt-in and additive. ✔

## 17. Verdict

**GO** for the seamless maintenance plugin control plane + external systemd runner as
a *staged, additive, fail-closed* extension of the existing live-migration/ACK graph,
sequenced as M9 (watcher) → M10 (external runner) → M11 (installer UX) — **conditional
on external supervisor verification of the native Claude Code `--model opus` route**
recorded in §Route evidence. The watcher is read-only and the runner is doubly opt-in,
out-of-cgroup, exact-allowlisted, canary-gated, and receipt-first, so "install only and
it works" is delivered as *proposals on install* while every privileged restart remains
behind explicit, one-time, scoped trust.
