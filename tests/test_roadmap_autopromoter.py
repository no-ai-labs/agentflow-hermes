from __future__ import annotations

from typing import Any

from agentflow_hermes.graph_creator import FakeKanbanGraphAdapter, GraphIntentCandidate
from agentflow_hermes.roadmap import (
    InMemoryRoadmapApplyLedger,
    InMemoryRoadmapPromotionLedger,
    RoadmapPromotionPolicy,
    RoadmapTransition,
    RoadmapTransitionRegistry,
    apply_roadmap_promotion,
    propose_roadmap_promotion,
)


def _registry() -> RoadmapTransitionRegistry:
    return RoadmapTransitionRegistry(
        version="test-v1",
        source_ref="test-registry",
        transitions={
            "m14->m15.impl_review_fanin": RoadmapTransition(
                transition_id="m14->m15.impl_review_fanin",
                roadmap_id="hermes.live-migration",
                from_slice="m14",
                to_slice="m15",
                slice_template=("impl", "review", "fanin"),
                policy_refs=("design_opus", "implementation_default"),
                max_chain_depth=2,
                version="template-v1",
            )
        },
    )


def _policy(**kwargs) -> RoadmapPromotionPolicy:
    defaults = {
        "auto_continue": True,
        "allowlisted_transitions": ("m14->m15.impl_review_fanin",),
        "trusted_assignees": ("ccreviewer",),
        "expected_origin": "Discord Devhub / #hermes-main",
        "expected_return_to": "Discord Devhub / #hermes-main",
        "promote_cooldown_seconds": 900,
    }
    defaults.update(kwargs)
    return RoadmapPromotionPolicy(**defaults)


def _summary(**overrides) -> str:
    verdict = overrides.get("verdict", "GO")
    transition = overrides.get("transition", "m14->m15.impl_review_fanin")
    next_slice = overrides.get("next_slice", "m15")
    review = overrides.get("review", "verified")
    ack = overrides.get("ack", "verified")
    auto = overrides.get("auto", "true")
    origin = overrides.get("origin", "Discord Devhub / #hermes-main")
    return_to = overrides.get("return_to", "Discord Devhub / #hermes-main")
    return "\n".join([
        f"Verdict: {verdict}",
        f"Origin/return_to: {origin}",
        f"Return-To: {return_to}",
        f"Auto-Continue: {auto}",
        f"Roadmap-Transition: {transition}",
        f"Next-Slice: {next_slice}",
        f"Review-Edge: {review}",
        f"ACK-Edge: {ack}",
        "Parent-GO: verified",
    ])


def _propose(summary: str, *, ledger=None, policy=None, adapter=None, **kwargs):
    return propose_roadmap_promotion(
        summary,
        event_id=kwargs.pop("event_id", "evt-1"),
        source_final_ref=kwargs.pop("source_final_ref", "t_final"),
        source_assignee=kwargs.pop("source_assignee", "ccreviewer"),
        origin=kwargs.pop("origin", "Discord Devhub / #hermes-main"),
        return_to=kwargs.pop("return_to", "Discord Devhub / #hermes-main"),
        subscription_status=kwargs.pop("subscription_status", "verified"),
        policy_resolution_ref=kwargs.pop("policy_resolution_ref", "policy:model.implementation_default@v1"),
        chain_depth=kwargs.pop("chain_depth", 0),
        occurred_at=kwargs.pop("occurred_at", 1000.0),
        registry=_registry(),
        ledger=ledger or InMemoryRoadmapPromotionLedger(),
        policy=policy or _policy(),
        adapter=adapter,
        **kwargs,
    )


def _apply_policy(**kwargs) -> RoadmapPromotionPolicy:
    defaults = {
        "apply_enabled": True,
        "impl_assignee": "impl-agent",
        "review_assignee": "ccreviewer",
        "ack_trigger_agent": "ack-agent",
    }
    defaults.update(kwargs)
    return _policy(**defaults)


def _apply(summary: str, *, ledger=None, apply_ledger=None, policy=None, adapter=None, **kwargs):
    return apply_roadmap_promotion(
        summary,
        event_id=kwargs.pop("event_id", "evt-1"),
        source_final_ref=kwargs.pop("source_final_ref", "t_final"),
        source_assignee=kwargs.pop("source_assignee", "ccreviewer"),
        origin=kwargs.pop("origin", "Discord Devhub / #hermes-main"),
        return_to=kwargs.pop("return_to", "Discord Devhub / #hermes-main"),
        subscription_status=kwargs.pop("subscription_status", "verified"),
        policy_resolution_ref=kwargs.pop("policy_resolution_ref", "policy:model.implementation_default@v1"),
        chain_depth=kwargs.pop("chain_depth", 0),
        occurred_at=kwargs.pop("occurred_at", 1000.0),
        registry=_registry(),
        ledger=ledger or InMemoryRoadmapPromotionLedger(),
        apply_ledger=apply_ledger or InMemoryRoadmapApplyLedger(),
        policy=policy or _apply_policy(),
        adapter=adapter,
        **kwargs,
    )


class _PartialFailureAdapter:
    """Adapter that creates the first candidate then fails every call after."""

    def __init__(self, *, fail_after: int = 1) -> None:
        self.fail_after = fail_after
        self.create_calls: list[GraphIntentCandidate] = []

    def create_graph(self, intent: GraphIntentCandidate) -> dict[str, Any]:
        self.create_calls.append(intent)
        if len(self.create_calls) > self.fail_after:
            return {"success": False, "error": "adapter_partial_failure", "mutations": []}
        task_id = "task:" + intent.idempotency_key
        return {"success": True, "action": "partial", "task_id": task_id, "mutations": []}


def test_final_go_auto_continue_false_is_refused_no_candidates():
    policy_disabled = _propose(_summary(), policy=_policy(auto_continue=False))
    directive_disabled = _propose(_summary(auto="false"), event_id="evt-auto-false")

    for result in (policy_disabled, directive_disabled):
        assert result["action"] == "noop"
        assert result["reason"] == "autopromote_disabled"
        assert result["candidates"] == []
        assert result["mutations"] == []


def test_block_need_more_unknown_verdicts_are_refused():
    for verdict in ("BLOCK", "NEED_MORE", "UNKNOWN"):
        result = _propose(_summary(verdict=verdict), event_id=f"evt-{verdict}")
        assert result["action"] == "refuse"
        assert result["reason"] == "not_go"
        assert result["candidates"] == []


def test_missing_next_slice_or_template_is_refused():
    missing_transition = _summary().replace("Roadmap-Transition: m14->m15.impl_review_fanin\n", "")
    missing_next = _summary().replace("Next-Slice: m15\n", "")

    assert _propose(missing_transition)["reason"] == "missing_next_slice"
    assert _propose(missing_next, event_id="evt-2")["reason"] == "missing_next_slice"


def test_non_allowlisted_transition_is_refused():
    result = _propose(_summary(transition="m14->m16.freeform"))

    assert result["action"] == "refuse"
    assert result["reason"] == "unknown_transition"
    assert result["candidates"] == []


def test_missing_review_origin_or_ack_evidence_is_refused():
    assert _propose(_summary(review="missing"))["reason"] == "missing_review_edge"
    assert _propose(_summary(ack="missing"), event_id="evt-ack")["reason"] == "missing_ack_edge"
    assert _propose(_summary(origin="discord:#other"), event_id="evt-origin")["reason"] == "foreign_origin"


def test_stale_inline_policy_conflict_is_refused():
    result = _propose(_summary() + "\nRoute: claude-openrouter-opus via Kimi/Moonshot")

    assert result["action"] == "refuse"
    assert result["reason"] == "stale_inline_route"
    assert result["candidates"] == []


def test_valid_allowlisted_transition_returns_graph_json_only_no_board_write():
    adapter = FakeKanbanGraphAdapter()
    result = _propose(_summary(), adapter=adapter)

    assert result["success"] is True
    assert result["action"] == "propose"
    assert result["request_only"] is True
    assert result["mutations"] == []
    assert result["adapter_attempts"] == 0
    assert len(adapter.create_calls) == 0
    assert [c["kind"] for c in result["candidates"]] == ["impl", "review", "fanin"]
    assert all(c["metadata"]["dry_run_only"] is True for c in result["candidates"])
    assert all(c["policy_refs"] == ["design_opus", "implementation_default"] for c in result["candidates"])


def test_duplicate_event_idempotency_returns_prior_without_duplicate_proposal():
    ledger = InMemoryRoadmapPromotionLedger()
    first = _propose(_summary(), ledger=ledger)
    second = _propose(_summary(), ledger=ledger)

    assert first["action"] == "propose"
    assert second["action"] == "noop"
    assert second["reason"] == "duplicate_event"
    assert len(ledger.receipts) == 1


def test_max_chain_depth_repeat_and_cooldown_are_refused():
    depth = _propose(_summary(), chain_depth=2)
    assert depth["reason"] == "max_chain_depth"

    repeat_ledger = InMemoryRoadmapPromotionLedger()
    _propose(_summary(), ledger=repeat_ledger, occurred_at=1000.0)
    repeat = _propose(_summary(), ledger=repeat_ledger, event_id="evt-repeat", occurred_at=3000.0, policy=_policy(max_promotions_per_roadmap=1))
    assert repeat["reason"] == "max_promotions_per_roadmap"

    cooldown_ledger = InMemoryRoadmapPromotionLedger()
    _propose(_summary(), ledger=cooldown_ledger, occurred_at=1000.0)
    cooldown = _propose(_summary(), ledger=cooldown_ledger, event_id="evt-cooldown", occurred_at=1200.0)
    assert cooldown["action"] == "noop"
    assert cooldown["reason"] == "cooldown"


def test_receipt_sanitizes_raw_paths_and_secrets():
    result = _propose(
        _summary(),
        event_id="evt-secret",
        source_final_ref="/home/duckran/private/final API_KEY=abc123",
        origin="Discord Devhub / #hermes-main",
        return_to="Discord Devhub / #hermes-main",
    )
    receipt_text = str(result["receipt"])

    assert "/home/duckran" not in receipt_text
    assert "API_KEY" not in receipt_text
    assert "ref:sha256:" in receipt_text


def test_failed_partial_apply_does_not_poison_shared_promotion_ledger():
    """A failed adapter write must not commit a receipt to the *real* promotion
    ledger. Regression for the M14b apply path calling propose_roadmap_promotion
    with the real ledger before adapter writes succeed (poisons same-event
    retries as duplicate_event and pollutes depth/repeat counters)."""
    ledger = InMemoryRoadmapPromotionLedger()
    apply_ledger = InMemoryRoadmapApplyLedger()
    adapter = _PartialFailureAdapter(fail_after=1)

    result = _apply(_summary(), ledger=ledger, apply_ledger=apply_ledger, adapter=adapter)

    assert result["success"] is False
    assert result["applied"] is False
    assert result["reason"] == "adapter_create_failed"
    # The real, shared promotion ledger must remain untouched by the failed attempt.
    assert ledger.receipts == []
    key = result["idempotency_key"]
    assert not apply_ledger.has(key)


def test_same_event_retry_after_partial_failure_succeeds_with_valid_adapter():
    ledger = InMemoryRoadmapPromotionLedger()
    apply_ledger = InMemoryRoadmapApplyLedger()
    failing_adapter = _PartialFailureAdapter(fail_after=1)

    failed = _apply(_summary(), ledger=ledger, apply_ledger=apply_ledger, adapter=failing_adapter)
    assert failed["applied"] is False

    working_adapter = FakeKanbanGraphAdapter()
    retry = _apply(_summary(), ledger=ledger, apply_ledger=apply_ledger, adapter=working_adapter)

    assert retry["success"] is True
    assert retry["applied"] is True
    assert retry["reason"] == "roadmap_graph_applied"
    assert len(retry["created_task_ids"]) == 3
    assert len(working_adapter.create_calls) == 3
    # Exactly one promotion receipt committed for the eventually-successful apply.
    assert len(ledger.receipts) == 1


def test_failed_event_then_different_event_does_not_advance_depth_or_repeat_counters():
    ledger = InMemoryRoadmapPromotionLedger()
    apply_ledger = InMemoryRoadmapApplyLedger()
    failing_adapter = _PartialFailureAdapter(fail_after=0)

    failed = _apply(
        _summary(),
        ledger=ledger,
        apply_ledger=apply_ledger,
        adapter=failing_adapter,
        event_id="evt-fail",
        source_final_ref="t_final_fail",
    )
    assert failed["applied"] is False
    assert ledger.receipts == []
    assert ledger.current_chain_depth("hermes.live-migration") == 0
    assert ledger.count_promotions("hermes.live-migration") == 0

    working_adapter = FakeKanbanGraphAdapter()
    different = _apply(
        _summary(),
        ledger=ledger,
        apply_ledger=InMemoryRoadmapApplyLedger(),
        adapter=working_adapter,
        event_id="evt-other",
        source_final_ref="t_final_other",
    )
    assert different["applied"] is True
    # The other event's promotion is depth 1, unaffected by the earlier failure.
    assert different["chain_depth"] == 1
    assert ledger.current_chain_depth("hermes.live-migration") == 1
    assert ledger.count_promotions("hermes.live-migration") == 1


def test_valid_apply_records_both_ledgers_and_duplicate_skips_adapter():
    ledger = InMemoryRoadmapPromotionLedger()
    apply_ledger = InMemoryRoadmapApplyLedger()
    adapter = FakeKanbanGraphAdapter()

    first = _apply(_summary(), ledger=ledger, apply_ledger=apply_ledger, adapter=adapter)
    assert first["applied"] is True
    assert len(ledger.receipts) == 1
    key = first["idempotency_key"]
    assert apply_ledger.has(key)

    second = _apply(_summary(), ledger=ledger, apply_ledger=apply_ledger, adapter=adapter)
    assert second["applied"] is False
    assert second["duplicate"] is True
    assert second["created_task_ids"] == first["created_task_ids"]
    # No second board write, and no extra promotion receipt.
    assert len(adapter.create_calls) == 3
    assert len(ledger.receipts) == 1


def test_apply_request_only_behavior_unchanged_by_shadow_ledger():
    ledger = InMemoryRoadmapPromotionLedger()
    adapter = FakeKanbanGraphAdapter()

    result = _apply(_summary(), ledger=ledger, policy=_apply_policy(apply_enabled=False), adapter=adapter)

    assert result["applied"] is False
    assert result["apply_enabled"] is False
    assert result["action"] == "propose"
    assert len(adapter.create_calls) == 0
    # A non-applying proposal (apply disabled) is still recorded, matching the
    # pre-existing request-only recording semantics.
    assert len(ledger.receipts) == 1
