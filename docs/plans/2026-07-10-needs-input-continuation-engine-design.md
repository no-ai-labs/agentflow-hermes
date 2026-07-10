# AgentFlow Needs-Input Continuation Engine — Architecture & Implementation Plan

> **For Hermes:** implement this plan through reviewed vertical slices. The first slice must close one real Warroom `needs_input` loop end-to-end; do not stop at schemas, preview output, or a proposal-only CLI.

**Goal:** Make `BLOCK/NEED_MORE` outcomes that require operator evidence, approval, or artifacts continue automatically into an owner-anchored workflow without misclassifying them as code fixes or fabricating proof.

**Architecture:** Replace verdict-only branching with a typed continuation engine. A structured `OutcomeEnvelope` selects a handler (`GO`, code remediation, owner input, approval, or external wait); the handler records a durable continuation instance, materializes only the next currently-runnable board step, and resumes downstream work after a machine-readable receipt satisfies the contract.

**Tech stack:** Python 3.11+, dataclasses/Pydantic-style validation, SQLite, Hermes Kanban CLI adapter, existing AgentFlow sanitization/idempotency primitives, pytest.

---

## 운영자 요약

현재 AgentFlow는 세 가지 일을 각각 따로 한다.

1. `GO`이면 roadmap marker를 읽고 다음 slice graph를 만든다.
2. allowlisted reviewer `BLOCK`이면 코드 fix/review graph를 만든다.
3. `NEED_MORE`이면 `escalate/needs_input` receipt만 남기고 멈춘다.

Warroom G4.21은 세 번째 케이스의 실제 실패 사례다. `t_ab93a206`은 코드가 틀린 것이 아니라 다음 입력이 없어서 멈췄다.

- append-only exposure-resolution marker
- semantic evidence artifact
- local no-POST proof
- operator approval/confirmation

현재 엔진은 이 차이를 타입으로 표현하지 못한다. 결과적으로 completion ACK는 오지만 보드는 다음 행동을 소유하지 못하고, 사람이 다시 문맥을 읽고 수동 task를 만들어야 한다.

이 설계의 핵심은 단순하다.

```text
Verdict != Continuation
```

`GO/BLOCK/NEED_MORE`는 품질 판정이고, 그 뒤에 무엇을 해야 하는지는 별도의 `ContinuationKind`다.

```text
GO + roadmap_next       -> 다음 slice
BLOCK + code_fix        -> fix/review
BLOCK + needs_input     -> owner-anchor/wait
NEED_MORE + needs_input -> owner-anchor/wait
BLOCK + approval        -> explicit approval anchor
BLOCK + external_wait   -> poll/recheck anchor
```

첫 실제 vertical outcome은 다음이다.

```text
Warroom reviewer needs_input
  -> #research owner-anchor 자동 생성
  -> 필요한 필드와 blank scaffold 노출
  -> owner receipt 제출
  -> artifact/append task 생성
  -> independent review
  -> G4.21 packet rerun 생성
```

AgentFlow는 proof의 형식과 빈 template은 만들 수 있지만, operator-only assertion이나 approval을 채워 넣어서는 안 된다.

---

## 1. 조사로 확인한 현재 구조

### 1.1 Verdict parser가 너무 얕다

`src/agentflow_hermes/remediation.py`는 정규식으로 다음만 찾는다.

```python
Verdict: GO | BLOCK | NEED_MORE
```

BLOCK class도 네 개의 text pattern에 고정돼 있다.

- `stale_inline_route`
- `stale_trust_grant_wording`
- `missing_subscription`
- `stale_final_fanin`

`needs_input`, `approval_required`, `artifact_missing`, `external_wait`는 도메인 타입이 아니다.

### 1.2 Loop supervisor는 NEED_MORE를 의도적으로 dead-end로 만든다

`src/agentflow_hermes/loop_supervisor.py`의 현재 분기:

```python
if verdict == "NEED_MORE":
    return escalate(reason="needs_input")
```

테스트도 `test_loop_need_more_stops_escalates_no_auto_create`로 이 dead-end를 계약화했다. 이 계약은 “proof를 자동 생성하지 말라”는 의미로는 맞지만, “owner에게 입력을 요청하는 durable task도 만들지 말라”까지 포함해 버렸다.

### 1.3 GO와 code BLOCK의 continuation은 별도 subsystem이다

- GO: `roadmap.py`, `roadmap_config.py`, `roadmap_templates.py`
- code BLOCK: `remediation.py`, `graph_creator.py`
- live/ACK jobs: `states.py`, `store.py`
- Oracle auto-remediation: repo `scripts/` + legacy `/home/duckran/dev/agentflow`

이들은 각각 다른 policy, ledger, adapter, idempotency 형태를 쓴다. 동일한 “terminal outcome -> next board state” 문제를 네 번 구현하고 있다.

### 1.4 이미 필요한 상태는 있는데 연결되지 않았다

`src/agentflow_hermes/states.py`에는 `WAITING_USER`가 있다. 그러나 loop supervisor/roadmap/remediation은 이 상태를 continuation으로 사용하지 않는다.

즉 missing primitive는 새 안전 계층이 아니라 다음 연결이다.

```text
terminal outcome -> WAITING_USER continuation -> owner receipt -> resume
```

### 1.5 Kanban parent completion은 semantic verdict를 모른다

Hermes Kanban dependency는 lifecycle `done`을 본다. `status=done` + `Verdict: BLOCK`도 child를 promote할 수 있다. 따라서 needs-input graph의 downstream을 미리 모두 생성해 parent link로 막는 방식은 안전하지도, 단순하지도 않다.

**결론:** downstream task는 lazy materialization 해야 한다. 현재 조건을 만족한 다음 step만 만든다.

### 1.6 Event/ledger surface가 분열돼 있다

현재 발견된 durable surfaces:

- `~/.agentflow/agentflow.db` — packaged CLI default
- `~/.hermes/agentflow/agentflow.sqlite` — older operator-main queue
- roadmap JSON receipts
- loop in-memory receipts / fixture receipts
- release JSON receipts
- per-board Kanban event cursors
- Oracle auto-remediation watchdog JSON state

M24A에서 확인한 queued-job consumer gap과 동일한 구조적 문제다. 새로운 needs-input 기능이 또 별도 JSON ledger를 만들면 안 된다.

### 1.7 G4.21 domain contract는 authority 분리가 필수다

`ExposureResolutionMarkerRow`와 semantic artifact validator를 읽어보면 값은 세 종류다.

1. **system-derived**: target id, ledger hash, terminal-line hash, known round-trip refs
2. **operator-attested**: approval receipt, chosen resolution basis, explicit confirmation
3. **verifier-derived**: artifact hashes, schema validation, no-POST trace validation, marker binding

AgentFlow가 1과 3은 계산할 수 있다. 2를 추론하거나 생성하면 proof fabrication이다.

---

## 2. 설계 원칙

### 2.1 Board owns workflow state; agents only advance steps

에이전트의 chat context가 아니라 다음 상태가 durable해야 한다.

- 어떤 입력이 필요한가
- 누가 제공해야 하는가
- 무엇이 이미 제공됐는가
- 어떤 assertion이 아직 owner-only인가
- 어떤 step이 runnable인가
- 어떤 downstream이 아직 materialize되면 안 되는가

### 2.2 Verdict와 continuation을 분리한다

```python
class Verdict(str, Enum):
    GO = "GO"
    BLOCK = "BLOCK"
    NEED_MORE = "NEED_MORE"
    UNKNOWN = "UNKNOWN"

class ContinuationKind(str, Enum):
    COMPLETE = "complete"
    ROADMAP_NEXT = "roadmap_next"
    CODE_FIX = "code_fix"
    NEEDS_INPUT = "needs_input"
    APPROVAL_REQUIRED = "approval_required"
    EXTERNAL_WAIT = "external_wait"
    UNKNOWN = "unknown"
```

`BLOCK`은 continuation kind가 아니다. 같은 BLOCK도 code fix일 수 있고 owner input일 수 있다.

### 2.3 Structured metadata first, text fallback second

정규식은 backward compatibility용이다. 새 worker/reviewer contract는 Kanban run metadata에 canonical JSON을 넣는다.

```json
{
  "agentflow_outcome": {
    "schema_version": 1,
    "verdict": "BLOCK",
    "continuation_kind": "needs_input",
    "contract_ref": "warroom.g421.exposure-resolution.v1",
    "source_task_id": "t_ab93a206",
    "required_inputs": [
      "approval_receipt_id",
      "resolution_basis",
      "owner_confirmation"
    ]
  }
}
```

사람이 읽는 summary는 이 구조에서 render한다. summary text를 다시 parsing해서 authority를 복원하지 않는다.

### 2.4 Scaffold generation과 assertion generation을 분리한다

허용:

- schema-valid blank template
- system-derived fields
- required-field checklist
- relative destination path
- hashes after source bytes exist

금지:

- approval receipt 발명
- operator confirmation을 true로 채우기
- evidence classification을 추정해 사실처럼 저장
- owner receipt 전에 append-only marker 추가
- marker가 유효하다고 self-review

### 2.5 Lazy graph materialization

전체 graph를 처음에 만들지 않는다.

```text
source needs_input
  -> owner-anchor only

owner receipt accepted
  -> artifact materialize/append task

artifact task GO
  -> independent review

review GO
  -> packet rerun
```

이 구조는 lifecycle `done`/semantic `BLOCK` 혼동도 피한다.

### 2.6 Real vertical loop, not preview-only

첫 milestone은 schema와 dry-run 출력이 아니다. real Warroom board에서 owner-anchor가 생성되고, controlled owner input receipt가 continuation을 재개하는 것까지다.

Live trading execution은 이 설계 범위가 아니다. 그러나 Kanban writes와 artifact task creation은 실제로 검증한다.

---

## 3. 목표 아키텍처

```text
Hermes Kanban terminal event
        |
        v
Board Event Ingestor
  - board-scoped cursor
  - structured run metadata first
  - text fallback
        |
        v
Outcome Normalizer
  -> OutcomeEnvelope
        |
        v
Continuation Router
  +--------------------+--------------------+--------------------+
  |                    |                    |                    |
  v                    v                    v                    v
RoadmapNextHandler  CodeFixHandler   OwnerInputHandler    ExternalWaitHandler
  |                    |                    |                    |
  +--------------------+--------------------+--------------------+
                               |
                               v
                      Continuation Ledger
                  instance / steps / receipts / outbox
                               |
                               v
                         Board Adapter
          create | block/wait | subscribe | comment | complete
                               |
                               v
                  Hermes board + origin active-wake
```

### 3.1 `OutcomeEnvelope`

Create: `src/agentflow_hermes/outcome.py`

```python
@dataclass(frozen=True)
class OutcomeEnvelope:
    schema_version: int
    event_id: str
    board: str
    source_task_id: str
    source_graph_id: str
    verdict: Verdict
    continuation_kind: ContinuationKind
    contract_ref: str
    origin_ref: str
    return_to_ref: str
    workspace_ref: str
    assignee: str
    occurred_at: float
    requirements: tuple["RequirementRef", ...] = ()
    next_transition: str = ""
    confidence: str = "structured"
```

Invariants:

- `GO + CODE_FIX` invalid
- `NEEDS_INPUT` requires `contract_ref`
- `ROADMAP_NEXT` requires `next_transition`
- source board/task/event refs required
- text fallback can never claim `confidence=structured`
- unknown/malformed outcome routes to operator anchor only if a configured fallback contract exists; otherwise no mutation

### 3.2 `InputContract`

Create: `src/agentflow_hermes/input_contract.py`

```python
class FieldAuthority(str, Enum):
    SYSTEM = "system"
    OWNER = "owner"
    VERIFIER = "verifier"

@dataclass(frozen=True)
class InputField:
    name: str
    value_type: str
    authority: FieldAuthority
    required: bool = True
    allowed_values: tuple[str, ...] = ()
    description: str = ""
    secret: bool = False

@dataclass(frozen=True)
class ArtifactSpec:
    artifact_id: str
    template_path: str
    final_path: str
    fields: tuple[InputField, ...]
    write_mode: str  # scaffold | materialize | append_only

@dataclass(frozen=True)
class InputContract:
    contract_ref: str
    version: int
    owner_role: str
    fields: tuple[InputField, ...]
    artifacts: tuple[ArtifactSpec, ...]
    resume_transition: str
```

No free-form LLM result may grant an OWNER field. Owner fields are accepted only through an `OwnerInputReceipt` bound to the continuation instance.

### 3.3 `ContinuationInstance` state machine

Create: `src/agentflow_hermes/continuation.py`

```text
DETECTED
  -> WAITING_OWNER
  -> INPUT_ACCEPTED
  -> MATERIALIZING
  -> WAITING_REVIEW
  -> RESUMABLE
  -> RESUMED

Any state -> BLOCKED_INVALID
Any materialization state -> FAILED_RETRYABLE
```

Rules:

- `WAITING_OWNER` is durable, not terminal failure.
- source task remains semantically BLOCK/NEED_MORE; it is never rewritten to GO.
- a new continuation instance owns progress.
- owner input is append-only; corrections create a new receipt version.
- only one active instance per `(board, source_task_id, source_event_id, contract_ref)`.
- `RESUMED` receipt stores downstream task ids.

### 3.4 Board projection

Owner anchor is represented on Hermes Kanban as:

```text
Title: [owner-input] G4.21 exposure-resolution evidence
Status: blocked
Blocked reason: awaiting_owner_input
Assignee: operator-main (non-worker owner anchor)
Origin: #research
Notify: #research active-wake subscription
```

It has no downstream children yet.

When owner input is accepted:

1. AgentFlow records the receipt.
2. Anchor receives a sanitized comment and is completed as `owner_input_received`.
3. Materialization task is created.
4. Only after materialization GO is review created/promoted.
5. Only after review GO is packet rerun created.

If Hermes cannot support `operator-main` as a non-dispatching assignee, the adapter creates the task blocked before dispatcher eligibility and records `owner_anchor=true` metadata. The durable continuation ledger, not assignee naming, is authoritative.

### 3.5 Canonical continuation ledger

Create: `src/agentflow_hermes/continuation_store.py`

Use one profile-scoped DB selected explicitly:

```text
$HERMES_HOME/agentflow/agentflow.sqlite
```

Fallback only for standalone use:

```text
~/.agentflow/agentflow.db
```

But `doctor` must BLOCK if both stores contain active jobs/continuations and no explicit canonical store is configured.

New tables:

```sql
continuation_instances(
  id, board, source_task_id, source_event_id, source_graph_id,
  contract_ref, verdict, continuation_kind, state,
  origin_ref, return_to_ref, workspace_ref,
  idempotency_key, created_at, updated_at
)

continuation_steps(
  id, continuation_id, step_kind, state, board_task_id,
  parent_step_id, idempotency_key, created_at, updated_at
)

owner_input_receipts(
  id, continuation_id, version, owner_ref, fields_json,
  source_ref, created_at, supersedes_receipt_id
)

continuation_events(
  id, continuation_id, seq, kind, payload_json, created_at
)

board_cursors(
  board, db_identity, last_event_id, updated_at
)

board_outbox(
  id, continuation_id, step_id, operation, payload_json,
  idempotency_key, state, board_task_id, attempts, created_at, updated_at
)
```

The outbox solves the current partial-apply problem. Plan and intent are committed first. Each external board operation updates its own row. Retry reconciles by idempotency key rather than pretending a partial graph was atomic.

### 3.6 Continuation handlers

Create package: `src/agentflow_hermes/continuations/`

```text
base.py
roadmap_next.py
code_fix.py
owner_input.py
approval.py
external_wait.py
```

Existing roadmap and remediation functions become handler dependencies rather than parallel top-level engines.

Router contract:

```python
class ContinuationHandler(Protocol):
    kind: ContinuationKind

    def plan(self, outcome, policy, store) -> ContinuationPlan: ...
    def materialize_next(self, instance, adapter, store) -> StepResult: ...
    def on_receipt(self, instance, receipt, adapter, store) -> StepResult: ...
```

### 3.7 Board-aware event ingestor

Create: `src/agentflow_hermes/board_events.py`

Replace separate global cursors and one-board scripts with a board registry:

```yaml
boards:
  warroom-os:
    db: ~/.hermes/kanban/boards/warroom-os/kanban.db
    outcome_handlers:
      - roadmap_next
      - code_fix
      - needs_input
  oracle-lab:
    db: ~/.hermes/kanban/boards/oracle-lab/kanban.db
    outcome_handlers:
      - roadmap_next
      - code_fix
```

Cursor key is `(board, db_identity)`, never a global integer. A DB switch cannot silently skip or replay events.

### 3.8 Adapter interface

Extend `graph_creator.py` or create `board_adapter.py`:

```python
class BoardAdapter(Protocol):
    def create_task(self, intent: TaskIntent) -> TaskReceipt: ...
    def block_task(self, task_id: str, reason: str) -> MutationReceipt: ...
    def subscribe(self, task_id: str, endpoint: ReturnEndpoint) -> MutationReceipt: ...
    def comment(self, task_id: str, body: str) -> MutationReceipt: ...
    def complete_owner_anchor(self, task_id: str, receipt_ref: str) -> MutationReceipt: ...
```

Do not model all operations as `create_graph`. Owner continuation needs create+block+subscribe+comment and lazy downstream creation.

Every method is idempotent and writes a receipt to the outbox row.

---

## 4. Canonical outcome contract

### 4.1 Worker/reviewer output

Human summary:

```text
Verdict: BLOCK
Outcome-Kind: needs_input
Continuation-Contract: warroom.g421.exposure-resolution.v1
Next action: operator must confirm the resolution basis and provide approval receipt id.
```

Canonical run metadata:

```json
{
  "agentflow_outcome": {
    "schema_version": 1,
    "verdict": "BLOCK",
    "continuation_kind": "needs_input",
    "contract_ref": "warroom.g421.exposure-resolution.v1",
    "required_inputs": [
      {"name": "approval_receipt_id", "authority": "owner"},
      {"name": "resolution_basis", "authority": "owner"},
      {"name": "owner_confirmation", "authority": "owner"}
    ],
    "resume_transition": "warroom.g421.packet-rerun"
  }
}
```

### 4.2 Backward-compatible classifier

When structured metadata is absent:

1. parse explicit verdict
2. parse explicit `Outcome-Kind`
3. parse `Continuation-Contract`
4. if summary says operator must provide/approve/confirm and required artifact/input is named, classify `needs_input` with `confidence=text_explicit`
5. if only vague prose exists, classify `unknown`; create nothing

Do not use an LLM classifier in the watchdog hot path. LLM advice may suggest a contract, but only repo-config allowlisted contracts can materialize.

---

## 5. G4.21 concrete contract

Create config: `contracts/warroom.g421.exposure-resolution.v1.yaml`

```yaml
contract_ref: warroom.g421.exposure-resolution.v1
version: 1
owner_role: warroom-owner
resume_transition: warroom.g421.packet-rerun

fields:
  target_order_ref_id:
    authority: system
    type: opaque_id
  target_ledger_sha256:
    authority: system
    type: sha256
  target_terminal_source_line_sha256:
    authority: system
    type: sha256
  resolution_basis:
    authority: owner
    type: enum
    allowed_values:
      - target_never_submitted
      - later_round_trip_completed
  approval_receipt_id:
    authority: owner
    type: opaque_id
  owner_confirmation:
    authority: owner
    type: boolean
  transport_calls:
    authority: verifier
    type: list

artifacts:
  evidence:
    template_path: data/warroom/canary_execution/templates/g421_semantic_evidence.template.json
    final_path: data/warroom/canary_execution/g421_semantic_exposure_evidence_<timestamp>.json
    write_mode: materialize
  local_no_post_proof:
    template_path: data/warroom/canary_execution/templates/g421_local_no_post_proof.template.json
    final_path: data/warroom/canary_execution/g421_local_no_post_proof.json
    write_mode: materialize
  marker:
    final_path: data/warroom/canary_execution/exposure_resolution_ledger.jsonl
    write_mode: append_only
```

### Authority matrix

| Field/action | AgentFlow | Operator | Worker | Reviewer |
| --- | --- | --- | --- | --- |
| Create blank schema/template | yes | no | yes | inspect |
| Derive ledger/line hashes | yes | no | yes | verify |
| Select resolution basis | no | yes | no | verify consistency |
| Provide approval receipt id | no | yes | no | verify binding |
| Assert owner confirmation | no | yes | no | verify receipt |
| Derive artifact digest | yes | no | yes | verify |
| Append marker | only after receipt | authorizes | performs | independently verifies |
| Resume packet | after review GO | no | runs | gates |

### G4.21 board sequence

```text
t_ab93a206 (existing source BLOCK, immutable)
    |
    v
[owner-input] G4.21 evidence/approval anchor
    status=blocked awaiting_owner_input
    #research active-wake
    |
    | owner receipt
    v
G4.21 materialize artifacts + append marker
    |
    v
Review G4.21 owner-bound artifact/marker
    |
    | GO
    v
Rerun G4.21 approval packet prep
```

No live order task is created by this continuation.

---

## 6. 구조 전반 개선점

### P0 — 반드시 같이 고칠 것

#### A. One continuation engine

GO autopromoter, BLOCK remediation, needs-input, and approval handlers share one router/store/outbox. Keep current public APIs as compatibility wrappers during migration.

#### B. One canonical store

Remove the silent split between `~/.agentflow/agentflow.db` and `~/.hermes/agentflow/agentflow.sqlite`. Configuration must select one; doctor reports split-brain as BLOCK.

#### C. Structured outcome metadata

Stop depending on summary regex as the authority source. Summary remains user-facing; metadata drives automation.

#### D. Board-scoped cursors

All scanners use `(board, db identity, event id)`. No global `last_seen_event_id`.

#### E. Lazy downstream creation

Never rely on `status=done` to represent semantic GO. Create the next task only from a GO continuation receipt.

### P1 — 다음으로 개선할 것

#### F. Durable outbox/reconciliation

Current roadmap apply can create partial tasks and then return failure. A durable outbox makes each mutation observable/retryable and avoids ambiguous “uncommitted_task_ids”.

#### G. First-class return endpoint

Replace free-form `origin`/`return_to` strings with:

```python
ReturnEndpoint(platform, chat_id, thread_id, profile)
```

Task-body prose is only rendering. Board adapter uses the typed endpoint for subscriptions.

#### H. Template registry outside Python constants

`roadmap_templates.py` has a closed Python preset registry. Continuation contracts and task templates should live in versioned repo config, validated on load. Python owns schema and behavior, not every lane-specific workflow.

#### I. Operator UX

Add:

```bash
agentflow-hermes continuation list --state waiting_owner
agentflow-hermes continuation show <id>
agentflow-hermes continuation submit <id> --input owner-input.json
agentflow-hermes continuation retry <id>
agentflow-hermes continuation doctor
```

`show` renders exactly:

- why it paused
- required owner fields
- system-derived fields already available
- what will happen after submit
- what it will not do

### P2 — 확장 가능한 다음 단계

- `approval_required` handler for release/live-execution approvals
- `external_wait` handler for OAuth, CI, vendor response, settlement, or human review
- timeout/reminder policy for owner anchors
- dashboard projection of WAITING_OWNER continuations
- generic schema-driven web form, without embedding domain logic in Hermes core

---

## 7. Implementation plan

### Task 1: Add structured outcome model and parser

**Files:**

- Create: `src/agentflow_hermes/outcome.py`
- Modify: `src/agentflow_hermes/remediation.py`
- Test: `tests/test_outcome.py`

**Steps:**

1. Write tests for structured metadata precedence over summary text.
2. Write invalid combination tests (`GO + code_fix`, needs_input without contract).
3. Implement enums and `OutcomeEnvelope` validation.
4. Implement text fallback for explicit markers only.
5. Keep existing `parse_verdict_summary` compatibility.
6. Run:

```bash
uv run pytest tests/test_outcome.py tests/test_loop_supervisor.py -q
```

Expected: existing verdict tests remain green; new tests distinguish verdict from continuation kind.

### Task 2: Add input contracts and versioned registry

**Files:**

- Create: `src/agentflow_hermes/input_contract.py`
- Create: `src/agentflow_hermes/continuation_config.py`
- Create: `contracts/warroom.g421.exposure-resolution.v1.yaml`
- Test: `tests/test_input_contract.py`

**Steps:**

1. Test authority validation and unknown contract refusal.
2. Test that owner fields cannot be satisfied by system-derived values.
3. Test G4.21 config loading and exact allowed enum values.
4. Implement loader using the existing minimal YAML parser or move both config families to one validated loader.
5. Run targeted tests.

### Task 3: Add durable continuation store and migration

**Files:**

- Create: `src/agentflow_hermes/continuation_store.py`
- Modify: `src/agentflow_hermes/migrations.py`
- Modify: `src/agentflow_hermes/store.py`
- Test: `tests/test_continuation_store.py`

**Steps:**

1. Add migrations for instances, steps, owner receipts, events, board cursors, outbox.
2. Test unique active instance/idempotency constraints.
3. Test append-only owner receipt versioning.
4. Test state transition legality.
5. Add split-store doctor detection.
6. Verify migration against temp DB and existing tests.

### Task 4: Implement owner-input handler

**Files:**

- Create: `src/agentflow_hermes/continuation.py`
- Create: `src/agentflow_hermes/continuations/base.py`
- Create: `src/agentflow_hermes/continuations/owner_input.py`
- Test: `tests/test_owner_input_continuation.py`

**Steps:**

1. Test `needs_input` creates one WAITING_OWNER instance.
2. Test repeated event returns the existing anchor intent.
3. Test no downstream intents exist before owner receipt.
4. Test missing/invalid owner fields are refused without state advance.
5. Test valid receipt advances to INPUT_ACCEPTED and emits materialization intent.

### Task 5: Extend the real board adapter

**Files:**

- Create: `src/agentflow_hermes/board_adapter.py`
- Modify: `src/agentflow_hermes/graph_creator.py`
- Test: `tests/test_board_adapter.py`

**Steps:**

1. Add injectable CLI runner tests for create, block, subscribe, comment, and complete-anchor.
2. Use typed `ReturnEndpoint`; never parse channel names when numeric chat id is available.
3. Record each operation through outbox idempotency.
4. Reconcile duplicate CLI responses to the same task id.
5. Preserve old `RealKanbanGraphAdapter` as compatibility wrapper.

### Task 6: Board-aware event ingestion and continuation routing

**Files:**

- Create: `src/agentflow_hermes/board_events.py`
- Create: `src/agentflow_hermes/continuation_engine.py`
- Modify: `src/agentflow_hermes/loop_supervisor.py`
- Test: `tests/test_continuation_engine.py`

**Steps:**

1. Read structured run metadata from per-board Kanban DB.
2. Test board-scoped cursors with overlapping event ids.
3. Route GO to existing roadmap handler, code BLOCK to remediation handler, needs_input to owner handler.
4. Keep unknown outcomes non-mutating.
5. Verify one event produces one continuation receipt.

### Task 7: Add operator CLI

**Files:**

- Create: `src/agentflow_hermes/continuation_cli.py`
- Modify: `src/agentflow_hermes/cli.py`
- Test: `tests/test_continuation_cli.py`

**Commands:**

```bash
agentflow-hermes continuation ingest --board warroom-os --once
agentflow-hermes continuation list --state waiting_owner
agentflow-hermes continuation show <id>
agentflow-hermes continuation submit <id> --input owner-input.json
agentflow-hermes continuation retry <id>
agentflow-hermes continuation doctor
```

Tests must verify default commands do not leak raw paths/secrets and `submit` cannot invent omitted owner fields.

### Task 8: Real Warroom G4.21 vertical canary

**Files/config:**

- Modify: `/home/duckran/.hermes/agentflow/roadmaps/research-roadmap.yaml` or replace with unified board config
- Use: Warroom board `warroom-os`
- Source fixture: a controlled replay of `t_ab93a206` outcome metadata, not a historical event rewind

**Steps:**

1. Initialize warroom board cursor to current max event id.
2. Ingest one controlled fresh needs-input event.
3. Verify exactly one owner-anchor card on `warroom-os`.
4. Verify #research notify/active-wake edge.
5. Submit a controlled owner receipt fixture containing no credential/live approval.
6. Verify exactly one materialization/append task is created.
7. Verify downstream review is not created/runnable before materialization GO.
8. Complete controlled materialization and review through fake/sandbox artifact paths first.
9. Run one real local-artifact continuation only with the actual owner receipt.
10. Verify packet rerun is created only after review GO.
11. Duplicate ingest/submit/retry must create zero extra cards.

This canary performs no exchange call and grants no live order authority.

### Task 9: Consolidate old watchdogs

**Files:**

- Modify: `scripts/agentflow_auto_remediation_watchdog.py`
- Modify: roadmap watchdog entrypoint
- Deprecate legacy dependency: `/home/duckran/dev/agentflow/agentflow/kanban_auto_remediation.py`
- Test: integration fixture across `agentflow-hermes`, `warroom-os`, `oracle-lab`

**Steps:**

1. Point one watchdog at the board registry and continuation engine.
2. Keep old scripts as compatibility shims for one release.
3. Migrate board cursors, not global cursor values.
4. Verify M24B Oracle code remediation still works.
5. Verify existing GO roadmap promotion still works.
6. Remove split-store producer/consumer ambiguity from doctor output.

---

## 8. Test matrix

| Case | Expected |
| --- | --- |
| GO + roadmap marker | Existing next-slice handler; no owner anchor |
| BLOCK + code_fix | Existing fix/review handler |
| BLOCK + needs_input | One WAITING_OWNER anchor |
| NEED_MORE + needs_input | One WAITING_OWNER anchor |
| needs_input without contract | No mutation, explicit unknown-contract receipt |
| repeated source event | Existing continuation/anchor returned |
| owner receipt missing field | Refused, stays WAITING_OWNER |
| system attempts owner field | Refused |
| valid owner receipt | Materialization task created once |
| materialization BLOCK | Review/rerun not created |
| materialization GO | Review created |
| review BLOCK | Packet rerun not created; new remediation classification applies |
| review GO | Packet rerun created once |
| same event id on two boards | Both processed under separate board cursors |
| partial board mutation | Outbox remains retryable/reconcilable |
| store split detected | doctor BLOCK with both paths, no silent selection |
| raw secret/path in input | sanitized/refused before durable receipt |
| no structured metadata | explicit text fallback only; low confidence cannot append artifacts |

---

## 9. Migration/compatibility

### Phase A — additive

- Add outcome/contract/continuation tables and CLI.
- Existing GO and code remediation paths unchanged.
- Structured outcome optional.

### Phase B — first real needs-input vertical

- Warroom only.
- Real owner anchor and local artifact task creation.
- No live exchange side effect.

### Phase C — unify event ingestion

- Existing roadmap and Oracle remediation become handlers behind one board-aware engine.
- Board-specific cursors migrate.

### Phase D — retire split paths

- Legacy AgentFlow scanner becomes compatibility shim.
- One canonical store and one watchdog registry.

No big-bang rewrite is required, but the target abstraction must be introduced in Phase A; do not implement G4.21 as another special-case cron script.

---

## 10. Non-goals

- No Discord channel scraping.
- No LLM classifier in the autonomous event hot path.
- No automatic live trading approval or order execution.
- No automatic owner assertion/proof generation.
- No broad workflow language or arbitrary DAG engine in the first release.
- No Hermes-core patch unless the board adapter exposes a genuinely missing generic primitive.
- No forced migration of every existing task/history row.

---

## 11. Design verdict

**GO for implementation.**

The right fix is not a fifth special-case autopromoter. AgentFlow should become a typed continuation engine where `verdict` describes the result and `continuation_kind` describes the next durable state transition.

The most important architectural changes are:

1. structured `OutcomeEnvelope`;
2. first-class `WAITING_OWNER` continuation;
3. authority-typed `InputContract`;
4. lazy downstream materialization;
5. one canonical store/outbox;
6. board-scoped cursors;
7. GO/code-fix/needs-input handlers behind one router.

The first implementation must prove the architecture through the Warroom G4.21 loop:

```text
needs_input -> owner anchor -> owner receipt -> artifact/marker task -> review -> packet rerun
```

If that real loop works after a restart without a human manually reconstructing context or creating a task, AgentFlow has closed the gap.