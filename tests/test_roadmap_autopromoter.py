from __future__ import annotations

from agentflow_hermes.graph_creator import FakeKanbanGraphAdapter
from agentflow_hermes.roadmap import (
    InMemoryRoadmapPromotionLedger,
    RoadmapPromotionPolicy,
    RoadmapTransition,
    RoadmapTransitionRegistry,
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
