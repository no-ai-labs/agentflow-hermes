# M13 bounded real-executor canary design gate

Kanban task: `t_caf6865e` (Fable/Opus design consultation)
Origin/return_to: Discord Devhub / #hermes-main
Baseline: `main@37f4aca` (MP4d live policy-gated adapter boundary)
Status: design artifact only — no production code changes in this task.

## Route evidence

- Fable was requested first if configured/available. Supervisor preflight from this
  repository found no `fable` executable in `PATH`: `fable: NOT FOUND in PATH`.
- Fallback design route was native Claude Code Opus, not Sonnet/Kimi/Moonshot/
  OpenRouter: `claude` resolved to
  `/home/duckran/.hermes/profiles/ccsupervisor/home/bin/claude`,
  `claude --version` returned `2.1.198 (Claude Code)`, and
  `claude auth status --text` reported `Login method: Claude Max account`.
- The authoritative design consultation was invoked from this repository as
  `claude --model opus -p ... --output-format json --max-turns 12` using prompt
  `.kanban-t_caf6865e-opus-design-prompt.md`. It returned `session_id`
  `a326955f-5b81-4724-8754-80ad6f70b130` with `modelUsage` key
  `claude-opus-4-8` and `subtype=success`.
- Route self-inspection is not cryptographic proof; acceptance depends on the
  supervisor command evidence above and on this file remaining a design-only
  artifact.

## 0. Verdict

**Verdict: GO on the current structure, GO to implement M13 non-privileged
prerequisites, BLOCK on any real `systemctl` canary until those prerequisites are
implemented and reviewed.**

The current M10/M11/M12/MP4d layering is correct for moving toward a bounded real
executor canary:

- the gateway/control-plane side remains non-mutating;
- the external runner owns the service-action boundary and is outside the gateway
  cgroup;
- the installer renders/writes dry-run request-only unit/timer files by default;
- the trust grant is explicit, atomic, host-bound, expiring, and exact-unit scoped;
- the action adapter is reachable only after runner gates, and remains fake/noop by
  default.

The architecture is therefore sound as a foundation, but it is not yet sufficient
to run a real service restart canary. The missing pieces are durable receipts,
cross-process idempotency, the remaining policy gates, reviewed-GO request
consumption, post-smoke verification, and the failure/degraded path.

## 1. Structure audit

| Invariant | Status | Evidence |
| --- | --- | --- |
| Gateway remains non-mutating | GO | Current design keeps watcher/plugin paths observe/propose only; the service boundary is outside the gateway. |
| External runner owns service action boundary | GO | `src/agentflow_hermes/maintenance/runner.py` evaluates the runner gates; `units.py` places the runner in `agentflow-maintenance.slice`. |
| Installer is dry-run/request-only by default | GO | `installer.py` renders by default, writes only with explicit `write_files=True` and explicit `unit_dir`; no `systemctl` call is present. The rendered `ExecStart` is `maintenance runner evaluate --input-file <config>`, not a live runner. |
| Trust grant is explicit/atomic/host-bound/expiring/exact allowlist | GO | `trust.py` writes mode, requested action, target unit, one-unit allowlist, and grant record together; validates host binding, finite timestamps, expiry, unit suffix, and whole collection shape. |
| Adapter after gates, fake/noop default | GO | `evaluate_runner` checks kill switch, guarded mode, exact allowlist, and valid trust grant before resolving the adapter; with no explicit executor/adapter the eligible path returns a proposal only. `LiveActionAdapter` is disabled by default. |

One semantic cleanup remains: `load_maintenance_policy()` currently treats
`guarded_cycle` as a valid parsed mode even when the allowlist/grant is absent;
`evaluate_runner()` still blocks safely with `service_not_allowlisted` or
`no_trust_grant`. M13 should align the observable semantics with the design text:
`guarded_cycle` without a valid allowlisted grant should degrade to request-only or
record a clear `refused/no_trust_grant` receipt, without widening safety.

## 2. Load-bearing gaps before any real canary

1. **No durable service-cycle receipts.** The runner returns a sanitized in-memory
   report, but it does not yet persist `attempt`, `applied`, `failed`, or `refused`
   receipts to `operator_receipts` or a `maintenance_cycles` table.
2. **No cross-process idempotency.** `FakeActionAdapter` idempotency is in-memory.
   A systemd oneshot canary requires a DB-backed claim such as
   `maint:cycle:<repo_id>:<upstream_sha>:<unit>` so two processes cannot double
   restart the same unit.
3. **Missing runtime gates from the original design.** Activity/no-active-workers,
   max-cycles-per-day, min-interval, quiet-hours, and request graph reviewed-GO
   consumption are not wired end-to-end yet.
4. **No post-smoke/failure path.** There is no durable wait-for-active + doctor /
   canary smoke, degraded/circuit-breaker state, deadletter fallback, journal
   fallback, or no-retry-storm proof.
5. **No real reviewed request claim.** The current service path is driven by
   config `requested_action=service_cycle`; M13 must bind service action to a
   single claimed maintenance sync request with an explicit parsed `Verdict: GO`.

These are blockers for real execution, not blockers for the current structure.

## 3. M13 implementation scope

M13 should be a prerequisite implementation slice, not the live restart slice.
Minimal repo changes:

1. Add the next additive migration for `maintenance_cycles` and extend the
   no-data-loss migration test.
2. Add durable receipt/idempotency helpers that reuse existing
   `operator_receipts` and `idempotency_keys` patterns. The runner must persist
   `refused`, `attempt`, and terminal receipts with sanitized refs only.
3. Wire cross-process idempotency into the runner before any adapter construction.
   A duplicate claim returns the prior durable decision and performs no action.
4. Wire the remaining policy gates into runner evaluation:
   - activity snapshot / no active workers / worker tmux;
   - max cycles per day;
   - min seconds between cycles;
   - optional quiet-hours with injected host-local clock;
   - explicit reviewed sync GO via the shared verdict parser;
   - immediate pre-effect recheck for activity and kill switch.
5. Add post-smoke and failure-path scaffolding against fakes:
   wait-for-active abstraction, doctor/canary abstraction, degraded/circuit-breaker
   write, deadletter/journal-ref fallback, and no retry loop.
6. Add a disabled-by-default real executor boundary only after the fake path has
   durable gates and receipts. It must not be reachable from the existing
   installer-rendered timer/service template.

Implementation may start on items 1-5 after this design review. The actual real
`systemctl --user restart` canary must remain a later, separately reviewed lab
step.

## 4. Acceptance criteria for M13 prerequisites

- With no grant, malformed grant, expired grant, host mismatch, wrong unit,
  kill switch, missing reviewed GO, activity present, throttle exhausted, or
  quiet-hours miss: durable `refused` receipt, zero adapter construction, zero OS
  effect.
- With a valid grant, explicit reviewed GO, all gates passing, and fake canary
  green: exactly one fake service action, durable `attempt` then terminal `applied`
  receipt, and idempotent replay on a second process/run.
- Duplicate oneshot invocation with the same service-cycle claim returns the prior
  receipt and performs no second action.
- Crash/recovery between `attempt` and terminal receipt reconciles to a single
  terminal state and never double-fires.
- Post-smoke failure writes durable `failed`, sets degraded/circuit breaker, writes
  refs-only deadletter/journal fallback, and does not retry-storm.
- Durable rows contain source refs, policy refs, idempotency keys, unit refs, short
  reasons, and sanitized `repo_id` only. They must not contain raw private paths,
  raw smoke logs, secrets, transcripts, or task bodies.
- Existing maintenance, loop, live, store, ack, bridge, and migration suites remain
  green.

## 5. First real canary constraints (later gate)

The first real canary, after M13 prerequisites pass review, must be a lab-only
restart of a sacrificial systemd user unit on an isolated host. It must not target
the production Hermes gateway and must not be wired to the timer. Preconditions:

1. M13 durable receipts/idempotency/gates/post-smoke/failure path merged and
   reviewed.
2. Explicit operator-created runtime policy and host-bound trust grant for the
   sacrificial unit; no allowlist or grant broadening via task body.
3. Explicit reviewed sync GO or canary-specific GO event consumed by the runner.
4. Fake canary passes in the same run before the real executor is constructed.
5. Real executor enabled only by an explicit lab harness or CLI flag that is not
   present in the installed timer/service template.
6. Supervisor verifies exactly one restart, terminal receipt, idempotent replay,
   and post-smoke result.

## 6. Forbidden until later

- Real `systemctl` against the production Hermes gateway.
- Any CI demonstration of `guarded_cycle` against a real gateway.
- Timer-driven real execution through `agentflow-runner.timer`.
- `runtime_install`, `git merge --ff-only`, checkout movement, pinned install, or
  rollback of a real checkout.
- Live send, active wake, or board rewrite.
- Any model-callable privileged verb, including `trust-grant`, live runner, unit
  install, or real service action.
- Wildcard service allowlists, multiple-service grants, or task-body-driven grant
  broadening.
- Retrying a failed restart in a loop.

## 7. Adversarial reviewer tests

M13 reviewers must add or require tests for:

1. duplicate separate-process service-cycle invocation: one action maximum;
2. crash after `attempt` receipt and before terminal receipt: no double-fire on
   recovery;
3. `status=done` with semantic `Verdict: BLOCK`, missing verdict, or `UNKNOWN`:
   no service action;
4. activity appears after initial gate check: immediate pre-effect recheck blocks;
5. cycle cap, min-interval, and quiet-hours boundary conditions with injected
   clock;
6. post-smoke timeout/unhealthy result: degraded + deadletter + journal-ref, no
   retry storm;
7. real executor class exists but enabled flag is false: `BLOCK`/`NOOP`, no
   `systemctl` call;
8. copied config to another host: host binding mismatch refuses;
9. non-sacrificial or production unit in canary harness: exact allowlist refuses;
10. malformed trust-grant collection with one valid and one invalid record: whole
    collection invalid;
11. nonfinite timestamps and expiry-at-or-before-created: fail closed;
12. durable receipt leak audit for paths, secrets, raw logs, and transcripts.

## 8. Final recommendation

Proceed with M13 as a design-backed, non-privileged implementation of durable
receipts, DB idempotency, remaining gates, fake post-smoke, and failure-path
scaffolding. Do not proceed to a real `systemctl` canary in the same slice. Once
M13 prerequisites pass review, create a separate lab-only canary task for a
sacrificial unit with explicit route evidence, reviewed GO, and operator-scoped
trust grant.
