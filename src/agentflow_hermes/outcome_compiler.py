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
from .remediation import parse_verdict_summary
from .requirements import Requirement, RequirementKind

COMPILE_STAGE_STRUCTURED = "structured_metadata"
COMPILE_STAGE_FLAT = "flat_reviewer_metadata"
COMPILE_STAGE_DETERMINISTIC = "deterministic_grammar"
COMPILE_STAGE_MODEL = "model_compiled"
COMPILE_STAGE_UNRESOLVED = "unresolved"

_STAGE_CONFIDENCE = {
    COMPILE_STAGE_STRUCTURED: 1.0,
    COMPILE_STAGE_FLAT: 0.95,
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
    # Concrete blocker labels carried through for CODE_FIX materialization
    # (plan/M30A item 2). Populated from authoritative flat reviewer metadata
    # ``blockers`` or from the summary's named blockers; for a SEMANTIC_REFUSAL
    # this carries the offending unsafe blocker(s) so the durable refusal ACK
    # can record exactly what was refused. Empty for every other kind.
    blockers: tuple[str, ...] = ()
    # Unsafe categories that forced a SEMANTIC_REFUSAL (plan/M30A remediation).
    # Empty for every safe/auto-appliable outcome; non-empty iff
    # ``continuation_kind == SEMANTIC_REFUSAL``.
    refusal_categories: tuple[str, ...] = ()


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

_BLOCKER_FIELD_RE = re.compile(
    r"^\s*(?:Blockers?|Blocking findings?|Failure|Issue|Reason)\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$", re.MULTILINE)
_OWNER_EXTERNAL_RE = re.compile(
    r"\b(owner|operator|human|approval|approve|confirm|input|credentials?|external[_ -]?wait|waiting\s+for|pending\s+owner)\b",
    re.IGNORECASE,
)
_NO_BLOCKER_RE = re.compile(r"\b(none|no blockers?|n/?a|not applicable)\b", re.IGNORECASE)

# Unsafe blocker categories (plan/M30A remediation). These describe *categories*
# a reviewer BLOCK/NEED_MORE must never be auto-remediated into a CODE_FIX for —
# they are not a fixed "safe-blocker vocabulary" allowlist and carry no magic
# sentinel: a blocker matching any of these fails closed to a SEMANTIC_REFUSAL,
# preserving typed authoritative metadata (which is classified before this ever
# runs). Anything matching none of them stays eligible for the normal
# concrete-code-blocker CODE_FIX path.
_UNSAFE_CATEGORY_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "credentials_secrets",
        re.compile(
            r"\b(credentials?|secret|secrets|api[_ -]?key|access[_ -]?key|"
            r"private[_ -]?key|ssh[_ -]?key|password|passphrase|token|oauth|"
            r"\.env\b|env\s+var(?:iable)?s?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "destructive_data_loss",
        re.compile(
            r"\b(destructive|data[_ -]?loss|irreversible|unrecoverable|"
            r"delete|deletion|drop|truncate|wipe|purge|destroy|"
            r"rm\s+-rf|drop\s+table)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "owner_user_proof",
        re.compile(
            # owner/user/human/operator *together with* an approval/proof/input
            # ask, or the standalone owner-only / sign-off / attestation asks.
            # Requires the pairing so a bare "user cannot log in" code bug does
            # not get refused — only genuine human-proof/input gates do.
            r"\b(?:owner|operator|human|user)\b[^\n]*?"
            r"\b(?:approv\w*|sign[- ]?off|proof|attest\w*|input|confirm\w*|"
            r"decision|authoriz\w*|permission)\b"
            r"|\bowner[- ]?only\b"
            r"|\b(?:sign[- ]?off|attestation)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "live_money_financial",
        re.compile(
            r"\b(money|monetary|financial|finance|funds?|payment|payout|pay[- ]?out|"
            r"invoice|billing|charge|refund|wire[- ]?transfer|live[- ]?money|"
            r"purchase|transaction)\b|\$\d",
            re.IGNORECASE,
        ),
    ),
)


def _unsafe_blocker_categories(candidates: Any) -> tuple[str, ...]:
    """Return the sorted, de-duplicated unsafe categories present across every
    candidate blocker phrase. Empty when none are unsafe."""
    found: list[str] = []
    for candidate in candidates:
        text = str(candidate)
        for name, pattern in _UNSAFE_CATEGORY_RES:
            if name not in found and pattern.search(text):
                found.append(name)
    return tuple(sorted(found))


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

    # Authoritative flat reviewer metadata (``{verdict, blockers}`` with no
    # ``agentflow_outcome`` envelope). This is the M30A t_89 incident shape:
    # a done event whose run metadata is flat and whose summary says
    # ``Verdict: BLOCK``. A concrete blocker deterministically becomes a
    # CODE_FIX; owner-only/approval verdicts with no blocker stay typed and
    # fall through (fail closed) rather than being coerced into a code fix.
    flat = _compile_flat_reviewer(run_metadata, summary, **common)
    if flat is not None:
        envelope, blockers = flat
        refusal_categories: tuple[str, ...] = ()
        if envelope.continuation_kind == ContinuationKind.SEMANTIC_REFUSAL:
            refusal_categories = _unsafe_blocker_categories(blockers)
        return CompiledOutcome(
            envelope=envelope,
            stage=COMPILE_STAGE_FLAT,
            confidence=_STAGE_CONFIDENCE[COMPILE_STAGE_FLAT],
            blockers=blockers,
            refusal_categories=refusal_categories,
        )

    deterministic = _compile_deterministic(summary, **common)
    if deterministic is not None:
        envelope, requirements = deterministic
        blockers: tuple[str, ...] = ()
        refusal_categories = ()
        if envelope.continuation_kind == ContinuationKind.CODE_FIX:
            blockers = tuple(parse_verdict_summary(summary or "").blockers)
            if not blockers:
                blockers = _generic_blockers_from_text(summary or "")
        elif envelope.continuation_kind == ContinuationKind.SEMANTIC_REFUSAL:
            candidates = _blocker_candidates_from_text(summary or "")
            refusal_categories = _unsafe_blocker_categories(candidates)
            # Carry the offending unsafe blockers through for the durable ACK.
            blockers = tuple(c for c in candidates if _unsafe_blocker_categories((c,)))
        return CompiledOutcome(
            envelope=envelope,
            stage=COMPILE_STAGE_DETERMINISTIC,
            confidence=_STAGE_CONFIDENCE[COMPILE_STAGE_DETERMINISTIC],
            requirements=requirements,
            blockers=blockers,
            refusal_categories=refusal_categories,
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


def _extract_flat_blockers(run_metadata: dict[str, Any] | None) -> tuple[str, ...] | None:
    """Return the top-level ``blockers`` list from flat reviewer metadata, or
    ``None`` when the metadata is not flat reviewer metadata (missing a
    top-level ``verdict``, or carrying a real ``agentflow_outcome`` envelope
    which is handled by the authoritative structured stage instead)."""
    if not isinstance(run_metadata, dict):
        return None
    if "verdict" not in run_metadata or isinstance(run_metadata.get("agentflow_outcome"), dict):
        return None
    raw = run_metadata.get("blockers")
    if isinstance(raw, (list, tuple)):
        return tuple(str(b).strip() for b in raw if str(b).strip())
    if isinstance(raw, str) and raw.strip():
        return (raw.strip(),)
    return ()


def _compile_flat_reviewer(
    run_metadata: dict[str, Any] | None, summary: str, **common: Any
) -> tuple[OutcomeEnvelope, tuple[str, ...]] | None:
    """Normalize authoritative flat reviewer metadata into the typed envelope.

    A flat ``{verdict: 'BLOCK'|'NEED_MORE', blockers: [...]}`` with at least one
    concrete blocker becomes a CODE_FIX; a flat ``{verdict: 'GO'}`` becomes a
    complete/continue outcome. Every other flat shape (a BLOCK/NEED_MORE with no
    concrete blocker — i.e. owner-only/approval/external-wait territory) returns
    ``None`` so it stays typed and falls through to the deterministic summary
    grammar or unresolved fail-closed path. Uses no fixed four-class blocker
    vocabulary and no magic sentinel text — the metadata's own ``verdict`` and
    ``blockers`` are authoritative."""
    blockers = _extract_flat_blockers(run_metadata)
    if blockers is None:
        return None
    assert isinstance(run_metadata, dict)
    try:
        verdict = Verdict(str(run_metadata.get("verdict") or "").upper())
    except ValueError:
        return None

    if verdict in (Verdict.BLOCK, Verdict.NEED_MORE) and blockers:
        # Fail closed: an unsafe blocker category (credentials, destructive,
        # owner/user proof, live-money) is never auto-remediated — it compiles
        # to an explicit SEMANTIC_REFUSAL instead of a CODE_FIX, carrying the
        # offending blockers through for the durable refusal ACK.
        kind = (
            ContinuationKind.SEMANTIC_REFUSAL
            if _unsafe_blocker_categories(blockers)
            else ContinuationKind.CODE_FIX
        )
        try:
            envelope = OutcomeEnvelope(
                schema_version=1,
                verdict=verdict,
                continuation_kind=kind,
                confidence="flat_metadata",
                **common,
            )
        except OutcomeEnvelopeError:
            return None
        return envelope, blockers

    if verdict == Verdict.GO:
        try:
            envelope = OutcomeEnvelope(
                schema_version=1,
                verdict=verdict,
                continuation_kind=ContinuationKind.COMPLETE,
                confidence="flat_metadata",
                **common,
            )
        except OutcomeEnvelopeError:
            return None
        return envelope, ()

    return None


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

    generic_code_fix = _compile_generic_blocker_prose(text, **common)
    if generic_code_fix is not None:
        return generic_code_fix

    return _compile_natural_prose(text, **common)


def _compile_generic_blocker_prose(
    text: str, **common: Any
) -> tuple[OutcomeEnvelope, tuple[Requirement, ...]] | None:
    """Compile explicit natural BLOCK/NEED_MORE prose with blocker evidence
    without a fixed blocker vocabulary or magic sentinel.

    The grammar requires an explicit blocker/issue/failure field or bullets.
    An unsafe blocker category (credentials, destructive, owner/user proof,
    live-money) fails closed to an explicit SEMANTIC_REFUSAL; otherwise a
    concrete code blocker becomes a CODE_FIX. Prose with neither stays out of
    this stage so it can fall through to the owner-input grammar.
    """
    parsed = parse_verdict_summary(text)
    try:
        verdict = Verdict(parsed.verdict)
    except ValueError:
        return None
    if verdict not in (Verdict.BLOCK, Verdict.NEED_MORE):
        return None

    candidates = _blocker_candidates_from_text(text)
    if _unsafe_blocker_categories(candidates):
        try:
            envelope = OutcomeEnvelope(
                schema_version=1,
                verdict=verdict,
                continuation_kind=ContinuationKind.SEMANTIC_REFUSAL,
                confidence="deterministic_grammar",
                **common,
            )
        except OutcomeEnvelopeError:
            return None
        return envelope, ()

    blockers = tuple(c for c in candidates if _is_concrete_code_blocker(c))
    if not blockers:
        return None
    try:
        envelope = OutcomeEnvelope(
            schema_version=1,
            verdict=verdict,
            continuation_kind=ContinuationKind.CODE_FIX,
            confidence="deterministic_grammar",
            **common,
        )
    except OutcomeEnvelopeError:
        return None
    return envelope, ()


def _blocker_candidates_from_text(text: str) -> tuple[str, ...]:
    """Every candidate blocker phrase from explicit blocker/issue/failure
    fields or bullets under a bare ``Blockers:`` heading — unfiltered, so the
    unsafe-category gate can inspect owner/credential/destructive phrases that
    ``_is_concrete_code_blocker`` would otherwise silently drop."""
    candidates: list[str] = []
    for match in _BLOCKER_FIELD_RE.finditer(text or ""):
        candidate = match.group(1).strip()
        if candidate and not (_NO_BLOCKER_RE.fullmatch(candidate) or _NO_BLOCKER_RE.search(candidate)):
            candidates.append(candidate)
    if not candidates and re.search(r"^\s*Blockers?\s*:\s*$", text or "", re.IGNORECASE | re.MULTILINE):
        for match in _BULLET_RE.finditer(text or ""):
            candidate = match.group(1).strip()
            if candidate and not (_NO_BLOCKER_RE.fullmatch(candidate) or _NO_BLOCKER_RE.search(candidate)):
                candidates.append(candidate)
    return tuple(dict.fromkeys(candidates))


def _generic_blockers_from_text(text: str) -> tuple[str, ...]:
    return tuple(c for c in _blocker_candidates_from_text(text) if _is_concrete_code_blocker(c))


def _is_concrete_code_blocker(candidate: str) -> bool:
    candidate = candidate.strip().strip(".;")
    if len(candidate) < 8:
        return False
    if _NO_BLOCKER_RE.fullmatch(candidate) or _NO_BLOCKER_RE.search(candidate):
        return False
    if _OWNER_EXTERNAL_RE.search(candidate):
        return False
    return True


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
