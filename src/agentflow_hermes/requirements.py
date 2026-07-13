"""Requirement semantics shared by the outcome compiler, input contracts, and
the requirement resolver (see docs/plans/2026-07-12-zero-ceremony-agentflow-autopilot.md
section 4).

``RequirementKind`` is an additional semantic axis layered on top of the
existing ``FieldAuthority`` (who may set a value) from ``input_contract.py``:
kind answers *what kind of thing* a field represents, authority answers *who*
may supply it. The two axes are independent — an OWNER-authority field can be
any kind, and a FACT/EVIDENCE kind can never be satisfied by standing policy
regardless of authority (see ``POLICY_INELIGIBLE_KINDS`` and
``standing_policy.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class RequirementKind(str, Enum):
    FACT = "fact"
    EVIDENCE = "evidence"
    PREFERENCE = "preference"
    AUTHORIZATION = "authorization"
    CORRECTION = "correction"


class SatisfactionSource(str, Enum):
    SYSTEM_DERIVED = "system_derived"
    VERIFIED_ARTIFACT = "verified_artifact"
    STANDING_POLICY = "standing_policy"
    CURRENT_OWNER_REPLY = "current_owner_reply"
    VERIFIER = "verifier"


# Evidence is not a preference (design principle 2.8): standing policy may
# satisfy PREFERENCE and scoped AUTHORIZATION requirements, but it can never
# invent a FACT or EVIDENCE value bound to a real source.
POLICY_INELIGIBLE_KINDS: tuple[RequirementKind, ...] = (RequirementKind.FACT, RequirementKind.EVIDENCE)


@dataclass(frozen=True)
class Requirement:
    """One field the continuation still needs, expressed in both typed and
    natural-language terms. ``question``/``answer_hint`` are only ever used to
    render or parse human-facing prose; contract validation still governs
    what value is actually accepted (see ``InputContract.validate_owner_submission``)."""

    name: str
    kind: RequirementKind
    authority: str = "owner"
    question: str = ""
    answer_hint: str = ""
    allowed_values: tuple[str, ...] = ()
    required: bool = True
    description: str = ""


@dataclass(frozen=True)
class SatisfiedRequirement:
    requirement: Requirement
    value: Any
    source: SatisfactionSource
    source_ref: str = ""
    policy_id: str = ""


@dataclass(frozen=True)
class Contradiction:
    name: str
    reason: str
    candidates: tuple[Any, ...] = ()


@dataclass(frozen=True)
class ResolutionResult:
    satisfied: tuple[SatisfiedRequirement, ...] = ()
    unresolved: tuple[Requirement, ...] = ()
    contradictions: tuple[Contradiction, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    interaction_needed: bool = True

    def satisfied_values(self) -> dict[str, Any]:
        return {s.requirement.name: s.value for s in self.satisfied}

    def is_h0(self) -> bool:
        """True when every requirement resolved before the ask step (no
        owner interaction was needed) — see plan section 4.4."""
        return not self.interaction_needed and not self.unresolved and not self.contradictions
