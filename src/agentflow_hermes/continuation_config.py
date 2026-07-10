"""Loader/registry for versioned InputContract repo config files.

Reuses the dependency-free minimal-YAML parser already shipped for the
roadmap repo config (``roadmap_config.parse_minimal_yaml``) instead of adding
a second config family with its own parsing rules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .input_contract import ArtifactSpec, FieldAuthority, InputContract, InputField
from .roadmap_config import parse_minimal_yaml


class UnknownContractError(LookupError):
    """Raised when a contract_ref is not present in the loaded registry."""


def _load_payload(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        payload = parse_minimal_yaml(text)
    if not isinstance(payload, dict):
        raise ValueError("input contract config root must be a mapping")
    return payload


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _load_fields(raw: Any) -> tuple[InputField, ...]:
    if not isinstance(raw, dict):
        return ()
    fields: list[InputField] = []
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise ValueError(f"invalid field entry: {name}")
        authority = FieldAuthority(str(spec.get("authority") or "owner"))
        allowed = spec.get("allowed_values")
        fields.append(
            InputField(
                name=str(name),
                value_type=str(spec.get("type") or ""),
                authority=authority,
                required=_bool(spec.get("required"), True),
                allowed_values=tuple(str(v) for v in allowed) if isinstance(allowed, (list, tuple)) else (),
                description=str(spec.get("description") or ""),
                secret=_bool(spec.get("secret"), False),
            )
        )
    return tuple(fields)


def _load_artifacts(raw: Any) -> tuple[ArtifactSpec, ...]:
    if not isinstance(raw, dict):
        return ()
    artifacts: list[ArtifactSpec] = []
    for artifact_id, spec in raw.items():
        if not isinstance(spec, dict):
            raise ValueError(f"invalid artifact entry: {artifact_id}")
        artifacts.append(
            ArtifactSpec(
                artifact_id=str(artifact_id),
                template_path=str(spec.get("template_path") or ""),
                final_path=str(spec.get("final_path") or ""),
                fields=_load_fields(spec.get("fields")),
                write_mode=str(spec.get("write_mode") or "materialize"),
            )
        )
    return tuple(artifacts)


def load_input_contract(path: str | Path) -> InputContract:
    payload = _load_payload(path)
    return InputContract(
        contract_ref=str(payload.get("contract_ref") or ""),
        version=int(payload.get("version") or 1),
        owner_role=str(payload.get("owner_role") or ""),
        fields=_load_fields(payload.get("fields")),
        artifacts=_load_artifacts(payload.get("artifacts")),
        resume_transition=str(payload.get("resume_transition") or ""),
    )


@dataclass(frozen=True)
class ContractRegistry:
    contracts: dict[str, InputContract] = field(default_factory=dict)

    def get(self, contract_ref: str) -> InputContract:
        contract = self.contracts.get(contract_ref)
        if contract is None:
            raise UnknownContractError(contract_ref)
        return contract

    def has(self, contract_ref: str) -> bool:
        return contract_ref in self.contracts


def load_contract_registry(paths: list[str | Path]) -> ContractRegistry:
    contracts: dict[str, InputContract] = {}
    for path in paths:
        contract = load_input_contract(path)
        contracts[contract.contract_ref] = contract
    return ContractRegistry(contracts=contracts)
