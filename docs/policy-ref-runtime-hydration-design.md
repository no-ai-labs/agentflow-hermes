# PolicyRef runtime hydration for Kanban contracts

Kanban task: `t_113808c3` (native Opus design/check)
Origin/return_to: Discord Devhub / #hermes-main
Repo: `<repo>`
Status: design artifact only — no production code changes in this task.

## Route evidence

- Supervisor preflight resolved `claude` to the ccsupervisor profile wrapper, `claude --version` returned `2.1.196 (Claude Code)`, `claude auth status --text` reported a native Claude Max account, and `tmux` was available at `/usr/bin/tmux`.
- The design check was invoked as native Claude Code Opus: `claude --model opus -p ... --output-format json --max-turns 8` from this repo.
- The Opus run returned `session_id` `cadbccc6-981a-4b79-bb83-d16012e05b26` with `modelUsage` key `claude-opus-4-8` and no OpenRouter/Kimi/Moonshot command path.
- Route self-inspection is still not a cryptographic proof of provider transport. Acceptance depends on the supervisor command evidence above, not on a model's self-report.

## Verdict

**GO (conditional).** PolicyRef + run-time hydration is the right abstraction for durable Kanban / AgentFlow task contracts. It matches the existing design line: `LivePolicy` and `MaintenancePolicy` are resolved centrally at effect time, `GraphIntent` carries refs/facts rather than raw delivery state, `OperatorReceiptLedger` stores compact evidence instead of bodies, and design docs already record resolved route evidence after the fact.

The condition: implement this as a three-layer contract with explicit conflict classes. A naive "refs everywhere, always fail closed" version can deadlock the board when central policy is temporarily unavailable. Contradicted inline policy must stop execution; merely unverifiable central policy should degrade only when prior pinned evidence is available and must surface a risk flag.

## Core abstraction

Kanban bodies must separate three layers that are currently conflated:

| Layer | Binding? | Contents | Lifecycle |
| --- | --- | --- | --- |
| Binding | yes | Policy ref keys such as `policy:model.design`, `policy:model.impl`, `policy:raw_retention` | immutable task contract; resolved at worker preflight |
| Informational preview | no | optional resolved value as of graph creation, labeled non-binding with `resolved_at` and `policy_version` | may go stale; never used as worker instruction |
| Run evidence | no | what the worker actually resolved and used | append-only run metadata / ledger / Kanban comment |

The observed bug is that concrete route preview text in old task bodies was treated as binding policy. The fix is not to delete all previews; it is to ensure only refs bind, previews are clearly non-binding, and run evidence records the actual resolution.

## Data model

Additive objects:

```python
@dataclass(frozen=True)
class PolicyRef:
    key: str              # e.g. policy:model.design, policy:model.impl
    required: bool = True

@dataclass(frozen=True)
class PolicyResolution:
    key: str
    policy_version: str   # central policy hash/version at resolve time
    resolved_at: float
    source: str           # central | sanctioned_override | pinned_evidence
    provider: str         # e.g. anthropic-native
    model: str            # e.g. claude-opus-4-8
    command_ref: str      # sanitized command shape, not raw shell transcript
    evidence_ref: str     # bounded smoke/result ref or hash
    conflict_class: str   # none | redundant | contradicted | override | unverifiable

@dataclass(frozen=True)
class PolicyOverride:
    ref_key: str
    pinned_value: str
    reason: str
    author_ref: str
    expires_at: float
```

Persistence should be additive and sanitized:

- `jobs.policy_refs_json` or equivalent graph metadata for binding refs.
- `policy_resolution` ledger channel in `job_events` / `operator_receipts`, storing refs, short enums, version hashes, and sanitized evidence refs only.
- Operator-only `policy_overrides` with exact-key matching and expiry.
- No raw tokens, raw command logs, raw task bodies, private paths, or gateway transcripts in the policy ledger.

Policy keys need a versioned, deprecation-only lifecycle. Do not rename or silently change key meaning without retaining old-key compatibility or an explicit migration proposal.

## Refs vs immutable task facts

Use a simple rule: make it a policy ref if it is operator-owned, shared across many tasks, and can change independently of a specific task. Keep it as an immutable task fact if it defines this task's scope, provenance, or graph topology.

| Domain | Ref? | Rationale |
| --- | --- | --- |
| Model route / provider / command | Ref | This is exactly the drifting value; resolve at run time and record evidence. |
| Service-cycle / maintenance policy | Ref | `MaintenancePolicy` modes, gates, trust grants, and service allowlists are central runtime posture. |
| Approval / live policy | Ref | `LivePolicy` flags, kill switch, live dispatch, active wake, throttles, and allowlists must not be copied into bodies. |
| ACK / subscription policy | Ref | Subscription gate, verify requirements, active wake posture, and delivery policy are central. |
| Raw-retention / sanitization policy | Ref | Uniform safety posture; a task body must not weaken retention or leak rules. |
| Review / fan-in policy | Mixed | Dependency edges and graph topology are immutable task facts; semantic verdict rules and remediation/supersession policy are refs. |
| Origin, return target, correlation, graph id, blocker ref, baseline SHA | Immutable task facts | These identify and scope the unit of work. They may be validated against policy, but central policy must not rewrite what the task is. |

## Precedence and conflict handling

Task-specific facts win for task scope and provenance. Central policy wins for policy-class values unless a sanctioned override exists.

| Conflict class | Condition | Action |
| --- | --- | --- |
| `none` | Body carries refs only | Resolve, record evidence, proceed. |
| `redundant` | Inline concrete value agrees with central policy | Proceed, flag for migration/cleanup. |
| `contradicted` | Inline binding value conflicts with central policy and no sanctioned override exists | BLOCK/NEED_MORE with refused receipt; do not run on the stale route. |
| `override` | Structured, allowlisted, unexpired `PolicyOverride` exists | Proceed on pinned value, record override evidence, surface in report. |
| `unverifiable` with prior pinned evidence | Central policy unavailable/malformed, but previous policy version/evidence is available | Degrade-proceed with `policy_stale_unverified` flag only for low-risk route continuity; never for weakening retention/live safety. |
| `unverifiable` without evidence | No central policy and no pinned evidence | NEED_MORE; cannot resolve the contract. |

Important distinction: contradicted policy is a safety stop; temporarily unverifiable policy is an availability problem. Treating both as hard fail-closed can make the whole board unavailable.

Policy overrides must be structured data, not prose. They should be operator CLI only, exact-key matched, expiring, and recorded. Wildcard or model-editable overrides reintroduce the blast-radius problem.

## Stale inline policy detector

Build a classifier, not a grep rule. It must identify the role of concrete policy text:

- Binding instruction to the worker: candidate conflict.
- Historical route evidence: allowed and preserved.
- Fenced fixture / changelog / design prose: allowed unless the body tells the worker to execute it.
- Sanctioned override block: allowed only if structured and validated.

Initial detector patterns should include concrete route/provider strings such as `claude-openrouter-opus`, `OpenRouter Opus`, `anthropic/claude-opus-*`, `claude --model opus` when phrased as a binding directive, `Kimi`, `Moonshot`, and wrapper aliases. The classifier must avoid false positives in existing docs' route-evidence sections.

Ship first as a read-only dry-run migration scan across all boards. Output should include task id, matched snippet class, policy ref candidate, conflict class, and proposed action. No destructive rewrite by default.

## Worker preflight

Every Kanban worker should run a policy preflight before delegation:

1. Load central policy and policy key registry.
2. Parse binding `PolicyRef`s from GraphIntent / task metadata.
3. Scan the body for stale inline policy and classify matches.
4. Resolve each ref to provider/model/command/source/version.
5. Verify route smoke where applicable: e.g. command exists, auth status is the expected native route, model probe returns expected model usage/evidence.
6. Write a sanitized `policy_resolution` ledger event and a compact Kanban comment or run metadata entry.
7. Apply conflict table: proceed, proceed-with-risk, BLOCK, or NEED_MORE.

Ambiguous conflicts fail closed. Raw smoke logs and secrets never enter the ledger.

## Graph creation wrapper

Graph creators should stop copying binding policy values into task bodies. Instead:

```yaml
Policy refs:
  - policy:model.design
  - policy:raw_retention
  - policy:ack_subscription

Resolved preview (informational, non-binding):
  policy_version: <hash>
  resolved_at: <timestamp>
  model.design: native Claude Code Opus
```

The wrapper may include a preview for human readability, but workers must ignore it as an instruction and resolve refs at run time. Tests should assert that changing the central policy after task creation changes the next run's resolution even when the old preview remains in the body.

## Migration path

1. Read-only scan all boards and active tasks for inline concrete policy values.
2. Classify candidates and generate a dry-run report.
3. Propose non-destructive migrations: append a `Policy refs` block and a deprecation annotation; do not silently delete old durable contract text.
4. For old bodies with contradictory binding text, worker preflight should BLOCK/NEED_MORE until an operator accepts a migration or writes a sanctioned override.
5. Apply mode, if added, must be gated, idempotent, receipt-first, and append-only by default.

This mirrors the stale-final design stance: immutable durable rows are not rewritten out from under history; they are superseded or annotated with evidence.

## Integration with existing AgentFlow components

- `GraphIntent`: add/carry `policy_refs` beside origin/return/correlation. Origin and return target remain task facts validated against live allowlists.
- `SubscriptionEnsurer`: unchanged core behavior; uses hydrated ACK/subscription policy and includes policy evidence in final reports.
- `OperatorReceiptLedger`: add a `policy_resolution` read channel so resolvers can query what policy version was used without re-reading raw text.
- `VerdictParser`: policy conflicts should produce explicit `Verdict: BLOCK` / `NEED_MORE` with named blockers such as `stale_inline_route`, independent of task status.
- `RemediationPlanner`: a named policy-conflict BLOCK can propose a narrow remediation graph: append policy refs, review migration, final-v2. No broad auto-unblock.
- `StaleFinalResolver`: no direct change, but use the same immutable-row / superseding-candidate philosophy for migrated task contracts.
- Seamless maintenance runner: resolve route/service-cycle policy through the same preflight; its trust grants and maintenance gates remain central policy, not copied task text. This keeps the seamless maintenance runner design aligned with PolicyRef hydration instead of copying service-cycle policy into task bodies.

## Milestones

### MP0 — read-only detector and board scan

- Add policy key registry and stale inline classifier.
- Add dry-run scan CLI for all boards/tasks.
- Verify existing docs' route-evidence sections are not misclassified as binding conflicts.
- Acceptance: scan reports stale binding route text without rewriting anything.

### MP1 — resolution and evidence spine

- Add central resolver and `PolicyResolution` data structure.
- Add `policy_resolution` ledger channel / sanitized run metadata.
- Add route smoke probe for native Claude Code route evidence.
- Acceptance: worker records policy version, resolved provider/model/command ref, resolved_at, evidence_ref; no enforcement yet.

### MP2 — preflight enforcement

- Enable conflict table in workers.
- BLOCK contradicted inline policy; degrade or NEED_MORE on unverifiable per table.
- Add structured `PolicyOverride` support, operator-only and expiring.
- Acceptance: stale route in an old task prevents wrong-route delegation; sanctioned override proceeds with surfaced evidence.

### MP3 — graph creation wrapper and migration

- Extend GraphIntent / graph creation to emit policy refs and non-binding previews.
- Add non-destructive migration proposal/apply path.
- Acceptance: new cards contain refs, not binding commands; old cards can be annotated idempotently; no destructive rewrite by default.

### MP4 — integration and hardening

- Wire policy-conflict BLOCKs into VerdictParser and RemediationPlanner.
- Extend maintenance runner and ACK/subscription reporting with policy evidence.
- Add leak and adversarial tests.
- Acceptance: existing live/ack/maintenance suites remain green; policy ledger has no raw secrets/private paths/log bodies.

## E2E and adversarial probes

Required tests/probes:

- Old task with `claude-openrouter-opus` while central policy resolves native Opus: classified `contradicted`, BLOCK, no wrong-route run.
- Central model policy changes after task creation: next run resolves new policy while old run evidence remains auditable.
- Explicit override: accepted only with structured, allowlisted, unexpired override; free-text "use OpenRouter" does not override.
- Malicious body injection: fake policy text in the task body cannot trick resolver into using a weaker route or retention policy.
- Missing policy: NEED_MORE if no prior evidence; degrade-with-flag only when allowed and evidence exists.
- Malformed policy: fail closed for safety-sensitive retention/live policy; do not silently assume permissive defaults.
- Detector false positives: historical route evidence, fenced fixtures, and design docs are classified non-binding.
- Ledger sanitization: no raw secrets, private absolute paths, raw command logs, or raw task bodies in `policy_resolution` events.
- Cross-board propagation: scan covers all configured boards and reports board/task refs without destructive writes.
- Graph wrapper: new tasks contain `PolicyRef`s and optional non-binding preview only.

## Risks

- Board-wide denial of service if central route policy outage is treated the same as contradiction.
- Ref-key drift replacing value drift unless key lifecycle is versioned and deprecation-only.
- Informational previews reintroducing stale binding values unless worker tests prove previews are ignored.
- Override allowlist becoming a privileged exfiltration surface if wildcard/model-editable.
- Bootstrap problem: the location and trust root for central policy must be operator-owned and outside task-body control.

## Implementation stance

Proceed. The abstraction is right, but implement it incrementally: scan first, record evidence second, enforce third, migrate last. The first production slice should be MP0 + MP1 only; MP2 enforcement should wait until the evidence spine has soaked on live boards and detector false positives are under control.
