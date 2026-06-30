from __future__ import annotations

import json

from agentflow_hermes.maintenance.gitprobe import GitProbeResult
from agentflow_hermes.maintenance.policy import load_maintenance_policy
from agentflow_hermes.maintenance.watcher import propose_sync_graph, sync_dedupe_key
from agentflow_hermes.policy_ref import (
    PolicyRef,
    default_policy_document,
    load_policy_document,
    preflight_task_body,
    resolve_policy_ref,
)
from agentflow_hermes.graph_creator import (
    FakeKanbanGraphAdapter,
    GraphIntentCandidate,
    KanbanGraphAdapter,
    RemediationGraphPolicy,
    apply_remediation_graph,
    propose_remediation_graph,
    resolve_stale_final_candidate,
)
from agentflow_hermes.remediation import plan_remediation


def _policy_file(tmp_path):
    path = tmp_path / "model_policy.json"
    path.write_text(json.dumps({
        "version": "policy-v1",
        "routes": {
            "design_opus": {
                "provider": "anthropic-native",
                "model": "claude-opus-4-8",
                "command_family": "claude-code-opus",
            },
            "implementation_default": {
                "provider": "anthropic-native",
                "model": "claude-sonnet-4-6",
                "command_family": "claude-code-sonnet",
            },
        },
    }), encoding="utf-8")
    return path


def test_stale_warroom_inline_route_conflicts_with_current_policy(tmp_path):
    policy = load_policy_document(_policy_file(tmp_path))
    body = "Route: implementation via claude-openrouter-opus. Use this model for the worker."
    result = preflight_task_body(body, policy=policy, refs=("design_opus",))
    assert result["success"] is False
    assert result["error"] == "stale_inline_route"
    assert result["findings"][0]["blocker"] == "stale_inline_route"
    assert result["findings"][0]["policy_ref"] == "design_opus"


def test_policy_ref_resolves_current_anthropic_opus_with_redacted_evidence(tmp_path):
    policy = load_policy_document(_policy_file(tmp_path))
    resolution = resolve_policy_ref("policy:model.design_opus", policy).as_dict()
    assert resolution["provider"] == "anthropic-native"
    assert resolution["model_class"] == "opus"
    assert resolution["command_family"] == "claude-code-opus"
    evidence = json.dumps(resolution["evidence"])
    assert "policy-v1" in evidence
    assert str(tmp_path) not in evidence
    assert "TOKEN" not in evidence


def test_malformed_policy_fails_closed(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not-json", encoding="utf-8")
    result = preflight_task_body("Policy refs: policy:model.design_opus", policy_path=path)
    assert result["success"] is False
    assert result["error"] == "malformed_policy"
    try:
        load_policy_document(path)
    except Exception as exc:
        assert "malformed_policy" in str(exc)
    else:
        raise AssertionError("malformed policy unexpectedly loaded")


def test_unknown_required_policy_ref_fails_closed_with_redacted_evidence(tmp_path):
    # Negative smoke: caller asks to resolve a required ref that is not in the
    # central policy. Must fail closed instead of silently succeeding with an
    # empty/partial resolutions list.
    policy = load_policy_document(_policy_file(tmp_path))
    result = preflight_task_body(
        "Resolve policy refs for this task.",
        policy=policy,
        refs=("design_opus", "ghost_ref_not_in_policy"),
    )
    assert result["success"] is False
    # The known ref still resolves; the unknown one does not leak into resolutions.
    resolved_keys = {r["key"] for r in result["resolutions"]}
    assert "design_opus" in resolved_keys
    assert "ghost_ref_not_in_policy" not in resolved_keys
    unknown = [f for f in result["findings"] if f["blocker"] == "unknown_policy_ref"]
    assert unknown, "expected an unknown_policy_ref blocker"
    assert unknown[0]["policy_ref"] == "ghost_ref_not_in_policy"
    # Evidence stays redacted/safe: no raw private paths or secrets.
    blob = json.dumps(result)
    assert str(tmp_path) not in blob
    assert "TOKEN" not in blob


def test_unknown_optional_policy_ref_does_not_block(tmp_path):
    policy = load_policy_document(_policy_file(tmp_path))
    result = preflight_task_body(
        "Optional route discovery should not block.",
        policy=policy,
        refs=("design_opus", PolicyRef("missing_optional_ref", required=False)),
    )
    assert result["success"] is True
    assert result["error"] == ""
    assert {r["key"] for r in result["resolutions"]} == {"design_opus"}
    assert not [f for f in result["findings"] if f["blocker"] == "unknown_policy_ref"]


def test_maintenance_policy_malformed_numeric_fields_fail_closed(tmp_path):
    # Negative smoke: malformed integer fields must not raise; loader fails closed.
    path = tmp_path / "maintenance.json"
    path.write_text(json.dumps({
        "mode": "guarded_cycle",
        "maintenance_kill_switch": False,
        "allowed_services": ["live-bridge", 123, "cron", None],
        "max_cycles_per_day": "not-int",
        "min_seconds_between_cycles": "bad",
    }), encoding="utf-8")
    policy = load_maintenance_policy(path)
    assert policy.maintenance_kill_switch is True
    assert policy.mode in {"request_only", "disabled"}
    assert policy.error == "malformed_numeric_field"
    # Allowlist safety preserved: filtered to strings only.
    assert policy.allowed_services == ("live-bridge", "cron")


def test_stale_trust_grant_wording_block_yields_narrow_doc_fix_proposal():
    summary = "Verdict: BLOCK — stale_trust_grant_wording remains in docs; fix only the trust-grant wording."
    plan = plan_remediation(summary, source_ref="t_3434c714")
    assert plan["success"] is True
    proposal = plan["proposals"][0]
    assert proposal["blocker"] == "stale_trust_grant_wording"
    assert proposal["action"] == "narrow_doc_fix_trust_grant_wording"
    assert proposal["metadata"]["candidate_sequence"] == ["fix", "review"]
    assert plan["mutations"] == []


def test_stale_inline_route_block_yields_policy_ref_migration_proposal():
    summary = "Verdict: BLOCK — stale_inline_route: old claude-openrouter-opus route conflicts with policy refs."
    plan = plan_remediation(summary, source_ref="review:policy")
    assert plan["success"] is True
    proposal = plan["proposals"][0]
    assert proposal["blocker"] == "stale_inline_route"
    assert proposal["action"] == "append_policy_refs_and_migrate_inline_route_preview"
    assert proposal["metadata"]["candidate_sequence"] == ["fix", "review", "final-vN"]


def test_upstream_watcher_noop_when_existing_sync_graph_present():
    probe = GitProbeResult(
        repo_id="repo:abc",
        upstream_sha="a" * 40,
        behind=3,
        ahead=0,
        dirty=False,
        local_carried=False,
        ff_eligible=True,
    )
    dedupe = sync_dedupe_key(probe.repo_id, probe.upstream_sha)
    result = propose_sync_graph(probe, existing_cards=[{"status": "ready", "metadata": {"dedupe_key": dedupe}}])
    assert result["action"] == "noop"
    assert result["reason"] == "existing_sync_graph"
    assert result["mutations"] == []


def test_upstream_watcher_proposes_when_behind_and_no_graph():
    probe = GitProbeResult(
        repo_id="repo:abc",
        upstream_sha="b" * 40,
        behind=2,
        ahead=1,
        dirty=False,
        local_carried=True,
        ff_eligible=False,
    )
    result = propose_sync_graph(probe, existing_cards=[])
    assert result["success"] is True
    proposal = result["proposal"]
    assert proposal["action"] == "create_sync_graph_proposal"
    assert proposal["metadata"]["behind"] == 2
    assert proposal["metadata"]["ff_eligible"] is False
    assert result["mutations"] == []


def test_t64cf3160_style_block_yields_fix_review_final_vn_intent_dry_run_only():
    summary = "Verdict: BLOCK — stale final fanin: old final task t_64cf3160 needs supersession."
    result = propose_remediation_graph(
        summary,
        source_ref="t_64cf3160",
        origin="discord:#hermes-main",
        policy=RemediationGraphPolicy(max_proposals_per_blocker_class=3),
    )
    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["request_only"] is True
    assert result["mutations"] == []
    real = [c for c in result["candidates"] if c.get("action") != "noop"]
    kinds = {c["kind"] for c in real}
    assert "fix" in kinds
    assert "review" in kinds
    assert "final-vN" in kinds
    for c in real:
        assert c["metadata"]["dry_run_only"] is True
        assert c["metadata"]["request_only"] is True
        assert c["mutations"] == []


def test_t3434c714_style_stale_trust_grant_block_yields_narrow_doc_cleanup_dry_run_only():
    summary = "Verdict: BLOCK — stale_trust_grant_wording remains in docs; fix only the trust-grant wording."
    result = propose_remediation_graph(summary, source_ref="t_3434c714")
    assert result["success"] is True
    assert result["mutations"] == []
    real = [c for c in result["candidates"] if c.get("action") != "noop"]
    kinds = {c["kind"] for c in real}
    assert "fix" in kinds
    assert "review" in kinds
    assert "final-vN" not in kinds
    assert len(real) == 2


def test_stale_inline_route_yields_policy_ref_migration_no_legacy_route_copied():
    summary = "Verdict: BLOCK — stale_inline_route: old claude-openrouter-opus route conflicts with policy."
    result = propose_remediation_graph(summary, source_ref="review:policy")
    assert result["success"] is True
    real = [c for c in result["candidates"] if c.get("action") != "noop"]
    assert real
    blob = json.dumps(result)
    # No legacy route values leaked into the output.
    assert "claude-openrouter-opus" not in blob
    # Policy refs present and contain only symbolic keys, not route attribute values.
    for c in real:
        assert c["policy_refs"]
        assert "Policy refs:" in c["body"]
        assert "policy:model.design_opus" in c["body"]
        assert c["metadata"]["resolved_preview"]["binding"] is False
        assert c["metadata"]["resolved_preview"]["redacted"] is True
        for ref in c["policy_refs"]:
            assert "anthropic-native" not in ref
            assert "claude-opus" not in ref
            assert "openrouter" not in ref


def test_real_kanban_adapter_defaults_to_noop_without_adapter_apply_enable():
    intent = GraphIntentCandidate(
        kind="fix",
        blocker="stale_inline_route",
        title="policy migration [fix]",
        idempotency_key="remediation:stale_inline_route:test:fix",
        origin="discord:#hermes",
        return_to="discord:#hermes",
        policy_refs=("design_opus", "implementation_default"),
        subscription_required=True,
        supersedes="",
        metadata={},
    )
    adapter = KanbanGraphAdapter()
    result = apply_remediation_graph(
        intent,
        policy=RemediationGraphPolicy(
            apply_enabled=True,
            active_mode="apply",
            allowlisted_blockers=("stale_inline_route",),
        ),
        adapter=adapter,
    )
    assert result["success"] is False
    assert result["error"] == "adapter_apply_disabled"
    assert result["mutations"] == []
    assert adapter.created_intents == []


def test_stale_final_v1_block_plus_remediation_go_yields_final_v2_intent():
    old_final = {"id": "t_old_final_v1", "status": "blocked", "title": "Final v1 task"}
    review = {
        "id": "t_review_01",
        "status": "done",
        "title": "Remediation review",
        "body": "Verdict: GO — fix confirmed, ready for supersession.",
    }
    result = resolve_stale_final_candidate(old_final, review, origin="discord:#hermes-main")
    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["mutations"] == []
    candidate = result["candidate"]
    assert candidate["kind"] == "final-v2"
    assert candidate["blocker"] == "stale_final_fanin"
    assert candidate["supersedes"] != ""
    assert candidate["metadata"]["request_only"] is True
    # No automatic re-run: mutations list is always empty.
    assert candidate["mutations"] == []


def test_stale_final_resolver_noop_when_review_has_no_go():
    old_final = {"id": "t_final_v1", "status": "done"}
    review = {"id": "t_review_02", "body": "Verdict: BLOCK — more work needed."}
    result = resolve_stale_final_candidate(old_final, review)
    assert result["success"] is False
    assert result["error"] == "remediation_review_no_go_verdict"
    assert result["candidate"] is None


def test_idempotency_dedupe_prevents_duplicate_graph_proposals():
    summary = "Verdict: BLOCK — stale_trust_grant_wording remains in docs."
    result1 = propose_remediation_graph(summary, source_ref="t_idem_test")
    assert result1["success"] is True
    real1 = [c for c in result1["candidates"] if c.get("action") != "noop"]
    assert real1
    base_key = real1[0]["metadata"]["base_idempotency_key"]
    prior = [{"idempotency_key": base_key, "blocker": "stale_trust_grant_wording", "metadata": {}}]
    result2 = propose_remediation_graph(summary, source_ref="t_idem_test", prior_proposals=prior)
    assert result2["success"] is True
    assert all(c.get("action") == "noop" for c in result2["candidates"])
    assert result2["candidates"][0]["reason"] == "existing_proposal"


def test_idempotency_dedupe_via_fake_adapter():
    adapter = FakeKanbanGraphAdapter()
    summary = "Verdict: BLOCK — stale_trust_grant_wording in docs."
    result1 = propose_remediation_graph(summary, source_ref="t_adapter_test", adapter=adapter)
    assert result1["success"] is True
    real1 = [c for c in result1["candidates"] if c.get("action") != "noop"]
    assert real1
    # Register one candidate into the adapter by its base key.
    base_key = real1[0]["metadata"]["base_idempotency_key"]
    adapter._proposals.append({"idempotency_key": base_key, "blocker": "stale_trust_grant_wording", "kind": "fix"})
    result2 = propose_remediation_graph(summary, source_ref="t_adapter_test", adapter=adapter)
    assert result2["success"] is True
    assert all(c.get("action") == "noop" for c in result2["candidates"])


def test_default_policy_prevents_real_apply_fake_adapter_sees_no_mutations_unless_enabled():
    intent = GraphIntentCandidate(
        kind="fix",
        blocker="stale_final_fanin",
        title="test intent [fix]",
        idempotency_key="remediation:stale_final_fanin:test:fix",
        origin="discord:#hermes",
        return_to="discord:#hermes",
        policy_refs=("design_opus",),
        subscription_required=True,
        supersedes="",
        metadata={},
    )
    adapter = FakeKanbanGraphAdapter()
    # Default policy → apply must fail closed.
    result_default = apply_remediation_graph(intent)
    assert result_default["success"] is False
    assert result_default["error"] == "apply_disabled_by_policy"
    assert result_default["mutations"] == []
    assert len(adapter.create_calls) == 0

    # Explicit test-only policy with every apply gate open → fake adapter records call, no real mutations.
    test_policy = RemediationGraphPolicy(
        apply_enabled=True,
        active_mode="apply",
        allowlisted_blockers=("stale_final_fanin",),
    )
    result_enabled = apply_remediation_graph(intent, policy=test_policy, adapter=adapter)
    assert result_enabled["success"] is True
    assert result_enabled["mutations"] == []
    assert len(adapter.create_calls) == 1


def test_storm_guard_max_proposals_per_blocker_class():
    summary = "Verdict: BLOCK — stale_trust_grant_wording remains."
    policy = RemediationGraphPolicy(max_proposals_per_blocker_class=1)
    prior = [{"blocker": "stale_trust_grant_wording", "idempotency_key": "remediation:stale_trust_grant_wording:other", "metadata": {}}]
    result = propose_remediation_graph(summary, source_ref="t_storm", prior_proposals=prior, policy=policy)
    assert result["success"] is True
    assert all(c.get("action") == "noop" for c in result["candidates"])
    assert result["candidates"][0]["reason"] == "max_proposals_per_blocker_class"


def test_storm_guard_caps_count_current_request_candidates_for_multi_blocker_same_source():
    # Adversarial regression: multiple blockers from the SAME source generate
    # many candidates in a single request. Caps must count candidates produced in
    # this request (not just prior context), bounding real actionable candidates
    # by max_proposals_per_source and per-blocker counts by
    # max_proposals_per_blocker_class.
    summary = (
        "Verdict: BLOCK — stale_inline_route and missing_subscription and "
        "stale_final_fanin all present in the same review source."
    )
    policy = RemediationGraphPolicy(max_proposals_per_source=3, max_proposals_per_blocker_class=2)
    result = propose_remediation_graph(summary, source_ref="t_multi_blocker", policy=policy)
    assert result["success"] is True
    assert result["mutations"] == []

    real = [c for c in result["candidates"] if c.get("action") != "noop"]
    # Per-source cap bounds the total real actionable candidates.
    assert len(real) <= policy.max_proposals_per_source

    # Per-blocker-class cap bounds candidates for each blocker.
    per_blocker: dict[str, int] = {}
    for c in real:
        per_blocker[c["blocker"]] = per_blocker.get(c["blocker"], 0) + 1
    for count in per_blocker.values():
        assert count <= policy.max_proposals_per_blocker_class

    # Candidates beyond the caps are explicit noop/capped entries, not dropped.
    capped = [c for c in result["candidates"] if c.get("action") == "noop"]
    assert capped
    assert {c["reason"] for c in capped} <= {
        "max_proposals_per_source",
        "max_proposals_per_blocker_class",
    }


def test_no_raw_private_path_or_secret_persistence_in_proposals_and_evidence(tmp_path):
    policy = default_policy_document()
    resolution = resolve_policy_ref("design_opus", policy).as_dict()
    summary = "Verdict: BLOCK — stale_inline_route from /home/alice/private TOKEN=abc123 claude-openrouter-opus"
    plan = plan_remediation(summary, source_ref="/home/alice/private/TOKEN=abc123")
    probe = GitProbeResult(
        repo_id="repo:abc",
        upstream_sha="c" * 40,
        behind=1,
        ahead=0,
        dirty=False,
        local_carried=False,
        ff_eligible=True,
    )
    watcher = propose_sync_graph(probe)
    durable = json.dumps({"resolution": resolution, "plan": plan, "watcher": watcher})
    assert "/home/alice" not in durable
    assert "TOKEN=abc123" not in durable
    assert "private/TOKEN" not in durable


def test_no_raw_private_path_or_secret_in_graph_intent_candidates():
    summary = "Verdict: BLOCK — stale_inline_route: /home/alice/private TOKEN=abc123 claude-openrouter-opus"
    result = propose_remediation_graph(
        summary,
        source_ref="/home/alice/private/TOKEN=abc123",
        origin="discord:#hermes",
    )
    blob = json.dumps(result)
    assert "/home/alice" not in blob
    assert "TOKEN=abc123" not in blob
    assert "private/TOKEN" not in blob
    # The stale route name must not leak into candidates.
    assert "claude-openrouter-opus" not in blob


# ---------------------------------------------------------------------------
# MP3: gated auto-create tests
# ---------------------------------------------------------------------------

def _mp3_apply_policy(**kwargs) -> RemediationGraphPolicy:
    """Convenience: create a minimal valid apply-mode policy for tests."""
    defaults = {
        "apply_enabled": True,
        "active_mode": "apply",
        "kill_switch": False,
    }
    defaults.update(kwargs)
    return RemediationGraphPolicy(**defaults)


def test_mp3_default_policy_no_auto_creates():
    """Default policy must never trigger adapter auto-create."""
    adapter = FakeKanbanGraphAdapter()
    summary = "Verdict: BLOCK — stale_inline_route: old claude-openrouter-opus route conflicts."
    result = propose_remediation_graph(
        summary,
        source_ref="test_mp3_default",
        origin="discord:#hermes",
        adapter=adapter,
    )
    assert result["success"] is True
    assert result["mutations"] == []
    assert len(adapter.create_calls) == 0
    assert len(adapter.tasks) == 0


def test_mp3_apply_enabled_stale_inline_route_creates_intents():
    """apply_enabled + active_mode=apply + allowlisted stale_inline_route => adapter creates fix/review/final-vN."""
    adapter = FakeKanbanGraphAdapter()
    policy = _mp3_apply_policy(
        allowlisted_blockers=("stale_inline_route",),
        max_proposals_per_blocker_class=3,
    )
    summary = "Verdict: BLOCK — stale_inline_route: old claude-openrouter-opus route conflicts."
    result = propose_remediation_graph(
        summary,
        source_ref="test_mp3_inline",
        origin="discord:#hermes",
        return_to="discord:#hermes",
        policy=policy,
        adapter=adapter,
    )
    assert result["success"] is True
    assert result["mutations"] == []
    kinds = {c.kind for c in adapter.create_calls}
    assert "fix" in kinds
    assert "review" in kinds
    assert "final-vN" in kinds
    # No legacy route values in created intent payloads.
    blob = json.dumps([c.as_dict() for c in adapter.create_calls])
    assert "claude-openrouter-opus" not in blob
    assert "anthropic-native" not in blob
    # Policy refs are symbolic keys only (no route attribute values).
    for call in adapter.create_calls:
        assert call.policy_refs
        for ref in call.policy_refs:
            assert "claude-opus" not in ref
            assert "openrouter" not in ref
    # origin/return_to and subscription_required set on every created intent.
    for call in adapter.create_calls:
        assert "hermes" in call.origin
        assert call.subscription_required is True
    # Parent links: review -> fix, final-vN -> review, fix -> none.
    by_kind = {c.kind: c for c in adapter.create_calls}
    assert by_kind["fix"].parent_key == ""
    assert by_kind["review"].parent_key == by_kind["fix"].idempotency_key
    assert by_kind["final-vN"].parent_key == by_kind["review"].idempotency_key
    # FakeAdapter tracks tasks and links.
    assert len(adapter.tasks) == 3
    assert len(adapter.links) == 2  # review->fix, final-vN->review


def test_mp3_missing_subscription_creates_subscription_intents():
    """missing_subscription creates intents with subscription_required and return_to metadata."""
    adapter = FakeKanbanGraphAdapter()
    policy = _mp3_apply_policy(allowlisted_blockers=("missing_subscription",))
    summary = "Verdict: BLOCK — missing_subscription: subscription edge missing for this task."
    result = propose_remediation_graph(
        summary,
        source_ref="test_mp3_sub",
        origin="discord:#hermes",
        return_to="discord:#hermes",
        policy=policy,
        adapter=adapter,
    )
    assert result["success"] is True
    assert result["mutations"] == []
    assert len(adapter.create_calls) > 0
    for call in adapter.create_calls:
        assert call.subscription_required is True
        assert call.return_to != ""
    # FakeAdapter subscription records created for each intent.
    assert len(adapter.subscriptions) == len(adapter.create_calls)
    for sub in adapter.subscriptions:
        assert sub["return_to"] != ""
        assert sub["task_key"] != ""
        assert sub["requirement"] == "notify-subscribe"
    for task in adapter.tasks:
        assert task["notify_subscribe_required"] is True


def test_mp3_stale_final_fanin_propose_creates_intents_via_adapter():
    """propose_remediation_graph with stale_final_fanin allowlisted creates fix/review/final-vN."""
    adapter = FakeKanbanGraphAdapter()
    policy = _mp3_apply_policy(
        allowlisted_blockers=("stale_final_fanin",),
        max_proposals_per_blocker_class=3,
    )
    summary = "Verdict: BLOCK — stale final fanin: old final task t_64cf3160 needs supersession."
    result = propose_remediation_graph(
        summary,
        source_ref="t_64cf3160",
        origin="discord:#hermes",
        return_to="discord:#hermes",
        policy=policy,
        adapter=adapter,
    )
    assert result["success"] is True
    assert result["mutations"] == []
    kinds = {c.kind for c in adapter.create_calls}
    assert "fix" in kinds
    assert "review" in kinds
    assert "final-vN" in kinds


def test_mp3_stale_final_fanin_resolve_creates_final_v2_with_supersedes():
    """resolve_stale_final_candidate with apply enabled creates final-v2 with supersedes metadata."""
    adapter = FakeKanbanGraphAdapter()
    policy = _mp3_apply_policy(allowlisted_blockers=("stale_final_fanin",))
    old_final = {"id": "t_old_v1", "status": "blocked"}
    review = {"id": "t_review_go", "body": "Verdict: GO — supersession confirmed, fix ready."}
    result = resolve_stale_final_candidate(
        old_final, review,
        origin="discord:#hermes",
        return_to="discord:#hermes",
        policy=policy,
        adapter=adapter,
    )
    assert result["success"] is True
    assert result["mutations"] == []
    assert len(adapter.create_calls) == 1
    intent = adapter.create_calls[0]
    assert intent.kind == "final-v2"
    assert intent.blocker == "stale_final_fanin"
    assert intent.supersedes != ""
    assert intent.return_to != ""
    assert intent.subscription_required is True


def test_mp3_unknown_blocker_not_allowlisted_no_mutation():
    """Non-allowlisted blocker must not trigger adapter auto-create."""
    adapter = FakeKanbanGraphAdapter()
    # missing_subscription NOT in allowlist — only stale_inline_route is.
    policy = _mp3_apply_policy(allowlisted_blockers=("stale_inline_route",))
    summary = "Verdict: BLOCK — missing_subscription: subscription edge missing."
    result = propose_remediation_graph(
        summary,
        source_ref="test_mp3_not_allowed",
        origin="discord:#hermes",
        policy=policy,
        adapter=adapter,
    )
    assert result["success"] is True
    assert result["mutations"] == []
    assert len(adapter.create_calls) == 0


def test_mp3_max_auto_creates_per_run_cap():
    """max_auto_creates_per_run caps the number of adapter calls in a single request."""
    adapter = FakeKanbanGraphAdapter()
    # stale_inline_route yields fix/review/final-vN = 3 intents; cap at 2.
    policy = _mp3_apply_policy(
        allowlisted_blockers=("stale_inline_route",),
        max_proposals_per_blocker_class=3,
        max_auto_creates_per_run=2,
    )
    summary = "Verdict: BLOCK — stale_inline_route: old claude-openrouter-opus route."
    result = propose_remediation_graph(
        summary,
        source_ref="test_mp3_cap",
        origin="discord:#hermes",
        policy=policy,
        adapter=adapter,
    )
    assert result["success"] is True
    assert len(adapter.create_calls) <= 2
    capped = [c for c in result["candidates"] if c.get("reason") == "max_auto_creates_per_run"]
    assert capped


def test_mp3_max_auto_creates_per_run_counts_failed_adapter_attempts():
    """A failing adapter still consumes the auto-create attempt budget."""
    class FailingAdapter(FakeKanbanGraphAdapter):
        def create_graph(self, intent):
            self.create_calls.append(intent)
            return {"success": False, "error": "transient_adapter_failure", "mutations": []}

    adapter = FailingAdapter()
    policy = _mp3_apply_policy(
        allowlisted_blockers=("stale_inline_route",),
        max_proposals_per_blocker_class=3,
        max_auto_creates_per_run=1,
    )
    summary = "Verdict: BLOCK — stale_inline_route: old claude-openrouter-opus route."
    result = propose_remediation_graph(
        summary,
        source_ref="test_mp3_failing_adapter_cap",
        origin="discord:#hermes",
        policy=policy,
        adapter=adapter,
    )
    assert result["success"] is True
    assert len(adapter.create_calls) == 1
    capped = [c for c in result["candidates"] if c.get("reason") == "max_auto_creates_per_run"]
    assert len(capped) == 2
    assert all(c.get("action") == "noop" for c in capped)


def test_mp3_dedupe_prevents_repeated_auto_create():
    """Second call with same source_ref skips auto-create via adapter dedupe."""
    adapter = FakeKanbanGraphAdapter()
    policy = _mp3_apply_policy(allowlisted_blockers=("stale_trust_grant_wording",))
    summary = "Verdict: BLOCK — stale_trust_grant_wording remains in docs."
    result1 = propose_remediation_graph(
        summary,
        source_ref="test_mp3_dedupe",
        policy=policy,
        adapter=adapter,
    )
    assert result1["success"] is True
    first_count = len(adapter.create_calls)
    assert first_count > 0
    # Second call: adapter already recorded those proposals => dedupe triggers.
    result2 = propose_remediation_graph(
        summary,
        source_ref="test_mp3_dedupe",
        policy=policy,
        adapter=adapter,
    )
    assert result2["success"] is True
    assert len(adapter.create_calls) == first_count
    assert all(c.get("action") == "noop" for c in result2["candidates"])


def test_mp3_kill_switch_prevents_apply():
    """kill_switch=True must prevent all auto-creates even when apply_enabled=True."""
    adapter = FakeKanbanGraphAdapter()
    policy = _mp3_apply_policy(
        kill_switch=True,
        allowlisted_blockers=("stale_inline_route",),
    )
    summary = "Verdict: BLOCK — stale_inline_route: old claude-openrouter-opus route."
    result = propose_remediation_graph(
        summary,
        source_ref="test_mp3_kill",
        origin="discord:#hermes",
        policy=policy,
        adapter=adapter,
    )
    assert result["success"] is True
    assert result["mutations"] == []
    assert len(adapter.create_calls) == 0


def test_mp3_malformed_apply_policy_fails_closed():
    """Malformed or inconsistent apply policy must fail closed — no exception, no mutation."""
    adapter = FakeKanbanGraphAdapter()
    summary = "Verdict: BLOCK — stale_inline_route: old claude-openrouter-opus route."
    # Non-policy objects can appear from malformed JSON/YAML config; they must
    # fail closed without raising before any adapter call.
    result_malformed_object = propose_remediation_graph(
        summary,
        source_ref="test_mp3_malformed_object",
        policy={"apply_enabled": True, "active_mode": "apply", "allowlisted_blockers": ["stale_inline_route"]},  # type: ignore[arg-type]
        adapter=adapter,
    )
    assert result_malformed_object["success"] is True
    assert result_malformed_object["mutations"] == []
    assert len(adapter.create_calls) == 0

    # Malformed allowlist/cap fields also fail closed.
    malformed_policy = RemediationGraphPolicy(
        apply_enabled=True,
        active_mode="apply",
        allowlisted_blockers=["stale_inline_route"],  # type: ignore[arg-type]
    )
    result_bad_allowlist = propose_remediation_graph(
        summary,
        source_ref="test_mp3_bad_allowlist",
        policy=malformed_policy,
        adapter=adapter,
    )
    assert result_bad_allowlist["success"] is True
    assert len(adapter.create_calls) == 0

    # apply_enabled=True but active_mode is not "apply" => gates closed.
    for mode in ("request_only", "observe_only", "invalid_mode", ""):
        a = FakeKanbanGraphAdapter()
        policy = RemediationGraphPolicy(
            apply_enabled=True,
            active_mode=mode,
            allowlisted_blockers=("stale_inline_route",),
        )
        result = propose_remediation_graph(
            summary,
            source_ref=f"test_mp3_malformed_{mode}",
            policy=policy,
            adapter=a,
        )
        assert result["success"] is True, f"mode={mode!r} should still succeed"
        assert result["mutations"] == [], f"mode={mode!r} should produce no mutations"
        assert len(a.create_calls) == 0, f"mode={mode!r} should produce no adapter calls"
    # apply_enabled=False despite other gates set => closed.
    policy_disabled = RemediationGraphPolicy(
        apply_enabled=False,
        active_mode="apply",
        allowlisted_blockers=("stale_inline_route",),
    )
    result_disabled = propose_remediation_graph(
        summary,
        source_ref="test_mp3_disabled",
        policy=policy_disabled,
        adapter=adapter,
    )
    assert result_disabled["success"] is True
    assert len(adapter.create_calls) == 0


def test_mp3_no_raw_private_path_or_secret_in_auto_created_intents():
    """Auto-created intent payloads must not contain private paths or secrets."""
    adapter = FakeKanbanGraphAdapter()
    policy = _mp3_apply_policy(allowlisted_blockers=("stale_inline_route",))
    summary = "Verdict: BLOCK — stale_inline_route: /home/alice/private TOKEN=abc123 claude-openrouter-opus"
    result = propose_remediation_graph(
        summary,
        source_ref="/home/alice/private/TOKEN=abc123",
        origin="discord:#hermes",
        return_to="discord:#hermes",
        policy=policy,
        adapter=adapter,
    )
    assert result["success"] is True
    blob = json.dumps([c.as_dict() for c in adapter.create_calls])
    assert "/home/alice" not in blob
    assert "TOKEN=abc123" not in blob
    assert "private/TOKEN" not in blob
    assert "claude-openrouter-opus" not in blob
