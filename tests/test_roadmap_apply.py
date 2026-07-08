from __future__ import annotations

from agentflow_hermes.graph_creator import FakeKanbanGraphAdapter
from agentflow_hermes.roadmap import (
    InMemoryRoadmapApplyLedger,
    InMemoryRoadmapPromotionLedger,
    RoadmapPromotionPolicy,
    RoadmapTransition,
    RoadmapTransitionRegistry,
    apply_roadmap_promotion,
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
        "apply_enabled": True,
        "impl_assignee": "impl-agent",
        "review_assignee": "ccreviewer",
        "ack_trigger_agent": "ack-agent",
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
        policy=policy or _policy(),
        adapter=adapter,
        **kwargs,
    )


def test_apply_disabled_by_default_returns_proposal_only_no_mutations():
    adapter = FakeKanbanGraphAdapter()
    result = _apply(_summary(), policy=_policy(apply_enabled=False), adapter=adapter)

    assert result["applied"] is False
    assert result["apply_enabled"] is False
    assert result["mutations"] == []
    assert result["created_task_ids"] == []
    assert len(adapter.create_calls) == 0
    # Underlying proposal is still returned for observability.
    assert result["action"] == "propose"


def test_valid_armed_event_creates_expected_task_graph():
    adapter = FakeKanbanGraphAdapter()
    result = _apply(_summary(), adapter=adapter)

    assert result["success"] is True
    assert result["action"] == "apply"
    assert result["applied"] is True
    assert result["dry_run"] is False
    assert [t["kind"] for t in result["tasks"]] == ["impl", "review", "fanin"]
    assert len(result["created_task_ids"]) == 3
    assert all(tid for tid in result["created_task_ids"])
    assert len(adapter.create_calls) == 3
    assert len(adapter.tasks) == 3

    tasks = {t["kind"]: t for t in result["tasks"]}
    # Explicit assignees.
    assert tasks["impl"]["assignee"] == "impl-agent"
    assert tasks["review"]["assignee"] == "ccreviewer"
    assert tasks["fanin"]["assignee"] == "ack-agent"
    # Parent links chain impl -> review -> fanin.
    assert tasks["impl"]["parent_task_id"] == ""
    assert tasks["review"]["parent_task_id"] == tasks["impl"]["task_id"]
    assert tasks["fanin"]["parent_task_id"] == tasks["review"]["task_id"]
    # ack-trigger-agent on the fan-in/ack task.
    assert tasks["fanin"]["ack_trigger_agent"] == "ack-agent"
    # Acceptance criteria present and origin/return_to carried through.
    assert all(t["acceptance_criteria"] for t in result["tasks"])
    assert all(t["origin"] and t["return_to"] for t in result["tasks"])
    # Machine-readable receipt.
    assert result["receipt"]["created_task_ids"] == result["created_task_ids"]
    assert result["receipt"]["template_id"] == "template-v1"
    assert result["idempotency_key"]


def test_duplicate_event_returns_existing_ids_no_duplicate_creates():
    adapter = FakeKanbanGraphAdapter()
    apply_ledger = InMemoryRoadmapApplyLedger()
    ledger = InMemoryRoadmapPromotionLedger()

    first = _apply(_summary(), adapter=adapter, apply_ledger=apply_ledger, ledger=ledger)
    second = _apply(_summary(), adapter=adapter, apply_ledger=apply_ledger, ledger=ledger)

    assert first["applied"] is True
    assert second["applied"] is False
    assert second["duplicate"] is True
    assert second["reason"] == "duplicate_graph"
    assert second["created_task_ids"] == first["created_task_ids"]
    assert second["mutations"] == []
    # No second board write.
    assert len(adapter.create_calls) == 3
    assert len(adapter.tasks) == 3


def test_duplicate_source_different_event_id_is_deduped():
    adapter = FakeKanbanGraphAdapter()
    apply_ledger = InMemoryRoadmapApplyLedger()

    first = _apply(_summary(), adapter=adapter, apply_ledger=apply_ledger, event_id="evt-a")
    # Same source_final_ref/template, different event id -> same idempotency key.
    second = _apply(_summary(), adapter=adapter, apply_ledger=apply_ledger, event_id="evt-b")

    assert first["applied"] is True
    assert second["applied"] is False
    assert second["duplicate"] is True
    assert second["created_task_ids"] == first["created_task_ids"]
    assert len(adapter.create_calls) == 3


def test_missing_gates_refuse_and_create_nothing():
    for overrides, event_id in (
        ({"review": "missing"}, "evt-review"),
        ({"ack": "missing"}, "evt-ack"),
        ({"origin": "discord:#other"}, "evt-origin"),
    ):
        adapter = FakeKanbanGraphAdapter()
        result = _apply(_summary(**overrides), adapter=adapter, event_id=event_id)
        assert result["applied"] is False
        assert result["mutations"] == []
        assert len(adapter.create_calls) == 0

    # Missing next-slice directive.
    adapter = FakeKanbanGraphAdapter()
    missing_next = _summary().replace("Next-Slice: m15\n", "")
    result = _apply(missing_next, adapter=adapter, event_id="evt-next")
    assert result["applied"] is False
    assert result["reason"] == "missing_next_slice"
    assert len(adapter.create_calls) == 0


def test_stale_inline_policy_refused_no_creates():
    adapter = FakeKanbanGraphAdapter()
    result = _apply(_summary() + "\nRoute: claude-openrouter-opus via Kimi/Moonshot", adapter=adapter)

    assert result["applied"] is False
    assert result["reason"] == "stale_inline_route"
    assert result["mutations"] == []
    assert len(adapter.create_calls) == 0


def test_non_allowlisted_transition_refused_no_creates():
    adapter = FakeKanbanGraphAdapter()
    result = _apply(_summary(transition="m14->m16.freeform"), adapter=adapter)

    assert result["applied"] is False
    assert result["reason"] == "unknown_transition"
    assert result["mutations"] == []
    assert len(adapter.create_calls) == 0


def test_depth_repeat_and_cooldown_caps_refuse_apply():
    adapter = FakeKanbanGraphAdapter()
    depth = _apply(_summary(), adapter=adapter, chain_depth=2)
    assert depth["applied"] is False
    assert depth["reason"] == "max_chain_depth"
    assert len(adapter.create_calls) == 0

    repeat_ledger = InMemoryRoadmapPromotionLedger()
    repeat_adapter = FakeKanbanGraphAdapter()
    _apply(_summary(), adapter=repeat_adapter, ledger=repeat_ledger, occurred_at=1000.0)
    repeat = _apply(
        _summary(),
        adapter=repeat_adapter,
        ledger=repeat_ledger,
        apply_ledger=InMemoryRoadmapApplyLedger(),
        event_id="evt-repeat",
        occurred_at=3000.0,
        policy=_policy(max_promotions_per_roadmap=1),
    )
    assert repeat["applied"] is False
    assert repeat["reason"] == "max_promotions_per_roadmap"

    cooldown_ledger = InMemoryRoadmapPromotionLedger()
    cooldown_adapter = FakeKanbanGraphAdapter()
    _apply(_summary(), adapter=cooldown_adapter, ledger=cooldown_ledger, occurred_at=1000.0)
    cooldown = _apply(
        _summary(),
        adapter=cooldown_adapter,
        ledger=cooldown_ledger,
        apply_ledger=InMemoryRoadmapApplyLedger(),
        event_id="evt-cooldown",
        occurred_at=1200.0,
    )
    assert cooldown["applied"] is False
    assert cooldown["reason"] == "cooldown"

    attempt_adapter = FakeKanbanGraphAdapter()
    capped = _apply(_summary(), adapter=attempt_adapter, event_id="evt-attempt-cap", policy=_policy(max_apply_tasks_per_graph=2))
    assert capped["applied"] is False
    assert capped["apply_reason"] == "max_apply_tasks_per_graph"
    assert len(attempt_adapter.create_calls) == 0


def test_apply_receipt_sanitizes_raw_paths_and_secrets():
    adapter = FakeKanbanGraphAdapter()
    result = _apply(
        _summary() + "\nSecret: API_KEY=abc123 at /home/duckran/private/final",
        adapter=adapter,
        event_id="evt-secret",
        source_final_ref="/home/duckran/private/final API_KEY=abc123",
    )

    receipt_text = str(result["receipt"])
    assert "/home/duckran" not in receipt_text
    assert "API_KEY" not in receipt_text
    assert "abc123" not in receipt_text
    # Applied graph still produced with a safe hashed source ref.
    assert result["applied"] is True
    assert "ref:sha256:" in receipt_text
