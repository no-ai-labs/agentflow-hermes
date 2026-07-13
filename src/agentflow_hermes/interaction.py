"""Interaction Inbox (plan section 6): batches compatible needs_input owner
questions by origin endpoint + owner identity + project/graph + non-
conflicting authority class within a short coalescing window, composes one
concise human-facing question per batch, and tracks ``question_count`` for
H0/H1/H2 human-effort classification (plan section "Human Effort Budget").

``ContinuationStore`` (see its "interaction inbox" section) owns dumb
persistence for ``interaction_cases``/``interaction_members``/
``inbound_reply_receipts``; this module owns the batching/composition/
classification business logic on top of it — the same split already used by
``requirement_resolver.py`` (logic) over ``requirement_satisfactions`` (store).
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .continuation_store import ContinuationStore
from .requirements import Requirement, RequirementKind

DEFAULT_COALESCE_WINDOW_SECONDS = 10.0

STATE_COLLECTING = "collecting"
STATE_ASKED = "asked"
STATE_ANSWERED = "answered"
STATE_APPLIED = "applied"
STATE_NEEDS_CLARIFICATION = "needs_clarification"


@dataclass(frozen=True)
class InteractionCase:
    id: str
    origin_endpoint: str
    continuation_ids: tuple[int, ...]
    unresolved_fields: tuple[Requirement, ...]
    state: str
    batch_key: str
    question_count: int
    created_at: float
    asked_at: float | None = None


def _default_id_factory() -> str:
    return f"ic_{uuid.uuid4().hex[:12]}"


def make_batch_key(*, origin_endpoint: str, owner_ref: str, project_scope: str, authority_class: str) -> str:
    """Compatible requests batch only when endpoint, owner identity, project/
    graph scope, AND authority class all match (plan 6.2) — a live-money
    approval never coalesces with an unrelated docs URL request even if they
    share an endpoint/owner."""
    return "|".join((origin_endpoint, owner_ref, project_scope, authority_class))


def requirement_to_dict(requirement: Requirement) -> dict[str, Any]:
    return {
        "name": requirement.name,
        "kind": requirement.kind.value,
        "authority": requirement.authority,
        "question": requirement.question,
        "answer_hint": requirement.answer_hint,
        "allowed_values": list(requirement.allowed_values),
        "required": requirement.required,
        "description": requirement.description,
    }


def requirement_from_dict(payload: dict[str, Any]) -> Requirement:
    return Requirement(
        name=str(payload["name"]),
        kind=RequirementKind(str(payload.get("kind") or "fact")),
        authority=str(payload.get("authority") or "owner"),
        question=str(payload.get("question") or ""),
        answer_hint=str(payload.get("answer_hint") or ""),
        allowed_values=tuple(payload.get("allowed_values") or ()),
        required=bool(payload.get("required", True)),
        description=str(payload.get("description") or ""),
    )


def classify_effort(question_count: int) -> str:
    """H0/H1/H2/H3+ classification driven purely by question_count (see the
    plan's Human Effort Budget): H0 means fully auto-resolved (a case with a
    nonzero question_count is by definition never H0)."""
    if question_count <= 0:
        return "H0"
    if question_count == 1:
        return "H1"
    if question_count == 2:
        return "H2"
    return "H3+"


def compose_question(
    case: InteractionCase,
    *,
    resolved_summary: tuple[str, ...] = (),
    resume_summary: str = "",
) -> str:
    """Render the concise owner-facing question (plan 6.3). Must answer
    exactly: what's blocked, what's already resolved, what exact input
    remains, and what happens after the reply — nothing else. No task ids
    unless needed for disambiguation (only surfaced here as a 1-based
    position number when more than one field is outstanding), and no
    contract_ref/receipt syntax."""
    fields = case.unresolved_fields
    if len(fields) > 1:
        remaining = "; ".join(
            f"{i + 1}) {f.question or f.name.replace('_', ' ')}" for i, f in enumerate(fields)
        )
    elif fields:
        remaining = fields[0].question or fields[0].name.replace("_", " ")
    else:
        remaining = "the missing input"

    parts = [f"Blocked on: {remaining}."]
    if resolved_summary:
        parts.append("Already resolved automatically: " + "; ".join(resolved_summary) + ".")
    parts.append(f"Reply with: {remaining}.")
    parts.append(resume_summary or "I'll resume automatically once you reply.")
    return " ".join(parts)


@dataclass
class InteractionInbox:
    """Batching/composition/reply-application coordinator over a
    ``ContinuationStore``. ``clock``/``id_factory`` are injectable seams so
    tests never need to sleep for the coalescing window."""

    store: ContinuationStore
    window_seconds: float = DEFAULT_COALESCE_WINDOW_SECONDS
    clock: Callable[[], float] = time.time
    id_factory: Callable[[], str] = _default_id_factory

    def open_or_batch_case(
        self,
        *,
        origin_endpoint: str,
        owner_ref: str,
        project_scope: str,
        authority_class: str,
        continuation_id: int,
        unresolved_fields: tuple[Requirement, ...],
    ) -> InteractionCase:
        """Create a new collecting case, or fold ``continuation_id`` into a
        still-``collecting`` compatible case opened within the coalescing
        window (plan 6.2's "3 reviewer needs_input events -> one owner
        message" example)."""
        batch_key = make_batch_key(
            origin_endpoint=origin_endpoint,
            owner_ref=owner_ref,
            project_scope=project_scope,
            authority_class=authority_class,
        )
        now = self.clock()
        existing = self._find_open_batch(batch_key, now)
        requirement_dicts = [requirement_to_dict(r) for r in unresolved_fields]

        if existing is not None:
            case_id = existing["id"]
        else:
            case_id = self.id_factory()
            self.store.create_interaction_case(
                id=case_id, endpoint=origin_endpoint, batch_key=batch_key, state=STATE_COLLECTING, created_at=now
            )

        self.store.add_interaction_member(case_id, continuation_id=continuation_id, requirements=requirement_dicts)
        case = self.get_case(case_id)
        assert case is not None
        return case

    def _find_open_batch(self, batch_key: str, now: float) -> dict[str, Any] | None:
        for row in self.store.list_interaction_cases(state=STATE_COLLECTING):
            if row["batch_key"] != batch_key:
                continue
            if now - row["created_at"] <= self.window_seconds:
                return row
        return None

    def get_case(self, case_id: str) -> InteractionCase | None:
        row = self.store.get_interaction_case(case_id)
        if row is None:
            return None
        members = self.store.list_interaction_members(case_id)
        continuation_ids = tuple(m["continuation_id"] for m in members)
        unresolved: list[Requirement] = []
        for member in members:
            for item in member["requirements"]:
                unresolved.append(requirement_from_dict(item))
        return InteractionCase(
            id=row["id"],
            origin_endpoint=row["endpoint"],
            continuation_ids=continuation_ids,
            unresolved_fields=tuple(unresolved),
            state=row["state"],
            batch_key=row["batch_key"],
            question_count=int(row["question_count"]),
            created_at=row["created_at"],
            asked_at=row["asked_at"],
        )

    def list_cases(self, *, endpoint: str | None = None, state: str | None = None) -> tuple[InteractionCase, ...]:
        rows = self.store.list_interaction_cases(state=state, endpoint=endpoint)
        cases = (self.get_case(row["id"]) for row in rows)
        return tuple(c for c in cases if c is not None)

    def mark_asked(self, case_id: str) -> InteractionCase:
        row = self.store.get_interaction_case(case_id)
        assert row is not None
        new_count = int(row["question_count"]) + 1
        self.store.update_interaction_case(case_id, state=STATE_ASKED, question_count=new_count, asked_at=self.clock())
        case = self.get_case(case_id)
        assert case is not None
        return case

    def mark_needs_clarification(self, case_id: str) -> InteractionCase:
        self.store.update_interaction_case(case_id, state=STATE_NEEDS_CLARIFICATION)
        case = self.get_case(case_id)
        assert case is not None
        return case

    def mark_answered(self, case_id: str) -> InteractionCase:
        self.store.update_interaction_case(case_id, state=STATE_ANSWERED, answered_at=self.clock())
        case = self.get_case(case_id)
        assert case is not None
        return case

    def mark_applied(self, case_id: str) -> InteractionCase:
        self.store.update_interaction_case(case_id, state=STATE_APPLIED, applied_at=self.clock())
        case = self.get_case(case_id)
        assert case is not None
        return case

    def record_inbound_reply(
        self, case_id: str, *, message_ref: str, raw_text: str, compile_result: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Record reply provenance without ever persisting ``raw_text``
        itself — only its content hash plus the message ref and the already-
        validated typed compile result (plan 6/7.2)."""
        content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        return self.store.record_inbound_reply_receipt(
            case_id, message_ref=message_ref, content_sha256=content_hash, compile_result=compile_result or {}
        )

    def apply_fields(
        self,
        case_id: str,
        *,
        fields_by_continuation: dict[int, dict[str, Any]],
        source: str = "current_owner_reply",
        source_ref: str = "",
    ) -> InteractionCase:
        """Apply already contract-validated typed fields to every
        continuation this reply covers. A single accepted reply can satisfy N
        continuation_ids when the batch was joint (plan 6.2)."""
        for continuation_id, fields in fields_by_continuation.items():
            for field_name, value in fields.items():
                self.store.record_requirement_satisfaction(
                    continuation_id,
                    field_name=field_name,
                    value=value,
                    source_kind=source,
                    source_ref=source_ref,
                )
        return self.mark_answered(case_id)
