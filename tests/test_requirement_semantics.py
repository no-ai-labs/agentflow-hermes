from __future__ import annotations

from pathlib import Path

from agentflow_hermes.continuation_config import load_input_contract
from agentflow_hermes.input_contract import FieldAuthority, InputContract, InputField
from agentflow_hermes.requirements import (
    POLICY_INELIGIBLE_KINDS,
    Contradiction,
    Requirement,
    RequirementKind,
    ResolutionResult,
    SatisfactionSource,
    SatisfiedRequirement,
)

_GENERIC_YAML = Path(__file__).resolve().parents[1] / "contracts" / "generic.owner-input.v1.yaml"


def test_requirement_kind_enum_values():
    assert {k.value for k in RequirementKind} == {
        "fact",
        "evidence",
        "preference",
        "authorization",
        "correction",
    }


def test_fact_and_evidence_are_policy_ineligible():
    assert RequirementKind.FACT in POLICY_INELIGIBLE_KINDS
    assert RequirementKind.EVIDENCE in POLICY_INELIGIBLE_KINDS
    assert RequirementKind.PREFERENCE not in POLICY_INELIGIBLE_KINDS
    assert RequirementKind.AUTHORIZATION not in POLICY_INELIGIBLE_KINDS


def test_input_field_defaults_preserve_existing_authority_model():
    # No kind/question/answer_hint supplied — behaves exactly like before.
    field = InputField(name="owner_confirmation", value_type="boolean", authority=FieldAuthority.OWNER)
    assert field.kind == RequirementKind.FACT
    assert field.question == ""
    assert field.answer_hint == ""


def test_input_field_carries_natural_language_question_and_kind():
    field = InputField(
        name="result_url",
        value_type="text",
        authority=FieldAuthority.OWNER,
        kind=RequirementKind.FACT,
        question="검증 결과 URL을 알려줘",
        answer_hint="a URL",
    )
    assert field.kind == RequirementKind.FACT
    assert field.question == "검증 결과 URL을 알려줘"
    assert field.answer_hint == "a URL"


def test_dynamic_owner_input_builds_fields_from_requirements():
    requirements = (
        Requirement(name="result_url", kind=RequirementKind.FACT, question="URL을 알려줘"),
        Requirement(
            name="release_choice",
            kind=RequirementKind.PREFERENCE,
            allowed_values=("A", "B"),
            question="A 또는 B?",
        ),
    )
    contract = InputContract.dynamic_owner_input(
        contract_ref="generic.owner-input.v1",
        owner_role="board-owner",
        resume_transition="generic.owner-input.resume",
        requirements=requirements,
    )
    names = {f.name for f in contract.fields}
    assert names == {"result_url", "release_choice"}
    result_url_field = contract.field("result_url")
    assert result_url_field.kind == RequirementKind.FACT
    assert result_url_field.question == "URL을 알려줘"
    assert result_url_field.authority == FieldAuthority.OWNER
    release_choice = contract.field("release_choice")
    assert release_choice.allowed_values == ("A", "B")


def test_dynamic_owner_input_is_not_fixed_to_approve_boolean_pair():
    # The old shape was always exactly owner_decision + owner_confirmation.
    # A single-requirement outcome must produce a single dynamic field, proving
    # the contract is templated rather than fixed.
    requirements = (Requirement(name="artifact_id", kind=RequirementKind.EVIDENCE, question="artifact id?"),)
    contract = InputContract.dynamic_owner_input(
        contract_ref="generic.owner-input.v1",
        owner_role="board-owner",
        resume_transition="generic.owner-input.resume",
        requirements=requirements,
    )
    assert [f.name for f in contract.fields] == ["artifact_id"]


def test_generic_contract_yaml_still_loads_and_declares_dynamic_fields():
    payload_contract = load_input_contract(_GENERIC_YAML)
    assert payload_contract.contract_ref == "generic.owner-input.v1"
    # Fallback static fields remain loadable for outcomes with no typed
    # requirements at all.
    names = {f.name for f in payload_contract.fields}
    assert names == {"owner_decision", "owner_confirmation"}
    raw_text = _GENERIC_YAML.read_text(encoding="utf-8")
    assert "dynamic_fields: true" in raw_text


def test_resolution_result_satisfied_values_and_h0():
    requirement = Requirement(name="result_url", kind=RequirementKind.FACT)
    satisfied = SatisfiedRequirement(
        requirement=requirement, value="https://x", source=SatisfactionSource.SYSTEM_DERIVED
    )
    result = ResolutionResult(satisfied=(satisfied,), interaction_needed=False)
    assert result.satisfied_values() == {"result_url": "https://x"}
    assert result.is_h0() is True


def test_resolution_result_not_h0_when_unresolved_remains():
    requirement = Requirement(name="result_url", kind=RequirementKind.FACT)
    result = ResolutionResult(unresolved=(requirement,), interaction_needed=True)
    assert result.is_h0() is False


def test_resolution_result_not_h0_with_contradiction():
    result = ResolutionResult(
        contradictions=(Contradiction(name="result_url", reason="conflicting_values"),),
        interaction_needed=False,
    )
    assert result.is_h0() is False
