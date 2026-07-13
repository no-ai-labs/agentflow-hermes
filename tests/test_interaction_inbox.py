from __future__ import annotations

from agentflow_hermes.continuation_store import ContinuationStore
from agentflow_hermes.continuations.owner_input import OwnerInputHandler
from agentflow_hermes.input_contract import ArtifactSpec, FieldAuthority, InputContract, InputField
from agentflow_hermes.interaction import (
    STATE_ANSWERED,
    STATE_ASKED,
    STATE_COLLECTING,
    InteractionInbox,
    classify_effort,
    compose_question,
)
from agentflow_hermes.outcome import ContinuationKind, OutcomeEnvelope, RequirementRef, Verdict
from agentflow_hermes.requirements import Requirement, RequirementKind


def _inbox(store: ContinuationStore, *, now: float = 1000.0, window: float = 10.0) -> tuple[InteractionInbox, dict]:
    clock_box = {"t": now}
    inbox = InteractionInbox(store=store, window_seconds=window, clock=lambda: clock_box["t"])
    return inbox, clock_box


def _req(name: str = "result_url", **overrides) -> Requirement:
    kwargs = dict(name=name, kind=RequirementKind.FACT, authority="owner", question=f"{name}?")
    kwargs.update(overrides)
    return Requirement(**kwargs)


def test_first_case_opens_in_collecting_state(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    inbox, _ = _inbox(store)

    case = inbox.open_or_batch_case(
        origin_endpoint="discord:#research",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="owner",
        continuation_id=1,
        unresolved_fields=(_req(),),
    )

    assert case.state == STATE_COLLECTING
    assert case.continuation_ids == (1,)
    assert case.question_count == 0
    assert len(case.unresolved_fields) == 1


def test_compatible_requests_batch_within_window(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    inbox, clock_box = _inbox(store)

    case_1 = inbox.open_or_batch_case(
        origin_endpoint="discord:#research",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="owner",
        continuation_id=1,
        unresolved_fields=(_req("result_url"),),
    )
    clock_box["t"] += 3  # still inside the 10s coalescing window
    case_2 = inbox.open_or_batch_case(
        origin_endpoint="discord:#research",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="owner",
        continuation_id=2,
        unresolved_fields=(_req("approval_id"),),
    )

    assert case_1.id == case_2.id
    assert set(case_2.continuation_ids) == {1, 2}
    assert {f.name for f in case_2.unresolved_fields} == {"result_url", "approval_id"}
    assert len(store.list_interaction_cases()) == 1


def test_requests_outside_window_get_separate_cases(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    inbox, clock_box = _inbox(store, window=10.0)

    case_1 = inbox.open_or_batch_case(
        origin_endpoint="discord:#research",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="owner",
        continuation_id=1,
        unresolved_fields=(_req(),),
    )
    clock_box["t"] += 20  # past the window
    case_2 = inbox.open_or_batch_case(
        origin_endpoint="discord:#research",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="owner",
        continuation_id=2,
        unresolved_fields=(_req(),),
    )

    assert case_1.id != case_2.id
    assert len(store.list_interaction_cases()) == 2


def test_conflicting_authority_class_does_not_batch(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    inbox, _ = _inbox(store)

    case_1 = inbox.open_or_batch_case(
        origin_endpoint="discord:#research",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="owner",
        continuation_id=1,
        unresolved_fields=(_req(),),
    )
    case_2 = inbox.open_or_batch_case(
        origin_endpoint="discord:#research",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="live-money-approval",
        continuation_id=2,
        unresolved_fields=(_req("payout_confirm"),),
    )

    assert case_1.id != case_2.id


def test_different_endpoint_does_not_batch(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    inbox, _ = _inbox(store)

    case_1 = inbox.open_or_batch_case(
        origin_endpoint="discord:#research",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="owner",
        continuation_id=1,
        unresolved_fields=(_req(),),
    )
    case_2 = inbox.open_or_batch_case(
        origin_endpoint="discord:#shaman",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="owner",
        continuation_id=2,
        unresolved_fields=(_req(),),
    )

    assert case_1.id != case_2.id


def test_mark_asked_increments_question_count_and_transitions_state(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    inbox, _ = _inbox(store)
    case = inbox.open_or_batch_case(
        origin_endpoint="discord:#research",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="owner",
        continuation_id=1,
        unresolved_fields=(_req(),),
    )

    asked = inbox.mark_asked(case.id)
    assert asked.state == STATE_ASKED
    assert asked.question_count == 1
    assert asked.asked_at is not None

    clarify = inbox.mark_needs_clarification(case.id)
    asked_again = inbox.mark_asked(clarify.id)
    assert asked_again.question_count == 2


def test_classify_effort_h0_h1_h2_h3():
    assert classify_effort(0) == "H0"
    assert classify_effort(1) == "H1"
    assert classify_effort(2) == "H2"
    assert classify_effort(3) == "H3+"
    assert classify_effort(9) == "H3+"


def test_compose_question_answers_four_things_no_task_ids_or_contract_ref(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    inbox, _ = _inbox(store)
    case = inbox.open_or_batch_case(
        origin_endpoint="discord:#research",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="owner",
        continuation_id=1,
        unresolved_fields=(_req("result_url", question="what's the verified result URL?"),),
    )

    question = compose_question(
        case,
        resolved_summary=("4 other verification values reused from existing artifacts",),
        resume_summary="I'll resume the review automatically once you reply.",
    )

    assert "Blocked on" in question
    assert "Already resolved automatically" in question
    assert "Reply with" in question
    assert "resume" in question.lower()
    assert "contract_ref" not in question
    assert "receipt:" not in question
    assert "task:" not in question
    assert str(case.continuation_ids[0]) not in question


def test_compose_question_numbers_multiple_unresolved_fields(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    inbox, _ = _inbox(store)
    case = inbox.open_or_batch_case(
        origin_endpoint="discord:#research",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="owner",
        continuation_id=1,
        unresolved_fields=(_req("result_url"), _req("approval_id")),
    )

    question = compose_question(case)
    assert "1)" in question
    assert "2)" in question


def test_record_inbound_reply_never_stores_raw_text(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    inbox, _ = _inbox(store)
    case = inbox.open_or_batch_case(
        origin_endpoint="discord:#research",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="owner",
        continuation_id=1,
        unresolved_fields=(_req(),),
    )

    secret_text = "https://example.com/result super-secret-token-abcdef"
    receipt = inbox.record_inbound_reply(
        case.id, message_ref="discord:msg:123", raw_text=secret_text, compile_result={"result_url": "https://example.com/result"}
    )

    assert "content_sha256" in receipt
    assert receipt["content_sha256"] != secret_text
    assert secret_text not in receipt["content_sha256"]
    stored = store.list_inbound_reply_receipts(case.id)
    assert len(stored) == 1
    assert secret_text not in str(stored[0])
    assert stored[0]["compile_result"] == {"result_url": "https://example.com/result"}


def test_apply_fields_satisfies_multiple_continuations_from_one_case(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    inbox, _ = _inbox(store)
    for cid in (1, 2):
        store.create_instance(
            board="warroom-os",
            source_task_id=f"t_{cid}",
            source_event_id=f"ev_{cid}",
            source_graph_id="g_1",
            contract_ref="generic.owner-input.v1",
        )
    case = inbox.open_or_batch_case(
        origin_endpoint="discord:#research",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="owner",
        continuation_id=1,
        unresolved_fields=(_req("result_url"),),
    )
    inbox.open_or_batch_case(
        origin_endpoint="discord:#research",
        owner_ref="operator-main",
        project_scope="warroom-os",
        authority_class="owner",
        continuation_id=2,
        unresolved_fields=(_req("approval_id"),),
    )

    updated = inbox.apply_fields(
        case.id,
        fields_by_continuation={1: {"result_url": "https://x"}, 2: {"approval_id": "recv_1"}},
    )

    assert updated.state == STATE_ANSWERED
    assert store.list_requirement_satisfactions(1)[0]["value"] == "https://x"
    assert store.list_requirement_satisfactions(2)[0]["value"] == "recv_1"


class FakeAdapter:
    def __init__(self) -> None:
        self.created_tasks: list[dict] = []

    def create_task(self, intent: dict) -> dict:
        task_id = f"task:{len(self.created_tasks) + 1}"
        self.created_tasks.append({**intent, "task_id": task_id})
        return {"success": True, "task_id": task_id}

    def subscribe(self, task_id: str, endpoint: str) -> dict:
        return {"success": True}

    def complete_owner_anchor(self, task_id: str, *, receipt_ref: str) -> dict:
        return {"success": True}


def _contract() -> InputContract:
    return InputContract(
        contract_ref="generic.owner-input.v1",
        version=1,
        owner_role="board-owner",
        fields=(InputField(name="result_url", value_type="text", authority=FieldAuthority.OWNER),),
        artifacts=(ArtifactSpec(artifact_id="evidence", template_path="t.json", final_path="f.json"),),
        resume_transition="generic.owner-input.resume",
    )


def _outcome(**overrides) -> OutcomeEnvelope:
    kwargs = dict(
        schema_version=1,
        event_id="ev_1",
        board="warroom-os",
        source_task_id="t_1",
        source_graph_id="g_1",
        verdict=Verdict.BLOCK,
        continuation_kind=ContinuationKind.NEEDS_INPUT,
        contract_ref="generic.owner-input.v1",
        origin_ref="discord:#research",
        return_to_ref="discord:#research",
        requirements=(RequirementRef(name="result_url", authority="owner"),),
    )
    kwargs.update(overrides)
    return OutcomeEnvelope(**kwargs)


def test_owner_input_handler_plan_without_inbox_is_unchanged(tmp_path):
    """Backward-compat guard: omitting interaction_inbox must produce the
    exact same anchor intent shape as before this commit."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeAdapter()
    handler = OwnerInputHandler()

    plan = handler.plan(_outcome(), store=store, adapter=adapter, contract=_contract())

    assert "agentflow_interaction" not in plan.step_intents[0]
    assert store.list_interaction_cases() == []


def test_owner_input_handler_plan_with_inbox_creates_case_and_correlation_metadata(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeAdapter()
    handler = OwnerInputHandler()
    inbox, _ = _inbox(store)

    plan = handler.plan(
        _outcome(),
        store=store,
        adapter=adapter,
        contract=_contract(),
        interaction_inbox=inbox,
        unresolved_requirements=(_req("result_url"),),
        owner_ref="operator-main",
        project_scope="warroom-os",
    )

    intent = plan.step_intents[0]
    assert "agentflow_interaction" in intent
    correlation = intent["agentflow_interaction"]
    assert correlation["origin_endpoint"] == "discord:#research"
    assert correlation["reply_mode"] == "natural_language"
    assert plan.instance_id in correlation["continuation_ids"]

    cases = store.list_interaction_cases()
    assert len(cases) == 1
    assert cases[0]["state"] == STATE_COLLECTING
