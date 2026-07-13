"""Standing policy receipts (plan section 8): a versioned, scoped owner
receipt that removes repeated approval questions within its declared scope.
This is not a new gate — it reuses explicit intent the owner already stated.

Semantic limit (plan 2.8 / 8.4): PREFERENCE and scoped AUTHORIZATION
requirements may be satisfied by a matching enabled standing policy. FACT and
EVIDENCE requirement kinds must NEVER be satisfiable by standing policy — a
policy can authorize evidence collection, it cannot assert the evidence
result. That boundary is enforced by ``StandingPolicyMatcher.can_satisfy``
using ``requirements.POLICY_INELIGIBLE_KINDS`` and is exercised directly in
requirement_resolver.py's resolution ladder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .continuation_store import ContinuationStore
from .requirements import POLICY_INELIGIBLE_KINDS, Requirement


@dataclass(frozen=True)
class StandingPolicy:
    policy_id: str
    version: int
    owner_ref: str
    project_scope: str
    action_scope: str
    conditions: dict[str, Any]
    decision: str
    created_from_message_ref: str
    enabled: bool


def create_standing_policy(
    store: ContinuationStore,
    *,
    policy_id: str,
    owner_ref: str,
    project_scope: str,
    action_scope: str,
    decision: str,
    conditions: dict[str, Any] | None = None,
    source_message_ref: str = "",
) -> StandingPolicy:
    """Create a new (auto-incrementing) version of a standing policy. One
    owner confirmation creates the versioned policy; future matching requests
    become H0 (plan 8.3)."""
    row = store.create_standing_policy(
        policy_id=policy_id,
        owner_ref=owner_ref,
        project_scope=project_scope,
        action_scope=action_scope,
        conditions=conditions or {},
        decision={"decision": decision},
        source_message_ref=source_message_ref,
        enabled=True,
    )
    return _row_to_policy(row)


def _row_to_policy(row: dict[str, Any]) -> StandingPolicy:
    decision_field = row["decision"]
    decision = decision_field.get("decision", "") if isinstance(decision_field, dict) else str(decision_field)
    return StandingPolicy(
        policy_id=row["policy_id"],
        version=row["version"],
        owner_ref=row["owner_ref"],
        project_scope=row["project_scope"],
        action_scope=row["action_scope"],
        conditions=row["conditions"],
        decision=str(decision),
        created_from_message_ref=row["source_message_ref"],
        enabled=row["enabled"],
    )


def _conditions_match(policy_conditions: dict[str, Any], candidate_conditions: dict[str, Any]) -> bool:
    """A policy's declared conditions must all be present and equal in the
    candidate's conditions (subset match); extra candidate keys are fine."""
    for key, value in policy_conditions.items():
        if candidate_conditions.get(key) != value:
            return False
    return True


class StandingPolicyMatcher:
    """Scoped policy store/matcher: versioned, scoped by
    owner_ref/project_scope/action_scope. Only the latest enabled version of
    a given ``policy_id`` is eligible to match a candidate continuation."""

    def __init__(self, store: ContinuationStore) -> None:
        self.store = store

    @staticmethod
    def can_satisfy(requirement: Requirement) -> bool:
        return requirement.kind not in POLICY_INELIGIBLE_KINDS

    def find_match(
        self,
        *,
        owner_ref: str,
        project_scope: str,
        action_scope: str,
        conditions: dict[str, Any] | None = None,
    ) -> StandingPolicy | None:
        conditions = conditions or {}
        latest_by_id: dict[str, dict[str, Any]] = {}
        for row in self.store.list_standing_policies(enabled_only=True):
            existing = latest_by_id.get(row["policy_id"])
            if existing is None or row["version"] > existing["version"]:
                latest_by_id[row["policy_id"]] = row

        for row in latest_by_id.values():
            if row["owner_ref"] != owner_ref:
                continue
            if row["project_scope"] != project_scope:
                continue
            if row["action_scope"] != action_scope:
                continue
            if not _conditions_match(row["conditions"], conditions):
                continue
            return _row_to_policy(row)
        return None

    def resolve(
        self,
        requirement: Requirement,
        *,
        owner_ref: str,
        project_scope: str,
        action_scope: str,
        conditions: dict[str, Any] | None = None,
    ) -> StandingPolicy | None:
        """Return a matching enabled policy for ``requirement``, or ``None``
        if no policy matches OR the requirement's kind can never be policy-
        satisfied (FACT/EVIDENCE) — the kind check runs first and is
        unconditional, so a superficially matching policy never leaks
        through for those kinds."""
        if not self.can_satisfy(requirement):
            return None
        return self.find_match(
            owner_ref=owner_ref, project_scope=project_scope, action_scope=action_scope, conditions=conditions
        )
