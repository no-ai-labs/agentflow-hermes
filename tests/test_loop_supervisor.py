from __future__ import annotations

import json

from agentflow_hermes.graph_creator import FakeKanbanGraphAdapter
from agentflow_hermes.loop_supervisor import InMemoryLoopLedger, LoopEvent, LoopPolicy, evaluate_loop_event


SAFE_POLICY = LoopPolicy(
    active_mode="request_only",
    allowlisted_blockers=("stale_inline_route", "missing_subscription", "stale_final_fanin"),
    expected_origin="discord:#hermes-main",
    expected_return_to="discord:#hermes-main",
    cooldown_seconds=900,
)


def _event(**kwargs):
    defaults = {
        "event_id": "evt-1",
        "source_graph_id": "graph-1",
        "source_task_id": "t_source",
        "origin": "discord:#hermes-main",
        "return_to": "discord:#hermes-main",
        "subscription_status": "verified",
        "policy_resolution_ref": "policy:model.implementation_default@v1",
        "occurred_at": 1000.0,
    }
    defaults.update(kwargs)
    return LoopEvent(**defaults)


def test_loop_go_terminal_stops_stabilizes_no_graph_call():
    ledger = InMemoryLoopLedger()
    calls = []

    def creator(*args, **kwargs):
        calls.append((args, kwargs))
        return {"success": True, "candidates": [], "mutations": []}

    decision = evaluate_loop_event(_event(verdict="GO", summary="Verdict: GO"), ledger, SAFE_POLICY, graph_creator=creator)

    assert decision.action == "stabilize"
    assert decision.reason == "go_terminal"
    assert decision.mutations == ()
    assert calls == []
    assert ledger.has_event("evt-1") is True


def test_loop_need_more_stops_escalates_no_auto_create():
    ledger = InMemoryLoopLedger()
    adapter = FakeKanbanGraphAdapter()
    decision = evaluate_loop_event(
        _event(verdict="NEED_MORE", summary="Verdict: NEED_MORE — operator choice needed"),
        ledger,
        SAFE_POLICY,
        adapter=adapter,
    )

    assert decision.action == "escalate"
    assert decision.reason == "needs_input"
    assert len(adapter.create_calls) == 0


def test_loop_first_allowlisted_block_returns_request_only_proposal_no_mutations():
    ledger = InMemoryLoopLedger()
    adapter = FakeKanbanGraphAdapter()
    decision = evaluate_loop_event(
        _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route old route", blocker_class="stale_inline_route"),
        ledger,
        SAFE_POLICY,
        adapter=adapter,
    )

    assert decision.action == "propose"
    assert decision.reason == "bounded_remediation"
    assert decision.mutations == ()
    assert len(adapter.create_calls) == 0
    assert decision.candidates
    assert all(c.get("mutations") == [] for c in decision.candidates if c.get("action") != "noop")


def test_loop_repeated_same_blocker_stops_at_max_same_blocker():
    ledger = InMemoryLoopLedger()
    first = evaluate_loop_event(
        _event(event_id="evt-a", verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route"),
        ledger,
        SAFE_POLICY,
    )
    assert first.action == "propose"

    second = evaluate_loop_event(
        _event(event_id="evt-b", verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route", occurred_at=3000.0),
        ledger,
        SAFE_POLICY,
    )

    assert second.action == "escalate"
    assert second.reason == "max_same_blocker"


def test_loop_max_rounds_stops_before_graph_creator():
    ledger = InMemoryLoopLedger()
    decision = evaluate_loop_event(
        _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route", round_no=2),
        ledger,
        SAFE_POLICY,
    )

    assert decision.action == "escalate"
    assert decision.reason == "max_rounds"


def test_loop_cooldown_suppresses_repeated_create_before_same_blocker_cap_when_cap_allows():
    policy = LoopPolicy(
        active_mode="request_only",
        allowlisted_blockers=("stale_inline_route",),
        expected_origin="discord:#hermes-main",
        expected_return_to="discord:#hermes-main",
        max_same_blocker=3,
        cooldown_seconds=900,
    )
    ledger = InMemoryLoopLedger()
    first = evaluate_loop_event(
        _event(event_id="evt-c1", verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route", occurred_at=1000.0),
        ledger,
        policy,
    )
    assert first.action == "propose"

    second = evaluate_loop_event(
        _event(event_id="evt-c2", verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route", occurred_at=1200.0),
        ledger,
        policy,
    )

    assert second.action == "noop"
    assert second.reason == "cooldown"
    assert second.metadata["next_eligible_at"] == 1900.0


def test_loop_duplicate_event_id_noops_without_second_receipt():
    ledger = InMemoryLoopLedger()
    first = evaluate_loop_event(_event(verdict="GO", summary="Verdict: GO"), ledger, SAFE_POLICY)
    before = len(ledger.receipts)
    second = evaluate_loop_event(_event(verdict="GO", summary="Verdict: GO"), ledger, SAFE_POLICY)

    assert first.action == "stabilize"
    assert second.action == "noop"
    assert second.reason == "duplicate_event"
    assert len(ledger.receipts) == before


def test_loop_stale_final_v1_later_remediation_go_creates_final_v2_once():
    ledger = InMemoryLoopLedger()
    event = _event(
        event_id="evt-final-go",
        event_type="remediation_review_go",
        source_graph_id="graph-final",
        source_final_id="t_final_v1",
        remediation_review_id="t_review_go",
        old_final_card={"id": "t_final_v1", "status": "blocked"},
        remediation_review_card={"id": "t_review_go", "body": "Verdict: GO — remediation passed."},
    )
    first = evaluate_loop_event(event, ledger, SAFE_POLICY)
    second = evaluate_loop_event(_event(
        event_id="evt-final-go-2",
        event_type="remediation_review_go",
        source_graph_id="graph-final",
        source_final_id="t_final_v1",
        remediation_review_id="t_review_go",
        old_final_card={"id": "t_final_v1", "status": "blocked"},
        remediation_review_card={"id": "t_review_go", "body": "Verdict: GO — remediation passed."},
        occurred_at=2000.0,
    ), ledger, SAFE_POLICY)

    assert first.action == "supersede"
    assert first.reason == "final_vn_proposal"
    assert first.candidate is not None
    assert first.candidate["kind"] == "final-v2"
    assert first.mutations == ()
    assert second.action == "noop"
    assert second.reason == "existing_supersession"


def test_loop_unknown_blocker_noops_escalates():
    ledger = InMemoryLoopLedger()
    decision = evaluate_loop_event(
        _event(verdict="BLOCK", summary="Verdict: BLOCK — mystery blocker", blocker_class="mystery_blocker"),
        ledger,
        SAFE_POLICY,
    )

    assert decision.action == "escalate"
    assert decision.reason == "blocker_not_allowlisted"
    assert decision.candidates == ()


def test_loop_origin_return_to_mismatch_or_missing_subscription_escalates():
    ledger = InMemoryLoopLedger()
    foreign = evaluate_loop_event(
        _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route", origin="discord:#other"),
        ledger,
        SAFE_POLICY,
    )
    missing_sub = evaluate_loop_event(
        _event(event_id="evt-sub", verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route", subscription_status="missing"),
        InMemoryLoopLedger(),
        SAFE_POLICY,
    )

    assert foreign.action == "escalate"
    assert foreign.reason == "foreign_origin"
    assert missing_sub.action == "escalate"
    assert missing_sub.reason == "subscription_unverified"


def test_loop_kill_switch_and_malformed_policy_fail_closed():
    adapter = FakeKanbanGraphAdapter()
    kill = evaluate_loop_event(
        _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route"),
        InMemoryLoopLedger(),
        LoopPolicy(kill_switch=True, allowlisted_blockers=("stale_inline_route",)),
        adapter=adapter,
    )
    malformed = evaluate_loop_event(
        _event(event_id="evt-mal", verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route", blocker_class="stale_inline_route"),
        InMemoryLoopLedger(),
        {"active_mode": "apply"},  # type: ignore[arg-type]
        adapter=adapter,
    )

    assert kill.action == "escalate"
    assert kill.reason == "kill_switch"
    assert malformed.action == "escalate"
    assert malformed.reason == "malformed_policy"
    assert len(adapter.create_calls) == 0


def test_loop_apply_mode_inherits_graph_creator_failed_adapter_attempt_budget():
    class FailingAdapter(FakeKanbanGraphAdapter):
        def create_graph(self, intent):
            self.create_calls.append(intent)
            return {"success": False, "error": "transient_adapter_failure", "mutations": []}

    adapter = FailingAdapter()
    policy = LoopPolicy(
        active_mode="apply",
        allowlisted_blockers=("stale_inline_route",),
        expected_origin="discord:#hermes-main",
        expected_return_to="discord:#hermes-main",
        max_auto_creates_per_run=1,
        max_tasks_per_graph=3,
    )
    decision = evaluate_loop_event(
        _event(verdict="BLOCK", summary="Verdict: BLOCK — stale_inline_route old route", blocker_class="stale_inline_route"),
        InMemoryLoopLedger(),
        policy,
        adapter=adapter,
    )

    assert decision.action == "apply"
    assert len(adapter.create_calls) == 1
    capped = [c for c in decision.candidates if c.get("reason") == "max_auto_creates_per_run"]
    assert len(capped) == 2
    assert all(c.get("action") == "noop" for c in capped)


def test_loop_decision_receipts_do_not_persist_private_paths_or_secrets():
    ledger = InMemoryLoopLedger()
    decision = evaluate_loop_event(
        _event(
            verdict="BLOCK",
            summary="Verdict: BLOCK — stale_inline_route /home/alice/private TOKEN=abc123 claude-openrouter-opus",
            source_task_id="/home/alice/private/TOKEN=abc123",
            blocker_class="stale_inline_route",
        ),
        ledger,
        SAFE_POLICY,
    )
    blob = json.dumps({"decision": decision.as_dict(), "receipts": ledger.receipts})

    assert "/home/alice" not in blob
    assert "TOKEN=abc123" not in blob
    assert "private/TOKEN" not in blob
    assert "claude-openrouter-opus" not in blob
