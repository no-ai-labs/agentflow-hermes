# M14 roadmap-GO autopromoter

Kanban task: `t_fe7736ad` (native Opus design consultation)
Origin/return_to: Discord Devhub / #hermes-main
Baseline: `main@f81ef31` (M13 maintenance canary; MP4 loop supervisor shipped)
Status: design artifact only — no production code changes in this task.

## Route evidence

- This design was produced via **native Claude Code Opus** (`claude --model opus`,
  model id `claude-opus-4-8`, Opus 4.8), not the OpenRouter/Kimi/Moonshot/Sonnet
  wrapper route. The run reads the live repo (`loop_supervisor.py`,
  `graph_creator.py`, `policy_ref.py`, `remediation.py`, `migrations.py`) and the
  two prior design docs it extends before writing this file.
- Route self-inspection from inside the process is not cryptographic proof of
  provider transport. **Acceptance depends on the supervisor's external command
  evidence** (`claude --version`, `claude auth status --text` = native Claude Max,
  `session_id` + `modelUsage` key `claude-opus-4-8`, no OpenRouter/Kimi command
  path), exactly as recorded for `t_caf6865e`, `t_c6f8f79c`, and `t_113808c3`.
- This file remains a design-only artifact. It edits no production code or tests.

## 0. Problem statement and design stance

MP4 (`docs/mp4-bounded-remediation-loop-supervisor.md`) gave AgentFlow Hermes a
bounded loop supervisor over the **failure** side of the graph: an allowlisted
`BLOCK` can produce one bounded fix→review→final remediation step, and a stale
final can be superseded by a later provenance-matched remediation `GO`.

The mirror gap is on the **success** side. Today, when a final fan-in returns an
explicit `GO`, `evaluate_loop_event` records a `stabilize`/`go_terminal` receipt
and stops (`loop_supervisor.py:228`). The roadmap does **not** advance: no next
implementation/review/fan-in slice is synthesized, so the operator must hand-build
the next graph even though the just-completed final already declares the next
slice. The M13 final parent was itself a `GO` that produced no successor graph.

M14 adds a **roadmap-GO autopromoter**: a deterministic, ledger-derived controller
that, on a trusted final fan-in `GO` carrying an explicit next-slice directive,
proposes (and, only under a doubly-gated apply mode, creates) the next roadmap
slice's implementation→review→fan-in graph — using **allowlisted roadmap
transitions and template IDs only**, never free-text synthesis.

Design stance (inherited, non-negotiable):

- **No arbitrary task synthesis.** Only allowlisted `RoadmapTransition` /
  template IDs / PolicyRefs may be instantiated. A `GO` summary that names an
  unknown transition is a refuse, not a "best-effort" graph.
- **Request-only by default; `auto_continue` is opt-in and false by default.**
  Autopromotion is a separate gate from remediation apply and from live dispatch.
- **Bounded so it cannot storm.** Max chain depth, per-roadmap repeat count,
  cooldown, and an idempotency ledger bound the total slices a `GO` cascade can
  create.
- **PolicyRefs, not copied route values** (`docs/policy-ref-runtime-hydration-design.md`).
  Synthesized task bodies carry symbolic refs; workers resolve central policy at
  run time; stale inline route values in the source `GO` body BLOCK.
- **No live side effects.** The autopromoter never dispatches, restarts, or sends;
  it only creates Kanban tasks, and only through the existing fake/no-op adapter
  boundary (`graph_creator.py` `FakeKanbanGraphAdapter` / armed `KanbanGraphAdapter`)
  under an explicit `apply_enabled` gate.

M14 is additive around MP4, not a fork: it reuses `LoopPolicy` gates, the
`GraphIntentCandidate` shape, the graph-creator adapter boundary, `parse_verdict_summary`,
and the PolicyRef preflight. It adds one new decision branch, one allowlist, and
one ledger channel.

## 1. Architecture

```text
final fan-in GO report
        │
        ▼
parse_verdict_summary ─► GO + explicit next_slice directive?
        │                         │ no → noop/refuse (see §6)
        ▼ yes                     
RoadmapAutopromoter ─► RoadmapTransition allowlist lookup (template id)
        │                         │ miss → refuse (unknown_transition)
        ▼                         
gate chain (trust, origin/ACK, PolicyRef, depth/repeat/cooldown, idempotency)
        │
        ▼
graph_creator.propose_next_slice_graph  ─► request-only NextSlicePlan
        │  (apply mode only, all gates pass)      │
        ▼                                         ▼
RoadmapPromotionLedger receipt          FakeKanbanGraphAdapter / armed adapter (no-op default)
        │
        ▼
operator receipt + origin ACK: "auto-created slice <id> (transition <t>, depth n)"
```

New module shape:

- `graph_creator.py` (extend): `RoadmapTransition`, `NextSliceDirective`,
  `NextSlicePlan`, `propose_next_slice_graph(...)`, `apply_next_slice_graph(...)`.
  Reuse `GraphIntentCandidate`, `_policy_ref_body`, `_is_apply_gated`, the adapter
  boundary, and `_coerce_policy`.
- `roadmap.py` (new, small): the `RoadmapTransition` registry loader and the
  `parse_next_slice_directive` classifier (structured directive extractor).
- `loop_supervisor.py` (extend): a new `_evaluate_autopromote` branch reached from
  the existing `verdict == "GO"` case, plus `PromotionPolicy` fields on `LoopPolicy`.
- `RoadmapPromotionLedger`: a read model over a new `roadmap_promotions` channel
  (migration v5, §7), mirroring `InMemoryLoopLedger`.

The autopromoter is deliberately **not** a worker dispatcher, gateway sender,
service restarter, or board-wide scanner. It processes one final `GO` event /
one source roadmap at a time and delegates graph creation to the existing
request-only creator.

## 2. Data model

### Roadmap transition allowlist (the anti-free-text spine)

A `RoadmapTransition` is an operator-owned, versioned template. It is the **only**
thing an autopromotion may instantiate. It is loaded from central config
(`default_home() / "roadmap_transitions.json"`), never from a task body.

```python
@dataclass(frozen=True)
class RoadmapTransition:
    transition_id: str          # e.g. "m14->m15.impl_review_fanin"; allowlist key
    roadmap_id: str             # stable roadmap/graph lineage id, e.g. "hermes.live-migration"
    from_slice: str             # e.g. "m14"
    to_slice: str               # e.g. "m15"
    slice_template: tuple[str, ...]  # ordered kinds, e.g. ("impl", "review", "fanin")
    policy_refs: tuple[str, ...]     # symbolic refs each synthesized body carries
    max_chain_depth: int = 3         # how many auto-hops this transition may cascade
    version: str = ""                # template content hash / version
```

```python
@dataclass(frozen=True)
class RoadmapTransitionRegistry:
    version: str
    transitions: dict[str, RoadmapTransition]   # keyed by transition_id
    content_hash: str
    source_ref: str
```

Loading fails closed exactly like `load_policy_document`: malformed JSON, missing
`transitions`, or an incomplete transition raises and the caller treats it as
`unverifiable` → refuse. There is no permissive default registry in production;
tests may pass an explicit in-memory registry.

### Next-slice directive (parsed from the GO, never trusted as instruction)

The final `GO` report must carry a **structured** directive naming the transition,
not prose. The classifier extracts the transition id and refs; it does not free-read
task descriptions.

```python
@dataclass(frozen=True)
class NextSliceDirective:
    transition_id: str          # must match an allowlisted RoadmapTransition
    next_slice: str             # to_slice claimed by the report
    review_edge: bool           # report asserts a review edge exists in the closed slice
    ack_edge: bool              # report asserts the ACK/return edge was verified
    confidence: str             # "explicit" | "none"
    source_ref: str = ""        # sanitized ref to the GO artifact, never raw body
```

`parse_next_slice_directive(text)` returns `confidence="none"` (→ refuse) unless it
finds an explicit, structured `Next-Slice:` / `Roadmap-Transition:` marker with a
transition id and explicit `Review-Edge:`/`ACK-Edge:` assertions. An ambiguous or
prose-only "we should do the next milestone" is `none`. This mirrors the
`parse_verdict_summary` "explicit marker wins, else UNKNOWN fails closed" rule.

### Plan and intents (reuse the existing shape)

```python
@dataclass(frozen=True)
class NextSlicePlan:
    transition_id: str
    roadmap_id: str
    chain_depth: int
    idempotency_key: str        # roadmap:<roadmap_id>:promote:<transition_id>:<depth>:<digest>
    candidates: tuple[GraphIntentCandidate, ...]  # one per slice_template kind
    request_only: bool = True
```

Each candidate is a `GraphIntentCandidate` (already defined in `graph_creator.py`)
with `kind` from `slice_template`, `origin`/`return_to` copied verbatim from the
source `GO`, `policy_refs` from the transition, `subscription_required=True`,
`parent_key` chaining impl→review→fanin, and `body=_policy_ref_body(policy_refs)`.
The fan-in candidate additionally carries the `Roadmap-Transition:` marker so the
next final `GO` can be classified in turn (bounded by depth).

### Promotion policy (extends LoopPolicy)

```python
# additive fields on LoopPolicy, defaults keep autopromotion OFF
auto_continue: bool = False                # master opt-in; false by default
autopromote_apply_enabled: bool = False    # apply gate (separate from remediation apply)
allowlisted_transitions: tuple[str, ...] = ()
max_chain_depth: int = 3
max_promotions_per_roadmap: int = 6
promote_cooldown_seconds: int = 900
require_review_edge: bool = True
require_ack_edge: bool = True
require_trusted_assignee: bool = True
trusted_assignees: tuple[str, ...] = ()    # exact-match reviewer/assignee ids
```

`_coerce_policy` (both `graph_creator.py` and `loop_supervisor.py` styles) is
extended to fail closed on malformed autopromote fields, identically to the existing
numeric/tuple/mode validation.

## 3. Gating flow / state machine

The autopromoter is reached from the existing GO branch. `GO` still stabilizes the
current graph; autopromotion is an **additional** bounded action, gated in this
fixed, fail-closed order:

| # | Gate | Refuse reason on failure |
| --- | --- | --- |
| 1 | `kill_switch` / malformed policy / malformed registry | `kill_switch` / `malformed_policy` |
| 2 | Event not a duplicate (`event_id` idempotency) | `noop`/`duplicate_event` (return prior decision) |
| 3 | `auto_continue is True` (master opt-in) | `autopromote_disabled` |
| 4 | Semantic verdict is explicit `GO` | `not_go` (BLOCK/NEED_MORE/UNKNOWN never promote) |
| 5 | `NextSliceDirective.confidence == "explicit"` and transition id present | `missing_next_slice` / `ambiguous_go` |
| 6 | `transition_id ∈ allowlisted_transitions` AND resolves in registry | `unknown_transition` |
| 7 | `require_review_edge` ⇒ `directive.review_edge is True` | `missing_review_edge` |
| 8 | `require_ack_edge` ⇒ `directive.ack_edge is True` **and** ledger subscription_status verified | `missing_ack_edge` / `subscription_unverified` |
| 9 | `require_trusted_assignee` ⇒ final reviewer/assignee ∈ `trusted_assignees` | `untrusted_assignee` |
| 10 | Origin/return_to exactly match expected (reuse `_origin_ok`) | `foreign_origin` |
| 11 | PolicyRef preflight of the source GO body succeeds (no `contradicted`/stale) | `stale_inline_route` / `policy_unresolved` |
| 12 | `chain_depth < min(policy.max_chain_depth, transition.max_chain_depth)` | `max_chain_depth` |
| 13 | `ledger.count_promotions(roadmap_id) < max_promotions_per_roadmap` | `max_promotions_per_roadmap` |
| 14 | Cooldown since last promotion for this roadmap has elapsed | `noop`/`cooldown` |
| 15 | Promotion idempotency key unclaimed | `noop`/`existing_promotion` |

Only when **all** pass does the autopromoter build the `NextSlicePlan`:

- `active_mode != apply` **or** `autopromote_apply_enabled is False` → `propose`:
  emit the plan + receipt; adapter untouched; `mutations: []`.
- `active_mode == apply` AND `autopromote_apply_enabled` AND all gates → `apply`:
  call the graph-creator adapter path once per candidate under the existing
  attempt-budget semantics; failed adapter attempts consume the per-run budget so
  a flaky adapter cannot retry-storm.

### State machine (per source roadmap)

| State | Input | Guard | Decision |
| --- | --- | --- | --- |
| any | explicit `GO`, no directive | — | `stabilize` (unchanged MP4 behavior) |
| stabilized | explicit `GO` + valid directive | all §3 gates pass | `promote`/`apply_promotion`: one bounded next slice |
| promoting | next slice `GO` again | `chain_depth >= max_chain_depth` | `escalate`/`max_chain_depth` |
| promoting | same transition repeats | `count >= max_promotions_per_roadmap` | `escalate`/`max_promotions_per_roadmap` |
| promoting | duplicate promotion key | idempotency claimed | `noop`/`existing_promotion` |
| any | GO within cooldown | cooldown active | `noop`/`cooldown` |
| any | malformed policy/registry | fail-closed | `escalate`/`kill_switched`, no mutation |

`GO` continues to mean **stop/stabilize the current graph**; autopromotion is a
distinct, separately-gated forward step, never implied by stabilization.

## 4. PolicyRef / stale-inline policy handling

Autopromotion sits directly on the PolicyRef hydration contract:

1. **Preflight the source GO body before promoting.** Gate 11 runs
   `preflight_task_body(source_go_body, ...)`. A `contradicted`/`stale_inline_route`
   finding (e.g. an old `claude-openrouter-opus` or `Moonshot` directive phrased as
   binding) yields `stale_inline_route` → refuse. A `GO` sitting on a stale route
   must not seed new work on that route.
2. **Synthesized bodies carry symbolic refs only.** Each candidate body is built by
   `_policy_ref_body(transition.policy_refs)` — `Policy refs:` block plus a
   non-binding, redacted preview line. No provider/model/command values are copied
   into the new tasks. The next worker resolves central policy at run time.
3. **Registry values are refs, not routes.** `RoadmapTransition.policy_refs` are
   symbolic keys (`design_opus`, `implementation_default`), resolved centrally by
   the downstream worker's preflight, never pinned in the transition or the body.
4. **Test the drift invariant.** Changing central `model_policy.json` after a slice
   is created must change the next worker's resolution even though the synthesized
   preview text is unchanged (asserted in §8).

The autopromotion promotion receipt records only a `policy_resolution_ref` /
`policy_version`, never resolved route values.

## 5. Idempotency, ledger, and no-storm

The autopromoter is ledger-derived; it never reconstructs promotion history from
raw prose. `RoadmapPromotionLedger` mirrors `InMemoryLoopLedger` and answers:

1. Has this final-GO `event_id` already been processed? (`has_event`)
2. Has this exact promotion key already been claimed? (`has_promotion_key`)
3. How many promotions has this `roadmap_id` consumed? (`count_promotions`)
4. What is the current chain depth for this roadmap? (`current_chain_depth`)
5. When was the last promotion for this roadmap? (`last_promotion_time`, for cooldown)

Idempotency keys (stable, scoped, sanitized via `safe_durable_ref`):

- Event: `roadmap:event:<roadmap_id>:<event_id>`.
- Promotion: `roadmap:<roadmap_id>:promote:<transition_id>:<depth>:<digest>` where
  `digest = sha256(transition_id:source_final_ref)[:16]`.
- Per-candidate adapter create: `<promotion_key>:<kind>` (reuses the existing
  `graph_creator` `list_existing` prefix-match dedupe).

No-storm bounds, all fail-closed:

- **Chain depth** — `min(policy.max_chain_depth, transition.max_chain_depth)`;
  depth is read from the ledger, not trusted from the event alone
  (`effective_depth = max(event.chain_depth, ledger.current_chain_depth(roadmap))`),
  mirroring the MP4 `max(round_no, ledger_max_round)` guard.
- **Per-roadmap repeat** — `max_promotions_per_roadmap` over all promote/apply
  receipts for the roadmap.
- **Cooldown** — `promote_cooldown_seconds` since the last promote/apply for the
  roadmap; a GO cascade inside cooldown is `noop`/`cooldown`.
- **Per-run adapter attempt budget** — reuse `max_auto_creates_per_run`; failed
  adapter attempts consume budget (the `5297bb7` invariant).

## 6. Exact conditions that refuse / noop

Per the hard constraint, the autopromoter **must not** auto-continue and returns a
receipt with the named reason for each of:

| Condition | Decision / reason |
| --- | --- |
| Verdict is `BLOCK` or `NEED_MORE` | no promotion; MP4 remediation/escalate path only (`not_go`) |
| Verdict `UNKNOWN` / no explicit marker | `escalate`/`not_go`; never promote on inference |
| GO present but no structured next-slice directive | `noop`/`missing_next_slice` |
| Directive present but transition id absent/ambiguous | `refuse`/`ambiguous_go` |
| Transition id not in `allowlisted_transitions` or not in registry | `refuse`/`unknown_transition` |
| `review_edge` not asserted (and required) | `refuse`/`missing_review_edge` |
| `ack_edge` not asserted, or ledger subscription not verified | `refuse`/`missing_ack_edge` / `subscription_unverified` |
| Final reviewer/assignee not in `trusted_assignees` | `refuse`/`untrusted_assignee` |
| Origin/return_to not exactly preserved | `refuse`/`foreign_origin` |
| Source GO body carries stale/contradicted inline policy | `refuse`/`stale_inline_route` |
| `auto_continue` false (default) | `noop`/`autopromote_disabled` |
| Chain depth / per-roadmap cap reached | `escalate`/`max_chain_depth` \| `max_promotions_per_roadmap` |
| Cooldown active | `noop`/`cooldown` |
| Duplicate event / claimed promotion key | `noop`/`duplicate_event` \| `existing_promotion` |
| Malformed policy or registry | `escalate`/`kill_switched`; no mutation |

In every refuse/noop case: zero candidates, `mutations: []`, adapter untouched.

## 7. Migration / ledger needs

One additive migration, `SQL_V5` (`SCHEMA_VERSION = 5`), refs/enums only — no raw
bodies, transcripts, private paths, or secrets:

```sql
-- M14 roadmap autopromotion receipts / idempotency claims.
create table if not exists roadmap_promotions (
    id integer primary key autoincrement,
    idempotency_key text not null,
    event_id text not null default '',
    roadmap_id text not null default '',
    transition_id text not null default '',
    from_slice text not null default '',
    to_slice text not null default '',
    chain_depth integer not null default 0,
    decision text not null default 'propose',   -- propose | apply | noop | escalate
    reason text not null default '',
    origin_ref text not null default '',
    return_to_ref text not null default '',
    subscription_status text not null default 'unverified',
    policy_resolution_ref text not null default '',
    dry_run integer not null default 1,
    created_at real not null
);
create unique index if not exists uniq_roadmap_promotions_key on roadmap_promotions(idempotency_key);
create index if not exists idx_roadmap_promotions_roadmap on roadmap_promotions(roadmap_id, chain_depth, created_at);
create index if not exists idx_roadmap_promotions_event on roadmap_promotions(event_id);
```

Alternatively, the promotion channel can live in the existing `operator_receipts`
table under `channel='roadmap_promotion'` (no migration), and the read model derives
counts by channel — mirroring how MP4 loop receipts reuse `operator_receipts`/`job_events`.
The dedicated table is preferred for the `chain_depth` index; either satisfies the
constraint. `roadmap_transitions.json` is operator-owned config outside the DB and
outside task-body control (the PolicyRef bootstrap-trust stance).

`test_v4_to_v5_migration_no_data_loss` accompanies whichever is chosen.

## 8. Local fixture / unit acceptance tests (no Discord history)

All tests use in-memory registries, fake ledgers (`RoadmapPromotionLedger` in-memory),
the `FakeKanbanGraphAdapter`, and local board fixtures. None depend on Discord
history, a real gateway, a real board writer, or a live sender.

Fixtures (abstract, scrubbed refs only): `fixtures/roadmap_go_valid.json` (final GO
with a structured allowlisted `Roadmap-Transition:`, `Review-Edge: yes`,
`ACK-Edge: verified`, trusted assignee, clean policy refs) and negative variants.

| Test | Expected |
| --- | --- |
| `test_go_with_valid_directive_proposes_one_slice` | request-only `NextSlicePlan`, impl→review→fanin candidates, `mutations: []`, adapter untouched |
| `test_go_apply_mode_creates_slice_once` | `auto_continue` + `autopromote_apply_enabled` + all gates → adapter called once per candidate; idempotent on replay |
| `test_go_without_directive_noops` | `missing_next_slice`; zero candidates |
| `test_go_prose_only_directive_refuses` | `ambiguous_go`; no promotion |
| `test_unknown_transition_refuses` | transition not allowlisted → `unknown_transition` |
| `test_block_and_need_more_never_promote` | BLOCK/NEED_MORE → no promotion (remediation path only) |
| `test_missing_review_edge_refuses` | `missing_review_edge` |
| `test_missing_or_unverified_ack_edge_refuses` | `missing_ack_edge` / `subscription_unverified` |
| `test_untrusted_assignee_refuses` | `untrusted_assignee` |
| `test_foreign_origin_or_return_to_refuses` | `foreign_origin`; origin/return_to preserved exactly or refuse |
| `test_stale_inline_route_in_go_body_blocks` | contradicted inline route → `stale_inline_route`, no promotion |
| `test_policyref_drift_changes_next_worker_resolution` | central policy change after creation changes next resolution; preview text unchanged |
| `test_auto_continue_false_by_default_noops` | default policy → `autopromote_disabled` |
| `test_max_chain_depth_stops_cascade` | depth cap reached → `max_chain_depth`, no further slices |
| `test_max_promotions_per_roadmap_stops` | per-roadmap cap → `max_promotions_per_roadmap` |
| `test_cooldown_suppresses_repeated_promotion` | GO within cooldown → `noop`/`cooldown` |
| `test_duplicate_event_id_noops` | replay returns prior decision |
| `test_existing_promotion_key_noops` | claimed key → `existing_promotion`, no double create |
| `test_malformed_policy_or_registry_kill_switches` | fail-closed, no adapter call |
| `test_operator_receipt_and_origin_ack_describe_autocreation` | receipt + ACK state names transition, to_slice, chain depth, created candidate keys |
| `test_no_private_paths_or_secrets_in_promotion_receipts` | durable receipt JSON has only sanitized refs/enums |

Existing suites (store/ack/PolicyRef/remediation/graph_creator/loop_supervisor/
migrations) must stay green; the new GO branch is additive to `evaluate_loop_event`.

## 9. Operator receipt and origin ACK

Every promote/apply emits a compact, sanitized receipt **and** an origin ACK back to
the preserved `return_to`, stating what was auto-created:

```text
[AUTOPROMOTE] roadmap=hermes.live-migration transition=m14->m15.impl_review_fanin
  from=m14 to=m15 depth=1 decision=propose
  created=[roadmap:...:promote:...:impl, :review, :fanin]  mutations=0
  ack_edge=verified origin=<origin_ref> return_to=<return_to_ref>
  policy_version=<hash>  auto_continue=true
```

The ACK is refs/enums only (reuses `safe_event_payload` / `short_text`). It names
the transition, the target slice, the chain depth, and the created candidate keys so
the operator can see exactly which next slice was synthesized and stop the cascade if
undesired. No raw bodies, transcripts, or private paths.

## 10. Safety invariants (compliance check)

1. **No free-text synthesis.** Only allowlisted `RoadmapTransition` templates /
   PolicyRefs instantiate; unknown transition → refuse. ✔ (§2, §3 gate 6)
2. **Trusted final GO + preserved origin.** Trusted-assignee gate + exact
   origin/return_to match required before any promotion. ✔ (§3 gates 9–10)
3. **`auto_continue` opt-in, false by default.** Separate master gate. ✔ (§2, §3 gate 3)
4. **Bounded / no-storm.** Chain depth, per-roadmap repeat, cooldown, idempotency
   ledger. ✔ (§5)
5. **Stale inline policy BLOCKs; refs resolved at runtime.** PolicyRef preflight of
   the GO body; synthesized bodies carry refs only. ✔ (§4)
6. **No live side effects.** Only Kanban task creation via fake/no-op adapter under
   `autopromote_apply_enabled`; dry-run/request-only default. ✔ (§1, §3)
7. **Never auto-continue on BLOCK/NEED_MORE, ambiguous GO, missing review/next_slice/
   ACK edge.** Enumerated refuses. ✔ (§6)
8. **Operator receipt + origin ACK describe the auto-creation.** ✔ (§9)
9. **Testable on local fixtures; no Discord history dependency.** ✔ (§8)

## 11. Whether implementation can proceed, and the first slice

**Verdict: GO to implement, request-only, gated — conditional on external supervisor
verification of the native Claude Code `--model opus` route in §Route evidence.**

The abstraction is right and reuses the shipped MP4/PolicyRef machinery. Proceed
incrementally; do **not** ship apply-mode autopromotion in the first slice.

### First implementation slice (M14a — registry + parser + dry-run proposer)

1. Add `RoadmapTransition` / `RoadmapTransitionRegistry` and a fail-closed
   `load_roadmap_transitions()` (mirror `load_policy_document`), plus
   `parse_next_slice_directive()` (structured-marker classifier, `none` by default).
2. Add `propose_next_slice_graph(...)` to `graph_creator.py` reusing
   `GraphIntentCandidate`, `_policy_ref_body`, and the adapter boundary; return a
   request-only `NextSlicePlan` with `mutations: []`.
3. Add the `PromotionPolicy` fields to `LoopPolicy` (defaults keep autopromotion
   **off**) and a `_evaluate_autopromote` branch reached from the existing GO case
   in `evaluate_loop_event` — **decision-only, no adapter apply path yet**.
4. Add `RoadmapPromotionLedger` in-memory read model.
5. Ship the §8 tests that need no adapter apply: valid-proposal, all refuse/noop
   conditions, PolicyRef stale/drift, caps/cooldown/idempotency, sanitization,
   receipt/ACK content.

Subsequent slices, each behind its own review + fixture gate: **M14b** bounded
apply through the fake adapter (attempt-budget preserved); **M14c** operator CLI
`agentflow-hermes roadmap promote --event <ref> [--apply --live]` (dry-run default)
and status by roadmap id; **M14d** migration v5 (or `operator_receipts` channel) and
a live opt-in only after a separate review explicitly approves the armed board writer.
Default config stays `auto_continue=False`, request-only, no production board writer.

## 12. Open risks

- **Directive trust root.** The next-slice directive is parsed from the GO report;
  it must name an allowlisted transition, and the transition registry must be
  operator-owned config outside task-body control. If a compromised report could
  supply its own transition template, the anti-free-text guarantee is lost — hence
  registry-only instantiation and the trusted-assignee gate.
- **Depth vs. legitimate long roadmaps.** A genuinely long roadmap may exceed
  `max_chain_depth` and require an operator to re-arm. That is the intended
  fail-safe; raising the cap is a policy decision, not a code default.
- **ACK-edge dependency.** `require_ack_edge` leans on the subscription receipt
  channel from `docs/ack-remediation-control-plane.md`. If `SubscriptionEnsurer` is
  not verifying, apply-mode autopromotion must stay disabled or ACK-loss protection
  is only nominal.
- **Cooldown vs. throughput.** Aggressive cooldown may slow a healthy roadmap; that
  is acceptable for M14a/M14b and tunable via central policy later.
