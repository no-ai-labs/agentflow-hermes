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
