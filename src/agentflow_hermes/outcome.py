"""Structured outcome model, separate from Verdict.

``Verdict`` is a quality judgement (GO/BLOCK/NEED_MORE). ``ContinuationKind``
is the orthogonal question of what durable state transition happens next.
Structured Kanban run metadata is the authority source; regex text-marker
parsing is a backward-compatible fallback only, and can never claim
``confidence="structured"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .remediation import parse_verdict_summary

# "deterministic_grammar" and "model_compiled" are additive confidence levels
# used by outcome_compiler.py's stage 2/3 (see that module's docstring); they
# never claim "structured" since only real agentflow_outcome metadata may.
_CONFIDENCE_LEVELS = {"structured", "text_explicit", "deterministic_grammar", "model_compiled", "none"}

# Versioned default contract for an explicit needs_input outcome that carries
# no domain-specific contract_ref. A domain contract (e.g.
# ``warroom.g421.exposure-resolution.v1``) always overrides this because it
# supplies its own contract_ref; this is only the generic fallback.
GENERIC_OWNER_INPUT_CONTRACT = "generic.owner-input.v1"

_OUTCOME_KIND_RE = re.compile(r"\bOutcome-Kind\s*:\s*([a-z_]+)", re.IGNORECASE)
_CONTINUATION_CONTRACT_RE = re.compile(r"\bContinuation-Contract\s*:\s*([\w.\-]+)", re.IGNORECASE)
_OPERATOR_MUST_RE = re.compile(
    r"\boperator\s+must\s+(?:provide|approve|confirm)\b", re.IGNORECASE
)


class Verdict(str, Enum):
    GO = "GO"
    BLOCK = "BLOCK"
    NEED_MORE = "NEED_MORE"
    UNKNOWN = "UNKNOWN"


class ContinuationKind(str, Enum):
    COMPLETE = "complete"
    ROADMAP_NEXT = "roadmap_next"
    CODE_FIX = "code_fix"
    NEEDS_INPUT = "needs_input"
    APPROVAL_REQUIRED = "approval_required"
    EXTERNAL_WAIT = "external_wait"
    UNKNOWN = "unknown"


class OutcomeEnvelopeError(ValueError):
    """Raised when an OutcomeEnvelope violates a structural invariant."""


@dataclass(frozen=True)
class RequirementRef:
    name: str
    authority: str = "owner"


@dataclass(frozen=True)
class OutcomeEnvelope:
    schema_version: int
    event_id: str
    board: str
    source_task_id: str
    source_graph_id: str
    verdict: Verdict
    continuation_kind: ContinuationKind
    contract_ref: str = ""
    origin_ref: str = ""
    return_to_ref: str = ""
    workspace_ref: str = ""
    assignee: str = ""
    occurred_at: float = 0.0
    requirements: tuple[RequirementRef, ...] = ()
    next_transition: str = ""
    confidence: str = "structured"

    def __post_init__(self) -> None:
        if not self.event_id:
            raise OutcomeEnvelopeError("event_id is required")
        if not self.board:
            raise OutcomeEnvelopeError("board is required")
        if not self.source_task_id:
            raise OutcomeEnvelopeError("source_task_id is required")
        if not self.source_graph_id:
            raise OutcomeEnvelopeError("source_graph_id is required")
        if self.confidence not in _CONFIDENCE_LEVELS:
            raise OutcomeEnvelopeError(f"invalid confidence: {self.confidence!r}")
        if self.verdict == Verdict.GO and self.continuation_kind == ContinuationKind.CODE_FIX:
            raise OutcomeEnvelopeError("GO + code_fix is not a valid continuation")
        if self.continuation_kind == ContinuationKind.NEEDS_INPUT and not self.contract_ref:
            raise OutcomeEnvelopeError("needs_input requires contract_ref")
        if self.continuation_kind == ContinuationKind.ROADMAP_NEXT and not self.next_transition:
            raise OutcomeEnvelopeError("roadmap_next requires next_transition")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "board": self.board,
            "source_task_id": self.source_task_id,
            "source_graph_id": self.source_graph_id,
            "verdict": self.verdict.value,
            "continuation_kind": self.continuation_kind.value,
            "contract_ref": self.contract_ref,
            "origin_ref": self.origin_ref,
            "return_to_ref": self.return_to_ref,
            "workspace_ref": self.workspace_ref,
            "assignee": self.assignee,
            "occurred_at": self.occurred_at,
            "requirements": [{"name": r.name, "authority": r.authority} for r in self.requirements],
            "next_transition": self.next_transition,
            "confidence": self.confidence,
        }


def parse_outcome_envelope(
    *,
    run_metadata: dict[str, Any] | None,
    summary: str,
    event_id: str,
    board: str,
    source_task_id: str,
    source_graph_id: str,
    origin_ref: str = "",
    return_to_ref: str = "",
    workspace_ref: str = "",
    assignee: str = "",
    occurred_at: float = 0.0,
) -> OutcomeEnvelope:
    """Classify an outcome. Structured run metadata is authoritative first;
    explicit text markers are a fallback second. Vague/malformed input always
    routes to ``ContinuationKind.UNKNOWN`` with ``confidence="none"`` rather
    than guessing or falling through to a different authority source.
    """
    common = dict(
        event_id=event_id,
        board=board,
        source_task_id=source_task_id,
        source_graph_id=source_graph_id,
        origin_ref=origin_ref,
        return_to_ref=return_to_ref,
        workspace_ref=workspace_ref,
        assignee=assignee,
        occurred_at=occurred_at,
    )

    structured = _extract_structured(run_metadata)
    if structured is not None:
        envelope = _from_structured(structured, **common)
        if envelope is not None:
            return envelope
        return _unknown_envelope(**common)

    return _from_text_fallback(summary, **common)


def _extract_structured(run_metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(run_metadata, dict):
        return None
    block = run_metadata.get("agentflow_outcome")
    return block if isinstance(block, dict) else None


def _from_structured(block: dict[str, Any], **common: Any) -> OutcomeEnvelope | None:
    if block.get("schema_version") != 1:
        return None
    try:
        verdict = Verdict(str(block.get("verdict") or ""))
        continuation_kind = ContinuationKind(str(block.get("continuation_kind") or ""))
    except ValueError:
        return None

    requirements: list[RequirementRef] = []
    for item in block.get("required_inputs") or ():
        if isinstance(item, dict) and item.get("name"):
            requirements.append(RequirementRef(name=str(item["name"]), authority=str(item.get("authority") or "owner")))
        elif isinstance(item, str):
            requirements.append(RequirementRef(name=item, authority="owner"))

    contract_ref = str(block.get("contract_ref") or "")
    if continuation_kind == ContinuationKind.NEEDS_INPUT and not contract_ref:
        contract_ref = GENERIC_OWNER_INPUT_CONTRACT

    try:
        return OutcomeEnvelope(
            schema_version=1,
            verdict=verdict,
            continuation_kind=continuation_kind,
            contract_ref=contract_ref,
            requirements=tuple(requirements),
            next_transition=str(block.get("resume_transition") or block.get("next_transition") or ""),
            confidence="structured",
            **common,
        )
    except OutcomeEnvelopeError:
        return None


def _from_text_fallback(summary: str, **common: Any) -> OutcomeEnvelope:
    text = summary or ""
    parsed = parse_verdict_summary(text)
    try:
        verdict = Verdict(parsed.verdict)
    except ValueError:
        verdict = Verdict.UNKNOWN

    kind_match = _OUTCOME_KIND_RE.search(text)
    contract_match = _CONTINUATION_CONTRACT_RE.search(text)

    if kind_match:
        try:
            continuation_kind = ContinuationKind(kind_match.group(1).lower())
        except ValueError:
            continuation_kind = None
        # An explicit Outcome-Kind marker still needs an explicit contract for
        # kinds other than needs_input; needs_input without a domain contract
        # falls back to the versioned generic owner-input contract.
        contract_ref = contract_match.group(1) if contract_match else ""
        if continuation_kind == ContinuationKind.NEEDS_INPUT and not contract_ref:
            contract_ref = GENERIC_OWNER_INPUT_CONTRACT
        if continuation_kind is not None and (contract_match or continuation_kind == ContinuationKind.NEEDS_INPUT):
            try:
                return OutcomeEnvelope(
                    schema_version=1,
                    verdict=verdict,
                    continuation_kind=continuation_kind,
                    contract_ref=contract_ref,
                    confidence="text_explicit",
                    **common,
                )
            except OutcomeEnvelopeError:
                return _unknown_envelope(**common)

    if verdict in (Verdict.BLOCK, Verdict.NEED_MORE) and _OPERATOR_MUST_RE.search(text):
        return OutcomeEnvelope(
            schema_version=1,
            verdict=verdict,
            continuation_kind=ContinuationKind.NEEDS_INPUT,
            contract_ref=contract_match.group(1) if contract_match else GENERIC_OWNER_INPUT_CONTRACT,
            confidence="text_explicit",
            **common,
        )

    if verdict == Verdict.BLOCK and parsed.blockers:
        return OutcomeEnvelope(
            schema_version=1,
            verdict=verdict,
            continuation_kind=ContinuationKind.CODE_FIX,
            confidence="text_explicit",
            **common,
        )

    if verdict == Verdict.GO:
        return OutcomeEnvelope(
            schema_version=1,
            verdict=verdict,
            continuation_kind=ContinuationKind.COMPLETE,
            confidence="text_explicit",
            **common,
        )

    return _unknown_envelope(verdict=verdict, **common)


def _unknown_envelope(*, verdict: Verdict = Verdict.UNKNOWN, **common: Any) -> OutcomeEnvelope:
    return OutcomeEnvelope(
        schema_version=1,
        verdict=verdict,
        continuation_kind=ContinuationKind.UNKNOWN,
        confidence="none",
        **common,
    )
