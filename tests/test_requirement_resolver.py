from __future__ import annotations

from agentflow_hermes.continuation_store import ContinuationStore
from agentflow_hermes.requirement_resolver import HumanEffortResolver
from agentflow_hermes.requirements import RequirementKind, Requirement, SatisfactionSource
from agentflow_hermes.standing_policy import create_standing_policy


def _instance(store: ContinuationStore) -> int:
    created = store.create_instance(
        board="warroom-os",
        source_task_id="t_1",
        source_event_id="ev_1",
        source_graph_id="g_1",
        contract_ref="generic.owner-input.v1",
    )
    return created["instance"]["id"]


def test_system_derivation_wins_over_artifact_and_policy(tmp_path):
    resolver = HumanEffortResolver()
    requirement = Requirement(name="result_url", kind=RequirementKind.FACT)
    context = {
        "system_derived": {"result_url": "https://system-derived"},
        "verified_artifacts": {"result_url": {"value": "https://artifact", "source_ref": "artifact:1"}},
    }
    result = resolver.resolve((requirement,), context=context)
    assert result.satisfied[0].value == "https://system-derived"
    assert result.satisfied[0].source == SatisfactionSource.SYSTEM_DERIVED
    assert result.interaction_needed is False
    assert result.is_h0() is True


def test_artifact_reuse_used_when_system_derivation_absent(tmp_path):
    resolver = HumanEffortResolver()
    requirement = Requirement(name="result_url", kind=RequirementKind.FACT)
    context = {"verified_artifacts": {"result_url": {"value": "https://artifact", "source_ref": "artifact:1"}}}
    result = resolver.resolve((requirement,), context=context)
    assert result.satisfied[0].value == "https://artifact"
    assert result.satisfied[0].source == SatisfactionSource.VERIFIED_ARTIFACT
    assert result.satisfied[0].source_ref == "artifact:1"
    assert "artifact:1" in result.evidence_refs


def test_standing_policy_satisfies_preference_requirement(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    create_standing_policy(
        store,
        policy_id="docs-release-autoapprove",
        owner_ref="operator-main",
        project_scope="agentflow-hermes",
        action_scope="docs-only-release",
        decision="approve",
    )
    resolver = HumanEffortResolver(store=store)
    requirement = Requirement(name="release_decision", kind=RequirementKind.PREFERENCE)

    result = resolver.resolve(
        (requirement,),
        owner_ref="operator-main",
        project_scope="agentflow-hermes",
        action_scope="docs-only-release",
    )

    assert result.interaction_needed is False
    satisfied = result.satisfied[0]
    assert satisfied.source == SatisfactionSource.STANDING_POLICY
    assert satisfied.policy_id == "docs-release-autoapprove"
    assert satisfied.value == "approve"


def test_standing_policy_satisfies_scoped_authorization_requirement(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    create_standing_policy(
        store,
        policy_id="oracle-recheck-autoretry",
        owner_ref="operator-main",
        project_scope="oracle-lab",
        action_scope="browser-recheck",
        decision="retry",
    )
    resolver = HumanEffortResolver(store=store)
    requirement = Requirement(name="retry_authorization", kind=RequirementKind.AUTHORIZATION)

    result = resolver.resolve(
        (requirement,), owner_ref="operator-main", project_scope="oracle-lab", action_scope="browser-recheck"
    )

    assert result.interaction_needed is False
    assert result.satisfied[0].source == SatisfactionSource.STANDING_POLICY


def test_fact_requirement_never_satisfied_by_standing_policy_even_if_scope_matches(tmp_path):
    """Evidence is not a preference (plan 2.8/8.4): a policy scoped exactly
    to this owner/project/action must still be refused for a FACT
    requirement — the resolver must not fabricate factual evidence."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    create_standing_policy(
        store,
        policy_id="result-url-reuse",
        owner_ref="operator-main",
        project_scope="warroom-os",
        action_scope="review-result-url",
        decision="https://policy-fabricated-url",
    )
    resolver = HumanEffortResolver(store=store)
    requirement = Requirement(name="result_url", kind=RequirementKind.FACT)

    result = resolver.resolve(
        (requirement,), owner_ref="operator-main", project_scope="warroom-os", action_scope="review-result-url"
    )

    assert result.interaction_needed is True
    assert result.unresolved == (requirement,)
    assert result.satisfied == ()


def test_evidence_requirement_never_satisfied_by_standing_policy(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    create_standing_policy(
        store,
        policy_id="evidence-reuse",
        owner_ref="operator-main",
        project_scope="warroom-os",
        action_scope="evidence-confirm",
        decision="confirmed",
    )
    resolver = HumanEffortResolver(store=store)
    requirement = Requirement(name="evidence_ref", kind=RequirementKind.EVIDENCE)

    result = resolver.resolve(
        (requirement,), owner_ref="operator-main", project_scope="warroom-os", action_scope="evidence-confirm"
    )

    assert result.interaction_needed is True
    assert result.unresolved == (requirement,)


def test_context_candidate_used_as_last_resort_before_ask(tmp_path):
    resolver = HumanEffortResolver()
    requirement = Requirement(name="result_url", kind=RequirementKind.FACT)
    context = {"context_candidates": {"result_url": "https://from-conversation"}}
    result = resolver.resolve((requirement,), context=context)
    assert result.satisfied[0].source == SatisfactionSource.CURRENT_OWNER_REPLY
    assert result.satisfied[0].value == "https://from-conversation"


def test_unresolved_requirement_forces_interaction(tmp_path):
    resolver = HumanEffortResolver()
    requirement = Requirement(name="result_url", kind=RequirementKind.FACT)
    result = resolver.resolve((requirement,))
    assert result.interaction_needed is True
    assert result.unresolved == (requirement,)
    assert result.is_h0() is False


def test_resolve_and_record_persists_satisfactions_to_store(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    instance_id = _instance(store)
    resolver = HumanEffortResolver(store=store)
    requirement = Requirement(name="result_url", kind=RequirementKind.FACT)
    context = {"system_derived": {"result_url": "https://system-derived"}}

    result = resolver.resolve_and_record(instance_id, (requirement,), context=context)

    assert result.is_h0() is True
    rows = store.list_requirement_satisfactions(instance_id)
    assert len(rows) == 1
    assert rows[0]["field_name"] == "result_url"
    assert rows[0]["value"] == "https://system-derived"
    assert rows[0]["source_kind"] == "system_derived"


def test_h0_result_when_all_requirements_resolve_before_ask(tmp_path):
    resolver = HumanEffortResolver()
    requirements = (
        Requirement(name="result_url", kind=RequirementKind.FACT),
        Requirement(name="release_choice", kind=RequirementKind.PREFERENCE),
    )
    context = {"system_derived": {"result_url": "https://x", "release_choice": "A"}}
    result = resolver.resolve(requirements, context=context)
    assert result.is_h0() is True
    assert len(result.satisfied) == 2
