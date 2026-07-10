from __future__ import annotations

import pytest

from agentflow_hermes.outcome import (
    ContinuationKind,
    OutcomeEnvelope,
    OutcomeEnvelopeError,
    RequirementRef,
    Verdict,
    parse_outcome_envelope,
)


def _base(**overrides):
    kwargs = dict(
        schema_version=1,
        event_id="ev1",
        board="warroom-os",
        source_task_id="t_1",
        source_graph_id="g_1",
        verdict=Verdict.GO,
        continuation_kind=ContinuationKind.COMPLETE,
    )
    kwargs.update(overrides)
    return kwargs


def test_go_plus_code_fix_is_invalid():
    with pytest.raises(OutcomeEnvelopeError):
        OutcomeEnvelope(**_base(verdict=Verdict.GO, continuation_kind=ContinuationKind.CODE_FIX))


def test_needs_input_requires_contract_ref():
    with pytest.raises(OutcomeEnvelopeError):
        OutcomeEnvelope(
            **_base(
                verdict=Verdict.BLOCK,
                continuation_kind=ContinuationKind.NEEDS_INPUT,
                contract_ref="",
            )
        )


def test_needs_input_with_contract_ref_is_valid():
    env = OutcomeEnvelope(
        **_base(
            verdict=Verdict.BLOCK,
            continuation_kind=ContinuationKind.NEEDS_INPUT,
            contract_ref="warroom.g421.exposure-resolution.v1",
            requirements=(RequirementRef(name="approval_receipt_id", authority="owner"),),
        )
    )
    assert env.contract_ref == "warroom.g421.exposure-resolution.v1"
    assert env.requirements[0].name == "approval_receipt_id"


def test_roadmap_next_requires_next_transition():
    with pytest.raises(OutcomeEnvelopeError):
        OutcomeEnvelope(
            **_base(
                verdict=Verdict.GO,
                continuation_kind=ContinuationKind.ROADMAP_NEXT,
                next_transition="",
            )
        )


def test_missing_source_refs_invalid():
    with pytest.raises(OutcomeEnvelopeError):
        OutcomeEnvelope(**_base(event_id=""))
    with pytest.raises(OutcomeEnvelopeError):
        OutcomeEnvelope(**_base(board=""))
    with pytest.raises(OutcomeEnvelopeError):
        OutcomeEnvelope(**_base(source_task_id=""))


def test_invalid_confidence_value_rejected():
    with pytest.raises(OutcomeEnvelopeError):
        OutcomeEnvelope(**_base(confidence="made_up"))


# --- structured-metadata-first parser ---


def test_structured_metadata_takes_precedence_over_summary_text():
    run_metadata = {
        "agentflow_outcome": {
            "schema_version": 1,
            "verdict": "BLOCK",
            "continuation_kind": "needs_input",
            "contract_ref": "warroom.g421.exposure-resolution.v1",
            "required_inputs": [
                {"name": "approval_receipt_id", "authority": "owner"},
                {"name": "resolution_basis", "authority": "owner"},
            ],
            "resume_transition": "warroom.g421.packet-rerun",
        }
    }
    env = parse_outcome_envelope(
        run_metadata=run_metadata,
        summary="Verdict: GO — everything is fine",  # text says something else entirely
        event_id="ev1",
        board="warroom-os",
        source_task_id="t_ab93a206",
        source_graph_id="g_1",
    )
    assert env.verdict == Verdict.BLOCK
    assert env.continuation_kind == ContinuationKind.NEEDS_INPUT
    assert env.contract_ref == "warroom.g421.exposure-resolution.v1"
    assert env.confidence == "structured"
    assert [r.name for r in env.requirements] == ["approval_receipt_id", "resolution_basis"]


def test_malformed_structured_metadata_does_not_fall_back_to_text():
    run_metadata = {"agentflow_outcome": {"schema_version": 1, "verdict": "NOT_A_VERDICT"}}
    env = parse_outcome_envelope(
        run_metadata=run_metadata,
        summary="Verdict: GO",
        event_id="ev1",
        board="warroom-os",
        source_task_id="t_1",
        source_graph_id="g_1",
    )
    assert env.continuation_kind == ContinuationKind.UNKNOWN
    assert env.confidence == "none"


def test_text_fallback_needs_input_requires_explicit_markers():
    summary = (
        "Verdict: BLOCK\n"
        "Outcome-Kind: needs_input\n"
        "Continuation-Contract: warroom.g421.exposure-resolution.v1\n"
        "Next action: operator must confirm the resolution basis and provide approval receipt id."
    )
    env = parse_outcome_envelope(
        run_metadata=None,
        summary=summary,
        event_id="ev2",
        board="warroom-os",
        source_task_id="t_ab93a206",
        source_graph_id="g_1",
    )
    assert env.verdict == Verdict.BLOCK
    assert env.continuation_kind == ContinuationKind.NEEDS_INPUT
    assert env.contract_ref == "warroom.g421.exposure-resolution.v1"
    assert env.confidence == "text_explicit"


def test_text_fallback_vague_prose_is_unknown_and_non_mutating():
    env = parse_outcome_envelope(
        run_metadata=None,
        summary="Verdict: BLOCK — something seems off, needs a look maybe.",
        event_id="ev3",
        board="warroom-os",
        source_task_id="t_1",
        source_graph_id="g_1",
    )
    assert env.continuation_kind == ContinuationKind.UNKNOWN
    assert env.confidence == "none"


def test_text_fallback_never_claims_structured_confidence():
    env = parse_outcome_envelope(
        run_metadata=None,
        summary="Verdict: BLOCK — stale_inline_route detected",
        event_id="ev4",
        board="warroom-os",
        source_task_id="t_1",
        source_graph_id="g_1",
    )
    assert env.confidence != "structured"


def test_text_fallback_known_blocker_classifies_code_fix():
    env = parse_outcome_envelope(
        run_metadata=None,
        summary="Verdict: BLOCK — stale_inline_route detected",
        event_id="ev5",
        board="warroom-os",
        source_task_id="t_1",
        source_graph_id="g_1",
    )
    assert env.verdict == Verdict.BLOCK
    assert env.continuation_kind == ContinuationKind.CODE_FIX
    assert env.confidence == "text_explicit"


def test_existing_verdict_parser_compatibility_preserved():
    from agentflow_hermes.remediation import parse_verdict_summary

    parsed = parse_verdict_summary("Verdict: NEED_MORE")
    assert parsed.verdict == "NEED_MORE"
