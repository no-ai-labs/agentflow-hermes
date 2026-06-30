from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from .live.sanitize import safe_durable_ref, safe_event_payload, short_text
from .remediation import plan_remediation

_GO_VERDICT_RE = re.compile(r"\bVerdict\s*:\s*GO\b", re.IGNORECASE)

# Policy ref keys (not route values) to carry per blocker type.
_BLOCKER_POLICY_REFS: dict[str, tuple[str, ...]] = {
    "stale_inline_route": ("design_opus", "implementation_default"),
    "stale_trust_grant_wording": (),
    "missing_subscription": ("implementation_default",),
    "stale_final_fanin": ("design_opus", "implementation_default"),
}


@dataclass(frozen=True)
class RemediationGraphPolicy:
    apply_enabled: bool = False
    max_proposals_per_source: int = 3
    max_proposals_per_blocker_class: int = 2


@dataclass(frozen=True)
class GraphIntentCandidate:
    kind: str
    blocker: str
    title: str
    idempotency_key: str
    origin: str
    return_to: str
    policy_refs: tuple[str, ...]
    subscription_required: bool
    supersedes: str
    metadata: dict[str, Any]
    body: str = ""

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": self.kind,
            "blocker": self.blocker,
            "title": self.title,
            "idempotency_key": self.idempotency_key,
            "origin": self.origin,
            "return_to": self.return_to,
            "policy_refs": list(self.policy_refs),
            "subscription_required": self.subscription_required,
            "supersedes": self.supersedes,
            "body": self.body,
            "metadata": {**self.metadata, "dry_run_only": True, "request_only": True},
            "mutations": [],
        }
        return safe_event_payload(d)


class KanbanGraphAdapter:
    """Real adapter boundary that is no-op unless explicitly armed.

    This slice intentionally ships no production board writer. Operators can
    inject a board-writing create_fn in a later gated apply slice, but the
    default adapter records no external mutation and fails closed even when the
    caller's policy is enabled.
    """

    def __init__(self, *, apply_enabled: bool = False, existing: list[dict[str, Any]] | None = None):
        self.apply_enabled = apply_enabled
        self._existing = list(existing or [])
        self.created_intents: list[GraphIntentCandidate] = []

    def create_graph(self, intent: GraphIntentCandidate) -> dict[str, Any]:
        if not self.apply_enabled:
            return {"success": False, "error": "adapter_apply_disabled", "mutations": []}
        self.created_intents.append(intent)
        return {
            "success": True,
            "action": "adapter_noop",
            "idempotency_key": intent.idempotency_key,
            "mutations": [],
        }

    def list_existing(self, idempotency_key: str) -> list[dict[str, Any]]:
        return [
            p for p in self._existing
            if p.get("idempotency_key") == idempotency_key
            or str(p.get("idempotency_key") or "").startswith(f"{idempotency_key}:")
        ]


class FakeKanbanGraphAdapter:
    """In-memory no-op adapter for tests. Never mutates external state."""

    def __init__(self) -> None:
        self.create_calls: list[GraphIntentCandidate] = []
        self._proposals: list[dict[str, Any]] = []

    def create_graph(self, intent: GraphIntentCandidate) -> dict[str, Any]:
        self.create_calls.append(intent)
        self._proposals.append({
            "idempotency_key": intent.idempotency_key,
            "blocker": intent.blocker,
            "kind": intent.kind,
        })
        return {
            "success": True,
            "action": "fake_noop",
            "idempotency_key": intent.idempotency_key,
            "mutations": [],
        }

    def list_existing(self, idempotency_key: str) -> list[dict[str, Any]]:
        return [
            p for p in self._proposals
            if p.get("idempotency_key") == idempotency_key
            or str(p.get("idempotency_key") or "").startswith(f"{idempotency_key}:")
        ]


def propose_remediation_graph(
    summary: str,
    *,
    source_ref: str = "",
    origin: str = "",
    return_to: str = "",
    prior_proposals: list[dict[str, Any]] | None = None,
    policy: RemediationGraphPolicy | None = None,
    adapter: FakeKanbanGraphAdapter | KanbanGraphAdapter | None = None,
) -> dict[str, Any]:
    """Request-only graph intent proposal from a BLOCK/NEED_MORE summary.

    Never mutates a real Kanban board. apply_remediation_graph must be called
    separately and requires explicit policy.apply_enabled=True.
    """
    effective_policy = policy or RemediationGraphPolicy()
    prior = prior_proposals or []
    safe_src = _safe_src_ref(source_ref)

    plan = plan_remediation(summary, source_ref=source_ref)
    if not plan["success"]:
        return {
            "success": False,
            "error": plan.get("error", "no_blocking_verdict"),
            "candidates": [],
            "mutations": [],
        }

    plan_proposals = plan.get("proposals") or []
    candidates: list[dict[str, Any]] = []

    # Storm-guard running counts seeded from prior proposals, then incremented as
    # real candidates are generated in THIS request so caps bound the combined
    # total (prior + current), not just prior context.
    blocker_counts: dict[str, int] = {}
    src_count = 0
    for p in prior:
        b = p.get("blocker")
        if b:
            blocker_counts[b] = blocker_counts.get(b, 0) + 1
        if safe_src and (p.get("metadata") or {}).get("source_ref_safe") == safe_src:
            src_count += 1

    for prop in plan_proposals:
        blocker: str = prop["blocker"]
        base_key: str = prop["idempotency_key"]

        # Idempotency dedupe: check adapter and prior_proposals.
        adapter_existing = adapter.list_existing(base_key) if adapter else []
        prior_match = [
            p for p in prior
            if p.get("idempotency_key") == base_key
            or str(p.get("idempotency_key") or "").startswith(f"{base_key}:")
        ]
        if adapter_existing or prior_match:
            candidates.append(_noop(base_key, blocker, "existing_proposal"))
            continue

        sequence: list[str] = (prop.get("metadata") or {}).get("candidate_sequence") or ["fix", "review", "final-vN"]
        policy_refs = _BLOCKER_POLICY_REFS.get(blocker, ())
        action: str = prop.get("action") or ""
        body = _policy_ref_body(policy_refs)

        for kind in sequence:
            idem = f"{base_key}:{kind}"

            # Storm guard: max proposals per blocker class (prior + current).
            if blocker_counts.get(blocker, 0) >= effective_policy.max_proposals_per_blocker_class:
                candidates.append(_noop(idem, blocker, "max_proposals_per_blocker_class"))
                continue

            # Storm guard: max proposals per source (prior + current).
            if safe_src and src_count >= effective_policy.max_proposals_per_source:
                candidates.append(_noop(idem, blocker, "max_proposals_per_source"))
                continue

            intent = GraphIntentCandidate(
                kind=kind,
                blocker=blocker,
                title=f"{prop.get('title', blocker)} [{kind}]",
                idempotency_key=idem,
                origin=short_text(origin or "remediation"),
                return_to=short_text(return_to or origin or ""),
                policy_refs=policy_refs,
                subscription_required=True,
                supersedes="",
                metadata={
                    "base_idempotency_key": base_key,
                    "source_ref_safe": safe_src,
                    "action": action,
                    "resolved_preview": {"binding": False, "redacted": True},
                },
                body=body,
            )
            candidates.append(intent.as_dict())
            blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
            if safe_src:
                src_count += 1

    return {
        "success": True,
        "dry_run": True,
        "request_only": True,
        "candidates": candidates,
        "mutations": [],
    }


def apply_remediation_graph(
    intent: GraphIntentCandidate,
    *,
    policy: RemediationGraphPolicy | None = None,
    adapter: FakeKanbanGraphAdapter | KanbanGraphAdapter | None = None,
) -> dict[str, Any]:
    """Apply a graph intent via the adapter. Fails closed unless policy.apply_enabled."""
    effective_policy = policy or RemediationGraphPolicy()
    if not effective_policy.apply_enabled:
        return {"success": False, "error": "apply_disabled_by_policy", "mutations": []}
    if adapter is None:
        return {"success": False, "error": "no_adapter_provided", "mutations": []}
    return adapter.create_graph(intent)


def resolve_stale_final_candidate(
    old_final_card: dict[str, Any],
    remediation_review_card: dict[str, Any],
    *,
    origin: str = "",
    return_to: str = "",
    policy: RemediationGraphPolicy | None = None,
) -> dict[str, Any]:
    """Generate a final-v2 candidate if old final is done/BLOCK and review has GO verdict.

    Never re-runs a completed final. Returns a request-only candidate only.
    """
    old_status = str(old_final_card.get("status") or "").lower()
    old_id = str(old_final_card.get("id") or "")

    _DONE_OR_BLOCKED = {"done", "completed", "blocked", "block"}
    if old_status not in _DONE_OR_BLOCKED:
        return {
            "success": False,
            "error": "old_final_not_done_or_blocked",
            "candidate": None,
            "mutations": [],
        }

    review_text = " ".join(
        str(remediation_review_card.get(k) or "")
        for k in ("title", "body", "summary", "result")
    )
    if not _GO_VERDICT_RE.search(review_text):
        return {
            "success": False,
            "error": "remediation_review_no_go_verdict",
            "candidate": None,
            "mutations": [],
        }

    safe_old_id, _ = safe_durable_ref(old_id or "stale_final", field="old_final_id")
    digest = hashlib.sha256(f"final-v2:{safe_old_id}".encode()).hexdigest()[:16]
    idem_key = f"remediation:stale_final_fanin:final-v2:{digest}"

    policy_refs = _BLOCKER_POLICY_REFS.get("stale_final_fanin", ())
    intent = GraphIntentCandidate(
        kind="final-v2",
        blocker="stale_final_fanin",
        title=f"final-v2 supersession for {short_text(old_id or 'unknown', max_len=40)}",
        idempotency_key=idem_key,
        origin=short_text(origin or "remediation"),
        return_to=short_text(return_to or origin or ""),
        policy_refs=policy_refs,
        subscription_required=True,
        supersedes=safe_old_id,
        metadata={
            "supersedes_final": safe_old_id,
            "old_final_status": old_status,
            "resolved_preview": {"binding": False, "redacted": True},
        },
        body=_policy_ref_body(policy_refs),
    )

    return {
        "success": True,
        "dry_run": True,
        "request_only": True,
        "candidate": intent.as_dict(),
        "mutations": [],
    }


def _safe_src_ref(source_ref: str) -> str:
    if not source_ref:
        return ""
    safe, _ = safe_durable_ref(source_ref, field="source_ref")
    return safe


def _noop(idempotency_key: str, blocker: str, reason: str) -> dict[str, Any]:
    return {
        "action": "noop",
        "reason": reason,
        "idempotency_key": idempotency_key,
        "blocker": blocker,
        "mutations": [],
    }


def _policy_ref_body(policy_refs: tuple[str, ...]) -> str:
    if not policy_refs:
        return ""
    lines = ["Policy refs:"]
    lines.extend(f"- policy:model.{ref}" for ref in policy_refs)
    lines.extend([
        "Resolved preview: informational, non-binding, redacted.",
        "Workers must resolve PolicyRef values at run time.",
    ])
    return "\n".join(lines)
