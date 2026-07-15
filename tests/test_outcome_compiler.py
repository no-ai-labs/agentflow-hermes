from __future__ import annotations

from agentflow_hermes.outcome import ContinuationKind, Verdict
from agentflow_hermes.outcome_compiler import (
    COMPILE_STAGE_DETERMINISTIC,
    COMPILE_STAGE_FLAT,
    COMPILE_STAGE_MODEL,
    COMPILE_STAGE_STRUCTURED,
    COMPILE_STAGE_UNRESOLVED,
    compile_outcome,
    default_model_compiler,
)
from agentflow_hermes.requirements import RequirementKind


def _common(**overrides):
    kwargs = dict(
        event_id="ev1",
        board="warroom-os",
        source_task_id="t_1",
        source_graph_id="g_1",
    )
    kwargs.update(overrides)
    return kwargs


def test_structured_metadata_stage_wins_and_is_full_confidence():
    run_metadata = {
        "agentflow_outcome": {
            "schema_version": 1,
            "verdict": "BLOCK",
            "continuation_kind": "needs_input",
            "contract_ref": "warroom.g421.exposure-resolution.v1",
            "required_inputs": [{"name": "approval_receipt_id", "authority": "owner", "kind": "authorization"}],
        }
    }
    compiled = compile_outcome(run_metadata=run_metadata, summary="Verdict: GO", **_common())
    assert compiled.stage == COMPILE_STAGE_STRUCTURED
    assert compiled.confidence == 1.0
    assert compiled.envelope.confidence == "structured"
    assert compiled.envelope.continuation_kind == ContinuationKind.NEEDS_INPUT
    assert compiled.requirements[0].name == "approval_receipt_id"
    assert compiled.requirements[0].kind == RequirementKind.AUTHORIZATION


def test_malformed_structured_metadata_is_unresolved_no_fallback():
    run_metadata = {"agentflow_outcome": {"schema_version": 1, "verdict": "NOT_A_VERDICT"}}
    compiled = compile_outcome(run_metadata=run_metadata, summary="Verdict: GO", **_common())
    assert compiled.stage == COMPILE_STAGE_UNRESOLVED
    assert compiled.envelope.continuation_kind == ContinuationKind.UNKNOWN


def test_flat_reviewer_metadata_block_with_blockers_compiles_to_code_fix():
    """M30A item 1 / the t_89e3c71f incident: a done event whose run metadata
    is flat ``{verdict:'BLOCK', blockers:[...]}`` with no ``agentflow_outcome``
    envelope and whose summary only says ``Verdict: BLOCK`` must deterministically
    become CODE_FIX carrying the concrete blockers — not silently advance."""
    compiled = compile_outcome(
        run_metadata={"verdict": "BLOCK", "blockers": ["packet rerun url missing", "stale review edge"]},
        summary="Verdict: BLOCK",
        **_common(),
    )
    assert compiled.stage == COMPILE_STAGE_FLAT
    assert compiled.confidence == 0.95
    assert compiled.envelope.verdict == Verdict.BLOCK
    assert compiled.envelope.continuation_kind == ContinuationKind.CODE_FIX
    assert compiled.envelope.confidence == "flat_metadata"
    assert compiled.blockers == ("packet rerun url missing", "stale review edge")


def test_flat_reviewer_metadata_need_more_variant_also_code_fix():
    compiled = compile_outcome(
        run_metadata={"verdict": "NEED_MORE", "blockers": "single blocker string"},
        summary="",
        **_common(),
    )
    assert compiled.stage == COMPILE_STAGE_FLAT
    assert compiled.envelope.continuation_kind == ContinuationKind.CODE_FIX
    assert compiled.blockers == ("single blocker string",)


def test_flat_reviewer_metadata_block_without_blocker_stays_typed_fail_closed():
    """Owner-only/approval/external-wait territory: a flat BLOCK with no concrete
    blocker must NOT be coerced into a code fix. With a bare ``Verdict: BLOCK``
    summary and no named blocker it stays unresolved (fail closed)."""
    compiled = compile_outcome(
        run_metadata={"verdict": "BLOCK", "blockers": []},
        summary="Verdict: BLOCK",
        **_common(),
    )
    assert compiled.stage == COMPILE_STAGE_UNRESOLVED
    assert compiled.envelope.continuation_kind == ContinuationKind.UNKNOWN
    assert compiled.blockers == ()


def test_flat_reviewer_metadata_go_compiles_to_complete():
    compiled = compile_outcome(
        run_metadata={"verdict": "GO"},
        summary="",
        **_common(),
    )
    assert compiled.stage == COMPILE_STAGE_FLAT
    assert compiled.envelope.continuation_kind == ContinuationKind.COMPLETE


def test_natural_block_with_generic_concrete_blocker_compiles_to_code_fix_without_vocab():
    compiled = compile_outcome(
        run_metadata=None,
        summary="Verdict: BLOCK\nBlockers: packet rerun URL missing from final acknowledgement",
        **_common(),
    )
    assert compiled.stage == COMPILE_STAGE_DETERMINISTIC
    assert compiled.envelope.verdict == Verdict.BLOCK
    assert compiled.envelope.continuation_kind == ContinuationKind.CODE_FIX
    assert compiled.blockers == ("packet rerun URL missing from final acknowledgement",)


def test_owner_approval_blocker_heading_stays_fail_closed_not_code_fix():
    compiled = compile_outcome(
        run_metadata=None,
        summary="Verdict: BLOCK\nBlockers: owner approval is required before continuing",
        **_common(),
    )
    assert compiled.stage == COMPILE_STAGE_UNRESOLVED
    assert compiled.envelope.continuation_kind == ContinuationKind.UNKNOWN


def test_agentflow_outcome_envelope_still_wins_over_flat_sibling_keys():
    """A real ``agentflow_outcome`` block is authoritative even if flat sibling
    ``verdict``/``blockers`` keys also happen to be present."""
    compiled = compile_outcome(
        run_metadata={
            "verdict": "BLOCK",
            "blockers": ["ignore me"],
            "agentflow_outcome": {
                "schema_version": 1,
                "verdict": "BLOCK",
                "continuation_kind": "needs_input",
                "contract_ref": "warroom.g421.exposure-resolution.v1",
                "required_inputs": [{"name": "approval_receipt_id", "authority": "owner"}],
            },
        },
        summary="",
        **_common(),
    )
    assert compiled.stage == COMPILE_STAGE_STRUCTURED
    assert compiled.envelope.continuation_kind == ContinuationKind.NEEDS_INPUT
    assert compiled.blockers == ()


def test_summary_named_blocker_code_fix_carries_blockers():
    compiled = compile_outcome(
        run_metadata=None,
        summary="Verdict: BLOCK — stale_inline_route detected",
        **_common(),
    )
    assert compiled.envelope.continuation_kind == ContinuationKind.CODE_FIX
    assert "stale_inline_route" in compiled.blockers


def test_natural_prose_without_marker_compiles_to_needs_input_fact_requirement():
    """The core M27 behavioral requirement (plan 13.3): a reviewer writing
    natural prose with no Outcome-Kind marker still compiles deterministically
    — no stub-LLM stage 3 involvement required."""
    compiled = compile_outcome(
        run_metadata=None,
        summary="BLOCK pending the owner's result URL.",
        **_common(),
    )
    assert compiled.stage == COMPILE_STAGE_DETERMINISTIC
    assert compiled.envelope.verdict == Verdict.BLOCK
    assert compiled.envelope.continuation_kind == ContinuationKind.NEEDS_INPUT
    assert compiled.envelope.contract_ref == "generic.owner-input.v1"
    assert compiled.envelope.confidence == "deterministic_grammar"
    assert len(compiled.requirements) == 1
    requirement = compiled.requirements[0]
    assert requirement.name == "result_url"
    assert requirement.kind == RequirementKind.FACT


def test_natural_prose_need_more_variant_also_compiles():
    compiled = compile_outcome(
        run_metadata=None,
        summary="NEED_MORE, pending the owner's approval id",
        **_common(),
    )
    assert compiled.stage == COMPILE_STAGE_DETERMINISTIC
    assert compiled.envelope.verdict == Verdict.NEED_MORE
    assert compiled.requirements[0].name == "approval_id"


def test_explicit_marker_case_still_compiles_through_deterministic_stage():
    summary = (
        "Verdict: BLOCK\n"
        "Outcome-Kind: needs_input\n"
        "Continuation-Contract: warroom.g421.exposure-resolution.v1\n"
        "Next action: operator must confirm the resolution basis and provide approval receipt id."
    )
    compiled = compile_outcome(run_metadata=None, summary=summary, **_common())
    assert compiled.stage == COMPILE_STAGE_DETERMINISTIC
    assert compiled.envelope.continuation_kind == ContinuationKind.NEEDS_INPUT
    assert compiled.envelope.contract_ref == "warroom.g421.exposure-resolution.v1"


def test_default_model_compiler_is_a_deterministic_noop():
    assert default_model_compiler(
        title="t", summary="s", event_kind="blocked", assignee="a", known_contract_refs=()
    ) is None


def test_vague_prose_without_model_compiler_stays_unresolved():
    compiled = compile_outcome(
        run_metadata=None,
        summary="Verdict: BLOCK — something seems off, needs a look maybe.",
        **_common(),
    )
    assert compiled.stage == COMPILE_STAGE_UNRESOLVED
    assert compiled.envelope.continuation_kind == ContinuationKind.UNKNOWN


def test_injectable_model_compiler_resolves_ambiguous_case_to_needs_input():
    def stub_compiler(*, title, summary, event_kind, assignee, known_contract_refs):
        return {
            "verdict": "BLOCK",
            "continuation_kind": "needs_input",
            "required_inputs": [{"name": "result_url", "kind": "fact", "authority": "owner", "question": "url?"}],
            "resume_transition": "retry-review",
            "confidence": 0.8,
        }

    compiled = compile_outcome(
        run_metadata=None,
        summary="Verdict: BLOCK — something seems off, needs a look maybe.",
        model_compiler=stub_compiler,
        **_common(),
    )
    assert compiled.stage == COMPILE_STAGE_MODEL
    assert compiled.confidence == 0.8
    assert compiled.envelope.continuation_kind == ContinuationKind.NEEDS_INPUT
    assert compiled.envelope.confidence == "model_compiled"
    assert compiled.requirements[0].name == "result_url"


def test_model_compiler_cannot_claim_go_verdict_directly():
    """Plan 5.4: the model may only ever propose a reversible needs_input
    owner-anchor; it can never itself assert GO/complete."""

    def stub_compiler(*, title, summary, event_kind, assignee, known_contract_refs):
        return {"verdict": "GO", "continuation_kind": "complete"}

    compiled = compile_outcome(
        run_metadata=None,
        summary="Verdict: BLOCK — something seems off, needs a look maybe.",
        model_compiler=stub_compiler,
        **_common(),
    )
    assert compiled.stage == COMPILE_STAGE_UNRESOLVED
    assert compiled.envelope.continuation_kind == ContinuationKind.UNKNOWN
