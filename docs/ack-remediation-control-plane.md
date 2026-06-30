# AgentFlow ACK subscription edge + BLOCK auto-remediation control plane

Kanban task: `t_9ac3318d` (Opus design gate)
Origin/return_to: Discord Devhub / #hermes-main
Baseline: branch `af/live-dispatch-control-plane` at `4530434` (M6 live gates committed)
Related implementation task: `t_dc0c0996` (M6 live-dispatch-control-plane code)
Status: design artifact only — no production code changes in this task.

## Route evidence

- This artifact was produced under **native Claude Code `--model opus`** on the Anthropic Claude Code subscription route (model id `claude-opus-4-8`, Opus 4.8). It does **not** use the OpenRouter/Kimi wrapper route that the earlier `docs/m1-m2-design.md`, `docs/plugin-architecture.md`, and `docs/live-migration-design.md` were authored under.
- Runtime self-inspection of the route from inside the process is not fully self-verifiable; **supervisor verification of the native `--model opus` route is required externally** and is a precondition of accepting this design's verdict.
- This document **extends** `docs/live-migration-design.md` (M6/M7/M8 live control plane) and `docs/plugin-architecture.md` (§5 channel separation, §8 guarded resolver). It does not supersede their decisions; it adds the ACK-subscription edge and the BLOCK→remediation planning layer on top of the same staged, opt-in, fail-closed graph.

## 0. Problem statement

Two recurring control-plane failures are not yet covered by the committed code:

1. **Missing subscription edge.** A task is created and dispatched, but the origin/operator is never reliably *subscribed* to that task's completion. When the worker ACKs, the verdict has nowhere to return — the operator never learns the outcome. This is the abstract **Warroom G8.x `missing_subscription`** incident.
2. **Stale final fan-in.** A fan-in job records a terminal verdict (`done` / semantic `BLOCK`) and is locked by the `FINAL_STATES` guard. A *later* remediation review returns `GO`, but the stale final is never superseded, so the graph stalls on a verdict that is no longer true. This is the abstract **AgentFlow `stale_final_fanin`** incident.

Both share a root cause already named in `docs/plugin-architecture.md` §5 and `docs/live-migration-design.md` §2.9: **`task.status` is not a verdict, and a verdict is not a delivery.** The control plane conflates "the worker stopped" with "the operator was told" and with "the semantic answer is GO." This design makes each edge first-class, evidence-first, and policy-gated, reusing the M6 receipt/idempotency/gateway machinery rather than forking it.

Design stance (inherited, non-negotiable): **additive and gated, never a mode switch.** Every new path is dry-run/proposal by default; live effect requires config enablement AND per-call `live=True` AND allowlist AND idempotency AND a receipt-before-effect. Any miss fails closed to dry-run.

## 1. First-class `GraphIntent` / `origin_return` capture

Today `origin_return` is a free-text column on `jobs` (`store.enqueue(origin_return=...)`) and a line in the dispatch prompt. It is captured but never *structured*, *validated against the allowlist*, or used to close the loop. We promote it to a first-class `GraphIntent` value object.

Add `src/agentflow_hermes/graph/intent.py`:

```python
@dataclass(frozen=True)
class GraphIntent:
    origin_channel: str        # e.g. "discord:#hermes-main" — where the request came from
    return_target: str         # where the verdict must be delivered (often == origin_channel)
    correlation_id: str        # graph-wide correlation; defaults to job id
    causation_id: str = ""     # the job/event that caused this one
    graph_id: str = ""         # fan-in/graph grouping id, if part of a graph
    intent_kind: str = "task"  # 'task' | 'review' | 'fanin' | 'remediation'
    wants_subscription: bool = True   # operator expects to be told the verdict
    wants_active_wake: bool = False   # operator expects a proactive resume (separately gated)
```

Rules:

- `GraphIntent` is parsed/normalized once at enqueue time from the existing `enqueue()` args plus the cron/kanban bridge metadata. No new raw payloads — only refs/ids/targets/short kinds, identical to the existing leak posture.
- `return_target` is validated against the live policy allowlist **at capture time** (warn-only / metadata in dry-run; enforced when subscription delivery goes live). A `return_target` that is not allowlist-eligible is recorded as `intent_target_unverified`, never silently delivered.
- Persisted additively (migration v4, §7): `jobs.graph_id`, `jobs.intent_kind`, `jobs.wants_subscription`, `jobs.wants_active_wake`. `origin_return`, `correlation_id`, `causation_id` already exist — `GraphIntent` reads them, it does not duplicate them.
- The dispatch prompt (`render_dispatch_prompt`) gains a compact `[GRAPH INTENT]` block (kind, graph_id, return_target ref) so a worker's ACK can echo the correlation back. No transcripts, no absolute paths.

`GraphIntent` is the single object the SubscriptionEnsurer (§2), VerdictParser (§4), RemediationPlanner (§5), and StaleFinalResolver (§6) all read to know *where a verdict is supposed to go and which graph it belongs to*.

## 2. `SubscriptionEnsurer`: create → notify-subscribe → verify → `ack_subscription_status`

The `missing_subscription` incident is a missing edge between "task created" and "operator will hear the verdict." `SubscriptionEnsurer` makes that edge explicit and **verified**, not assumed.

Add `src/agentflow_hermes/graph/subscription.py`. It is a thin orchestrator over the M6 gateway capability boundary (`live/gateway.py`) — it never imports Hermes core.

State machine (`ack_subscription_status` column on `jobs`, migration v4):

| Status | Meaning |
| --- | --- |
| `unsubscribed` | default; no subscription attempted (dry-run, or wake/subscribe gate off) |
| `subscribe_attempted` | notify-subscribe call issued, awaiting verify |
| `subscribed` | notify-list verify confirmed the return_target is subscribed |
| `subscribe_failed` | subscribe or verify failed; fail-closed, ret/throttle applies |
| `verify_unavailable` | gateway has no notify-list capability; cannot prove subscription |

Flow (`ensure_subscription(store, job_id, *, gateway=None, policy=None, live=False)`):

1. **create** — the job already exists; read its `GraphIntent`. If `wants_subscription` is false → `unsubscribed`, done.
2. **notify-subscribe** — if `live` and the subscribe gate is on and `return_target ∈ allowed_targets`: write an `operator_receipt` (`channel='subscription'`, `phase='attempt'`) **before** calling `gateway.subscribe(target=...)`. No live opt-in / gate off / target not allowed ⇒ refusal receipt + `unsubscribed`, no call.
3. **notify-list verify** — call the gateway's list/confirm capability and assert `return_target` appears. This is the load-bearing step the incident was missing: *we do not trust the subscribe call, we verify it.* If the gateway exposes no list capability → `verify_unavailable` (NOT `subscribed`), receipt `phase='refused', reason='verify_unavailable'`.
4. **`ack_subscription_status`** — persist the resulting status + a terminal receipt (`applied`/`failed`/`refused`). Emit a `subscription` ledger event (distinct channel, §3).

Hard rules:

- Subscription is its **own gate** (`subscription_enabled`), distinct from `live_dispatch_enabled` and `active_wake_enabled`. Enabling dispatch does not enable subscribe/verify.
- A subscription is only `subscribed` when **verify** confirms it — an unverified subscribe is `subscribe_attempted`, never `subscribed`. This is the direct fix for `missing_subscription`: the bug was treating "subscribe issued" as "operator will be told."
- All effects are receipt-first and idempotent on `(channel='subscription', job_id, return_target, correlation_id)`, reusing `store._make_idempotency_key` and the `idempotency_keys` table.
- Dry-run default emits a structured *plan* (`would_subscribe`, target ref, gate state) and mutates nothing external.

## 3. `OperatorReceiptLedger`: four separated channels

`docs/live-migration-design.md` §2.9 already separates `task_verdict` / `passive_delivery` / `active_wake` / `operator_receipt`. This design adds the **subscription** sub-channel and formalizes the ledger as a read model so the resolvers/planners query it instead of re-deriving state. No schema fork — it reads the existing `job_events` + `operator_receipts` tables.

| Channel | Storage | Event kinds | Means |
| --- | --- | --- | --- |
| `task_verdict` | `jobs.status` + ACK ledger; **plus** parsed semantic verdict (§4) | `ack_applied`, `duplicate_ack`, `ack_rejected`, `verdict_parsed` | What a worker/reviewer *decided*. `status=done` ≠ `GO`. |
| `passive_delivery` | `passive_delivery` events + `jobs.live_delivery_ref` | `delivery_attempted/succeeded/failed` | A report was sent. NOT proof of success. |
| `active_wake` | `active_wake` events | `wake_requested/dispatched/disabled/refused` | A proactive resume was requested/fired. Separately gated. |
| `subscription` | `subscription` events + `jobs.ack_subscription_status` | `subscribe_attempted/subscribed/subscribe_failed/verify_unavailable` | The return edge was established **and verified**. |
| `operator_receipt` | `operator_receipts` table (`channel` now incl. `'subscription'`) | own table | Audit of proposed/refused/applied operator action. |

Add a thin `graph/ledger.py` `OperatorReceiptLedger` read API: `verdict_of(job)`, `subscription_of(job)`, `deliveries_of(job)`, `last_receipt(job, channel)`. It is pure read — planners use it to assert "this BLOCK was never superseded" or "this verdict was never delivered" without duplicating SQL.

Invariants (smoke-tested, extends the existing §2.9 invariant):

- `delivery_succeeded` MUST NOT set/imply a `task_verdict`.
- `subscribed` MUST NOT imply a `task_verdict` or a delivery — it only proves the return edge exists.
- A terminal `task_verdict` MUST NOT imply any `subscription`/`delivery`/`wake` event exists. (This is exactly the `missing_subscription` failure surfaced as a checkable invariant.)

## 4. `VerdictParser`: semantic `GO` / `BLOCK` / `NEED_MORE`, independent of `task.status`

The `_extract_verdict` regex inside `bridges/kanban.py` already distinguishes `GO`/`BLOCK`/`NEED_MORE` but it is private to the kanban resolver. Promote it to a shared, single source of truth so the lifecycle status and the semantic verdict can never be conflated.

Add `src/agentflow_hermes/graph/verdict.py`:

```python
@dataclass(frozen=True)
class SemanticVerdict:
    verdict: str          # 'GO' | 'BLOCK' | 'NEED_MORE' | 'UNKNOWN'
    confidence: str       # 'explicit' | 'none'
    source_ref: str       # ref/hash of the review artifact, never raw text
    blocker_ref: str = "" # for BLOCK: the named blocker, if structured

def parse_verdict(review_text_or_meta, *, source_ref="") -> SemanticVerdict: ...
```

Rules:

- **Independent of `task.status`.** `parse_verdict` reads the review's *content marker* (`Verdict: GO|BLOCK|NEED_MORE`), exactly the existing kanban regexes (lift `_VERDICT_*_RE` here; kanban imports them back so behavior is unchanged). A job can be `status=succeeded`/`done` and carry `verdict=BLOCK`, or vice versa — the parser never infers verdict from status.
- Only `confidence='explicit'` verdicts drive any auto action. A review with no explicit marker is `UNKNOWN` and fails closed (no remediation, no supersession).
- The parsed verdict is recorded as a `verdict_parsed` event on the reviewed job (task_verdict channel) carrying `verdict` + `source_ref` only — no review body.
- `bridges/kanban.py` is refactored to call `parse_verdict` (it currently has the only verdict logic). This removes the duplicate-truth risk and is the only required cross-module change; it is behavior-preserving and covered by existing `test_kanban_bridge.py`.

## 5. `RemediationPlanner`: fan-in `BLOCK` → narrow fix / review / final-v2 proposals

When a fan-in review is `BLOCK` (per §4), the graph needs a *narrow, evidence-first* remediation proposal — not a broad auto-unblock (the explicit non-goal in `plugin-architecture.md` §8 and `live-migration-design.md` §5). `RemediationPlanner` produces a **proposal**, never a graph mutation, by default.

Add `src/agentflow_hermes/graph/remediation.py`:

`plan_remediation(blocked_fanin, block_verdict, *, dry_run=True, policy=None, live=False) -> RemediationPlan`

A `RemediationPlan` is a bounded, ordered proposal of at most three correlated successor jobs:

1. **`fix`** — a narrowly scoped fix job targeting the *named blocker only* (`SemanticVerdict.blocker_ref`). If the blocker is not explicitly named/structured → refuse closed (`block_has_no_named_blocker`). No "fix everything" jobs.
2. **`review`** — a remediation review job correlated to the fix, whose GO is what later supersedes the stale final (§6).
3. **`final-v2`** — a re-run of the fan-in/final correlated to the original via `graph_id` + `causation_id`, proposed only after fix+review.

Guards (all fail-closed, mirroring the kanban resolver):

- Single-subject: one blocked fan-in + one BLOCK verdict. Never scans all blocked jobs.
- The BLOCK must be `confidence='explicit'` and name its blocker (structured metadata preferred, per kanban's `_has_structured_remediation_relation`).
- Provenance: every proposed successor carries `correlation_id`/`graph_id`/`causation_id` back to the blocked fan-in. No orphan proposals.
- **Dry-run default**: emits the plan as data (`proposals: [{kind, scope_ref, correlation, reason}]`, `mutations: []`). It does **not** enqueue anything.
- **Apply mode** is opt-in, `remediation_apply_enabled` + `live=True`, receipt-first. Apply only *enqueues correlated successor jobs* (queued, dry-run dispatch) and writes operator receipts; it never rewrites any final and never auto-dispatches live. This is the single place allowed to create successor jobs, and it stays the strictest gate.
- No-storm: planning is throttled and idempotent on `(graph_id, blocker_ref)` so a flapping BLOCK cannot fan out repeated remediation graphs.

## 6. `StaleFinalResolver`: done/BLOCK fan-in superseded by a later remediation GO

This is the `stale_final_fanin` fix and the concrete realization of `live-migration-design.md` §2.8 (`bridges/supersession.py`, not yet implemented). It is intentionally the §5 RemediationPlanner's downstream counterpart: the planner proposes the fix→review→final-v2; the resolver acts when that review later returns GO and the original final is stale.

Add `src/agentflow_hermes/graph/supersession.py` (registered as `bridges/supersession.py` compatibility shim, matching the `cron_bridge.py` pattern):

`resolve_stale_final(stale_final, superseding_review, *, dry_run=True, policy=None, live=False) -> SupersessionCandidate`

Guards (fail closed):

- `stale_final` is genuinely in `FINAL_STATES` (the §store guard correctly refuses to mutate it — we never rewrite it).
- `superseding_review` parses (§4) to `GO` with `confidence='explicit'`.
- The superseding review is **newer** (`created_at`/`seq`) than the stale final.
- Provenance: the review correlates to the stale final via `graph_id`/`correlation_id`/`causation_id` (reuse kanban's provenance helpers). Unrelated GO cannot supersede an unrelated final.
- Not ambiguous: exactly one superseding GO for that graph; multiple ⇒ refuse (`ambiguous_supersessors`).

Behavior:

- **Dry-run default**: emits `{action: "supersede_and_requeue", stale_ref, superseding_ref, evidence, mutations: []}`. The immutable final is preserved; the candidate is what the next stage reviews.
- **Apply mode** (`kanban_apply_enabled` + `live=True`, receipt-first): requeues a **fresh correlated successor** (the final-v2 from §5 if present, else a new correlated fan-in job) and writes an operator receipt. It never edits the stale final row. Uses the same gate/idempotency/receipt machinery as M6 `dispatch_live`.

This keeps the `FINAL_STATES` immutability invariant intact while still letting the graph move past a verdict that a later GO has made stale.

## 7. Policy gates: dry-run first, allowlist, idempotency, dedupe, no-storm, kill switch

Everything above reuses the M6 `live/policy.py` `LivePolicy` and the receipt/idempotency/throttle machinery — no parallel gating. Additive policy fields (defaults off):

```python
subscription_enabled: bool = False        # §2 subscribe/verify
remediation_apply_enabled: bool = False    # §5 apply (enqueue successors)
# kanban_apply_enabled (exists) gates §6 supersession apply
```

Gate evaluation order (unchanged, fail-closed): `kill_switch` first → per-call `live` → channel `*_enabled` → `target ∈ allowed_targets` (exact match, no globs) → idempotency claim → throttle/breaker → receipt(attempt) → effect → receipt(terminal).

| Control | Applies to | Default |
| --- | --- | --- |
| `kill_switch` (evaluated first) | all live: dispatch, wake, subscribe, remediation apply, supersession apply | off |
| per-call `live=True` | every channel | required, absent ⇒ dry-run |
| exact-match allowlist | dispatch target, subscription return_target, supersession requeue target | `()` |
| idempotency (DB unique) | `(channel, job, target, correlation)` incl. `subscription`/`remediation`/`supersession` | persistent |
| dedupe | remediation plan `(graph_id, blocker_ref)`; supersession `(graph_id, superseding_ref)` | persistent |
| no-storm throttle + circuit breaker | all live channels via `live/throttle.py` | 3/min global, 10/target/hr, breaker @3 fails |
| subscription sub-limit | subscribe/verify (chatty) | ≤ dispatch limits |

**No remediation/supersession graph is auto-created by default.** Planning and resolution are proposal/dry-run first; apply is doubly gated (config flag + `live=True`) and receipt-first. This satisfies the hard constraint "do not auto-create remediation graphs by default."

## 8. Incident → acceptance fixture mapping

Abstract fixture names, scrubbed refs only — no raw transcripts, no real channel/user ids, no private absolute paths.

### Warroom `G8.x missing_subscription` → `fixtures/incident_missing_subscription.json`

Abstract shape: a job is created with `GraphIntent{wants_subscription=true, return_target="discord:#fixture-origin"}`, dispatched, and ACKed `succeeded` — but no `subscription` event/receipt ever exists.

Acceptance fixtures:
- `test_intent_capture_records_return_target` — enqueue captures structured `GraphIntent`; `return_target` validated against allowlist (unverified flagged, not silently delivered).
- `test_missing_subscription_invariant_detected` — a `task_verdict` with no `subscription` event is flagged by the §3 invariant check (the incident, as a failing assertion that now passes once ensured).
- `test_ensure_subscription_dry_run_emits_plan` — dry-run emits `would_subscribe` plan, no gateway call.
- `test_subscribe_requires_verify` — subscribe without notify-list verify ends `subscribe_attempted`/`verify_unavailable`, never `subscribed`.
- `test_subscription_idempotent_and_gated` — gate off / target not allowed / duplicate ⇒ refusal receipt, no double call.

### AgentFlow `stale_final_fanin` → `fixtures/incident_stale_final_fanin.json`

Abstract shape: a fan-in job is final (`done`, semantic `BLOCK`); a later review correlated by `graph_id` parses to explicit `GO` and is newer.

Acceptance fixtures:
- `test_verdict_independent_of_status` — `status=done` + `Verdict: BLOCK` parses to `BLOCK`; status never overrides the marker.
- `test_remediation_plan_dry_run_no_mutation` — BLOCK with a named blocker yields a ≤3-step plan; `mutations: []`.
- `test_remediation_refuses_unnamed_blocker` / `_broad_scope` — fail closed.
- `test_supersession_dry_run_emits_candidate` — later GO yields `supersede_and_requeue` candidate, stale final untouched.
- `test_supersession_refuses_non_final` / `_not_newer` / `_unrelated_provenance` / `_ambiguous` — fail closed.
- `test_supersession_apply_requeues_with_receipt_and_leaves_final_immutable`.
- `test_no_secret_or_private_path_in_subscription_or_remediation_events` — extends existing leak tests to the new channels.

## 9. Sequencing within the existing live-migration graph

This work slots into the M6/M7/M8 graph from `docs/live-migration-design.md` §3 — it does not fork a new track.

### `t_dc0c0996` (current M6 implementation): **continue, narrowed**

M6 (live dispatch foundation: policy/gateway/throttle/sanitize, migration v3, `dispatch_live`, receipt/idempotency, CLI `live *`, plugin `agentflow_dispatch`/`agentflow_live_status`) is committed at `4530434` and is the correct foundation. **Continue it**, but **narrow its remaining scope to M6-as-shipped** — i.e. do not let it grow to absorb subscription/remediation/supersession. Those are M7/M8. The narrowing: confirm M6 canary acceptance (`docs/live-migration-design.md` §4 M6 list) is green and stop; new modules below are separate cards. No pause is warranted — M6 is sound and is the dependency for everything here.

### M6.5 — graph-intent + verdict + ledger (read-only, dry-run, NO new live effect)

Pure additive, no new external effect, so it can land alongside M6 hardening:
- `graph/intent.py` (`GraphIntent`, migration v4 columns), `graph/verdict.py` (`SemanticVerdict`, lift kanban regexes), `graph/ledger.py` (`OperatorReceiptLedger` read API).
- Refactor `bridges/kanban.py` to use `parse_verdict` (behavior-preserving).
- The §3 channel-separation invariants become enforced smokes.
- Belongs here because it introduces **no live path** — it is the data spine the gated layers stand on.

### M7 — subscription edge (alongside active wake)

- `graph/subscription.py` (`SubscriptionEnsurer`), `subscription_enabled` gate, notify-subscribe + **notify-list verify**, `ack_subscription_status`, subscription throttle sub-limit.
- Lands with M7 active wake (both are gateway-capability-dependent, both close the return edge). Subscription is the lower-risk half (it reads/confirms; wake proactively resumes), so it can canary first within M7.

### M8 — remediation planner + stale-final supersession

- `graph/remediation.py` (`RemediationPlanner`, dry-run default, gated apply), `graph/supersession.py` (+ `bridges/supersession.py` shim), `remediation_apply_enabled`, supersession under existing `kanban_apply_enabled`.
- Lands with M8 (the existing §2.8 supersession milestone) — this design supplies the concrete planner/resolver that §2.8 only sketched.

Each milestone keeps its own review + canary gate; M7/M8 do not start until the prior milestone's acceptance passes. The fan-in terminal GO additionally requires the new §8 incident fixtures green and the §3 invariants demonstrated.

## 10. Tests/smokes and plugin/CLI surfaces (compact schema)

### CLI (additive; existing commands unchanged)

- `agentflow-hermes subscription ensure --job-id <id> [--live]` — dry-run plan by default; `--live` gated subscribe+verify.
- `agentflow-hermes subscription status --job-id <id>` — read `ack_subscription_status` + last subscription receipt.
- `agentflow-hermes remediation plan --fanin <id> --block-review <ref> [--apply --live]` — dry-run plan; apply doubly gated.
- `agentflow-hermes supersession resolve --stale <id> --superseded-by <ref> [--apply --live]` — (the §2.8 command, now concrete).
- `agentflow-hermes verdict parse --input-file <fixture>` — operator/debug, prints `SemanticVerdict`.
- `agentflow-hermes live status` already reports policy; extend it to print the new gate flags + subscription/breaker state.

### Plugin tools (stay within the ≤8-ish budget; read-only / dry-run only)

- Reuse existing `agentflow_live_status` (read-only) to also surface subscription/remediation gate state — **no new status tool.**
- Add **at most one** model-callable dry-run tool, `agentflow_graph_propose` (compact: `{kind: "remediation"|"supersession", subject_ref, evidence_ref}` → dry-run candidate, refs only). Subscription *ensure*, remediation/supersession *apply*, gate flips, allowlist edits, and kill-switch are **operator CLI only** — never model-callable (matches `plugin-architecture.md` §4 and `live-migration-design.md` §2.10). Keep tool schemas to refs/ids/kinds; no file paths, no bodies.

### Smokes

- Dry-run smokes for each CLI verb against the §8 fixtures with a fake gateway.
- Gated-apply smokes (fake gateway) proving receipt-first + idempotency + immutable-final.
- `doctor`/`live status` show the new gates resolved off by default.
- All baseline suites stay green: `test_store/ack/migrations/cron_bridge/kanban_bridge/plugin_adapter/live_dispatch/live_cli`. New: `test_graph_intent.py`, `test_verdict.py`, `test_subscription.py`, `test_remediation.py`, `test_supersession.py`, plus the `test_v3_to_v4_migration_no_data_loss` extension.

## 11. Hard constraints — compliance check

- **No Hermes core monkeypatch.** Subscription/wake go through the M6 `HermesGateway` capability boundary (feature-detected, injected by the plugin adapter); engine never imports Hermes core. ✔
- **Live dispatch/active wake/subscription opt-in and fail-closed.** Two-key gating (config flag + `live=True`), kill switch first, exact-match allowlist, degrade-to-dry-run on any miss or `GatewayUnavailable`. ✔
- **No raw transcripts/secrets/private absolute paths** in DB/events/prompts/receipts. All new channels store refs/hashes/short verdicts only; reuse `live/sanitize.py` + cron sanitizers; leak tests extended to the new channels. ✔
- **No auto-created remediation graphs by default.** Planner/resolver are dry-run proposals; apply is doubly gated and receipt-first. ✔
- **Abstract fixtures / scrubbed refs only** for the two incidents. ✔

## 12. Verdict and recommended sequencing

**Verdict: GO** for this ACK-subscription-edge + BLOCK-remediation control plane as a *staged, additive, fail-closed* extension of the existing live-migration graph — **conditional on external supervisor verification of the native Claude Code `--model opus` route** recorded in §Route evidence.

Recommended sequencing:

1. **`t_dc0c0996`: continue, narrowed** to M6-as-shipped; finish its canary acceptance and stop. Do not absorb subscription/remediation.
2. **M6.5** (graph-intent + verdict + ledger): land now — no live path, pure data spine + invariant smokes + kanban verdict refactor.
3. **M7**: subscription edge alongside active wake; subscription canaries first.
4. **M8**: remediation planner + stale-final supersession (concretizes §2.8).
5. Each milestone gated by its own review + canary; fan-in terminal GO requires the §8 incident fixtures green and the §3 invariants demonstrated, kill switch shown working.
