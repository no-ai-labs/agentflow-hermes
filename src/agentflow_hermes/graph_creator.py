from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Protocol

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
    active_mode: str = "request_only"  # "request_only" | "observe_only" | "apply"
    kill_switch: bool = False
    allowlisted_blockers: tuple[str, ...] = ()
    max_proposals_per_source: int = 3
    max_proposals_per_blocker_class: int = 2
    max_auto_creates_per_run: int = 5


_FAIL_CLOSED_POLICY = RemediationGraphPolicy(kill_switch=True)


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
    parent_key: str = ""  # idempotency key of the preceding task in the sequence

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
            "parent_key": self.parent_key,
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
        self.tasks: list[dict[str, Any]] = []
        self.links: list[dict[str, str]] = []
        self.subscriptions: list[dict[str, str]] = []
        self._proposals: list[dict[str, Any]] = []

    def create_graph(self, intent: GraphIntentCandidate) -> dict[str, Any]:
        self.create_calls.append(intent)
        # Deterministic (no clock/random) task id so duplicate applies resolve to
        # the same recorded id and never create a second board card.
        task_id = "task:" + hashlib.sha256(intent.idempotency_key.encode()).hexdigest()[:12]
        task: dict[str, Any] = {
            "task_id": task_id,
            "idempotency_key": intent.idempotency_key,
            "blocker": intent.blocker,
            "kind": intent.kind,
            "origin": intent.origin,
            "return_to": intent.return_to,
            "assignee": (intent.metadata or {}).get("assignee", ""),
            "acceptance_criteria": (intent.metadata or {}).get("acceptance_criteria", ""),
            "ack_trigger_agent": (intent.metadata or {}).get("ack_trigger_agent", ""),
            "subscription_required": intent.subscription_required,
            "notify_subscribe_required": intent.subscription_required,
            "supersedes": intent.supersedes,
        }
        self.tasks.append(task)
        self._proposals.append({
            "task_id": task_id,
            "idempotency_key": intent.idempotency_key,
            "blocker": intent.blocker,
            "kind": intent.kind,
        })
        if intent.parent_key:
            self.links.append({"from": intent.parent_key, "to": intent.idempotency_key})
        if intent.subscription_required:
            self.subscriptions.append({
                "task_key": intent.idempotency_key,
                "return_to": intent.return_to,
                "requirement": "notify-subscribe",
            })
        return {
            "success": True,
            "action": "fake_noop",
            "task_id": task_id,
            "idempotency_key": intent.idempotency_key,
            "mutations": [],
        }

    def list_existing(self, idempotency_key: str) -> list[dict[str, Any]]:
        return [
            p for p in self._proposals
            if p.get("idempotency_key") == idempotency_key
            or str(p.get("idempotency_key") or "").startswith(f"{idempotency_key}:")
        ]


class RealKanbanBoardClient(Protocol):
    """Minimal same-board client protocol for real roadmap graph writes."""

    def create_task(self, **kwargs: Any) -> dict[str, Any]: ...


class RealKanbanGraphAdapter:
    """Apply roadmap graph intents to a real same-board Kanban client.

    This adapter is only constructed by explicit apply-mode configuration. It
    preserves idempotency, carries template-derived assignees/body/acceptance,
    links tasks in graph order, and can comment a sanitized receipt on the
    source task. It has no live-send, active-wake, restart, or cross-board API.
    """

    def __init__(
        self,
        client: RealKanbanBoardClient,
        *,
        board: str = "",
        source_task_id: str = "",
        dispatch_created_impl: bool = False,
    ) -> None:
        self.client = client
        self.board = short_text(board)
        self.source_task_id = short_text(source_task_id)
        self.dispatch_created_impl = dispatch_created_impl
        self.create_calls: list[GraphIntentCandidate] = []
        self._key_to_task_id: dict[str, str] = {}

    def list_existing(self, idempotency_key: str) -> list[dict[str, Any]]:
        safe_key = short_text(idempotency_key)
        if not safe_key:
            return []
        finder = getattr(self.client, "find_tasks_by_idempotency_key", None)
        if callable(finder):
            result = finder(safe_key, board=self.board)
            rows = result if isinstance(result, list) else []
            return [dict(x) for x in rows if isinstance(x, dict)]
        lister = getattr(self.client, "list_tasks", None)
        if callable(lister):
            result = lister(board=self.board, idempotency_key=safe_key)
            rows = result if isinstance(result, list) else []
            return [dict(x) for x in rows if isinstance(x, dict)]
        return []

    def create_graph(self, intent: GraphIntentCandidate) -> dict[str, Any]:
        self.create_calls.append(intent)
        existing = self.list_existing(intent.idempotency_key)
        if existing:
            task_id = _task_id_from_result(existing[0])
            if task_id:
                self._key_to_task_id[intent.idempotency_key] = task_id
                return {"success": True, "action": "existing_task", "task_id": task_id, "idempotency_key": intent.idempotency_key, "mutations": []}

        parent_task_id = self._key_to_task_id.get(intent.parent_key, "") if intent.parent_key else ""
        parent_ids = [parent_task_id] if parent_task_id else []
        metadata = dict(intent.metadata or {})
        payload = safe_event_payload({
            "board": self.board,
            "title": intent.title,
            "assignee": metadata.get("assignee", ""),
            "body": intent.body,
            "parents": parent_ids,
            "idempotency_key": intent.idempotency_key,
            "origin": intent.origin,
            "return_to": intent.return_to,
            "acceptance_criteria": metadata.get("acceptance_criteria", ""),
            "ack_trigger_agent": metadata.get("ack_trigger_agent", ""),
            "initial_status": "ready",
        })
        try:
            result = self.client.create_task(**payload)
        except TypeError:
            result = self.client.create_task(payload)  # type: ignore[misc]
        if not isinstance(result, dict):
            return {"success": False, "error": "client_create_failed", "mutations": []}
        task_id = _task_id_from_result(result)
        if not task_id:
            return {"success": False, "error": "client_missing_task_id", "mutations": []}
        self._key_to_task_id[intent.idempotency_key] = task_id
        if parent_task_id:
            linker = getattr(self.client, "link_tasks", None)
            if callable(linker):
                linker(parent_id=parent_task_id, child_id=task_id, board=self.board)
        return {"success": True, "action": "real_create", "task_id": task_id, "idempotency_key": intent.idempotency_key, "mutations": [{"op": "create_task", "task_id": task_id}]}

    def record_source_receipt(self, receipt: dict[str, Any]) -> None:
        if not self.source_task_id:
            return
        commenter = getattr(self.client, "comment_task", None)
        if not callable(commenter):
            return
        ids = ",".join(str(x) for x in (receipt.get("created_task_ids") or []))
        body = short_text(
            "roadmap-autopromote applied "
            f"transition={receipt.get('transition_id') or ''} "
            f"template={receipt.get('template_id') or ''} "
            f"ids={ids} idempotency_key={receipt.get('idempotency_key') or ''}",
            max_len=480,
        )
        try:
            commenter(task_id=self.source_task_id, body=body, board=self.board)
        except TypeError:
            commenter(self.source_task_id, body)


def _task_id_from_result(result: dict[str, Any]) -> str:
    return short_text(str(result.get("task_id") or result.get("id") or result.get("task") or ""))


def _coerce_policy(policy: RemediationGraphPolicy | None) -> RemediationGraphPolicy:
    """Return a policy object, failing closed for malformed caller input."""
    if policy is None:
        return RemediationGraphPolicy()
    if not isinstance(policy, RemediationGraphPolicy):
        return _FAIL_CLOSED_POLICY
    if policy.active_mode not in {"request_only", "observe_only", "apply"}:
        return _FAIL_CLOSED_POLICY
    if not isinstance(policy.allowlisted_blockers, tuple):
        return _FAIL_CLOSED_POLICY
    if not all(isinstance(b, str) for b in policy.allowlisted_blockers):
        return _FAIL_CLOSED_POLICY
    for cap in (
        policy.max_proposals_per_source,
        policy.max_proposals_per_blocker_class,
        policy.max_auto_creates_per_run,
    ):
        if not isinstance(cap, int) or cap < 0:
            return _FAIL_CLOSED_POLICY
    return policy


def _is_apply_gated(policy: RemediationGraphPolicy, blocker: str) -> bool:
    """Return True only when all safety gates pass for a gated auto-create."""
    try:
        return (
            policy.apply_enabled is True
            and policy.active_mode == "apply"
            and not policy.kill_switch
            and bool(blocker)
            and blocker in policy.allowlisted_blockers
            and policy.max_auto_creates_per_run > 0
        )
    except Exception:
        return False


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
    effective_policy = _coerce_policy(policy)
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
    auto_create_attempts = 0

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

        prev_idem: str = ""
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

            auto_create_gated = adapter is not None and _is_apply_gated(effective_policy, blocker)
            if auto_create_gated and auto_create_attempts >= effective_policy.max_auto_creates_per_run:
                candidates.append(_noop(idem, blocker, "max_auto_creates_per_run"))
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
                parent_key=prev_idem,
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
            prev_idem = idem

            # Gated auto-create: all safety gates must pass. The cap is an
            # attempt budget, so failed/transient adapter calls consume it too.
            if auto_create_gated and adapter is not None:
                auto_create_attempts += 1
                adapter.create_graph(intent)

    return {
        "success": True,
        "dry_run": True,
        "request_only": True,
        "candidates": candidates,
        "mutations": [],
        "adapter_attempts": auto_create_attempts,
    }


def propose_next_slice_graph(
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
    registry: Any = None,
    ledger: Any = None,
    policy: Any = None,
    adapter: Any = None,
) -> dict[str, Any]:
    """Request-only next-slice roadmap graph proposal surface.

    Exposes the M14a roadmap-GO autopromoter through graph_creator without
    adding a board-writer/apply boundary. ``adapter`` is intentionally ignored:
    this function returns proposed graph JSON only, with no mutations.
    """

    from .roadmap import propose_roadmap_promotion

    result = propose_roadmap_promotion(
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
    return safe_event_payload({**result, "mutations": [], "adapter_attempts": 0})


def apply_remediation_graph(
    intent: GraphIntentCandidate,
    *,
    policy: RemediationGraphPolicy | None = None,
    adapter: FakeKanbanGraphAdapter | KanbanGraphAdapter | None = None,
) -> dict[str, Any]:
    """Apply a graph intent via the adapter. Fails closed unless every apply gate passes."""
    effective_policy = _coerce_policy(policy)
    if not _is_apply_gated(effective_policy, intent.blocker):
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
    adapter: "FakeKanbanGraphAdapter | KanbanGraphAdapter | None" = None,
) -> dict[str, Any]:
    """Generate a final-v2 candidate if old final is done/BLOCK and review has GO verdict.

    Never re-runs a completed final. Returns a request-only candidate only.
    """
    effective_policy = _coerce_policy(policy)
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

    # Gated auto-create for the final-v2 supersession intent.
    adapter_attempts = 0
    if adapter is not None and _is_apply_gated(effective_policy, "stale_final_fanin"):
        adapter_attempts += 1
        adapter.create_graph(intent)

    result: dict[str, Any] = {
        "success": True,
        "dry_run": True,
        "request_only": True,
        "candidate": intent.as_dict(),
        "mutations": [],
        "adapter_attempts": adapter_attempts,
    }

    return result


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
