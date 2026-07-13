"""Human Effort Resolver (plan section 4): the derivation -> artifact ->
policy -> context -> ask ladder. Each Requirement is resolved in that fixed
order; only what genuinely cannot be resolved any other way falls through to
``ResolutionResult.unresolved`` (an owner question). When every requirement
resolves before the ask step, the result is H0
(``ResolutionResult.interaction_needed=False`` / ``is_h0()`` True) and no
owner anchor needs to be created at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .continuation_store import ContinuationStore
from .requirements import (
    Contradiction,
    Requirement,
    ResolutionResult,
    SatisfactionSource,
    SatisfiedRequirement,
)
from .standing_policy import StandingPolicyMatcher

SystemDeriver = Callable[[Requirement, dict[str, Any]], Any]
ArtifactLookup = Callable[[Requirement, dict[str, Any]], tuple[Any, str] | None]
ContextExtractor = Callable[[Requirement, dict[str, Any]], Any]


def _default_system_deriver(requirement: Requirement, context: dict[str, Any]) -> Any:
    return (context.get("system_derived") or {}).get(requirement.name)


def _default_artifact_lookup(requirement: Requirement, context: dict[str, Any]) -> tuple[Any, str] | None:
    hit = (context.get("verified_artifacts") or {}).get(requirement.name)
    if hit is None:
        return None
    if isinstance(hit, dict):
        return hit.get("value"), str(hit.get("source_ref") or "")
    return hit, ""


def _default_context_extractor(requirement: Requirement, context: dict[str, Any]) -> Any:
    return (context.get("context_candidates") or {}).get(requirement.name)


@dataclass
class HumanEffortResolver:
    """Runs the resolution ladder for a set of Requirements. The three
    lookup callables are injectable seams so callers can wire real Kanban/
    task state without this module depending on board I/O; ``policy_matcher``
    defaults to a store-backed ``StandingPolicyMatcher`` when a store is
    supplied."""

    store: ContinuationStore | None = None
    policy_matcher: StandingPolicyMatcher | None = None
    system_deriver: SystemDeriver = _default_system_deriver
    artifact_lookup: ArtifactLookup = _default_artifact_lookup
    context_extractor: ContextExtractor = _default_context_extractor

    def __post_init__(self) -> None:
        if self.policy_matcher is None and self.store is not None:
            self.policy_matcher = StandingPolicyMatcher(self.store)

    def resolve(
        self,
        requirements: tuple[Requirement, ...],
        *,
        context: dict[str, Any] | None = None,
        owner_ref: str = "",
        project_scope: str = "",
        action_scope: str = "",
    ) -> ResolutionResult:
        context = context or {}
        satisfied: list[SatisfiedRequirement] = []
        unresolved: list[Requirement] = []
        contradictions: tuple[Contradiction, ...] = ()
        evidence_refs: list[str] = []

        for requirement in requirements:
            hit = self._resolve_one(
                requirement,
                context=context,
                owner_ref=owner_ref,
                project_scope=project_scope,
                action_scope=action_scope,
            )
            if hit is None:
                unresolved.append(requirement)
                continue
            satisfied.append(hit)
            if hit.source_ref:
                evidence_refs.append(hit.source_ref)

        interaction_needed = bool(unresolved) or bool(contradictions)
        return ResolutionResult(
            satisfied=tuple(satisfied),
            unresolved=tuple(unresolved),
            contradictions=contradictions,
            evidence_refs=tuple(evidence_refs),
            interaction_needed=interaction_needed,
        )

    def resolve_and_record(
        self,
        instance_id: int,
        requirements: tuple[Requirement, ...],
        *,
        context: dict[str, Any] | None = None,
        owner_ref: str = "",
        project_scope: str = "",
        action_scope: str = "",
    ) -> ResolutionResult:
        """Resolve, then durably persist every satisfaction via the
        continuation store (for audit and future artifact/policy reuse). This
        is the automatic decision receipt referenced in plan 4.4 when the
        result is H0 — no owner anchor is ever created for it."""
        if self.store is None:
            raise ValueError("resolve_and_record requires a ContinuationStore")
        result = self.resolve(
            requirements,
            context=context,
            owner_ref=owner_ref,
            project_scope=project_scope,
            action_scope=action_scope,
        )
        for satisfied in result.satisfied:
            self.store.record_requirement_satisfaction(
                instance_id,
                field_name=satisfied.requirement.name,
                value=satisfied.value,
                source_kind=satisfied.source.value,
                source_ref=satisfied.source_ref,
                policy_id=satisfied.policy_id,
            )
        return result

    def _resolve_one(
        self,
        requirement: Requirement,
        *,
        context: dict[str, Any],
        owner_ref: str,
        project_scope: str,
        action_scope: str,
    ) -> SatisfiedRequirement | None:
        # 1. system derivation: source task metadata, parent summaries,
        # current board rows, known artifact refs.
        derived = self.system_deriver(requirement, context)
        if derived is not None:
            return SatisfiedRequirement(
                requirement=requirement, value=derived, source=SatisfactionSource.SYSTEM_DERIVED
            )

        # 2. artifact reuse: an unexpired verified receipt bound to the same
        # source/contract/field semantics.
        artifact_hit = self.artifact_lookup(requirement, context)
        if artifact_hit is not None:
            value, source_ref = artifact_hit
            if value is not None:
                return SatisfiedRequirement(
                    requirement=requirement,
                    value=value,
                    source=SatisfactionSource.VERIFIED_ARTIFACT,
                    source_ref=source_ref,
                )

        # 3. standing policy: exact action/resource/project scope and
        # version. Never eligible for FACT/EVIDENCE kinds regardless of
        # whether a policy superficially matches (plan 2.8/8.4) — enforced
        # unconditionally inside StandingPolicyMatcher.resolve.
        if self.policy_matcher is not None:
            policy = self.policy_matcher.resolve(
                requirement,
                owner_ref=owner_ref,
                project_scope=project_scope,
                action_scope=action_scope,
                conditions=context.get("policy_conditions"),
            )
            if policy is not None:
                return SatisfiedRequirement(
                    requirement=requirement,
                    value=policy.decision,
                    source=SatisfactionSource.STANDING_POLICY,
                    policy_id=policy.policy_id,
                )

        # 4. context candidate: source summary / current origin-session
        # reply context.
        candidate = self.context_extractor(requirement, context)
        if candidate is not None:
            return SatisfiedRequirement(
                requirement=requirement, value=candidate, source=SatisfactionSource.CURRENT_OWNER_REPLY
            )

        # 5. owner question (left unresolved for the caller to ask).
        return None
