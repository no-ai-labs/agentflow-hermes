from __future__ import annotations

from pathlib import Path

import pytest

from agentflow_hermes.input_contract import (
    ArtifactSpec,
    FieldAuthority,
    InputContract,
    InputField,
)
from agentflow_hermes.continuation_config import (
    UnknownContractError,
    load_contract_registry,
    load_input_contract,
)

_G421_YAML = Path(__file__).resolve().parents[1] / "contracts" / "warroom.g421.exposure-resolution.v1.yaml"


def _sample_contract() -> InputContract:
    return InputContract(
        contract_ref="warroom.g421.exposure-resolution.v1",
        version=1,
        owner_role="warroom-owner",
        fields=(
            InputField(name="target_order_ref_id", value_type="opaque_id", authority=FieldAuthority.SYSTEM),
            InputField(
                name="resolution_basis",
                value_type="enum",
                authority=FieldAuthority.OWNER,
                allowed_values=("target_never_submitted", "later_round_trip_completed"),
            ),
            InputField(name="approval_receipt_id", value_type="opaque_id", authority=FieldAuthority.OWNER),
            InputField(name="owner_confirmation", value_type="boolean", authority=FieldAuthority.OWNER),
            InputField(name="transport_calls", value_type="list", authority=FieldAuthority.VERIFIER, required=False),
        ),
        artifacts=(
            ArtifactSpec(
                artifact_id="evidence",
                template_path="data/warroom/canary_execution/templates/g421_semantic_evidence.template.json",
                final_path="data/warroom/canary_execution/g421_semantic_exposure_evidence_<timestamp>.json",
                fields=(),
                write_mode="materialize",
            ),
        ),
        resume_transition="warroom.g421.packet-rerun",
    )


def test_owner_fields_returns_only_owner_authority():
    contract = _sample_contract()
    names = {f.name for f in contract.owner_fields()}
    assert names == {"resolution_basis", "approval_receipt_id", "owner_confirmation"}


def test_validate_submission_accepts_valid_owner_input():
    contract = _sample_contract()
    clean, errors = contract.validate_owner_submission({
        "resolution_basis": "target_never_submitted",
        "approval_receipt_id": "recv_123",
        "owner_confirmation": True,
    })
    assert errors == []
    assert clean["resolution_basis"] == "target_never_submitted"


def test_validate_submission_rejects_missing_required_owner_field():
    contract = _sample_contract()
    _clean, errors = contract.validate_owner_submission({
        "resolution_basis": "target_never_submitted",
        "approval_receipt_id": "recv_123",
    })
    assert any("owner_confirmation" in e for e in errors)


def test_validate_submission_rejects_value_outside_enum():
    contract = _sample_contract()
    _clean, errors = contract.validate_owner_submission({
        "resolution_basis": "made_up_value",
        "approval_receipt_id": "recv_123",
        "owner_confirmation": True,
    })
    assert any("resolution_basis" in e for e in errors)


def test_validate_submission_rejects_unknown_field():
    contract = _sample_contract()
    _clean, errors = contract.validate_owner_submission({
        "resolution_basis": "target_never_submitted",
        "approval_receipt_id": "recv_123",
        "owner_confirmation": True,
        "secret_token": "abc",
    })
    assert any("secret_token" in e for e in errors)


def test_validate_submission_rejects_system_authority_field_from_owner():
    """A submission cannot grant a SYSTEM- or VERIFIER-authority field; only
    AgentFlow itself derives those. This is the fabrication-prevention gate."""
    contract = _sample_contract()
    _clean, errors = contract.validate_owner_submission({
        "resolution_basis": "target_never_submitted",
        "approval_receipt_id": "recv_123",
        "owner_confirmation": True,
        "target_order_ref_id": "forged",
    })
    assert any("target_order_ref_id" in e for e in errors)


def test_g421_yaml_config_exists_and_loads():
    assert _G421_YAML.exists()
    contract = load_input_contract(_G421_YAML)
    assert contract.contract_ref == "warroom.g421.exposure-resolution.v1"
    assert contract.owner_role == "warroom-owner"
    assert contract.resume_transition == "warroom.g421.packet-rerun"
    resolution_basis = next(f for f in contract.fields if f.name == "resolution_basis")
    assert resolution_basis.allowed_values == ("target_never_submitted", "later_round_trip_completed")
    assert resolution_basis.authority == FieldAuthority.OWNER
    approval_receipt = next(f for f in contract.fields if f.name == "approval_receipt_id")
    assert approval_receipt.authority == FieldAuthority.OWNER
    transport_calls = next(f for f in contract.fields if f.name == "transport_calls")
    assert transport_calls.authority == FieldAuthority.VERIFIER
    artifact_ids = {a.artifact_id for a in contract.artifacts}
    assert artifact_ids == {"evidence", "local_no_post_proof", "marker"}
    marker = next(a for a in contract.artifacts if a.artifact_id == "marker")
    assert marker.write_mode == "append_only"


def test_registry_refuses_unknown_contract_ref():
    registry = load_contract_registry([_G421_YAML])
    with pytest.raises(UnknownContractError):
        registry.get("no.such.contract.v1")


def test_registry_returns_known_contract():
    registry = load_contract_registry([_G421_YAML])
    contract = registry.get("warroom.g421.exposure-resolution.v1")
    assert contract.contract_ref == "warroom.g421.exposure-resolution.v1"
