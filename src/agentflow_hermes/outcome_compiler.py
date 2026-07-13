"""Three-stage outcome compilation pipeline (plan section 5).

    structured agentflow_outcome metadata
            v absent
    deterministic natural-summary grammar
            v ambiguous
    injectable bounded model compile-to-schema
            v
    OutcomeEnvelope validation

Stage 1 reuses ``outcome.py``'s structured-metadata parsing verbatim — it
remains the sole source of ``confidence="structured"``.

Stage 2 is a dependency-free, regex/keyword grammar. It subsumes the existing
explicit-marker/known-blocker/GO fallback already in ``outcome.py`` *and* adds
recognition of natural reviewer prose that carries no ``Outcome-Kind`` marker
at all, e.g. "BLOCK pending the owner's result URL" compiles to a
``needs_input`` outcome with a FACT requirement named ``result_url`` — no
marker ceremony required. This is the behavioral requirement from plan
section 13.3; it must work without stage 3.

Stage 3 is an injectable, bounded "model compile-to-schema" callable. The
project ships zero runtime dependencies and has no LLM API wiring, so the
default implementation (``default_model_compiler``) is a deterministic no-op
that always declines — it never fabricates a candidate. A real bounded model
compiler can be injected later via the ``ModelCompiler`` protocol; per plan
5.4 it may only ever emit a schema-validated candidate ``OutcomeEnvelope`` for
reversible ``needs_input`` owner-anchor creation — it can never itself
generate an owner receipt, artifact proof, or authorization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from .outcome import (
    GENERIC_OWNER_INPUT_CONTRACT,
    ContinuationKind,
    OutcomeEnvelope,
    OutcomeEnvelopeError,
    RequirementRef,
    Verdict,
    _extract_structured,
    _from_structured,
    _from_text_fallback,
    _unknown_envelope,
)
from .requirements import Requirement, RequirementKind

COMPILE_STAGE_STRUCTURED = "structured_metadata"
COMPILE_STAGE_DETERMINISTIC = "deterministic_grammar"
COMPILE_STAGE_MODEL = "model_compiled"
COMPILE_STAGE_UNRESOLVED = "unresolved"

_STAGE_CONFIDENCE = {
    COMPILE_STAGE_STRUCTURED: 1.0,
    COMPILE_STAGE_DETERMINISTIC: 0.9,
    COMPILE_STAGE_MODEL: 0.5,
    COMPILE_STAGE_UNRESOLVED: 0.0,
}


@dataclass(frozen=True)
class CompiledOutcome:
    """Validated OutcomeEnvelope plus provenance: which pipeline stage
    resolved it, and a numeric confidence score (plan section 5.3)."""

    envelope: OutcomeEnvelope
    stage: str
    confidence: float
    requirements: tuple[Requirement, ...] = ()


class ModelCompiler(Protocol):
    """Bounded compile-to-schema callable. Input is intentionally narrow
    (plan 5.2): title, latest run summary, terminal event kind, assignee,
    and known contract refs — never full chat transcripts or secrets. Must
    return either ``None`` (decline) or a dict shaped like the schema in
    plan 5.3 (``verdict``/``continuation_kind``/``required_inputs``/...)."""

    def __call__(
        self,
        *,
        title: str,
        summary: str,
        event_kind: str,
        assignee: str,
        known_contract_refs: tuple[str, ...],
    ) -> dict[str, Any] | None: ...


def default_model_compiler(
    *,
    title: str,
    summary: str,
    event_kind: str,
    assignee: str,
    known_contract_refs: tuple[str, ...],
) -> dict[str, Any] | None:
    """Deterministic no-op stub. Always declines — the project has zero
    runtime dependencies and no network/LLM wiring exists yet. Inject a real
    bounded model compiler with this same signature to enable stage 3."""
    return None


_BARE_VERDICT_RE = re.compile(r"^\s*(?:Verdict\s*:\s*)?(GO|BLOCK|NEED_MORE)\b", re.IGNORECASE)

# Natural prose without an Outcome-Kind marker: "BLOCK pending the owner's
# result URL" / "NEED_MORE ... pending owner's approval id.". Captures the
# noun phrase naming the missing field, stopping at sentence punctuation.
_PENDING_OWNER_RE = re.compile(
    r"pending\s+(?:the\s+)?owner'?s?\s+([a-zA-Z][a-zA-Z0-9 _-]*?)(?=[.,;\n]|$)",
    re.IGNORECASE,
)


def _slugify_field_name(phrase: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", phrase.strip().lower()).strip("_")
    return slug


def compile_outcome(
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
    title: str = "",
    event_kind: str = "",
    known_contract_refs: tuple[str, ...] = (),
    model_compiler: ModelCompiler = default_model_compiler,
) -> CompiledOutcome:
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
            requirements = _requirements_from_envelope(envelope, structured)
            return CompiledOutcome(
                envelope=envelope,
                stage=COMPILE_STAGE_STRUCTURED,
                confidence=_STAGE_CONFIDENCE[COMPILE_STAGE_STRUCTURED],
                requirements=requirements,
            )
        unknown = _unknown_envelope(**common)
        return CompiledOutcome(
            envelope=unknown, stage=COMPILE_STAGE_UNRESOLVED, confidence=_STAGE_CONFIDENCE[COMPILE_STAGE_UNRESOLVED]
        )

    deterministic = _compile_deterministic(summary, **common)
    if deterministic is not None:
        envelope, requirements = deterministic
        return CompiledOutcome(
            envelope=envelope,
            stage=COMPILE_STAGE_DETERMINISTIC,
            confidence=_STAGE_CONFIDENCE[COMPILE_STAGE_DETERMINISTIC],
            requirements=requirements,
        )

    model_candidate = model_compiler(
        title=title,
        summary=summary,
        event_kind=event_kind,
        assignee=assignee,
        known_contract_refs=known_contract_refs,
    )
    if model_candidate is not None:
        compiled = _compile_model_candidate(model_candidate, **common)
        if compiled is not None:
            envelope, requirements = compiled
            return CompiledOutcome(
                envelope=envelope,
                stage=COMPILE_STAGE_MODEL,
                confidence=float(model_candidate.get("confidence") or _STAGE_CONFIDENCE[COMPILE_STAGE_MODEL]),
                requirements=requirements,
            )

    unknown = _unknown_envelope(**common)
    return CompiledOutcome(
        envelope=unknown, stage=COMPILE_STAGE_UNRESOLVED, confidence=_STAGE_CONFIDENCE[COMPILE_STAGE_UNRESOLVED]
    )


def _requirements_from_envelope(
    envelope: OutcomeEnvelope, structured: dict[str, Any] | None = None
) -> tuple[Requirement, ...]:
    kind_by_name: dict[str, RequirementKind] = {}
    question_by_name: dict[str, str] = {}
    if structured is not None:
        for item in structured.get("required_inputs") or ():
            if isinstance(item, dict) and item.get("name"):
                name = str(item["name"])
                raw_kind = str(item.get("kind") or "fact")
                try:
                    kind_by_name[name] = RequirementKind(raw_kind)
                except ValueError:
                    kind_by_name[name] = RequirementKind.FACT
                question_by_name[name] = str(item.get("question") or "")
    return tuple(
        Requirement(
            name=r.name,
            kind=kind_by_name.get(r.name, RequirementKind.FACT),
            authority=r.authority,
            question=question_by_name.get(r.name, ""),
        )
        for r in envelope.requirements
    )


def _compile_deterministic(
    summary: str, **common: Any
) -> tuple[OutcomeEnvelope, tuple[Requirement, ...]] | None:
    text = summary or ""

    # Existing marker/known-blocker/GO deterministic parser already covers a
    # large surface (Outcome-Kind marker, "operator must ..." phrase, known
    # blocker names, bare GO). Reuse it verbatim rather than duplicating it.
    fallback = _from_text_fallback(text, **common)
    if fallback.continuation_kind != ContinuationKind.UNKNOWN:
        return fallback, _requirements_from_envelope(fallback)

    return _compile_natural_prose(text, **common)


def _compile_natural_prose(text: str, **common: Any) -> tuple[OutcomeEnvelope, tuple[Requirement, ...]] | None:
    """Deterministic grammar for natural reviewer prose that names a missing
    owner field without any Outcome-Kind marker, e.g. "BLOCK pending the
    owner's result URL"."""
    verdict_match = _BARE_VERDICT_RE.search(text)
    if not verdict_match:
        return None
    try:
        verdict = Verdict(verdict_match.group(1).upper())
    except ValueError:
        return None
    if verdict not in (Verdict.BLOCK, Verdict.NEED_MORE):
        return None

    field_match = _PENDING_OWNER_RE.search(text)
    if not field_match:
        return None
    field_name = _slugify_field_name(field_match.group(1))
    if not field_name:
        return None

    requirement = Requirement(
        name=field_name,
        kind=RequirementKind.FACT,
        authority="owner",
        question=f"{field_name.replace('_', ' ')}?",
    )
    try:
        envelope = OutcomeEnvelope(
            schema_version=1,
            verdict=verdict,
            continuation_kind=ContinuationKind.NEEDS_INPUT,
            contract_ref=GENERIC_OWNER_INPUT_CONTRACT,
            requirements=(RequirementRef(name=field_name, authority="owner"),),
            confidence="deterministic_grammar",
            **common,
        )
    except OutcomeEnvelopeError:
        return None
    return envelope, (requirement,)


def _compile_model_candidate(
    candidate: dict[str, Any], **common: Any
) -> tuple[OutcomeEnvelope, tuple[Requirement, ...]] | None:
    """Validate a stage-3 model candidate against the same schema/invariants
    every other stage must satisfy (plan 5.4: the model cannot bypass
    OutcomeEnvelope validation, and can only ever land on ``needs_input``)."""
    try:
        verdict = Verdict(str(candidate.get("verdict") or ""))
        continuation_kind = ContinuationKind(str(candidate.get("continuation_kind") or ""))
    except ValueError:
        return None
    if continuation_kind != ContinuationKind.NEEDS_INPUT:
        # A model candidate may only ever propose a reversible needs_input
        # owner-anchor; it cannot itself claim GO/complete/roadmap_next/etc.
        return None

    requirements: list[Requirement] = []
    requirement_refs: list[RequirementRef] = []
    for item in candidate.get("required_inputs") or ():
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        try:
            kind = RequirementKind(str(item.get("kind") or "fact"))
        except ValueError:
            kind = RequirementKind.FACT
        requirements.append(
            Requirement(
                name=name,
                kind=kind,
                authority=str(item.get("authority") or "owner"),
                question=str(item.get("question") or ""),
            )
        )
        requirement_refs.append(RequirementRef(name=name, authority=str(item.get("authority") or "owner")))

    contract_ref = str(candidate.get("contract_ref") or "") or GENERIC_OWNER_INPUT_CONTRACT
    try:
        envelope = OutcomeEnvelope(
            schema_version=1,
            verdict=verdict,
            continuation_kind=continuation_kind,
            contract_ref=contract_ref,
            requirements=tuple(requirement_refs),
            next_transition=str(candidate.get("resume_transition") or ""),
            confidence="model_compiled",
            **common,
        )
    except OutcomeEnvelopeError:
        return None
    return envelope, tuple(requirements)
