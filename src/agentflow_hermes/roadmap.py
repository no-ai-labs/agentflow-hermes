from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .graph_creator import GraphIntentCandidate, _policy_ref_body
from .live.sanitize import safe_durable_ref, safe_event_payload, sanitize_summary, short_text
from .remediation import parse_verdict_summary

_EXPLICIT_TRUE = {"true", "yes", "verified", "present", "ok"}
_VERDICT_BLOCK_RE = re.compile(r"\bVerdict\s*:\s*(BLOCK|NEED_MORE|UNKNOWN|GO)\b", re.IGNORECASE)
_MARKER_RE = re.compile(r"^\s*([A-Za-z][A-Za-z -]*[A-Za-z])\s*:\s*(.*?)\s*$", re.MULTILINE)
_STALE_INLINE_POLICY_RE = re.compile(
    r"\b(?:claude-openrouter-opus|claude-venice-opus|openrouter\s*,\s*anthropic|Moonshot|Kimi)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RoadmapTransition:
    transition_id: str
    roadmap_id: str
    from_slice: str
    to_slice: str
    slice_template: tuple[str, ...]
    policy_refs: tuple[str, ...]
    max_chain_depth: int = 3
    version: str = ""


@dataclass(frozen=True)
class RoadmapTransitionRegistry:
    version: str
    transitions: dict[str, RoadmapTransition]
    content_hash: str = ""
    source_ref: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            payload = {
                "version": self.version,
                "transitions": {
                    key: {
                        "transition_id": value.transition_id,
                        "roadmap_id": value.roadmap_id,
                        "from_slice": value.from_slice,
                        "to_slice": value.to_slice,
                        "slice_template": list(value.slice_template),
                        "policy_refs": list(value.policy_refs),
                        "max_chain_depth": value.max_chain_depth,
                        "version": value.version,
                    }
                    for key, value in sorted(self.transitions.items())
                },
            }
            object.__setattr__(self, "content_hash", hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16])


@dataclass(frozen=True)
class NextSliceDirective:
    transition_id: str = ""
    next_slice: str = ""
    review_edge: bool = False
    ack_edge: bool = False
    parent_go: bool = False
    auto_continue: bool = False
    confidence: str = "none"
    source_ref: str = ""


@dataclass(frozen=True)
class NextSlicePlan:
    transition_id: str
    roadmap_id: str
    chain_depth: int
    idempotency_key: str
    candidates: tuple[GraphIntentCandidate, ...]
    request_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return safe_event_payload({
            "transition_id": self.transition_id,
            "roadmap_id": self.roadmap_id,
            "chain_depth": self.chain_depth,
            "idempotency_key": self.idempotency_key,
            "request_only": self.request_only,
            "candidates": [c.as_dict() for c in self.candidates],
            "mutations": [],
        })


@dataclass(frozen=True)
class RoadmapPromotionPolicy:
    auto_continue: bool = False
    allowlisted_transitions: tuple[str, ...] = ()
    max_chain_depth: int = 3
    max_promotions_per_roadmap: int = 6
    promote_cooldown_seconds: int = 900
    require_review_edge: bool = True
    require_ack_edge: bool = True
    require_trusted_assignee: bool = True
    trusted_assignees: tuple[str, ...] = ()
    require_origin_match: bool = True
    expected_origin: str = ""
    expected_return_to: str = ""
    require_policy_resolution: bool = True
    # M14b guarded apply-mode fields. apply_enabled is False by default so the
    # public surface remains request-only unless an operator explicitly arms it.
    apply_enabled: bool = False
    impl_assignee: str = ""
    review_assignee: str = ""
    ack_trigger_agent: str = ""
    max_apply_tasks_per_graph: int = 5


@dataclass
class InMemoryRoadmapPromotionLedger:
    receipts: list[dict[str, Any]] = field(default_factory=list)

    def has_event(self, event_id: str) -> bool:
        safe_event = _safe_ref(event_id, field="event_id")
        return any(r.get("event_id") == safe_event for r in self.receipts)

    def decision_for_event(self, event_id: str) -> dict[str, Any] | None:
        safe_event = _safe_ref(event_id, field="event_id")
        for receipt in reversed(self.receipts):
            if receipt.get("event_id") == safe_event:
                return dict(receipt.get("decision_payload") or {})
        return None

    def has_promotion_key(self, idempotency_key: str) -> bool:
        safe_key = _safe_ref(idempotency_key, field="idempotency_key")
        return any(r.get("idempotency_key") == safe_key for r in self.receipts)

    def count_promotions(self, roadmap_id: str) -> int:
        safe_roadmap = _safe_ref(roadmap_id, field="roadmap_id")
        return sum(1 for r in self.receipts if r.get("roadmap_id") == safe_roadmap and r.get("decision") == "propose")

    def current_chain_depth(self, roadmap_id: str) -> int:
        safe_roadmap = _safe_ref(roadmap_id, field="roadmap_id")
        depths = [int(r.get("chain_depth") or 0) for r in self.receipts if r.get("roadmap_id") == safe_roadmap]
        return max(depths) if depths else 0

    def last_promotion_time(self, roadmap_id: str) -> float | None:
        safe_roadmap = _safe_ref(roadmap_id, field="roadmap_id")
        times = [float(r.get("created_at") or 0) for r in self.receipts if r.get("roadmap_id") == safe_roadmap and r.get("decision") == "propose"]
        return max(times) if times else None

    def record(self, receipt: dict[str, Any]) -> None:
        self.receipts.append(safe_event_payload(receipt))


@dataclass
class InMemoryRoadmapApplyLedger:
    """Idempotency ledger for guarded apply-mode board writes.

    Keyed by the roadmap promotion idempotency key. A repeated apply for the same
    source final event/template resolves to the same key and returns the already
    recorded task ids instead of creating a duplicate graph.
    """

    applied: dict[str, dict[str, Any]] = field(default_factory=dict)

    def has(self, idempotency_key: str) -> bool:
        return _safe_ref(idempotency_key, field="idempotency_key") in self.applied

    def get(self, idempotency_key: str) -> dict[str, Any]:
        return dict(self.applied.get(_safe_ref(idempotency_key, field="idempotency_key")) or {})

    def record(self, idempotency_key: str, receipt: dict[str, Any]) -> None:
        safe_key = _safe_ref(idempotency_key, field="idempotency_key")
        self.applied[safe_key] = safe_event_payload(receipt)


def load_roadmap_transition_registry(path: str | Path) -> RoadmapTransitionRegistry:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    transitions_payload = payload.get("transitions")
    if not isinstance(transitions_payload, dict):
        raise ValueError("roadmap transition registry requires transitions object")
    transitions: dict[str, RoadmapTransition] = {}
    for key, raw in transitions_payload.items():
        if not isinstance(raw, dict):
            raise ValueError(f"invalid transition {key}")
        transition = RoadmapTransition(
            transition_id=str(raw["transition_id"]),
            roadmap_id=str(raw["roadmap_id"]),
            from_slice=str(raw["from_slice"]),
            to_slice=str(raw["to_slice"]),
            slice_template=tuple(str(x) for x in raw["slice_template"]),
            policy_refs=tuple(str(x) for x in raw["policy_refs"]),
            max_chain_depth=int(raw.get("max_chain_depth", 3)),
            version=str(raw.get("version", "")),
        )
        if key != transition.transition_id:
            raise ValueError("transition key must match transition_id")
        _validate_transition(transition)
        transitions[key] = transition
    return RoadmapTransitionRegistry(
        version=str(payload.get("version") or ""),
        transitions=transitions,
        source_ref=str(path),
    )


def parse_next_slice_directive(text: str, *, source_ref: str = "") -> NextSliceDirective:
    markers = {key.strip().lower().replace(" ", "-"): value.strip() for key, value in _MARKER_RE.findall(text or "")}
    transition_id = markers.get("roadmap-transition", "")
    next_slice = markers.get("next-slice", "")
    if not transition_id or not next_slice:
        return NextSliceDirective(confidence="none", source_ref=_safe_ref(source_ref, field="source_ref"))
    review_edge = _is_true_marker(markers.get("review-edge", ""))
    ack_edge = _is_true_marker(markers.get("ack-edge", ""))
    parent_go = _is_true_marker(markers.get("parent-go", ""))
    auto_continue = _is_true_marker(markers.get("auto-continue", ""))
    return NextSliceDirective(
        transition_id=transition_id,
        next_slice=next_slice,
        review_edge=review_edge,
        ack_edge=ack_edge,
        parent_go=parent_go,
        auto_continue=auto_continue,
        confidence="explicit",
        source_ref=_safe_ref(source_ref, field="source_ref"),
    )


def propose_roadmap_promotion(
    summary: str,
    *,
    event_id: str,
    source_final_ref: str,
    source_assignee: str = "",
    origin: str = "",
    return_to: str = "",
    subscription_status: str = "unverified",
    policy_resolution_ref: str = "",
    chain_depth: int = 0,
    occurred_at: float = 0.0,
    registry: RoadmapTransitionRegistry | None = None,
    ledger: InMemoryRoadmapPromotionLedger | None = None,
    policy: RoadmapPromotionPolicy | None = None,
    adapter: Any = None,
) -> dict[str, Any]:
    """Return a request-only roadmap autopromotion decision.

    M14a is intentionally proposal-only. This function never calls a board writer,
    never sends live messages, and never invokes the optional adapter.
    """

    effective_policy, malformed = _coerce_policy(policy)
    now = float(occurred_at or 0.0)
    ledger = ledger or InMemoryRoadmapPromotionLedger()
    safe_event = _safe_ref(event_id, field="event_id") or "missing_event_id"
    safe_source = _safe_ref(source_final_ref, field="source_final_ref") or "source_final"

    if ledger.has_event(safe_event):
        prior = ledger.decision_for_event(safe_event) or {}
        return _result("noop", "duplicate_event", safe_event, "", safe_source, now, prior_decision=prior)
    if malformed:
        return _record_and_result(ledger, "refuse", "malformed_policy", safe_event, "", safe_source, now)

    parsed = parse_verdict_summary(summary, source_ref=safe_source)
    verdict = parsed.verdict or _extract_verdict(summary)
    if verdict != "GO":
        return _record_and_result(ledger, "refuse", "not_go", safe_event, "", safe_source, now, verdict=verdict or "UNKNOWN")
    if not effective_policy.auto_continue:
        return _record_and_result(ledger, "noop", "autopromote_disabled", safe_event, "", safe_source, now, verdict="GO")

    directive = parse_next_slice_directive(summary, source_ref=safe_source)
    if directive.confidence != "explicit":
        return _record_and_result(ledger, "refuse", "missing_next_slice", safe_event, "", safe_source, now, verdict="GO")
    if not directive.auto_continue:
        return _record_and_result(ledger, "noop", "autopromote_disabled", safe_event, "", safe_source, now, verdict="GO", transition_id=directive.transition_id)
    if registry is None or directive.transition_id not in effective_policy.allowlisted_transitions or directive.transition_id not in registry.transitions:
        return _record_and_result(ledger, "refuse", "unknown_transition", safe_event, "", safe_source, now, verdict="GO", transition_id=directive.transition_id)
    transition = registry.transitions[directive.transition_id]
    try:
        _validate_transition(transition)
    except ValueError:
        return _record_and_result(ledger, "refuse", "unknown_transition", safe_event, "", safe_source, now, verdict="GO", transition_id=directive.transition_id)
    roadmap_id = transition.roadmap_id

    if directive.next_slice != transition.to_slice:
        return _record_and_result(ledger, "refuse", "unknown_transition", safe_event, roadmap_id, safe_source, now, verdict="GO", transition_id=directive.transition_id)
    if not _summary_origin_evidence_ok(summary, effective_policy):
        return _record_and_result(ledger, "refuse", "foreign_origin", safe_event, roadmap_id, safe_source, now, verdict="GO", transition_id=directive.transition_id)
    if effective_policy.require_review_edge and (not directive.review_edge or not directive.parent_go):
        return _record_and_result(ledger, "refuse", "missing_review_edge", safe_event, roadmap_id, safe_source, now, verdict="GO", transition_id=directive.transition_id)
    if effective_policy.require_ack_edge and (not directive.ack_edge or subscription_status != "verified"):
        reason = "missing_ack_edge" if not directive.ack_edge else "subscription_unverified"
        return _record_and_result(ledger, "refuse", reason, safe_event, roadmap_id, safe_source, now, verdict="GO", transition_id=directive.transition_id)
    if effective_policy.require_trusted_assignee and source_assignee not in effective_policy.trusted_assignees:
        return _record_and_result(ledger, "refuse", "untrusted_assignee", safe_event, roadmap_id, safe_source, now, verdict="GO", transition_id=directive.transition_id)
    if not _origin_ok(origin, return_to, effective_policy):
        return _record_and_result(ledger, "refuse", "foreign_origin", safe_event, roadmap_id, safe_source, now, verdict="GO", transition_id=directive.transition_id)
    if effective_policy.require_policy_resolution and not policy_resolution_ref:
        return _record_and_result(ledger, "refuse", "policy_unresolved", safe_event, roadmap_id, safe_source, now, verdict="GO", transition_id=directive.transition_id)
    if _STALE_INLINE_POLICY_RE.search(summary or ""):
        return _record_and_result(ledger, "refuse", "stale_inline_route", safe_event, roadmap_id, safe_source, now, verdict="GO", transition_id=directive.transition_id)

    ledger_depth = ledger.current_chain_depth(roadmap_id)
    effective_depth = max(int(chain_depth or 0), ledger_depth)
    max_depth = min(effective_policy.max_chain_depth, transition.max_chain_depth)
    if effective_depth >= max_depth:
        return _record_and_result(ledger, "refuse", "max_chain_depth", safe_event, roadmap_id, safe_source, now, verdict="GO", transition_id=directive.transition_id)
    if ledger.count_promotions(roadmap_id) >= effective_policy.max_promotions_per_roadmap:
        return _record_and_result(ledger, "refuse", "max_promotions_per_roadmap", safe_event, roadmap_id, safe_source, now, verdict="GO", transition_id=directive.transition_id)
    last = ledger.last_promotion_time(roadmap_id)
    if last is not None and now - last < effective_policy.promote_cooldown_seconds:
        return _record_and_result(ledger, "noop", "cooldown", safe_event, roadmap_id, safe_source, now, verdict="GO", transition_id=directive.transition_id, metadata={"next_eligible_at": last + effective_policy.promote_cooldown_seconds})

    promotion_key = _promotion_key(transition, safe_source, effective_depth + 1)
    if ledger.has_promotion_key(promotion_key):
        return _record_and_result(ledger, "noop", "existing_promotion", safe_event, roadmap_id, safe_source, now, verdict="GO", transition_id=directive.transition_id, idempotency_key=promotion_key)

    plan = _build_plan(transition, promotion_key, effective_depth + 1, origin=origin, return_to=return_to, source_final_ref=safe_source)
    receipt = _receipt(
        "propose",
        "roadmap_promotion_proposed",
        safe_event,
        roadmap_id,
        safe_source,
        now,
        transition_id=transition.transition_id,
        idempotency_key=promotion_key,
        verdict="GO",
        chain_depth=effective_depth + 1,
        origin=origin,
        return_to=return_to,
        subscription_status=subscription_status,
        policy_resolution_ref=policy_resolution_ref,
        registry=registry,
    )
    ledger.record(receipt)
    return safe_event_payload({
        "success": True,
        "action": "propose",
        "reason": "roadmap_promotion_proposed",
        "verdict": "GO",
        "transition_id": transition.transition_id,
        "roadmap_id": roadmap_id,
        "chain_depth": effective_depth + 1,
        "idempotency_key": promotion_key,
        "request_only": True,
        "dry_run": True,
        "plan": plan.as_dict(),
        "candidates": [c.as_dict() for c in plan.candidates],
        "mutations": [],
        "adapter_attempts": 0 if adapter is not None else 0,
        "receipt": receipt,
    })


def apply_roadmap_promotion(
    summary: str,
    *,
    event_id: str,
    source_final_ref: str,
    source_assignee: str = "",
    origin: str = "",
    return_to: str = "",
    subscription_status: str = "unverified",
    policy_resolution_ref: str = "",
    chain_depth: int = 0,
    occurred_at: float = 0.0,
    registry: RoadmapTransitionRegistry | None = None,
    ledger: InMemoryRoadmapPromotionLedger | None = None,
    apply_ledger: InMemoryRoadmapApplyLedger | None = None,
    policy: RoadmapPromotionPolicy | None = None,
    adapter: Any = None,
) -> dict[str, Any]:
    """Guarded apply-mode wrapper around ``propose_roadmap_promotion`` (M14b).

    Delegates every safety gate to the request-only proposer, then creates the
    implementation/review/final task graph via ``adapter`` only when
    ``policy.apply_enabled`` is True and the proposal is a fresh GO promotion.

    - ``apply_enabled=False`` (the default) returns the proposal with no board
      writes and ``mutations=[]``.
    - A repeated apply for the same source final event/template returns the
      already-created task ids and a duplicate receipt; the adapter is never
      called a second time.

    This function performs no live send, no active wake, and no gateway/systemd
    action. The only side effect is the injected board adapter, which is a
    fake/no-op unless an operator arms a real writer.
    """

    effective_policy, _ = _coerce_policy(policy)
    now = float(occurred_at or 0.0)
    ledger = ledger or InMemoryRoadmapPromotionLedger()
    apply_ledger = apply_ledger or InMemoryRoadmapApplyLedger()

    proposal = propose_roadmap_promotion(
        summary,
        event_id=event_id,
        source_final_ref=source_final_ref,
        source_assignee=source_assignee,
        origin=origin,
        return_to=return_to,
        subscription_status=subscription_status,
        policy_resolution_ref=policy_resolution_ref,
        chain_depth=chain_depth,
        occurred_at=occurred_at,
        registry=registry,
        ledger=ledger,
        policy=policy,
        adapter=None,
    )

    key = _apply_key_from_proposal(proposal)

    # Idempotency: a previously applied graph for this key returns existing ids.
    if key and apply_ledger.has(key):
        return _duplicate_apply_result(proposal, apply_ledger.get(key), key)

    # Request-only unless every gate passed (fresh propose) AND apply is armed.
    if proposal.get("action") != "propose":
        return _proposal_only_result(proposal, apply_enabled=effective_policy.apply_enabled)
    if not effective_policy.apply_enabled:
        return _proposal_only_result(proposal, apply_enabled=False, reason="apply_disabled")
    if registry is None or proposal.get("transition_id") not in registry.transitions:
        return _proposal_only_result(proposal, apply_enabled=True, reason="unknown_transition")
    if adapter is None:
        return _proposal_only_result(proposal, apply_enabled=True, reason="no_adapter")

    transition = registry.transitions[proposal["transition_id"]]
    depth = int(proposal.get("chain_depth") or 0)
    safe_source = _safe_ref(source_final_ref, field="source_final_ref") or "source_final"
    plan = _build_plan(transition, key, depth, origin=origin, return_to=return_to, source_final_ref=safe_source)
    if len(plan.candidates) > effective_policy.max_apply_tasks_per_graph:
        return _proposal_only_result(proposal, apply_enabled=True, reason="max_apply_tasks_per_graph")

    created_tasks: list[dict[str, Any]] = []
    created_task_ids: list[str] = []
    mutations: list[dict[str, Any]] = []
    key_to_task_id: dict[str, str] = {}

    for candidate in plan.candidates:
        assignee, acceptance, ack_agent = _apply_task_profile(candidate.kind, transition, effective_policy)
        enriched = replace(candidate, metadata={
            **candidate.metadata,
            "assignee": assignee,
            "acceptance_criteria": acceptance,
            "ack_trigger_agent": ack_agent,
        })
        result = adapter.create_graph(enriched)
        # Fail closed: an adapter that refuses (apply_disabled), errors, or returns
        # no usable task id must NOT produce applied=True and must NOT record the
        # idempotency ledger. Otherwise a partial/failed apply poisons future
        # retries and reports a graph that was never committed.
        if not isinstance(result, dict) or result.get("success") is not True:
            return _apply_failed_result(
                proposal, key, "adapter_create_failed", created_task_ids, transition,
            )
        task_id = str(result.get("task_id") or "")
        if not task_id:
            return _apply_failed_result(
                proposal, key, "missing_task_id", created_task_ids, transition,
            )
        key_to_task_id[candidate.idempotency_key] = task_id
        parent_task_id = key_to_task_id.get(candidate.parent_key, "") if candidate.parent_key else ""
        record = {
            "task_id": task_id,
            "kind": candidate.kind,
            "idempotency_key": candidate.idempotency_key,
            "assignee": assignee,
            "acceptance_criteria": acceptance,
            "ack_trigger_agent": ack_agent,
            "parent_task_id": parent_task_id,
            "origin": candidate.origin,
            "return_to": candidate.return_to,
        }
        created_tasks.append(record)
        created_task_ids.append(task_id)
        mutations.append({"op": "create_task", "task_id": task_id, "kind": candidate.kind, "idempotency_key": candidate.idempotency_key})

    sanitized_summary, _ = sanitize_summary(summary)
    template_id = transition.version or transition.transition_id
    receipt = safe_event_payload({
        "event_id": _safe_ref(event_id, field="event_id") or "missing_event_id",
        "idempotency_key": key,
        "source_final_ref": safe_source,
        "roadmap_id": transition.roadmap_id,
        "transition_id": transition.transition_id,
        "template_id": template_id,
        "policy_resolution_ref": _safe_ref(policy_resolution_ref, field="policy_resolution_ref"),
        "created_task_ids": created_task_ids,
        "summary": sanitized_summary,
        "decision": "apply",
        "reason": "roadmap_graph_applied",
        "chain_depth": depth,
        "registry_ref": getattr(registry, "source_ref", ""),
        "registry_hash": getattr(registry, "content_hash", ""),
        "dry_run": False,
        "request_only": False,
        "created_at": now,
    })

    apply_ledger.record(key, {
        "idempotency_key": key,
        "created_task_ids": created_task_ids,
        "tasks": created_tasks,
        "receipt": receipt,
    })

    return safe_event_payload({
        "success": True,
        "action": "apply",
        "applied": True,
        "reason": "roadmap_graph_applied",
        "apply_enabled": True,
        "request_only": False,
        "dry_run": False,
        "verdict": "GO",
        "transition_id": transition.transition_id,
        "roadmap_id": transition.roadmap_id,
        "template_id": template_id,
        "chain_depth": depth,
        "idempotency_key": key,
        "created_task_ids": created_task_ids,
        "tasks": created_tasks,
        "candidates": proposal.get("candidates", []),
        "mutations": mutations,
        "receipt": receipt,
    })


def _apply_task_profile(kind: str, transition: RoadmapTransition, policy: RoadmapPromotionPolicy) -> tuple[str, str, str]:
    """Return (assignee, acceptance_criteria, ack_trigger_agent) for a task kind.

    All strings are template-derived, never synthesized from free-text summary
    content, so the apply surface cannot smuggle raw source text into a card.
    """
    trusted = policy.trusted_assignees[0] if policy.trusted_assignees else ""
    ack_agent_default = policy.ack_trigger_agent or trusted
    to_slice = transition.to_slice
    tid = transition.transition_id
    if kind in {"impl", "implementation"}:
        return (
            policy.impl_assignee or trusted,
            f"Complete {to_slice} implementation slice per template {tid}",
            "",
        )
    if kind == "review":
        return (
            policy.review_assignee or trusted,
            f"Review {to_slice} implementation and emit GO/BLOCK verdict for {tid}",
            "",
        )
    # fanin / final: emit ACK edge back to origin.
    return (
        ack_agent_default,
        f"Fan-in {to_slice} outputs, confirm review GO, emit ACK for {tid}",
        ack_agent_default,
    )


def _apply_key_from_proposal(proposal: dict[str, Any]) -> str:
    if proposal.get("idempotency_key"):
        return str(proposal["idempotency_key"])
    receipt = proposal.get("receipt") or {}
    if receipt.get("idempotency_key"):
        return str(receipt["idempotency_key"])
    prior = proposal.get("prior_decision") or {}
    return str(prior.get("idempotency_key") or "")


def _proposal_only_result(proposal: dict[str, Any], *, apply_enabled: bool, reason: str = "") -> dict[str, Any]:
    payload = {
        **proposal,
        "applied": False,
        "apply_enabled": apply_enabled,
        "mutations": [],
        "created_task_ids": [],
        "tasks": [],
    }
    if reason:
        payload["apply_reason"] = reason
    return safe_event_payload(payload)


def _apply_failed_result(
    proposal: dict[str, Any],
    key: str,
    reason: str,
    uncommitted_task_ids: list[str],
    transition: RoadmapTransition,
) -> dict[str, Any]:
    """Fail-closed apply result. No idempotency ledger is recorded by the caller.

    Any task ids the adapter handed back before the failure are surfaced under
    ``uncommitted_task_ids`` so an operator can reconcile the board, but they are
    explicitly NOT recorded as an applied/committed graph: ``created_task_ids`` is
    empty and ``mutations`` is empty so a later retry is not deduped against a
    partial write.
    """
    return safe_event_payload({
        "success": False,
        "action": "apply",
        "applied": False,
        "reason": reason,
        "apply_reason": reason,
        "apply_enabled": True,
        "request_only": False,
        "dry_run": False,
        "transition_id": transition.transition_id,
        "roadmap_id": transition.roadmap_id,
        "idempotency_key": key,
        "created_task_ids": [],
        "uncommitted_task_ids": list(uncommitted_task_ids),
        "tasks": [],
        "candidates": proposal.get("candidates", []),
        "mutations": [],
        "receipt": {},
    })


def _duplicate_apply_result(proposal: dict[str, Any], existing: dict[str, Any], key: str) -> dict[str, Any]:
    receipt = dict(existing.get("receipt") or {})
    receipt["duplicate"] = True
    return safe_event_payload({
        "success": True,
        "action": "apply",
        "applied": False,
        "duplicate": True,
        "reason": "duplicate_graph",
        "apply_enabled": True,
        "request_only": False,
        "transition_id": proposal.get("transition_id", receipt.get("transition_id", "")),
        "roadmap_id": proposal.get("roadmap_id", receipt.get("roadmap_id", "")),
        "idempotency_key": key,
        "created_task_ids": existing.get("created_task_ids", []),
        "tasks": existing.get("tasks", []),
        "candidates": proposal.get("candidates", []),
        "mutations": [],
        "receipt": receipt,
    })


def _build_plan(transition: RoadmapTransition, promotion_key: str, chain_depth: int, *, origin: str, return_to: str, source_final_ref: str) -> NextSlicePlan:
    candidates: list[GraphIntentCandidate] = []
    parent_key = ""
    body = _policy_ref_body(transition.policy_refs)
    for kind in transition.slice_template:
        idem = f"{promotion_key}:{kind}"
        candidate_body = body
        if kind == "fanin":
            candidate_body = "\n".join(filter(None, [
                body,
                f"Roadmap-Transition: {transition.transition_id}",
                f"Next-Slice: {transition.to_slice}",
                "Auto-Continue: false",
            ]))
        intent = GraphIntentCandidate(
            kind=kind,
            blocker="roadmap_promotion",
            title=f"{transition.to_slice} {kind} [{transition.transition_id}]",
            idempotency_key=idem,
            origin=short_text(origin),
            return_to=short_text(return_to or origin),
            policy_refs=transition.policy_refs,
            subscription_required=True,
            supersedes=source_final_ref,
            metadata={
                "roadmap_id": transition.roadmap_id,
                "transition_id": transition.transition_id,
                "from_slice": transition.from_slice,
                "to_slice": transition.to_slice,
                "chain_depth": chain_depth,
                "source_final_ref": source_final_ref,
                "resolved_preview": {"binding": False, "redacted": True},
            },
            body=candidate_body,
            parent_key=parent_key,
        )
        candidates.append(intent)
        parent_key = idem
    return NextSlicePlan(
        transition_id=transition.transition_id,
        roadmap_id=transition.roadmap_id,
        chain_depth=chain_depth,
        idempotency_key=promotion_key,
        candidates=tuple(candidates),
    )


def _coerce_policy(policy: RoadmapPromotionPolicy | None) -> tuple[RoadmapPromotionPolicy, bool]:
    if policy is None:
        return RoadmapPromotionPolicy(), False
    if not isinstance(policy, RoadmapPromotionPolicy):
        return RoadmapPromotionPolicy(), True
    if not isinstance(policy.auto_continue, bool):
        return RoadmapPromotionPolicy(), True
    if not isinstance(policy.allowlisted_transitions, tuple) or not all(isinstance(x, str) for x in policy.allowlisted_transitions):
        return RoadmapPromotionPolicy(), True
    if not isinstance(policy.trusted_assignees, tuple) or not all(isinstance(x, str) for x in policy.trusted_assignees):
        return RoadmapPromotionPolicy(), True
    numeric = (policy.max_chain_depth, policy.max_promotions_per_roadmap, policy.promote_cooldown_seconds, policy.max_apply_tasks_per_graph)
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in numeric):
        return RoadmapPromotionPolicy(), True
    if not isinstance(policy.apply_enabled, bool):
        return RoadmapPromotionPolicy(), True
    if any(not isinstance(v, str) for v in (policy.impl_assignee, policy.review_assignee, policy.ack_trigger_agent)):
        return RoadmapPromotionPolicy(), True
    return policy, False


def _validate_transition(transition: RoadmapTransition) -> None:
    if not transition.transition_id or not transition.roadmap_id or not transition.from_slice or not transition.to_slice:
        raise ValueError("transition requires ids")
    if not transition.slice_template or any(not isinstance(k, str) or not k for k in transition.slice_template):
        raise ValueError("transition requires slice template")
    if any(not isinstance(ref, str) or not ref or any(token in ref.lower() for token in ("openrouter", "kimi", "moonshot", "claude-")) for ref in transition.policy_refs):
        raise ValueError("transition policy_refs must be symbolic refs")
    if isinstance(transition.max_chain_depth, bool) or not isinstance(transition.max_chain_depth, int) or transition.max_chain_depth < 0:
        raise ValueError("invalid max_chain_depth")


def _origin_ok(origin: str, return_to: str, policy: RoadmapPromotionPolicy) -> bool:
    if not policy.require_origin_match:
        return True
    if policy.expected_origin and origin != policy.expected_origin:
        return False
    if policy.expected_return_to and return_to != policy.expected_return_to:
        return False
    return bool(origin and return_to)


def _summary_origin_evidence_ok(summary: str, policy: RoadmapPromotionPolicy) -> bool:
    """Require the final GO text itself to carry origin/return edge evidence."""
    if not policy.require_origin_match:
        return True
    markers = {key.strip().lower().replace(" ", "-"): value.strip() for key, value in _MARKER_RE.findall(summary or "")}
    origin_value = markers.get("origin-return-to") or markers.get("origin") or ""
    for line in (summary or "").splitlines():
        if line.lower().startswith("origin/return_to:"):
            origin_value = line.split(":", 1)[1].strip()
            break
    return_value = markers.get("return-to") or markers.get("return_to") or origin_value
    if policy.expected_origin and origin_value != policy.expected_origin:
        return False
    if policy.expected_return_to and return_value != policy.expected_return_to:
        return False
    return bool(origin_value and return_value)


def _promotion_key(transition: RoadmapTransition, source_final_ref: str, depth: int) -> str:
    digest = hashlib.sha256(f"{transition.transition_id}:{source_final_ref}".encode()).hexdigest()[:16]
    return _safe_ref(f"roadmap:{transition.roadmap_id}:promote:{transition.transition_id}:{depth}:{digest}", field="idempotency_key")


def _record_and_result(ledger: InMemoryRoadmapPromotionLedger, action: str, reason: str, event_id: str, roadmap_id: str, source_final_ref: str, now: float, **kwargs: Any) -> dict[str, Any]:
    receipt = _receipt(action, reason, event_id, roadmap_id, source_final_ref, now, **kwargs)
    # Duplicate/no-op prior decisions intentionally do not append a second receipt.
    if reason not in {"duplicate_event"}:
        ledger.record(receipt)
    result = _result(action, reason, event_id, roadmap_id, source_final_ref, now, receipt=receipt, **kwargs)
    return result


def _result(action: str, reason: str, event_id: str, roadmap_id: str, source_final_ref: str, now: float, **kwargs: Any) -> dict[str, Any]:
    payload = {
        "success": action in {"propose", "noop", "refuse"},
        "action": action,
        "reason": reason,
        "verdict": kwargs.get("verdict", ""),
        "transition_id": kwargs.get("transition_id", ""),
        "roadmap_id": roadmap_id,
        "event_id": event_id,
        "source_final_ref": source_final_ref,
        "request_only": True,
        "dry_run": True,
        "candidates": [],
        "mutations": [],
        "adapter_attempts": 0,
        "metadata": kwargs.get("metadata", {}),
        "receipt": kwargs.get("receipt", {}),
        "prior_decision": kwargs.get("prior_decision", {}),
        "created_at": now,
    }
    return safe_event_payload(payload)


def _receipt(action: str, reason: str, event_id: str, roadmap_id: str, source_final_ref: str, now: float, **kwargs: Any) -> dict[str, Any]:
    transition_id = str(kwargs.get("transition_id") or "")
    idempotency_key = str(kwargs.get("idempotency_key") or f"roadmap:{action}:{event_id}")
    registry = kwargs.get("registry")
    return safe_event_payload({
        "event_id": event_id,
        "idempotency_key": idempotency_key,
        "source_final_ref": source_final_ref,
        "roadmap_id": roadmap_id,
        "transition_id": transition_id,
        "chain_depth": int(kwargs.get("chain_depth") or 0),
        "decision": action,
        "reason": reason,
        "origin_ref": _safe_ref(kwargs.get("origin", ""), field="origin_ref"),
        "return_to_ref": _safe_ref(kwargs.get("return_to", ""), field="return_to_ref"),
        "subscription_status": kwargs.get("subscription_status", "unverified") if kwargs.get("subscription_status", "unverified") in {"verified", "missing", "unverified", "not_required"} else "unverified",
        "policy_resolution_ref": _safe_ref(kwargs.get("policy_resolution_ref", ""), field="policy_resolution_ref"),
        "registry_ref": getattr(registry, "source_ref", "") if registry else "",
        "registry_hash": getattr(registry, "content_hash", "") if registry else "",
        "dry_run": True,
        "request_only": True,
        "created_at": now,
        "decision_payload": {
            "action": action,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "transition_id": transition_id,
        },
    })


def _extract_verdict(text: str) -> str:
    match = _VERDICT_BLOCK_RE.search(text or "")
    return match.group(1).upper() if match else "UNKNOWN"


def _is_true_marker(value: str) -> bool:
    return value.strip().lower() in _EXPLICIT_TRUE


def _safe_ref(value: Any, *, field: str) -> str:
    return safe_durable_ref(value, field=field)[0]
