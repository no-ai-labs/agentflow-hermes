from __future__ import annotations

from agentflow_hermes.continuation_store import ContinuationStore
from agentflow_hermes.requirements import Requirement, RequirementKind
from agentflow_hermes.standing_policy import StandingPolicyMatcher, create_standing_policy


def test_create_standing_policy_starts_at_version_one(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    policy = create_standing_policy(
        store,
        policy_id="docs-release-autoapprove",
        owner_ref="operator-main",
        project_scope="agentflow-hermes",
        action_scope="docs-only-release",
        decision="approve",
    )
    assert policy.version == 1
    assert policy.enabled is True
    assert policy.decision == "approve"


def test_recreating_a_policy_bumps_version(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    create_standing_policy(
        store,
        policy_id="docs-release-autoapprove",
        owner_ref="operator-main",
        project_scope="agentflow-hermes",
        action_scope="docs-only-release",
        decision="approve",
    )
    second = create_standing_policy(
        store,
        policy_id="docs-release-autoapprove",
        owner_ref="operator-main",
        project_scope="agentflow-hermes",
        action_scope="docs-only-release",
        decision="approve-with-changelog",
    )
    assert second.version == 2
    latest = store.latest_standing_policy("docs-release-autoapprove")
    assert latest["version"] == 2
    assert latest["decision"]["decision"] == "approve-with-changelog"


def test_matcher_finds_scoped_match(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    create_standing_policy(
        store,
        policy_id="docs-release-autoapprove",
        owner_ref="operator-main",
        project_scope="agentflow-hermes",
        action_scope="docs-only-release",
        decision="approve",
    )
    matcher = StandingPolicyMatcher(store)
    hit = matcher.find_match(owner_ref="operator-main", project_scope="agentflow-hermes", action_scope="docs-only-release")
    assert hit is not None
    assert hit.policy_id == "docs-release-autoapprove"


def test_matcher_refuses_out_of_scope_project(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    create_standing_policy(
        store,
        policy_id="docs-release-autoapprove",
        owner_ref="operator-main",
        project_scope="agentflow-hermes",
        action_scope="docs-only-release",
        decision="approve",
    )
    matcher = StandingPolicyMatcher(store)
    hit = matcher.find_match(owner_ref="operator-main", project_scope="oracle-lab", action_scope="docs-only-release")
    assert hit is None


def test_matcher_uses_only_latest_enabled_version(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    create_standing_policy(
        store,
        policy_id="docs-release-autoapprove",
        owner_ref="operator-main",
        project_scope="agentflow-hermes",
        action_scope="docs-only-release",
        decision="approve",
    )
    create_standing_policy(
        store,
        policy_id="docs-release-autoapprove",
        owner_ref="operator-main",
        project_scope="agentflow-hermes",
        action_scope="docs-only-release",
        decision="approve-v2",
    )
    matcher = StandingPolicyMatcher(store)
    hit = matcher.find_match(owner_ref="operator-main", project_scope="agentflow-hermes", action_scope="docs-only-release")
    assert hit.version == 2
    assert hit.decision == "approve-v2"


def test_matcher_conditions_must_be_subset_match(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    create_standing_policy(
        store,
        policy_id="docs-release-autoapprove",
        owner_ref="operator-main",
        project_scope="agentflow-hermes",
        action_scope="docs-only-release",
        decision="approve",
        conditions={"reviewer_verdict": "GO"},
    )
    matcher = StandingPolicyMatcher(store)
    no_match = matcher.find_match(
        owner_ref="operator-main",
        project_scope="agentflow-hermes",
        action_scope="docs-only-release",
        conditions={"reviewer_verdict": "BLOCK"},
    )
    match = matcher.find_match(
        owner_ref="operator-main",
        project_scope="agentflow-hermes",
        action_scope="docs-only-release",
        conditions={"reviewer_verdict": "GO", "extra": "ignored"},
    )
    assert no_match is None
    assert match is not None


def test_can_satisfy_true_for_preference_and_authorization():
    assert StandingPolicyMatcher.can_satisfy(Requirement(name="x", kind=RequirementKind.PREFERENCE)) is True
    assert StandingPolicyMatcher.can_satisfy(Requirement(name="x", kind=RequirementKind.AUTHORIZATION)) is True


def test_can_satisfy_false_for_fact_and_evidence():
    assert StandingPolicyMatcher.can_satisfy(Requirement(name="x", kind=RequirementKind.FACT)) is False
    assert StandingPolicyMatcher.can_satisfy(Requirement(name="x", kind=RequirementKind.EVIDENCE)) is False


def test_resolve_refuses_fact_requirement_even_with_exact_scope_match(tmp_path):
    """The kind check must be unconditional: a policy that scopes exactly to
    this owner/project/action still must not satisfy a FACT requirement."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    create_standing_policy(
        store,
        policy_id="result-url-reuse",
        owner_ref="operator-main",
        project_scope="warroom-os",
        action_scope="review-result-url",
        decision="https://policy-fabricated-url",
    )
    matcher = StandingPolicyMatcher(store)
    requirement = Requirement(name="result_url", kind=RequirementKind.FACT)
    hit = matcher.resolve(
        requirement, owner_ref="operator-main", project_scope="warroom-os", action_scope="review-result-url"
    )
    assert hit is None
