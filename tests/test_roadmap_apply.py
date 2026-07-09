from __future__ import annotations

import json
from typing import Any

from agentflow_hermes.graph_creator import (
    FakeKanbanGraphAdapter,
    GraphIntentCandidate,
    KanbanGraphAdapter,
    RealKanbanGraphAdapter,
)
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
            ),
            "research.default.scout_evidence_scorecard_review_brief": RoadmapTransition(
                transition_id="research.default.scout_evidence_scorecard_review_brief",
                roadmap_id="research.roadmap",
                from_slice="research-current",
                to_slice="research-next",
                slice_template=("scout", "evidence", "scorecard", "review", "brief"),
                policy_refs=("design_opus", "implementation_default"),
                max_chain_depth=2,
                version="template-v2",
                template_preset="research-loop",
                goal_anchor="#research custom standing anchor",
            ),
            "shaman.default.design_impl_browser_review_fanin": RoadmapTransition(
                transition_id="shaman.default.design_impl_browser_review_fanin",
                roadmap_id="shaman.roadmap",
                from_slice="shaman-current",
                to_slice="shaman-next",
                slice_template=("design", "impl", "browser_e2e", "review", "fanin"),
                policy_refs=("design_opus", "implementation_default"),
                max_chain_depth=2,
                version="template-v2",
                template_preset="shaman-loop",
                goal_anchor="#shaman custom standing anchor",
            ),
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


class _RecordingCliRunner:
    """Fake injectable CLI runner recording argv shape, no subprocess spawned."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        task_id = f"t_real_{len(self.calls)}"
        return 0, json.dumps({"success": True, "task_id": task_id}), ""


def test_real_kanban_adapter_calls_cli_runner_with_exact_argv_shape():
    runner = _RecordingCliRunner()
    adapter = RealKanbanGraphAdapter(runner, board="main", source_task_id="t_final_source", created_by="loop-agent")

    result = _apply(_summary(), adapter=adapter)

    assert result["success"] is True
    assert result["applied"] is True
    assert result["created_task_ids"] == ["t_real_1", "t_real_2", "t_real_3"]
    assert len(adapter.create_calls) == 3
    assert len(runner.calls) == 3

    impl_argv, review_argv, fanin_argv = runner.calls

    assert impl_argv == [
        "hermes", "kanban", "--board", "main", "create", "m15 impl [m14->m15.impl_review_fanin]",
        "--body", impl_argv[impl_argv.index("--body") + 1],
        "--assignee", "impl-agent",
        "--idempotency-key", impl_argv[impl_argv.index("--idempotency-key") + 1],
        "--created-by", "loop-agent",
        "--origin-platform", "discord", "--origin-chat-id", "hermes-main",
        "--json",
    ]
    assert "--parent" not in impl_argv
    assert "--initial-status" not in impl_argv
    assert "ready" not in impl_argv
    assert "Acceptance criteria:" in impl_argv[impl_argv.index("--body") + 1]

    assert "--parent" in review_argv
    assert review_argv[review_argv.index("--parent") + 1] == "t_real_1"
    assert "--assignee" in review_argv and review_argv[review_argv.index("--assignee") + 1] == "ccreviewer"
    assert "--ack-trigger-agent" not in review_argv

    assert "--parent" in fanin_argv
    assert fanin_argv[fanin_argv.index("--parent") + 1] == "t_real_2"
    assert fanin_argv[fanin_argv.index("--assignee") + 1] == "ack-agent"
    assert "--ack-trigger-agent" in fanin_argv


def test_real_kanban_adapter_maps_return_to_origin_thread_and_user_flags():
    runner = _RecordingCliRunner()
    adapter = RealKanbanGraphAdapter(runner, board="main")
    intent = GraphIntentCandidate(
        kind="impl",
        blocker="roadmap",
        title="impl task",
        idempotency_key="roadmap:test:impl",
        origin="",
        return_to="discord:#hermes-main:thread-7:user-9",
        policy_refs=(),
        subscription_required=True,
        supersedes="",
        metadata={"assignee": "impl-agent", "acceptance_criteria": "prove argv"},
        body="body",
    )

    result = adapter.create_graph(intent)

    assert result["success"] is True
    argv = runner.calls[0]
    assert argv[argv.index("--origin-platform") + 1] == "discord"
    assert argv[argv.index("--origin-chat-id") + 1] == "hermes-main"
    assert argv[argv.index("--origin-thread-id") + 1] == "thread-7"
    assert argv[argv.index("--origin-user-id") + 1] == "user-9"
    assert "Acceptance criteria:\nprove argv" in argv[argv.index("--body") + 1]
    assert "--initial-status" not in argv


def test_real_kanban_adapter_duplicate_go_uses_apply_ledger_no_extra_create():
    runner = _RecordingCliRunner()
    apply_ledger = InMemoryRoadmapApplyLedger()
    first = _apply(_summary(), adapter=RealKanbanGraphAdapter(runner, board="main"), apply_ledger=apply_ledger)
    second = _apply(_summary(), adapter=RealKanbanGraphAdapter(runner, board="main"), apply_ledger=apply_ledger, event_id="evt-dup")

    assert first["applied"] is True
    assert second["duplicate"] is True
    assert second["created_task_ids"] == first["created_task_ids"]
    assert len(runner.calls) == 3


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


class _NoTaskIdAdapter:
    """Adapter that reports success without ever returning a usable task_id."""

    def __init__(self) -> None:
        self.create_calls: list[GraphIntentCandidate] = []

    def create_graph(self, intent: GraphIntentCandidate) -> dict[str, Any]:
        self.create_calls.append(intent)
        return {"success": True, "action": "no_task_id", "task_id": "", "mutations": []}


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


def test_kanban_adapter_apply_disabled_refuses_no_ledger_poison():
    adapter = KanbanGraphAdapter(apply_enabled=False)
    apply_ledger = InMemoryRoadmapApplyLedger()

    result = _apply(_summary(), adapter=adapter, apply_ledger=apply_ledger)

    assert result["success"] is False
    assert result["applied"] is False
    assert result["reason"] == "adapter_create_failed"
    assert result["created_task_ids"] == []
    assert result["mutations"] == []
    key = result["idempotency_key"]
    assert not apply_ledger.has(key)

    # A retry with a working adapter must not be treated as a duplicate.
    retry_adapter = FakeKanbanGraphAdapter()
    retry = _apply(_summary(), adapter=retry_adapter, apply_ledger=apply_ledger, event_id="evt-retry")
    assert retry["applied"] is True
    assert len(retry["created_task_ids"]) == 3


def test_adapter_success_without_task_id_fails_closed_no_ledger_poison():
    adapter = _NoTaskIdAdapter()
    apply_ledger = InMemoryRoadmapApplyLedger()

    result = _apply(_summary(), adapter=adapter, apply_ledger=apply_ledger)

    assert result["success"] is False
    assert result["applied"] is False
    assert result["reason"] == "missing_task_id"
    assert result["created_task_ids"] == []
    assert result["mutations"] == []
    key = result["idempotency_key"]
    assert not apply_ledger.has(key)

    retry_adapter = FakeKanbanGraphAdapter()
    retry = _apply(_summary(), adapter=retry_adapter, apply_ledger=apply_ledger, event_id="evt-retry")
    assert retry["applied"] is True
    assert len(retry["created_task_ids"]) == 3


def test_partial_failure_after_first_create_fails_closed_no_ledger_poison():
    adapter = _PartialFailureAdapter(fail_after=1)
    apply_ledger = InMemoryRoadmapApplyLedger()

    result = _apply(_summary(), adapter=adapter, apply_ledger=apply_ledger)

    assert result["success"] is False
    assert result["applied"] is False
    assert result["reason"] == "adapter_create_failed"
    assert result["created_task_ids"] == []
    assert result["mutations"] == []
    # The first create did happen on the adapter side but is surfaced only as
    # an uncommitted id, never as a committed/idempotency-recorded graph.
    assert len(result["uncommitted_task_ids"]) == 1
    key = result["idempotency_key"]
    assert not apply_ledger.has(key)

    retry_adapter = FakeKanbanGraphAdapter()
    retry = _apply(_summary(), adapter=retry_adapter, apply_ledger=apply_ledger, event_id="evt-retry")
    assert retry["applied"] is True
    assert len(retry["created_task_ids"]) == 3


def _research_registry() -> RoadmapTransitionRegistry:
    return RoadmapTransitionRegistry(
        version="test-v2",
        source_ref="test-registry",
        transitions={
            "research.default.scout_evidence_scorecard_review_brief": RoadmapTransition(
                transition_id="research.default.scout_evidence_scorecard_review_brief",
                roadmap_id="research.roadmap",
                from_slice="research-current",
                to_slice="research-next",
                slice_template=("scout", "evidence", "scorecard", "review", "brief"),
                policy_refs=("design_opus", "implementation_default"),
                max_chain_depth=2,
                version="template-v2",
                template_preset="research-loop",
                goal_anchor="#research anchor text",
            )
        },
    )


def _shaman_registry() -> RoadmapTransitionRegistry:
    return RoadmapTransitionRegistry(
        version="test-v2",
        source_ref="test-registry",
        transitions={
            "shaman.default.design_impl_browser_review_fanin": RoadmapTransition(
                transition_id="shaman.default.design_impl_browser_review_fanin",
                roadmap_id="shaman.roadmap",
                from_slice="shaman-current",
                to_slice="shaman-next",
                slice_template=("design", "impl", "browser_e2e", "review", "fanin"),
                policy_refs=("design_opus", "implementation_default"),
                max_chain_depth=2,
                version="template-v2",
                template_preset="shaman-loop",
                goal_anchor="#shaman anchor text",
            )
        },
    )


def test_research_loop_apply_creates_five_tasks_with_role_based_assignees():
    adapter = FakeKanbanGraphAdapter()
    result = apply_roadmap_promotion(
        _summary(transition="research.default.scout_evidence_scorecard_review_brief", next_slice="research-next"),
        event_id="evt-research-1",
        source_final_ref="t_final",
        source_assignee="ccreviewer",
        origin="Discord Devhub / #hermes-main",
        return_to="Discord Devhub / #hermes-main",
        subscription_status="verified",
        policy_resolution_ref="policy:model.implementation_default@v1",
        chain_depth=0,
        occurred_at=1000.0,
        registry=_research_registry(),
        ledger=InMemoryRoadmapPromotionLedger(),
        apply_ledger=InMemoryRoadmapApplyLedger(),
        policy=_policy(allowlisted_transitions=("research.default.scout_evidence_scorecard_review_brief",)),
        adapter=adapter,
    )

    assert result["applied"] is True
    assert [t["kind"] for t in result["tasks"]] == ["scout", "evidence", "scorecard", "review", "brief"]
    tasks = {t["kind"]: t for t in result["tasks"]}
    assert tasks["scout"]["assignee"] == "impl-agent"
    assert tasks["evidence"]["assignee"] == "impl-agent"
    assert tasks["scorecard"]["assignee"] == "impl-agent"
    assert tasks["review"]["assignee"] == "ccreviewer"
    assert tasks["brief"]["assignee"] == "ack-agent"
    assert tasks["brief"]["ack_trigger_agent"] == "ack-agent"
    assert tasks["scout"]["ack_trigger_agent"] == ""
    assert tasks["review"]["ack_trigger_agent"] == ""

    scout_body = adapter.create_calls[0].body
    assert "#research anchor text" in scout_body
    assert "Role: work" in scout_body
    assert "Step: scout" in scout_body

    review_body = adapter.create_calls[3].body
    assert "Role: review" in review_body
    assert "Review-Edge: verified" in review_body
    assert "Do not set Auto-Continue: true" in review_body

    brief_body = adapter.create_calls[4].body
    assert "Role: terminal" in brief_body
    assert "Auto-Continue: false" in brief_body
    assert "Roadmap-Transition: research.default.scout_evidence_scorecard_review_brief" in brief_body
    assert "Next-Slice: research-next" in brief_body
    assert "Verdict: GO|BLOCK|NEED_MORE" in brief_body


def test_shaman_loop_apply_creates_five_tasks_browser_e2e_work_role():
    adapter = FakeKanbanGraphAdapter()
    result = apply_roadmap_promotion(
        _summary(transition="shaman.default.design_impl_browser_review_fanin", next_slice="shaman-next"),
        event_id="evt-shaman-1",
        source_final_ref="t_final",
        source_assignee="ccreviewer",
        origin="Discord Devhub / #hermes-main",
        return_to="Discord Devhub / #hermes-main",
        subscription_status="verified",
        policy_resolution_ref="policy:model.implementation_default@v1",
        chain_depth=0,
        occurred_at=1000.0,
        registry=_shaman_registry(),
        ledger=InMemoryRoadmapPromotionLedger(),
        apply_ledger=InMemoryRoadmapApplyLedger(),
        policy=_policy(allowlisted_transitions=("shaman.default.design_impl_browser_review_fanin",)),
        adapter=adapter,
    )

    assert result["applied"] is True
    assert [t["kind"] for t in result["tasks"]] == ["design", "impl", "browser_e2e", "review", "fanin"]
    tasks = {t["kind"]: t for t in result["tasks"]}
    assert tasks["design"]["assignee"] == "impl-agent"
    assert tasks["browser_e2e"]["assignee"] == "impl-agent"
    assert tasks["fanin"]["assignee"] == "ack-agent"
    fanin_body = adapter.create_calls[4].body
    assert "Auto-Continue: false" in fanin_body
    assert "#shaman anchor text" in adapter.create_calls[2].body


def test_legacy_impl_review_fanin_bodies_carry_terminal_markers_via_role():
    adapter = FakeKanbanGraphAdapter()
    result = _apply(_summary(), adapter=adapter)
    assert result["applied"] is True
    fanin_body = adapter.create_calls[2].body
    assert "Auto-Continue: false" in fanin_body
    assert "Roadmap-Transition: m14->m15.impl_review_fanin" in fanin_body
    assert "Next-Slice: m15" in fanin_body


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


def test_research_preset_apply_uses_role_based_assignees_and_bodies():
    adapter = FakeKanbanGraphAdapter()
    result = _apply(
        _summary(
            transition="research.default.scout_evidence_scorecard_review_brief",
            next_slice="research-next",
        ),
        adapter=adapter,
        event_id="evt-research",
        policy=_policy(
            allowlisted_transitions=("research.default.scout_evidence_scorecard_review_brief",),
            apply_enabled=True,
            impl_assignee="research-worker",
            review_assignee="research-reviewer",
            ack_trigger_agent="brief-agent",
        ),
    )

    assert result["applied"] is True
    assert [t["kind"] for t in result["tasks"]] == ["scout", "evidence", "scorecard", "review", "brief"]
    tasks = {t["kind"]: t for t in result["tasks"]}
    assert tasks["scout"]["assignee"] == "research-worker"
    assert tasks["evidence"]["assignee"] == "research-worker"
    assert tasks["scorecard"]["assignee"] == "research-worker"
    assert tasks["review"]["assignee"] == "research-reviewer"
    assert tasks["brief"]["assignee"] == "brief-agent"
    assert tasks["brief"]["ack_trigger_agent"] == "brief-agent"

    candidates = {c["kind"]: c for c in result["candidates"]}
    assert "#research custom standing anchor" in candidates["scout"]["body"]
    assert "Role: review" in candidates["review"]["body"]
    assert "Review output requirements:" in candidates["review"]["body"]
    assert "Review-Edge: verified" in candidates["review"]["body"]
    assert "Role: terminal" in candidates["brief"]["body"]
    assert "Final ACK schema" in candidates["brief"]["body"]
    assert "Auto-Continue: false" in candidates["brief"]["body"]


def test_shaman_preset_apply_treats_browser_e2e_as_work_and_fanin_terminal():
    adapter = FakeKanbanGraphAdapter()
    result = _apply(
        _summary(
            transition="shaman.default.design_impl_browser_review_fanin",
            next_slice="shaman-next",
        ),
        adapter=adapter,
        event_id="evt-shaman",
        policy=_policy(
            allowlisted_transitions=("shaman.default.design_impl_browser_review_fanin",),
            apply_enabled=True,
            impl_assignee="shaman-worker",
            review_assignee="shaman-reviewer",
            ack_trigger_agent="fanin-agent",
        ),
    )

    assert result["applied"] is True
    assert [t["kind"] for t in result["tasks"]] == ["design", "impl", "browser_e2e", "review", "fanin"]
    tasks = {t["kind"]: t for t in result["tasks"]}
    assert tasks["browser_e2e"]["assignee"] == "shaman-worker"
    assert tasks["review"]["assignee"] == "shaman-reviewer"
    assert tasks["fanin"]["assignee"] == "fanin-agent"
    candidates = {c["kind"]: c for c in result["candidates"]}
    assert "browser/user-flow smoke" in candidates["browser_e2e"]["body"]
    assert "Role: work" in candidates["browser_e2e"]["body"]
    assert "Role: terminal" in candidates["fanin"]["body"]
