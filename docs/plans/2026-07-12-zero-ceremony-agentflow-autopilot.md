# AgentFlow Zero-Ceremony Autopilot — Architecture & Implementation Plan

> **For Hermes/Claude Code:** implement this as one shipping milestone with incremental commits and one final implementation→review gate. Do not turn the phases below into separate human approval gates. The acceptance target is a real three-board, natural-language, event-driven continuation loop.

**Goal:** Reduce operator effort to zero interactions when the system can resolve a continuation from evidence or standing policy, and one natural-language reply when a genuine owner-only decision remains.

**Architecture:** Replace periodic, marker-sensitive workflow automation with one long-lived edge runtime (`agentflowd`) that discovers boards, consumes terminal events immediately, compiles typed outcomes, resolves requirements from evidence/policy, batches unresolved owner questions by origin lane, and resumes workflows from durable receipts. Keep a quiet periodic watchdog only for reconciliation.

**Tech stack:** Python 3.11+, SQLite, Hermes Kanban SQLite/event contracts, Hermes AgentFlow plugin tools, asyncio, Linux inotify with timed fallback, existing continuation store/outbox, pytest.

---

## 운영자 요약

현재 M26까지 다음은 live다.

```text
GO → next graph
code BLOCK → remediation (기존 경로 포함)
needs_input → owner anchor
owner receipt → resume task
#hermes-main / #research / #shaman active-wake
```

하지만 사용자는 여전히 내부 구조를 알아야 한다.

- reviewer가 `Outcome-Kind` marker를 정확히 써야 한다.
- owner input을 구조화해 제출해야 한다.
- 여러 needs-input이 오면 각각 응답해야 한다.
- 새 board는 registry에 수동 등록해야 한다.
- continuation은 최대 5분 polling 지연이 있다.
- GO, code-fix, needs-input이 서로 다른 watchdog/runtime에 남아 있다.

최종 UX는 이것이어야 한다.

```text
사용자: "이 기능 구현해서 리뷰까지 해줘"

[자동]
implementation → review → needs_input
→ 시스템이 계산 가능한 필드 자동 완성
→ 기존 evidence/standing policy 재사용

Hermes: "URL 하나만 알려줘. 나머지는 확인됐고, 답하면 리뷰를 재개할게."
사용자: "https://example.com/result"

[자동]
reply 해석 → receipt → artifact/resume → review → final GO
```

사용자는 다음을 몰라도 된다.

```text
Kanban task id
contract_ref
Outcome-Kind
receipt JSON
parent graph
watchdog
CLI submit syntax
board registry
```

### Human Effort Budget

모든 continuation은 아래 예산을 지켜야 한다.

```text
H0: 0회 — system evidence, verified artifact, standing policy로 해결 가능
H1: 1회 — 진짜 owner-only decision/input이 남음
H2: 2회 — 첫 답변이 상충하거나 필수값 하나가 실제로 빠짐
H3+: 제품 결함으로 취급
```

질문 횟수를 줄이는 것이 목표가 아니라, 사람이 판단해야 할 내용만 남기고 나머지를 시스템이 책임지는 것이 목표다.

---

## 1. Current-state findings

### 1.1 M26 is live, but ingress is still polling

The active runtime is a no-agent cron every five minutes:

```text
AgentFlow global needs_input continuation watchdog
```

`scripts/agentflow_needs_input_watchdog.py` reads every registered board and advances board-scoped cursors. Correctness is good, but latency and process shape are not optimal.

### 1.2 The runtime defaults to needs-input only

The watchdog passes:

```python
handle_kinds = (ContinuationKind.NEEDS_INPUT,)
```

unless `--all-kinds` is manually enabled. GO roadmap and code remediation therefore still retain parallel top-level runtimes.

### 1.3 Outcome compilation still depends on worker ceremony

`outcome.py` prefers structured metadata, then explicit text markers, then a narrow phrase:

```text
operator must provide|approve|confirm
```

A reviewer that naturally writes “blocked pending the owner’s URL” can still become `UNKNOWN` unless the exact marker contract is followed.

### 1.4 Owner anchors are machine-correct but not interaction-complete

`OwnerInputHandler` creates a blocked task and subscribes the origin. The active-wake tells the Hermes session that a task changed, but the engine does not yet own:

- a concise question optimized for humans;
- natural-language reply interpretation;
- pending-request correlation;
- multiple-request batching;
- system-derived prefill;
- standing policy resolution.

### 1.5 Generic input is too generic

`generic.owner-input.v1` currently requires:

```text
owner_decision = approve | reject | revise
owner_confirmation = boolean
```

That cannot express “provide the URL”, “choose A/B”, “confirm this artifact”, or “correct this identifier” without adding a domain contract or using free-form body text.

### 1.6 Board enrollment still requires manual config

`config/boards.yaml` hardcodes three boards and their fallback Discord endpoints. The code is generic, but onboarding effort remains.

### 1.7 Store surfaces remain fragmented

M26 uses:

```text
~/.hermes/state/agentflow_needs_input_continuations.sqlite
```

while other AgentFlow paths have historically used `~/.hermes/agentflow/agentflow.sqlite`, `~/.agentflow/agentflow.db`, and JSON receipt files. A zero-ceremony runtime cannot require the operator to know which store owns which continuation.

---

## 2. Design principles

### 2.1 Ask only for irreducible owner input

Before asking a human, resolve each requirement in this order:

```text
1. derive from trusted system state
2. reuse an existing verified artifact/receipt
3. satisfy from a scoped standing policy
4. infer a candidate from current conversation/task context
5. ask the owner once for the unresolved remainder
```

### 2.2 Questions are a runtime resource

An owner question is not a free side effect. It consumes attention. The runtime tracks an `interaction_count` and treats unnecessary follow-ups as a failed acceptance condition.

### 2.3 Natural language is the UI; typed contracts remain the authority

The user replies naturally. A reply compiler extracts candidate fields, but the contract validates and stores only typed values.

```text
natural-language reply → candidate fields → contract validation → receipt
```

Raw user text is not the receipt.

### 2.4 One runtime, many handlers

GO, code-fix, needs-input, approval, and external-wait are continuation handlers behind one event runtime—not separate cron products.

### 2.5 Channels are endpoints, not workflow implementations

No `if board == oracle-lab` or `if channel == shaman`. Board discovery and source task metadata determine routing. Domain contracts determine required data.

### 2.6 Event-first, reconciliation-always

The primary path should react in seconds. The periodic watchdog remains a quiet recovery path for missed events, pending outbox operations, and process restarts.

### 2.7 Standing intent reduces repeated approvals

The owner can state a reusable policy once, such as:

```text
"docs-only release after reviewer GO는 앞으로 자동 진행"
```

The runtime stores a versioned policy receipt and reuses it only within that declared scope. This is not a new gate; it removes repeated gates.

### 2.8 Evidence is not a preference

Standing policy can satisfy preference/authorization fields. It cannot invent factual evidence. The requirement model must distinguish them.

---

## 3. Target architecture

```text
Hermes Kanban board DBs
  task_events / task_runs / subscriptions
            |
            | WAL/inotify wake + 1s coalescing
            v
      AgentFlowD Event Runtime
  +----------------------------------------------+
  | Board Discovery + Route Learning             |
  | Event Cursor + Durable Inbox                  |
  | Outcome Compiler                             |
  | Continuation Router                          |
  | Requirement Resolver                         |
  | Interaction Coordinator                      |
  | Handler Runtime + Durable Outbox              |
  +----------------------------------------------+
        |              |                |
        v              v                v
   auto-resolve    owner inbox      external wait
        |              |                |
        +--------------+----------------+
                       v
                 Continuation Store
                       |
                       v
                  Board Adapter
                       |
                       v
          owner anchor / resume / review / next

Hermes origin session
  active-wake → plugin pending-input tool → one concise question
  user natural reply → plugin submit-text tool → typed receipt
```

### Runtime shape

```text
agentflowd.service       long-lived event/interaction runtime
agentflow-reconcile.timer 5m quiet recovery only
```

No per-board cron and no per-channel service.

### Why an edge daemon instead of a large Hermes core patch

AgentFlow is a standalone plugin/CLI product. The daemon can watch board SQLite WAL files, own its store/outbox, and use the existing Hermes Kanban CLI adapter. This preserves the Hermes narrow waist.

An optional future upstream primitive may emit a generic Kanban event hook, but M27 does not depend on it. If the hook later exists, it replaces the WAL wake source while the same durable cursor/reconciliation logic stays.

---

## 4. Human Effort Resolver

Create `src/agentflow_hermes/requirement_resolver.py`.

### 4.1 Requirement types

```python
class RequirementKind(str, Enum):
    FACT = "fact"                  # URL, artifact ref, external identifier
    EVIDENCE = "evidence"          # proof bound to a source
    PREFERENCE = "preference"      # choose A/B, style, priority
    AUTHORIZATION = "authorization"# permission for an action class
    CORRECTION = "correction"      # replace known-wrong value
```

### 4.2 Authority sources

```python
class SatisfactionSource(str, Enum):
    SYSTEM_DERIVED = "system_derived"
    VERIFIED_ARTIFACT = "verified_artifact"
    STANDING_POLICY = "standing_policy"
    CURRENT_OWNER_REPLY = "current_owner_reply"
    VERIFIER = "verifier"
```

### 4.3 Resolution result

```python
@dataclass(frozen=True)
class ResolutionResult:
    satisfied: tuple[SatisfiedRequirement, ...]
    unresolved: tuple[Requirement, ...]
    contradictions: tuple[Contradiction, ...]
    evidence_refs: tuple[str, ...]
    interaction_needed: bool
```

### 4.4 Resolution ladder

For every field:

1. **System derivation**: query source task metadata, parent summaries, current board rows, known artifact refs.
2. **Artifact reuse**: find an unexpired verified receipt bound to the same source/contract/field semantics.
3. **Standing policy**: match exact action/resource/project scope and policy version.
4. **Context candidate**: extract from the source summary and current origin-session reply context.
5. **Owner question**: include only unresolved owner fields.

If all fields are satisfied before step 5, create an automatic decision receipt and skip the owner anchor entirely.

---

## 5. Outcome Compiler

Create `src/agentflow_hermes/outcome_compiler.py`.

### 5.1 Compilation pipeline

```text
structured agentflow_outcome metadata
        ↓ absent
explicit markers / deterministic grammar
        ↓ ambiguous
bounded LLM compile-to-schema
        ↓
OutcomeEnvelope validation
```

The model does not execute actions. It only emits a candidate `OutcomeEnvelope`; the deterministic validator selects the handler.

### 5.2 Bounded compiler input

Input:

- source task title;
- latest full run summary;
- terminal event kind;
- assignee/role;
- parent graph semantic state;
- known contract names/field descriptions.

Do not send full chat transcripts or secrets.

### 5.3 Candidate schema

```json
{
  "verdict": "BLOCK",
  "continuation_kind": "needs_input",
  "required_inputs": [
    {
      "name": "result_url",
      "kind": "fact",
      "authority": "owner",
      "question": "검증 결과 URL을 알려줘"
    }
  ],
  "resume_transition": "retry-review",
  "confidence": 0.96
}
```

### 5.4 Action rule

- Structured metadata: immediately eligible.
- Deterministic explicit result: immediately eligible.
- LLM candidate: eligible for reversible `needs_input` owner-anchor creation after schema validation.
- LLM cannot generate an owner receipt, artifact proof, or authorization by itself.

This policy removes marker ceremony without allowing a classifier to impersonate the user.

---

## 6. Interaction Inbox

Create `src/agentflow_hermes/interaction.py` and new store tables.

### 6.1 Interaction case

```python
@dataclass(frozen=True)
class InteractionCase:
    id: str
    origin_endpoint: str
    continuation_ids: tuple[int, ...]
    unresolved_fields: tuple[Requirement, ...]
    state: str  # collecting | asked | answered | applied | needs_clarification
    batch_key: str
    question_count: int
    created_at: float
    asked_at: float | None
```

### 6.2 Batch policy

Within a short coalescing window (default 10 seconds), combine compatible requests with the same:

- origin endpoint;
- owner identity;
- project/graph;
- non-conflicting authority class.

Example:

```text
3 reviewer needs_input events
→ one owner message with 3 numbered decisions
→ one natural reply
→ 3 typed receipts
→ 3 resumes
```

Do not batch unrelated live-money approval with a docs URL request.

### 6.3 Question composer

The message must answer four things and nothing else.

```text
1. What is blocked?
2. What has already been resolved automatically?
3. What exact decision/input remains?
4. What happens after the reply?
```

Example:

```text
Oracle review가 URL 하나를 기다리고 있어.
나머지 4개 검증값은 기존 artifact에서 확인했어.
검증 결과 URL만 답장해줘. 답하면 review task를 자동 재개할게.
```

No task IDs unless useful for disambiguation. No `contract_ref` or receipt syntax in the human-facing prompt.

### 6.4 Correlation

Every owner anchor carries machine metadata:

```json
{
  "agentflow_interaction": {
    "case_id": "ic_...",
    "origin_endpoint": "discord:...",
    "continuation_ids": [42],
    "reply_mode": "natural_language"
  }
}
```

The human text remains clean. The plugin and daemon use `case_id` for correlation.

---

## 7. Natural-Language Reply Bridge

Extend `plugins/hermes-agentflow/__init__.py` with three tools.

### 7.1 `agentflow_input_inbox`

```text
Purpose: list the current origin session's unresolved interaction cases.
Arguments: endpoint inferred from the active gateway session; optional case_id.
```

### 7.2 `agentflow_submit_input_text`

```text
Arguments:
- case_id
- text: raw current user reply
- owner_ref/source_ref inferred from session metadata
```

Engine behavior:

1. compile reply into candidate fields;
2. validate against each contract;
3. apply fully satisfied cases;
4. return one concise missing-field message if incomplete;
5. never store raw text in owner receipt;
6. store only a content hash/source message ref for provenance.

### 7.3 `agentflow_input_status`

Returns human-oriented state:

```text
waiting for you | resolved | resumed | failed retryable
```

### 7.4 Agent behavior contract

When an active-wake indicates an owner-input case, Hermes should:

1. call `agentflow_input_inbox`;
2. ask the rendered question;
3. on the next relevant user reply, call `agentflow_submit_input_text` automatically;
4. report whether the workflow resumed.

The user never types a slash command or JSON payload.

### 7.5 Reply ambiguity

If exactly one interaction case exists in the current origin lane, a plain reply binds to it.

If multiple unrelated cases exist, present a numbered batch. A reply like:

```text
1 approve, 2 revise with URL X
```

is enough.

---

## 8. Standing Policy Receipts

Create `src/agentflow_hermes/standing_policy.py`.

### 8.1 Policy snapshot

```python
@dataclass(frozen=True)
class StandingPolicy:
    policy_id: str
    version: int
    owner_ref: str
    project_scope: str
    action_scope: str
    conditions: dict[str, Any]
    decision: str
    created_from_message_ref: str
    enabled: bool
```

### 8.2 Examples

```text
- reviewer GO 뒤 docs-only GitHub release는 자동 승인
- oracle-lab browser recheck는 추가 질문 없이 재시도
- research artifact에 URL만 빠졌으면 기존 source URL 재사용
- BLOCK이 code_fix면 owner에게 묻지 말고 fix/review 자동 생성
```

### 8.3 Creation UX

The user says naturally:

```text
"앞으로 docs-only release는 reviewer GO면 자동으로 해"
```

Hermes proposes one normalized scope summary and asks once:

```text
"agentflow-hermes repo의 docs-only release에만 적용할게. 맞지?"
```

One confirmation creates the versioned policy. Future matching requests become H0.

### 8.4 Policy limits by semantics

- PREFERENCE and scoped AUTHORIZATION may use standing policy.
- FACT and EVIDENCE must bind to actual current sources.
- A policy can authorize evidence collection; it cannot assert the evidence result.

---

## 9. Event Runtime (`agentflowd`)

Create `src/agentflow_hermes/daemon.py` and `scripts/agentflowd.py`.

### 9.1 Board discovery

Default behavior:

```text
scan ~/.hermes/kanban/boards/*/kanban.db
```

Every discovered non-disabled board is enrolled automatically.

`config/boards.yaml` becomes an override catalog, not an allowlist. It provides:

- disable/exclude;
- endpoint override;
- contract/policy override;
- friendly project metadata.

### 9.2 Route learning

Resolution priority:

```text
source task typed notify/ACK endpoint
→ latest verified endpoint receipt for board/project
→ board config default endpoint
→ unresolved route inbox visible in #hermes-main
```

When a task supplies a valid typed endpoint, persist it as a board-route observation. New boards no longer require the operator to copy a channel ID into config before the first continuation.

### 9.3 Fast wake source

Linux primary:

- inotify watches board DB directory/WAL changes;
- coalesce for 250–500 ms;
- read task events from durable cursor;
- no action based on filesystem event alone.

Fallback:

- one-second async scan if inotify unavailable;
- five-minute reconciliation timer for missed wake/process restart.

### 9.4 Unified handler router

```text
COMPLETE/ROADMAP_NEXT → roadmap handler
CODE_FIX             → remediation handler
NEEDS_INPUT          → resolver + interaction handler
APPROVAL_REQUIRED    → policy/owner interaction handler
EXTERNAL_WAIT        → condition watcher
UNKNOWN              → outcome compiler; unresolved only if still unknown
```

Once parity is proven, retire the separate GO and reviewer-BLOCK cron entrypoints. Keep compatibility CLI commands.

### 9.5 Daemon responsibilities

```text
consume events
compile outcome
resolve requirements
create/batch interaction cases
process accepted replies
advance semantic continuation steps
replay pending outbox
watch external conditions
emit material operator receipts
```

The daemon does not run worker code itself. Kanban workers remain the execution substrate.

---

## 10. Canonical control-plane store

Use one profile-scoped store:

```text
~/.hermes/agentflow/agentflow.sqlite
```

### New/extended tables

```sql
interaction_cases(
  id, endpoint, batch_key, state, question_count,
  created_at, asked_at, answered_at, applied_at
)

interaction_members(
  case_id, continuation_id, requirement_names_json
)

requirement_satisfactions(
  continuation_id, field_name, value_json, source_kind,
  source_ref, policy_id, created_at
)

standing_policies(
  policy_id, version, owner_ref, project_scope, action_scope,
  conditions_json, decision_json, enabled, source_message_ref, created_at
)

board_route_observations(
  board, project_ref, endpoint, source_task_id,
  observed_at, last_verified_at
)

inbound_reply_receipts(
  case_id, message_ref, content_sha256, compile_result_json, created_at
)
```

Do not store raw user messages in the receipt tables.

### Migration

Add `agentflow-hermes continuation migrate-store`:

1. detect active legacy DBs;
2. copy instances/cursors/outbox with source IDs preserved;
3. write migration receipt;
4. switch daemon only after row-count/idempotency verification;
5. leave old DB read-only for one release;
6. doctor reports canonical path and legacy residue.

This migration is automated and tested; it is not a per-board operator chore.

---

## 11. Zero-touch handlers beyond needs-input

### 11.1 `external_wait`

Create a durable condition specification:

```json
{
  "kind": "github_check",
  "target": "repo/ref",
  "desired": "success",
  "poll_interval_seconds": 60,
  "resume_transition": "release-review"
}
```

The daemon polls and resumes automatically. No owner question unless the condition fails permanently.

### 11.2 `approval_required`

Resolution:

```text
matching standing policy → H0 policy receipt → action
no policy              → H1 one owner question → action
```

### 11.3 `code_fix`

Code BLOCKs should never ask the owner when an actionable fix can be derived. The unified router creates fix/review immediately.

### 11.4 GO

GO next-slice promotion remains H0 and moves into the same daemon/outbox/cursor model.

---

## 12. Operator UX

### 12.1 Normal operation

The operator sees only material moments.

```text
- one concise question when truly needed
- final resumed/GO/BLOCK verdict
- optional daily waiting digest if anything remains unresolved
```

No per-tick messages.

### 12.2 Commands for inspection, not operation

```bash
agentflow-hermes autopilot status
agentflow-hermes autopilot waiting
agentflow-hermes autopilot explain <case-or-task>
agentflow-hermes autopilot policies
agentflow-hermes autopilot reconcile
```

The system should work without these commands. They exist for debugging and control.

### 12.3 Status output

```text
Board              Events  H0 auto  H1 asked  Waiting  Outbox  Latency p95
agentflow-hermes       42       38         4        0       0       1.2s
warroom-os             31       25         5        1       0       1.4s
oracle-lab             27       24         3        0       0       1.1s
```

Metrics remain local. No telemetry.

---

## 13. Acceptance criteria

### 13.1 Latency

For a terminal event written to a registered/discovered board:

```text
p95 event → continuation action < 5 seconds
```

The 5-minute timer is never the normal path.

### 13.2 H0 case

A continuation whose requirements are all system-derived or covered by standing policy:

```text
operator questions = 0
owner anchor = 0 (or auto-resolved projection only)
resume created exactly once
```

### 13.3 H1 natural reply case

```text
reviewer writes natural "blocked pending owner URL"
→ no explicit Outcome-Kind marker
→ one concise owner question
→ user replies with URL naturally
→ typed receipt
→ resume/review
operator questions = 1
```

### 13.4 Batched case

Three compatible needs-input events in one origin lane:

```text
one batched question
one natural response
three validated receipts
three resumed continuations
zero duplicate cards
```

### 13.5 Policy reuse

First matching approval requires one confirmation to create a standing policy. The second equivalent continuation is H0.

### 13.6 External wait

CI/external status pending:

```text
operator questions = 0
condition satisfied → automatic resume
```

### 13.7 Restart

Kill/restart `agentflowd` between outbox enqueue and board apply. It must reconcile and create one task, not zero or two.

### 13.8 Three-board live canary

Run real canaries on:

```text
agentflow-hermes / #hermes-main
warroom-os       / #research
oracle-lab       / #shaman
```

Each must prove:

- event latency;
- outcome compilation;
- correct origin question;
- natural reply receipt;
- resume;
- active-wake;
- duplicate suppression;
- daemon restart recovery.

---

## 14. Implementation plan

Implement as one M27 milestone with one implementation task and one final reviewer task. The sections below are code commits, not operator gates.

### Commit 1: Extend requirement and contract semantics

**Files:**

- Modify: `src/agentflow_hermes/input_contract.py`
- Create: `src/agentflow_hermes/requirements.py`
- Modify: `contracts/generic.owner-input.v1.yaml`
- Test: `tests/test_requirement_semantics.py`

**Work:**

1. Add FACT/EVIDENCE/PREFERENCE/AUTHORIZATION/CORRECTION kinds.
2. Add natural-language question/answer schema to fields.
3. Preserve current owner/system/verifier authority compatibility.
4. Replace generic approve+boolean-only shape with dynamic required inputs from the outcome.
5. Commit after targeted tests.

### Commit 2: Add outcome compiler

**Files:**

- Create: `src/agentflow_hermes/outcome_compiler.py`
- Modify: `src/agentflow_hermes/outcome.py`
- Modify: `src/agentflow_hermes/board_events.py`
- Test: `tests/test_outcome_compiler.py`

**Work:**

1. Structured metadata first.
2. Deterministic natural summary parser second.
3. Injectable bounded model compiler third.
4. Schema validation and provenance/confidence receipt.
5. Tests for natural summaries without marker ceremony.

### Commit 3: Add requirement resolver and standing policy

**Files:**

- Create: `src/agentflow_hermes/requirement_resolver.py`
- Create: `src/agentflow_hermes/standing_policy.py`
- Modify: `src/agentflow_hermes/continuation_store.py`
- Test: `tests/test_requirement_resolver.py`
- Test: `tests/test_standing_policy.py`

**Work:**

1. Implement derivation→artifact→policy→context→ask ladder.
2. Auto-create H0 receipts when complete.
3. Validate standing-policy scope and version.
4. Prove evidence fields cannot be satisfied by preference policies.

### Commit 4: Add interaction inbox and batching

**Files:**

- Create: `src/agentflow_hermes/interaction.py`
- Modify: `src/agentflow_hermes/continuation_store.py`
- Modify: `src/agentflow_hermes/continuations/owner_input.py`
- Test: `tests/test_interaction_inbox.py`

**Work:**

1. Create/batch cases by endpoint/project/authority.
2. Render concise questions.
3. Track question count and H0/H1/H2 classification.
4. Keep raw text out of durable owner receipts.
5. Apply one reply to multiple compatible cases.

### Commit 5: Add natural-language plugin tools

**Files:**

- Modify: `plugins/hermes-agentflow/__init__.py`
- Modify: `plugins/hermes-agentflow/plugin.yaml`
- Create/modify: `tests/test_plugin.py`
- Create: `tests/test_input_reply_bridge.py`

**Work:**

1. Add `agentflow_input_inbox`.
2. Add `agentflow_submit_input_text`.
3. Add `agentflow_input_status`.
4. Infer origin endpoint/owner/message refs from tool context rather than asking the user.
5. Natural reply → typed candidate → validation → receipt → resume.

### Commit 6: Add `agentflowd`

**Files:**

- Create: `src/agentflow_hermes/daemon.py`
- Create: `scripts/agentflowd.py`
- Create: `src/agentflow_hermes/service_install.py`
- Modify: `src/agentflow_hermes/cli.py`
- Test: `tests/test_agentflowd.py`
- Test: `tests/test_event_latency.py`

**Work:**

1. Discover board DBs automatically.
2. Add WAL/inotify wake with timed scan fallback.
3. Run one unified handler router.
4. Reconcile outbox after every wake/startup.
5. Implement graceful shutdown and single-instance lock.
6. Add install/status/uninstall service commands.

### Commit 7: Unify handlers and recovery runtime

**Files:**

- Modify: `src/agentflow_hermes/continuation_engine.py`
- Modify: `scripts/agentflow_needs_input_watchdog.py`
- Modify: GO/remediation watchdog shims
- Test: `tests/test_unified_continuation_router.py`
- Test: `tests/test_watchdog_compatibility.py`

**Work:**

1. Route GO/code-fix/needs-input through one daemon.
2. Keep old entrypoints as compatibility shims.
3. Convert 5m cron to reconciliation-only timer.
4. Prove existing M24/M26 behavior remains.

### Commit 8: Canonical store migration

**Files:**

- Modify: `src/agentflow_hermes/migrations.py`
- Modify: `src/agentflow_hermes/continuation_store.py`
- Modify: doctor/CLI
- Test: `tests/test_control_plane_store_migration.py`

**Work:**

1. Detect legacy DBs.
2. Migrate instances/cursors/outbox/receipts.
3. Verify row counts and unique keys.
4. Switch daemon to canonical store.
5. Report stale legacy state without requiring manual cleanup.

### Commit 9: Three-board human-effort canary

**Files:**

- Create: `tests/test_zero_ceremony_e2e.py`
- Create: `docs/m27-zero-ceremony-canary.md`
- Modify: `README.md`

**Real verification:**

1. Start `agentflowd`.
2. Create fresh natural-summary events on all three boards.
3. Prove <5s response.
4. Run H0, H1, batched, policy-reuse, and restart-recovery cases.
5. Verify active-wake in correct origin lane.
6. Verify one natural reply resumes without CLI syntax.
7. Archive canary artifacts after receipts.
8. Run full test suite and `git diff --check`.

---

## 15. Final design decisions

### Decision A: Daemon over more cron

**Chosen:** one long-lived edge daemon plus reconciliation timer.

Reason: lowest latency, one runtime, fewer duplicate scanners, no channel-specific rollout.

### Decision B: Natural reply over forms/JSON

**Chosen:** normal chat reply compiled into typed fields.

Reason: lowest operator ceremony. Forms/buttons may be optional renderers later, not required infrastructure.

### Decision C: H0 auto-resolution before owner anchor

**Chosen:** do not create a human task when no human input is actually required.

Reason: an automatically completed owner anchor is still noise.

### Decision D: Standing policy as a durable owner receipt source

**Chosen:** reuse explicit scoped intent.

Reason: repeating the same approval question is not correctness; it is wasted attention.

### Decision E: Automatic board discovery

**Chosen:** discover boards by default, config overrides only.

Reason: new boards should inherit continuation without per-channel implementation or manual registry editing.

### Decision F: Plugin tools, not new Hermes core tools

**Chosen:** AgentFlow plugin exposes inbox/submit/status tools.

Reason: capability stays at the edge and only AgentFlow-enabled sessions pay the tool-schema cost.

### Decision G: One final review gate

**Chosen:** implementation commits are internal checkpoints; the operator sees one implementation result and one reviewer verdict.

Reason: minimize human coordination while preserving independent verification.

---

## 16. Design verdict

**GO for implementation.**

The next architecture should not optimize for “more automatic task creation.” It should optimize for **the minimum number of times a human must reconstruct context or translate intent into workflow syntax**.

The target invariant is:

```text
If the system can know it, derive it.
If the owner already decided it, reuse the scoped policy.
If several questions can be combined, ask once.
If a human must decide, accept a natural reply.
After the reply, resume everything automatically.
```

A successful M27 changes AgentFlow from a board autopromoter into an operator-attention optimizer.
