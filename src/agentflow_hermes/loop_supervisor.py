from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .graph_creator import RemediationGraphPolicy, propose_next_slice_graph, propose_remediation_graph, resolve_stale_final_candidate
from .live.sanitize import safe_durable_ref, safe_event_payload, short_text
from .remediation import parse_verdict_summary
from .roadmap import (
    InMemoryRoadmapApplyLedger,
    InMemoryRoadmapPromotionLedger,
    RoadmapPromotionPolicy,
    apply_roadmap_promotion,
)


_VALID_MODES = {"disabled", "observe_only", "request_only", "apply"}


@dataclass(frozen=True)
class LoopPolicy:
    active_mode: str = "request_only"  # disabled | observe_only | request_only | apply
    apply_enabled: bool = False
    kill_switch: bool = False
    allowlisted_blockers: tuple[str, ...] = ()
    max_rounds: int = 2
    max_same_blocker: int = 1
    max_auto_creates_per_run: int = 3
    max_tasks_per_graph: int = 9
    cooldown_seconds: int = 900
    backoff_multiplier: float = 2.0
    require_subscription_verified: bool = True
    require_origin_match: bool = True
    require_policy_resolution: bool = True
    request_only_by_default: bool = True
    expected_origin: str = ""
    expected_return_to: str = ""
    # M14a roadmap-GO autopromoter config. Defaults keep GO terminal/stable
    # behavior unchanged and proposal-only.
    roadmap_auto_continue: bool = False
    roadmap_allowlisted_transitions: tuple[str, ...] = ()
    roadmap_max_chain_depth: int = 3
    roadmap_max_promotions_per_roadmap: int = 6
    roadmap_promote_cooldown_seconds: int = 900
    roadmap_require_review_edge: bool = True
    roadmap_require_ack_edge: bool = True
    roadmap_require_trusted_assignee: bool = True
    roadmap_trusted_assignees: tuple[str, ...] = ()
    roadmap_require_policy_resolution: bool = True
    # M15 guarded roadmap-apply config. apply is armed only when the loop apply
    # gate is open (active_mode="apply" AND apply_enabled) AND roadmap_apply_enabled
    # is True; otherwise the GO path stays request-only.
    roadmap_apply_enabled: bool = False
    roadmap_impl_assignee: str = ""
    roadmap_review_assignee: str = ""
    roadmap_ack_trigger_agent: str = ""
    roadmap_board_adapter_mode: str = "fake"


@dataclass(frozen=True)
class LoopEvent:
    event_id: str
    source_graph_id: str
    verdict: str = ""
    summary: str = ""
    event_type: str = "terminal_task_verdict"
    source_task_id: str = ""
    blocker_class: str = ""
    origin: str = ""
    return_to: str = ""
    subscription_status: str = "unverified"
    policy_resolution_ref: str = ""
    round_no: int = 0
    occurred_at: float = 0.0
    source_final_id: str = ""
    remediation_review_id: str = ""
    source_assignee: str = ""
    chain_depth: int = 0
    old_final_card: dict[str, Any] | None = None
    remediation_review_card: dict[str, Any] | None = None


@dataclass(frozen=True)
class LoopState:
    source_graph_id: str
    rounds_used: int = 0
    final_vn: int = 1
    stable: bool = False
    escalated: bool = False
    last_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return safe_event_payload({
            "source_graph_id": self.source_graph_id,
            "rounds_used": self.rounds_used,
            "final_vn": self.final_vn,
            "stable": self.stable,
            "escalated": self.escalated,
            "last_reason": self.last_reason,
        })


@dataclass(frozen=True)
class LoopDecision:
    action: str
    reason: str
    event_id: str
    source_graph_id: str
    idempotency_key: str
    verdict: str = ""
    blocker_class: str = ""
    candidates: tuple[dict[str, Any], ...] = ()
    candidate: dict[str, Any] | None = None
    mutations: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action,
            "reason": self.reason,
            "event_id": self.event_id,
            "source_graph_id": self.source_graph_id,
            "idempotency_key": self.idempotency_key,
            "verdict": self.verdict,
            "blocker_class": self.blocker_class,
            "candidates": list(self.candidates),
            "candidate": self.candidate,
            "mutations": list(self.mutations),
            "metadata": self.metadata,
            "receipt": self.receipt,
        }
        return safe_event_payload(payload)


class LoopLedger(Protocol):
    def has_event(self, event_id: str) -> bool: ...
    def decision_for_event(self, event_id: str) -> dict[str, Any] | None: ...
    def has_idempotency_key(self, idempotency_key: str) -> bool: ...
    def count_blocker(self, source_graph_id: str, blocker_class: str) -> int: ...
    def max_round_for_graph(self, source_graph_id: str) -> int: ...
    def last_action_time(self, source_graph_id: str, blocker_class: str, actions: tuple[str, ...]) -> float | None: ...
    def current_final_vn(self, source_graph_id: str) -> int: ...
    def record_decision(self, decision: LoopDecision, event: LoopEvent) -> None: ...


class InMemoryLoopLedger:
    """Small fake/read-model ledger for tests and dry-run callers.

    It records compact sanitized loop receipts only. It is not a Kanban writer,
    gateway sender, DB migration, or live apply surface.
    """

    def __init__(self, receipts: list[dict[str, Any]] | None = None) -> None:
        self.receipts: list[dict[str, Any]] = [safe_event_payload(r) for r in (receipts or [])]

    def has_event(self, event_id: str) -> bool:
        safe_event = _safe_ref(event_id, field="event_id")
        return any(r.get("event_id") == safe_event for r in self.receipts)

    def decision_for_event(self, event_id: str) -> dict[str, Any] | None:
        safe_event = _safe_ref(event_id, field="event_id")
        for receipt in reversed(self.receipts):
            if receipt.get("event_id") == safe_event:
                return dict(receipt.get("decision_payload") or {})
        return None

    def has_idempotency_key(self, idempotency_key: str) -> bool:
        safe_key = _safe_ref(idempotency_key, field="idempotency_key")
        return any(r.get("idempotency_key") == safe_key for r in self.receipts)

    def count_blocker(self, source_graph_id: str, blocker_class: str) -> int:
        graph = _safe_ref(source_graph_id, field="source_graph_id")
        blocker = short_text(blocker_class)
        return sum(
            1
            for r in self.receipts
            if r.get("source_graph_id") == graph
            and r.get("blocker_class") == blocker
            and r.get("decision") in {"propose", "apply", "supersede"}
        )

    def max_round_for_graph(self, source_graph_id: str) -> int:
        graph = _safe_ref(source_graph_id, field="source_graph_id")
        rounds = [int(r.get("round_no") or 0) for r in self.receipts if r.get("source_graph_id") == graph]
        return max(rounds) if rounds else 0

    def last_action_time(self, source_graph_id: str, blocker_class: str, actions: tuple[str, ...]) -> float | None:
        graph = _safe_ref(source_graph_id, field="source_graph_id")
        blocker = short_text(blocker_class)
        latest: float | None = None
        for r in self.receipts:
            if r.get("source_graph_id") != graph or r.get("blocker_class") != blocker:
                continue
            if r.get("decision") not in actions:
                continue
            at = float(r.get("created_at") or 0)
            latest = at if latest is None else max(latest, at)
        return latest

    def current_final_vn(self, source_graph_id: str) -> int:
        graph = _safe_ref(source_graph_id, field="source_graph_id")
        versions = [int(r.get("final_vn") or 1) for r in self.receipts if r.get("source_graph_id") == graph]
        return max(versions) if versions else 1

    def record_decision(self, decision: LoopDecision, event: LoopEvent) -> None:
        self.receipts.append(decision.receipt)


GraphCreator = Callable[..., dict[str, Any]]


def evaluate_loop_event(
    event: LoopEvent | dict[str, Any],
    ledger: LoopLedger,
    policy: LoopPolicy | None,
    *,
    graph_creator: GraphCreator | None = None,
    adapter: Any = None,
    roadmap_registry: Any = None,
    roadmap_ledger: Any = None,
    roadmap_apply_ledger: Any = None,
    roadmap_promoter: GraphCreator | None = None,
) -> LoopDecision:
    """Evaluate one loop event and return a dry-run/request-only decision.

    The function never writes to a real Kanban board by itself. It records only a
    compact loop receipt in the provided ledger when the ledger supports it.
    """

    ev = _coerce_event(event)
    effective_policy, malformed = _coerce_policy(policy)
    now = float(ev.occurred_at or time.time())
    # MP4a/MP4b: adapter calls must be impossible unless active_mode is "apply" AND
    # apply_enabled is explicitly True.
    apply_gate_open = _apply_effectively_enabled(effective_policy)
    effective_adapter = adapter if apply_gate_open else None

    if ledger.has_event(ev.event_id):
        prior = ledger.decision_for_event(ev.event_id) or {}
        return _decision(ev, "noop", "duplicate_event", effective_policy, now, metadata={"prior_decision": prior})

    if malformed:
        return _record(ledger, _decision(ev, "escalate", "malformed_policy", effective_policy, now), ev)
    if effective_policy.kill_switch:
        return _record(ledger, _decision(ev, "escalate", "kill_switch", effective_policy, now), ev)
    if effective_policy.active_mode == "disabled":
        return _record(ledger, _decision(ev, "noop", "disabled", effective_policy, now), ev)
    if not _origin_ok(ev, effective_policy):
        return _record(ledger, _decision(ev, "escalate", "foreign_origin", effective_policy, now), ev)

    if ev.event_type in {"remediation_review_go", "stale_final_block"} or (ev.old_final_card and ev.remediation_review_card):
        return _evaluate_supersession(ev, ledger, effective_policy, now, adapter=effective_adapter)

    parsed = parse_verdict_summary(ev.summary, source_ref=ev.source_task_id or ev.source_graph_id)
    verdict = (ev.verdict or parsed.verdict or "UNKNOWN").upper()
    blocker = short_text(ev.blocker_class or (parsed.blockers[0] if parsed.blockers else ""))

    if verdict == "GO":
        decision = _decision(ev, "stabilize", "go_terminal", effective_policy, now, verdict=verdict)
        decision = _attach_roadmap_autopromote(
            decision,
            ev,
            effective_policy,
            now,
            registry=roadmap_registry,
            roadmap_ledger=roadmap_ledger,
            apply_ledger=roadmap_apply_ledger,
            promoter=roadmap_promoter,
            adapter=effective_adapter,
        )
        return _record(ledger, decision, ev)
    if verdict == "NEED_MORE":
        return _record(ledger, _decision(ev, "escalate", "needs_input", effective_policy, now, verdict=verdict), ev)
    if verdict != "BLOCK":
        return _record(ledger, _decision(ev, "escalate", "unknown_verdict", effective_policy, now, verdict=verdict), ev)

    if not blocker or blocker not in effective_policy.allowlisted_blockers:
        reason = "unknown_blocker" if not blocker else "blocker_not_allowlisted"
        return _record(ledger, _decision(ev, "escalate", reason, effective_policy, now, verdict=verdict, blocker=blocker), ev)
    if effective_policy.require_policy_resolution and not ev.policy_resolution_ref:
        return _record(ledger, _decision(ev, "escalate", "policy_resolution_missing", effective_policy, now, verdict=verdict, blocker=blocker), ev)
    if effective_policy.require_subscription_verified and ev.subscription_status != "verified":
        return _record(ledger, _decision(ev, "escalate", "subscription_unverified", effective_policy, now, verdict=verdict, blocker=blocker), ev)
    ledger_max_round = ledger.max_round_for_graph(ev.source_graph_id)
    effective_round = max(ev.round_no, ledger_max_round)
    if effective_round >= effective_policy.max_rounds:
        return _record(
            ledger,
            _decision(ev, "escalate", "max_rounds", effective_policy, now, verdict=verdict, blocker=blocker, metadata={"ledger_round_no": ledger_max_round, "effective_round_no": effective_round}),
            ev,
        )
    if ledger.count_blocker(ev.source_graph_id, blocker) >= effective_policy.max_same_blocker:
        return _record(ledger, _decision(ev, "escalate", "max_same_blocker", effective_policy, now, verdict=verdict, blocker=blocker), ev)

    last = ledger.last_action_time(ev.source_graph_id, blocker, ("propose", "apply", "supersede"))
    if last is not None and now - last < effective_policy.cooldown_seconds:
        return _record(
            ledger,
            _decision(
                ev,
                "noop",
                "cooldown",
                effective_policy,
                now,
                verdict=verdict,
                blocker=blocker,
                metadata={"next_eligible_at": last + effective_policy.cooldown_seconds},
            ),
            ev,
        )

    if effective_policy.active_mode == "observe_only":
        return _record(ledger, _decision(ev, "noop", "observe_only", effective_policy, now, verdict=verdict, blocker=blocker), ev)
    if effective_policy.active_mode == "apply" and not apply_gate_open:
        return _record(ledger, _decision(ev, "escalate", "apply_disabled_by_policy", effective_policy, now, verdict=verdict, blocker=blocker), ev)

    creator = graph_creator or propose_remediation_graph
    graph_policy = _graph_policy(effective_policy, blocker)
    summary = ev.summary or f"Verdict: BLOCK — {blocker}"
    result = creator(
        summary,
        source_ref=ev.source_task_id or ev.source_graph_id,
        origin=ev.origin,
        return_to=ev.return_to,
        policy=graph_policy,
        adapter=effective_adapter,
    )
    action = "apply" if apply_gate_open else "propose"
    return _record(
        ledger,
        _decision(
            ev,
            action,
            "bounded_remediation",
            effective_policy,
            now,
            verdict=verdict,
            blocker=blocker,
            candidates=tuple(result.get("candidates") or ()),
            mutations=tuple(result.get("mutations") or ()),
            metadata={
                "graph_creator_result": _safe_creator_result(result),
                "ledger_round_no": ledger_max_round,
                "effective_round_no": effective_round,
                "receipt_round_no": effective_round + 1,
                "round_no_derived": effective_round + 1,
                "adapter_attempts": int(result.get("adapter_attempts") or 0),
            },
        ),
        ev,
    )


def _attach_roadmap_autopromote(
    decision: LoopDecision,
    event: LoopEvent,
    policy: LoopPolicy,
    now: float,
    *,
    registry: Any,
    roadmap_ledger: Any,
    apply_ledger: Any,
    promoter: GraphCreator | None,
    adapter: Any,
) -> LoopDecision:
    """Attach next-slice roadmap metadata/candidates to a GO decision.

    Request-only by default: proposal metadata and candidate JSON only, no board
    writes. When the loop apply gate is open (``adapter`` is the apply-gated
    adapter) AND ``roadmap_apply_enabled`` is set, delegate to
    ``apply_roadmap_promotion`` so an eligible GO creates the impl/review/fanin
    graph via the (fake/injected) adapter with idempotent apply-ledger dedup.
    """
    if not policy.roadmap_auto_continue:
        return decision

    roadmap_policy = _roadmap_policy(policy)
    apply_armed = roadmap_policy.apply_enabled and adapter is not None
    common = dict(
        event_id=event.event_id,
        source_final_ref=event.source_final_id or event.source_task_id or event.source_graph_id,
        source_assignee=event.source_assignee,
        origin=event.origin,
        return_to=event.return_to,
        subscription_status=event.subscription_status,
        policy_resolution_ref=event.policy_resolution_ref,
        chain_depth=event.chain_depth,
        occurred_at=now,
        registry=registry,
        ledger=roadmap_ledger or InMemoryRoadmapPromotionLedger(),
        policy=roadmap_policy,
    )
    if apply_armed:
        result = apply_roadmap_promotion(
            event.summary or "",
            apply_ledger=apply_ledger or InMemoryRoadmapApplyLedger(),
            adapter=adapter,
            **common,
        )
    else:
        promote = promoter or propose_next_slice_graph
        result = promote(event.summary or "", adapter=None, **common)
    safe_result = _safe_next_slice_result(result)
    metadata = dict(decision.metadata)
    metadata["roadmap_autopromote"] = safe_result
    receipt = _with_roadmap_receipt(decision.receipt, safe_result)
    candidates = tuple(result.get("candidates") or ()) if result.get("action") in {"propose", "apply"} else ()
    return LoopDecision(
        action=decision.action,
        reason=decision.reason,
        event_id=decision.event_id,
        source_graph_id=decision.source_graph_id,
        idempotency_key=decision.idempotency_key,
        verdict=decision.verdict,
        blocker_class=decision.blocker_class,
        candidates=tuple(safe_event_payload(c) for c in candidates),
        candidate=decision.candidate,
        mutations=(),
        metadata=safe_event_payload(metadata),
        receipt=receipt,
    )


def _with_roadmap_receipt(receipt: dict[str, Any], roadmap_result: dict[str, Any]) -> dict[str, Any]:
    """Persist compact roadmap autopromote data for duplicate-event receipts.

    The loop duplicate-event fast path returns ``decision_payload`` from the
    stored receipt, so successful M15 applies must store sanitized created ids
    there. Keep it comment-shaped and compact; never embed raw summaries.
    """
    updated = dict(receipt)
    payload = dict(updated.get("decision_payload") or {})
    payload["roadmap_autopromote"] = safe_event_payload({
        "action": roadmap_result.get("action", ""),
        "reason": roadmap_result.get("reason", ""),
        "transition_id": roadmap_result.get("transition_id", ""),
        "roadmap_id": roadmap_result.get("roadmap_id", ""),
        "template_id": roadmap_result.get("template_id", ""),
        "idempotency_key": roadmap_result.get("idempotency_key", ""),
        "applied": bool(roadmap_result.get("applied", False)),
        "duplicate": bool(roadmap_result.get("duplicate", False)),
        "created_task_ids": roadmap_result.get("created_task_ids") or [],
        "receipt": roadmap_result.get("receipt") or {},
        "source_comment": _roadmap_source_comment(roadmap_result),
    })
    updated["decision_payload"] = payload
    return safe_event_payload(updated)


def _roadmap_source_comment(roadmap_result: dict[str, Any]) -> str:
    ids = roadmap_result.get("created_task_ids") or []
    transition = short_text(str(roadmap_result.get("transition_id") or ""))
    key = short_text(str(roadmap_result.get("idempotency_key") or ""))
    if roadmap_result.get("applied"):
        return short_text(f"roadmap-autopromote applied transition={transition} ids={','.join(str(x) for x in ids)} idempotency_key={key}")
    if roadmap_result.get("duplicate"):
        return short_text(f"roadmap-autopromote duplicate transition={transition} ids={','.join(str(x) for x in ids)} idempotency_key={key}")
    reason = short_text(str(roadmap_result.get("reason") or ""))
    return short_text(f"roadmap-autopromote no-create transition={transition} reason={reason} idempotency_key={key}")


def _evaluate_supersession(ev: LoopEvent, ledger: LoopLedger, policy: LoopPolicy, now: float, *, adapter: Any) -> LoopDecision:
    # Fail-closed: stale_final_fanin must be explicitly allowlisted before any candidate resolution
    if "stale_final_fanin" not in policy.allowlisted_blockers:
        return _record(ledger, _decision(ev, "escalate", "blocker_not_allowlisted", policy, now, blocker="stale_final_fanin"), ev)
    # Provenance gates before resolve_stale_final_candidate
    if not _origin_ok(ev, policy):
        return _record(ledger, _decision(ev, "escalate", "foreign_origin", policy, now, blocker="stale_final_fanin"), ev)
    if policy.require_policy_resolution and not ev.policy_resolution_ref:
        return _record(ledger, _decision(ev, "escalate", "policy_resolution_missing", policy, now, blocker="stale_final_fanin"), ev)
    if policy.require_subscription_verified and ev.subscription_status != "verified":
        return _record(ledger, _decision(ev, "escalate", "subscription_unverified", policy, now, blocker="stale_final_fanin"), ev)
    if not _supersession_provenance_ok(ev):
        return _record(ledger, _decision(ev, "escalate", "supersession_provenance_missing", policy, now, blocker="stale_final_fanin"), ev)
    if policy.active_mode == "apply" and not _apply_effectively_enabled(policy):
        return _record(ledger, _decision(ev, "escalate", "apply_disabled_by_policy", policy, now, blocker="stale_final_fanin"), ev)
    key = _final_supersession_key(ev)
    if ledger.has_idempotency_key(key):
        return _record(ledger, _decision(ev, "noop", "existing_supersession", policy, now, idempotency_key=key, blocker="stale_final_fanin"), ev)
    old_final = ev.old_final_card or {"id": ev.source_final_id, "status": "blocked"}
    review = ev.remediation_review_card or {"id": ev.remediation_review_id, "body": ev.summary or "Verdict: GO"}
    graph_policy = _graph_policy(policy, "stale_final_fanin")
    result = resolve_stale_final_candidate(old_final, review, origin=ev.origin, return_to=ev.return_to, policy=graph_policy, adapter=adapter if _apply_effectively_enabled(policy) else None)
    if not result.get("success"):
        return _record(ledger, _decision(ev, "escalate", str(result.get("error") or "supersession_failed"), policy, now, blocker="stale_final_fanin"), ev)
    final_vn = ledger.current_final_vn(ev.source_graph_id) + 1
    return _record(
        ledger,
        _decision(
            ev,
            "supersede",
            "final_vn_proposal",
            policy,
            now,
            idempotency_key=key,
            verdict="GO",
            blocker="stale_final_fanin",
            candidate=result.get("candidate"),
            mutations=tuple(result.get("mutations") or ()),
            metadata={
                "final_vn": final_vn,
                "graph_creator_result": _safe_creator_result(result),
                "round_no_derived": ledger.max_round_for_graph(ev.source_graph_id),
                "adapter_attempts": int(result.get("adapter_attempts") or 0),
            },
        ),
        ev,
    )


def _supersession_provenance_ok(event: LoopEvent) -> bool:
    """Require concrete old-final and remediation-review provenance before final-vN proposals."""
    old_final_id = event.source_final_id or str((event.old_final_card or {}).get("id") or "")
    review_id = event.remediation_review_id or str((event.remediation_review_card or {}).get("id") or "")
    return bool(_safe_ref(old_final_id, field="source_final_id") and _safe_ref(review_id, field="remediation_review_id"))


def _coerce_event(event: LoopEvent | dict[str, Any]) -> LoopEvent:
    if isinstance(event, LoopEvent):
        return event
    if not isinstance(event, dict):
        return LoopEvent(event_id="malformed_event", source_graph_id="unknown", verdict="UNKNOWN")
    allowed = {f.name for f in LoopEvent.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in event.items() if k in allowed}
    kwargs.setdefault("event_id", "malformed_event")
    kwargs.setdefault("source_graph_id", "unknown")
    return LoopEvent(**kwargs)


def _coerce_policy(policy: LoopPolicy | None) -> tuple[LoopPolicy, bool]:
    if policy is None:
        return LoopPolicy(), False
    if not isinstance(policy, LoopPolicy):
        return LoopPolicy(kill_switch=True), True
    if policy.active_mode not in _VALID_MODES:
        return LoopPolicy(kill_switch=True), True
    if not isinstance(policy.apply_enabled, bool):
        return LoopPolicy(kill_switch=True), True
    if not isinstance(policy.allowlisted_blockers, tuple) or not all(isinstance(b, str) for b in policy.allowlisted_blockers):
        return LoopPolicy(kill_switch=True), True
    if not isinstance(policy.roadmap_auto_continue, bool):
        return LoopPolicy(kill_switch=True), True
    if not isinstance(policy.roadmap_allowlisted_transitions, tuple) or not all(isinstance(t, str) for t in policy.roadmap_allowlisted_transitions):
        return LoopPolicy(kill_switch=True), True
    if not isinstance(policy.roadmap_trusted_assignees, tuple) or not all(isinstance(a, str) for a in policy.roadmap_trusted_assignees):
        return LoopPolicy(kill_switch=True), True
    if not isinstance(policy.roadmap_apply_enabled, bool):
        return LoopPolicy(kill_switch=True), True
    if any(not isinstance(v, str) for v in (policy.roadmap_impl_assignee, policy.roadmap_review_assignee, policy.roadmap_ack_trigger_agent)):
        return LoopPolicy(kill_switch=True), True
    if policy.roadmap_board_adapter_mode not in {"fake", "real"}:
        return LoopPolicy(kill_switch=True), True
    numeric_values = (
        policy.max_rounds,
        policy.max_same_blocker,
        policy.max_auto_creates_per_run,
        policy.max_tasks_per_graph,
        policy.cooldown_seconds,
        policy.roadmap_max_chain_depth,
        policy.roadmap_max_promotions_per_roadmap,
        policy.roadmap_promote_cooldown_seconds,
    )
    if any(not isinstance(v, int) or v < 0 for v in numeric_values):
        return LoopPolicy(kill_switch=True), True
    if not isinstance(policy.backoff_multiplier, (int, float)) or policy.backoff_multiplier < 1:
        return LoopPolicy(kill_switch=True), True
    return policy, False


def _origin_ok(event: LoopEvent, policy: LoopPolicy) -> bool:
    if not policy.require_origin_match:
        return True
    if policy.expected_origin and event.origin != policy.expected_origin:
        return False
    if policy.expected_return_to and event.return_to != policy.expected_return_to:
        return False
    return True


def _apply_effectively_enabled(policy: LoopPolicy) -> bool:
    return policy.active_mode == "apply" and policy.apply_enabled is True


def _graph_policy(policy: LoopPolicy, blocker: str) -> RemediationGraphPolicy:
    return RemediationGraphPolicy(
        apply_enabled=_apply_effectively_enabled(policy),
        active_mode="apply" if policy.active_mode == "apply" else policy.active_mode,
        kill_switch=policy.kill_switch,
        allowlisted_blockers=(blocker,) if blocker in policy.allowlisted_blockers else (),
        max_proposals_per_source=policy.max_tasks_per_graph,
        max_proposals_per_blocker_class=policy.max_tasks_per_graph,
        max_auto_creates_per_run=policy.max_auto_creates_per_run,
    )


def _roadmap_policy(policy: LoopPolicy) -> RoadmapPromotionPolicy:
    return RoadmapPromotionPolicy(
        auto_continue=policy.roadmap_auto_continue,
        allowlisted_transitions=policy.roadmap_allowlisted_transitions,
        max_chain_depth=policy.roadmap_max_chain_depth,
        max_promotions_per_roadmap=policy.roadmap_max_promotions_per_roadmap,
        promote_cooldown_seconds=policy.roadmap_promote_cooldown_seconds,
        require_review_edge=policy.roadmap_require_review_edge,
        require_ack_edge=policy.roadmap_require_ack_edge,
        require_trusted_assignee=policy.roadmap_require_trusted_assignee,
        trusted_assignees=policy.roadmap_trusted_assignees,
        require_origin_match=policy.require_origin_match,
        expected_origin=policy.expected_origin,
        expected_return_to=policy.expected_return_to,
        require_policy_resolution=policy.roadmap_require_policy_resolution,
        # apply is only armed when the loop apply gate is open too.
        apply_enabled=policy.roadmap_apply_enabled and _apply_effectively_enabled(policy),
        impl_assignee=policy.roadmap_impl_assignee,
        review_assignee=policy.roadmap_review_assignee,
        ack_trigger_agent=policy.roadmap_ack_trigger_agent,
    )


def _safe_next_slice_result(result: dict[str, Any]) -> dict[str, Any]:
    # M15: covers both request-only proposals and guarded apply results. The
    # receipt is the sanitized apply/propose receipt (no secrets/raw paths) so
    # the source GO decision carries a comment-shaped audit trail.
    applied = bool(result.get("applied", False))
    return safe_event_payload({
        "success": bool(result.get("success")),
        "action": str(result.get("action") or ""),
        "reason": str(result.get("reason") or ""),
        "verdict": str(result.get("verdict") or ""),
        "transition_id": str(result.get("transition_id") or ""),
        "roadmap_id": str(result.get("roadmap_id") or ""),
        "template_id": str(result.get("template_id") or ""),
        "chain_depth": int(result.get("chain_depth") or 0),
        "idempotency_key": str(result.get("idempotency_key") or ""),
        "request_only": bool(result.get("request_only", True)),
        "dry_run": bool(result.get("dry_run", not applied)),
        "applied": applied,
        "apply_enabled": bool(result.get("apply_enabled", False)),
        "duplicate": bool(result.get("duplicate", False)),
        "apply_reason": str(result.get("apply_reason") or ""),
        "candidate_count": len(result.get("candidates") or []),
        "candidates": result.get("candidates") or [],
        "created_task_ids": result.get("created_task_ids") or [],
        "tasks": result.get("tasks") or [],
        "mutations": result.get("mutations") or [],
        "receipt": result.get("receipt") or {},
        "adapter_attempts": int(result.get("adapter_attempts") or len(result.get("mutations") or [])),
    })


def _safe_creator_result(result: dict[str, Any]) -> dict[str, Any]:
    return safe_event_payload({
        "success": bool(result.get("success")),
        "dry_run": bool(result.get("dry_run", True)),
        "request_only": bool(result.get("request_only", True)),
        "error": str(result.get("error") or ""),
        "candidate_count": len(result.get("candidates") or ([] if result.get("candidate") is None else [result.get("candidate")])) ,
        "mutations": result.get("mutations") or [],
        "adapter_attempts": int(result.get("adapter_attempts") or 0),
    })


def _decision(
    event: LoopEvent,
    action: str,
    reason: str,
    policy: LoopPolicy,
    now: float,
    *,
    idempotency_key: str = "",
    verdict: str = "",
    blocker: str = "",
    candidates: tuple[dict[str, Any], ...] = (),
    candidate: dict[str, Any] | None = None,
    mutations: tuple[dict[str, Any], ...] = (),
    metadata: dict[str, Any] | None = None,
) -> LoopDecision:
    safe_event = _safe_ref(event.event_id, field="event_id") or "missing_event_id"
    safe_graph = _safe_ref(event.source_graph_id, field="source_graph_id") or "unknown_graph"
    blocker = short_text(blocker)
    key = idempotency_key or f"loop:{action}:{safe_graph}:{safe_event}"
    key = _safe_ref(key, field="idempotency_key")
    final_vn = int((metadata or {}).get("final_vn") or 1)
    receipt_round_no = int((metadata or {}).get("receipt_round_no", event.round_no) or 0)
    adapter_attempts = int((metadata or {}).get("adapter_attempts") or 0)
    applied = action in {"apply", "supersede"} and adapter_attempts > 0
    round_no_derived = int((metadata or {}).get("round_no_derived", receipt_round_no) or 0)
    full_metadata = dict(metadata or {})
    full_metadata.update({
        "applied": applied,
        "dry_run": not applied,
        "adapter_attempts": adapter_attempts,
        "noop_reason": reason if action in {"noop", "escalate"} else "",
        "idempotency_key": key,
        "round_no_derived": round_no_derived,
    })
    receipt = safe_event_payload({
        "event_id": safe_event,
        "source_task_id": _safe_ref(event.source_task_id, field="source_task_id"),
        "source_graph_id": safe_graph,
        "source_final_id": _safe_ref(event.source_final_id, field="source_final_id"),
        "blocker_class": blocker,
        "round_no": receipt_round_no,
        "same_blocker_count": 0,
        "final_vn": final_vn,
        "decision": action,
        "idempotency_key": key,
        "policy_resolution_ref": _safe_ref(event.policy_resolution_ref, field="policy_resolution_ref"),
        "origin_ref": _safe_ref(event.origin, field="origin_ref"),
        "return_to_ref": _safe_ref(event.return_to, field="return_to_ref"),
        "subscription_status": event.subscription_status if event.subscription_status in {"verified", "missing", "unverified", "not_required"} else "unverified",
        "reason": reason,
        "created_at": now,
        "decision_payload": {
            "action": action,
            "reason": reason,
            "idempotency_key": key,
            "verdict": verdict,
            "blocker_class": blocker,
        },
        "mode": policy.active_mode,
    })
    return LoopDecision(
        action=action,
        reason=reason,
        event_id=safe_event,
        source_graph_id=safe_graph,
        idempotency_key=key,
        verdict=verdict,
        blocker_class=blocker,
        candidates=tuple(safe_event_payload(c) for c in candidates),
        candidate=safe_event_payload(candidate) if isinstance(candidate, dict) else None,
        mutations=tuple(safe_event_payload(m) for m in mutations),
        metadata=safe_event_payload(full_metadata),
        receipt=receipt,
    )


def build_loop_report(decision: LoopDecision) -> dict[str, Any]:
    """Assemble a machine-readable, sanitized CLI report from a LoopDecision.

    Pure function: no I/O, no adapter/ledger access. Safe to unit test directly.
    """
    payload = decision.as_dict()
    metadata = payload.get("metadata") or {}
    candidates = payload.get("candidates") or []
    candidate = payload.get("candidate")
    report = {
        "success": True,
        "action": payload.get("action", ""),
        "verdict": payload.get("verdict", ""),
        "reason": payload.get("reason", ""),
        "dry_run": bool(metadata.get("dry_run", True)),
        "applied": bool(metadata.get("applied", False)),
        "noop_reason": metadata.get("noop_reason", ""),
        "idempotency_key": payload.get("idempotency_key", ""),
        "round_no_derived": metadata.get("round_no_derived", 0),
        "adapter_attempts": metadata.get("adapter_attempts", 0),
        "blocker_class": payload.get("blocker_class", ""),
        "source_graph_id": payload.get("source_graph_id", ""),
        "event_id": payload.get("event_id", ""),
        "proposal": {
            "candidate_count": len(candidates) if candidates else (1 if candidate else 0),
            "candidates": candidates,
            "candidate": candidate,
        },
        "mutations": payload.get("mutations", []),
        "receipt": payload.get("receipt", {}),
    }
    return safe_event_payload(report)


def _record(ledger: LoopLedger, decision: LoopDecision, event: LoopEvent) -> LoopDecision:
    ledger.record_decision(decision, event)
    return decision


def _final_supersession_key(event: LoopEvent) -> str:
    graph = _safe_ref(event.source_graph_id, field="source_graph_id") or "unknown_graph"
    old_final = _safe_ref(event.source_final_id or (event.old_final_card or {}).get("id", ""), field="source_final_id") or "old_final"
    review = _safe_ref(event.remediation_review_id or (event.remediation_review_card or {}).get("id", ""), field="remediation_review_id") or _safe_ref(event.event_id, field="event_id")
    return _safe_ref(f"loop:final-vN:{graph}:{old_final}:{review}", field="idempotency_key")


def _safe_ref(value: Any, *, field: str) -> str:
    return safe_durable_ref(value, field=field)[0]
