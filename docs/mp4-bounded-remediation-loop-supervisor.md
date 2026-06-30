# MP4 bounded remediation loop supervisor

Kanban task: `t_c6f8f79c` (native Opus design consultation)
Origin/return_to: Discord Devhub / #hermes-main
Baseline: `main@5297bb7` (MP3 gated auto-create attempt-budget fix)
Status: design artifact only — no production code changes in this task.

## Route evidence

- Supervisor preflight resolved `claude` to the ccsupervisor profile wrapper, `claude --version` returned `2.1.196 (Claude Code)`, `claude auth status --text` reported a native Claude Max account, and `tmux` was available at `/usr/bin/tmux`.
- Design consultation was invoked from this repo as native Claude Code Opus: `claude --model opus -p ... --output-format json --max-turns 12` inside tmux session `cc-t_c6f8f79c-impl`.
- The Opus run returned `session_id` `ec1b0b7d-7097-4364-a3e0-ed054f007a3f` with `modelUsage` key `claude-opus-4-8`. No OpenRouter/Kimi/Moonshot command path was used.
- The Opus run could not persist the file because write approval was unavailable in non-interactive print mode; this supervisor wrote the final doc from the consulted design and independently verified it. Route self-inspection is not cryptographic proof; acceptance depends on the supervisor command evidence above.

## 0. Problem statement and design stance

MP1–MP3 gave AgentFlow Hermes three pieces of the remediation control plane:

1. PolicyRef preflight and stale inline policy BLOCK proposals.
2. Request-only remediation graph creation (`graph_creator.py`) with no production board writer by default.
3. MP3 gated auto-create for allowlisted safe classes, including the fix at `5297bb7` where failed adapter calls consume the per-run attempt budget.

The missing MP4 layer is a **loop supervisor**: a deterministic, ledger-derived controller that consumes terminal `GO` / `BLOCK` / `NEED_MORE` events and decides whether the source graph is stable, escalated, or eligible for one bounded remediation step.

The supervisor must prevent the four failure classes already surfaced by the ACK/remediation, PolicyRef, and maintenance designs:

- **Unbounded retry loops** — the same blocker keeps creating new fix/review/final graphs.
- **ACK loss** — a remediation or final task is created without a verified return/subscription edge.
- **Stale-final confusion** — an old `done`/semantic `BLOCK` final remains authoritative after a later remediation review returns `GO`.
- **Cross-board/channel policy drift** — a stale task body or foreign origin causes mutation in the wrong lane.

Design stance: **observe and decide from receipts; propose by default; apply only under explicit policy and caps.** MP4 does not make remediation automatic for all BLOCKs. It adds a bounded supervisor around the existing MP2/MP3 graph creator and keeps `request_only` as the default mode.

## 1. Architecture

```text
terminal/review/fan-in events
        │
        ▼
VerdictParser ──► LoopSupervisor ──► RemediationPlanner / StaleFinalResolver
        │              │                         │
        │              ▼                         ▼
        │       OperatorReceiptLedger      MP2/MP3 graph_creator
        │              │                         │
        ▼              ▼                         ▼
 policy_resolution + loop receipts     request-only proposal or gated fake/real adapter
```

New module shape (MP4a/MP4b):

- `graph/loop_state.py` — `LoopState`, `LoopPolicy`, `LoopDecision`, read-model helpers.
- `graph/loop_supervisor.py` — pure decision function: `evaluate_loop_event(event, ledger, policy, *, adapter=None) -> LoopDecision`.
- `graph/loop_cli.py` or existing CLI namespace — request-only entrypoint for operators and timer/watcher integration.

The supervisor is deliberately **not** a worker dispatcher, gateway sender, service restarter, or broad scanner. It processes one source event / source graph at a time and delegates any graph proposal to the existing request-only graph creator.

## 2. Input events and state machine

### Event types

| Event | Required evidence | Notes |
| --- | --- | --- |
| `terminal_task_verdict` | task id, graph id, parsed verdict, origin/return_to, event id | Worker completion report. |
| `review_verdict` | reviewed task id, review task id, parsed verdict, source graph | Implementation review result. |
| `fanin_verdict` | fan-in task id, parsed verdict, parent task refs | Final graph verdict. |
| `remediation_review_go` | remediation review id, explicit `GO`, newer-than stale final evidence | May supersede final-v1. |
| `stale_final_block` | old final id, semantic `BLOCK`, final_vN | Candidate for final-vN supersession only. |
| `operator_receipt_event` | channel, phase, idempotency key, target, policy evidence | Subscription/apply/delivery status changes. |

Every event carries a compact `event_id` and sanitized source refs only. Raw review bodies, transcripts, private absolute paths, and secrets are not durable loop state.

### State transitions

| Current state | Input | Guard | Decision |
| --- | --- | --- | --- |
| any | explicit `GO` | source graph matches, event not duplicate | `STABILIZE`: record stable receipt and stop. |
| any | explicit `NEED_MORE` | event not duplicate | `ESCALATE`: no auto-create by default; record operator-needed receipt. |
| observing | explicit `BLOCK` | blocker unknown or not allowlisted | `ESCALATE`: no-op proposal with reason. |
| observing | explicit `BLOCK` | blocker allowlisted, caps/cooldown pass, origin/subscription verified | `PROPOSE_OR_APPLY_REMEDIATION`: exactly one bounded step. |
| remediating | same blocker repeats | `same_blocker_count >= max_same_blocker` | `ESCALATE`: loop exhausted. |
| remediating | any BLOCK | `round_no >= max_rounds` | `ESCALATE`: graph exhausted. |
| final-v1 closed | later remediation review `GO` | provenance matches, newer, exactly one superseding GO | `FINAL_VN_SUPERSESSION`: create/propose final-vN once. |
| any | malformed policy/ref | fail-closed | `KILL_SWITCHED`/`ESCALATE`, no mutation. |
| any | duplicate event/idempotency | existing receipt | `NOOP_DUPLICATE`. |

Important semantic distinctions:

- `GO` means stop/stabilize for the current graph. It does **not** imply passive delivery or subscription succeeded.
- `NEED_MORE` means stop and surface a human/operator decision. The supervisor must not invent a remediation graph from insufficient scope.
- `BLOCK` is actionable only after classification and only for an allowlisted blocker class under all caps.
- A stale final is never edited. Later remediation `GO` creates a fresh correlated final-vN candidate and marks final-v1 superseded in loop receipts.

## 3. Loop policy schema

MP4 extends, rather than replaces, the existing `RemediationGraphPolicy` in `graph_creator.py`.

```python
@dataclass(frozen=True)
class LoopPolicy:
    active_mode: str = "request_only"  # disabled | observe_only | request_only | apply
    kill_switch: bool = False
    allowlisted_blockers: tuple[str, ...] = ()
    max_rounds: int = 2
    max_same_blocker: int = 1
    max_auto_creates_per_run: int = 3
    max_tasks_per_graph: int = 9
    cooldown_seconds: int = 900
    backoff_multiplier: float = 2.0
    require_subscription_verified: bool = True
    require_origin_match: bool = True
    require_policy_resolution: bool = True
    request_only_by_default: bool = True
```

Evaluation order is fixed and fail-closed:

1. `kill_switch` / malformed policy hard-stops all apply paths.
2. Event idempotency and event provenance are checked before classification.
3. `origin` / `return_to` / board/lane must match the source graph and configured allowlist.
4. PolicyRef preflight must resolve required refs; unknown/malformed refs become `NEED_MORE` or `BLOCK` with no mutation.
5. Semantic verdict must be explicit (`GO`, `BLOCK`, `NEED_MORE`); `UNKNOWN` cannot drive remediation.
6. For `BLOCK`, blocker class must be allowlisted.
7. `max_rounds`, `max_same_blocker`, `max_tasks_per_graph`, `max_auto_creates_per_run`, cooldown, and backoff must pass.
8. If `active_mode != apply`, emit a request-only plan and receipts; no board mutation.
9. If `active_mode == apply`, call MP3 only through its gated adapter path; failed adapter attempts still consume `max_auto_creates_per_run`.

Mode semantics:

| Mode | Behavior |
| --- | --- |
| `disabled` | read event, record refused/disabled receipt, no proposals. |
| `observe_only` | compute state and risks; no graph proposal. |
| `request_only` | emit a bounded plan/candidate; no real board write. Default. |
| `apply` | may call the MP3 adapter for allowlisted safe classes if every gate passes. |

Malformed policy must coerce to a hard-stop shape (same philosophy as `graph_creator._coerce_policy` and `live/policy.py` strict parsing). Never assume permissive defaults.

## 4. Ledger and receipts

MP4 is a ledger-derived state machine. It should not reconstruct truth from raw task prose when receipts are available.

### Loop receipt fields

Add a compact channel such as `loop_supervisor` to `operator_receipts` or `job_events`:

| Field | Purpose |
| --- | --- |
| `event_id` | Dedupes repeated webhook/cron/worker events. |
| `source_task_id` | The task whose verdict triggered the decision. |
| `source_graph_id` | Stable graph/fan-in id across remediation rounds. |
| `source_final_id` | Old final-v1/vN when supersession is evaluated. |
| `blocker_class` | Allowlist/cap key (`stale_inline_route`, `missing_subscription`, etc.). |
| `round_no` | 0 for initial graph, incremented per remediation round. |
| `same_blocker_count` | Count of this blocker in this source graph. |
| `final_vn` | Current final version number; final-v2+ for supersession. |
| `decision` | `stabilize`, `escalate`, `propose`, `apply`, `supersede`, `noop`. |
| `idempotency_key` | Stable key for the decision/action. |
| `policy_resolution_ref` | Ref/hash to PolicyRef evidence, not raw policy values. |
| `origin_ref` / `return_to_ref` | Sanitized target refs used for ACK verification. |
| `subscription_status` | `verified`, `missing`, `unverified`, `not_required`. |
| `reason` | Short enum, e.g. `max_same_blocker`, `cooldown`, `foreign_origin`. |

### Idempotency keys

Use stable, scoped keys:

- Event: `loop:event:<source_graph_id>:<event_id>`.
- Remediation round: `loop:round:<source_graph_id>:<blocker_class>:<round_no>`.
- Same-blocker cap: counts receipts by `(source_graph_id, blocker_class)`.
- Final supersession: `loop:final-vN:<source_graph_id>:<old_final_id>:<superseding_review_id>`.
- Adapter create: keep MP3 keys (`remediation:<blocker>:<digest>:fix|review|final-vN`) and include round metadata.

The receipt ledger must answer these questions without re-reading raw transcripts:

1. Has this event already been processed?
2. How many remediation rounds has this source graph consumed?
3. How many times has this same blocker appeared?
4. Was a remediation graph already proposed/applied for this blocker and source?
5. Was the origin/return_to subscription verified before side effects?
6. Which policy version/evidence was used?
7. Which final version is currently authoritative?

## 5. Interaction with existing components

### VerdictParser

MP4 consumes the shared semantic verdict abstraction from `ack-remediation-control-plane.md`: explicit marker wins, task status does not. `status=done` with `Verdict: BLOCK` remains a BLOCK. `UNKNOWN` fails closed.

### RemediationPlanner

The existing `remediation.py` classifier remains the narrow blocker parser. MP4 adds round/cooldown/cap context around it and rejects broad or unnamed blockers before MP3 sees them.

### StaleFinalResolver

The existing `resolve_stale_final_candidate` behavior is the seed. MP4 makes the `final-vN` version and supersession receipts first-class:

- old final remains immutable;
- later remediation review must parse explicit `GO`;
- review must be newer and provenance-matched;
- exactly one final-vN candidate is created/proposed per superseding review.

### PolicyRef resolver/preflight

Task bodies and generated remediation task bodies carry symbolic PolicyRefs only. The supervisor records `policy_resolution` evidence refs at run time and treats unknown/malformed policy as fail-closed. Inline concrete policy values in old cards are classified as potential blockers, not copied forward.

### MP2 graph_creator and MP3 gated auto-create

MP4 should call `propose_remediation_graph` for request-only plans and only use MP3 adapter apply when `LoopPolicy.active_mode == "apply"` and all MP4 caps pass. The MP3 attempt-budget invariant from `5297bb7` is load-bearing: failed/transient adapter calls consume `max_auto_creates_per_run` so a flaky adapter cannot retry-storm.

### Maintenance watcher/runner boundary

Maintenance remains outside MP4's action boundary. The watcher may emit loop events and request-only sync/remediation graphs. The runner may read `GO`/`BLOCK` results, but MP4 must never restart services, move git, or send live messages. Service-cycle actions remain gated by `MaintenancePolicy` and the external runner design.

## 6. Safety invariants

1. **No raw private path/secret persistence.** Loop receipts store refs, hashes, short enums, policy evidence refs, and sanitized origin refs only.
2. **PolicyRefs, not copied values.** Remediation task bodies contain binding refs and optional non-binding previews, never stale provider/model/route values as instructions.
3. **No live apply/restart/send by default.** Default mode is `request_only`; live delivery and service cycles remain separate policy domains.
4. **No foreign-lane mutation.** Origin, return_to, board/lane, graph id, and subscription edge must match before any apply path.
5. **Fail closed on unknowns.** Unknown verdict, blocker, policy ref, malformed policy, ambiguous supersessor, or missing subscription verification yields no mutation.
6. **Exact ACK edge before side effects.** If `require_subscription_verified` is true, the supervisor refuses apply unless the `SubscriptionEnsurer`/receipt ledger proves the return edge.
7. **Immutable finals.** Stale final rows are superseded by final-vN; never rewritten.
8. **Single-subject processing.** One event/source graph per evaluation; no board-wide scans that auto-create work.

## 7. Failure modes and required responses

| Failure | Response |
| --- | --- |
| Duplicate event id | `NOOP_DUPLICATE`; return prior decision receipt. |
| Missing/UNKNOWN verdict | `ESCALATE/unknown_verdict`; no graph. |
| NEED_MORE | `ESCALATE/needs_input`; no graph. |
| Unknown blocker | `ESCALATE/unknown_blocker`; no graph. |
| Repeated same blocker | Stop at `max_same_blocker`; surface operator action. |
| Round cap exhausted | Stop at `max_rounds`; surface graph exhausted. |
| Cooldown active | `NOOP_COOLDOWN`; record next eligible time. |
| Malformed policy | kill-switch behavior; no mutation. |
| PolicyRef unresolved | `NEED_MORE` or `BLOCK/policy_unresolved`; no mutation. |
| Subscription unverified | no apply; request-only/escalate depending mode. |
| Origin/return_to mismatch | no-op/escalate as `foreign_origin`. |
| Adapter failure | record failed attempt; consume attempt budget; no retry loop. |
| Multiple remediation GO supersessors | `ESCALATE/ambiguous_supersession`; no final-vN. |

## 8. Test matrix

| Test | Expected result |
| --- | --- |
| `test_loop_go_terminal_stops` | explicit GO records stabilize receipt; no planner/adapter call. |
| `test_loop_need_more_stops_no_auto_create` | NEED_MORE escalates; zero candidates. |
| `test_loop_first_safe_block_apply_creates_one_graph` | allowlisted BLOCK under apply creates bounded MP3 graph once. |
| `test_loop_request_only_safe_block_returns_plan_no_adapter` | request_only emits plan and leaves adapter untouched. |
| `test_loop_repeated_same_blocker_stops_at_cap` | second/over-cap same blocker no-ops with `max_same_blocker`. |
| `test_loop_max_rounds_stops` | graph over round cap escalates. |
| `test_loop_cooldown_suppresses_repeated_auto_create` | duplicate BLOCK inside cooldown records `noop_cooldown`. |
| `test_loop_stale_final_v1_later_go_creates_final_v2_once` | final-v2 candidate emitted once; stale final immutable. |
| `test_loop_duplicate_event_id_noops` | event id replay returns prior decision. |
| `test_loop_malformed_policy_kill_switches` | malformed config causes fail-closed receipt and no adapter call. |
| `test_loop_unknown_blocker_noops_escalates` | non-allowlisted blocker produces no mutation. |
| `test_loop_foreign_origin_return_to_mismatch_noops` | mismatched origin/return_to prevents apply. |
| `test_loop_failing_adapter_consumes_attempt_budget` | failed adapter attempt decrements/caps per-run creates, matching MP3. |
| `test_loop_policy_resolution_ref_required` | missing policy evidence blocks apply when required. |
| `test_loop_no_private_paths_or_secret_values_in_receipts` | durable receipt JSON has only sanitized refs. |

Run these with fake ledgers, fake adapters, and temp stores. No tests should call a real gateway, real Kanban board writer, real systemd, or live sender.

## 9. Implementation slices

### MP4a — ledger/state model + dry-run supervisor

- Add `LoopPolicy`, `LoopState`, `LoopEvent`, `LoopDecision` dataclasses.
- Add a read-only ledger helper that derives counts from receipts/events.
- Implement `evaluate_loop_event` in `observe_only`/`request_only`; no adapter apply path yet.
- Add tests for GO, NEED_MORE, unknown verdict/blocker, duplicate event, max rounds, same blocker, cooldown, and sanitization.
- Existing suites: store/ack/PolicyRef/remediation/graph_creator remain green.

### MP4b — bounded apply using MP3 with fake adapter tests

- Wire apply mode to MP3 `propose_remediation_graph(..., adapter=...)` only after MP4 gates pass.
- Preserve MP3 `max_auto_creates_per_run` semantics, including failed adapter attempts consuming budget.
- Add fake adapter tests for first safe BLOCK, repeated BLOCK cap, adapter failure, and subscription-required refusal.
- Keep default config `request_only` and no production board writer enabled by default.

### MP4c — integration entrypoint / CLI request-only

- Add CLI/operator entrypoint such as `agentflow-hermes loop evaluate --event <ref> [--apply --live]`, defaulting to dry-run/request-only output.
- Let maintenance watcher / graph creator / future bridge call the same pure supervisor API.
- Status command prints current loop state by graph id: round, same-blocker counts, last decision, final_vN, subscription status.
- Plugin/model-callable surface, if any, is read-only or dry-run proposal only.

### MP4d — live policy-gated opt-in only if needed later

- Do not ship live apply in the first implementation unless a separate review explicitly approves the real adapter boundary.
- If shipped later, require central policy opt-in, exact origin allowlist, verified subscription, kill switch, circuit breaker, fake adapter canary, and operator receipts before any effect.
- Live delivery, active wake, and maintenance restart remain outside MP4 and under their own policies.

## 10. Acceptance criteria

- The supervisor never creates remediation work for GO or NEED_MORE.
- A first allowlisted BLOCK can produce at most one bounded remediation graph under apply mode, and request-only remains the default.
- Same blocker, max rounds, max tasks, cooldown/backoff, duplicate events, malformed policy, unknown blocker, foreign origin, and failing adapter all stop without storms.
- A stale final-v1 semantic BLOCK plus later provenance-matched remediation review GO yields exactly one final-v2 candidate; the old final remains immutable.
- Loop receipts contain source graph/task refs, blocker class, round/final version, policy evidence ref, subscription status, and idempotency keys, but no raw private paths/secrets/transcripts.
- Existing MP2/MP3 tests continue to pass, including the MP3 attempt-budget regression.

## 11. Open risks

- `require_subscription_verified` depends on the subscription receipt channel from the ACK/remediation design. If `SubscriptionEnsurer` is not implemented, MP4 apply must remain disabled or ACK-loss protection is only nominal.
- The current real `KanbanGraphAdapter` is intentionally no-op unless explicitly armed. MP4d must not be interpreted as permission to silently install a real board writer.
- Overly aggressive fail-closed behavior on temporarily unavailable policy may increase NEED_MORE volume. That is acceptable for MP4a/MP4b; availability relaxations require a separate policy design.

## 12. Verdict

**Verdict: GO (GO-for-review)** for MP4 as a staged, additive, fail-closed loop supervisor, sequenced MP4a dry-run state/ledger → MP4b bounded fake-adapter apply tests → MP4c request-only CLI/integration → MP4d live opt-in only after separate review. The design should proceed only with request-only defaults, verified origin/ACK edges before side effects, immutable final-vN supersession, and strict loop caps so remediation cannot become an unbounded auto-retry daemon.
