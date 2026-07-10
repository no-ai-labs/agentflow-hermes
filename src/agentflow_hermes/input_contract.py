"""Versioned InputContract with SYSTEM/OWNER/VERIFIER field authority.

No free-form worker/LLM output may grant a value to an OWNER-authority field.
Owner fields are only ever accepted through a validated owner submission bound
to a continuation instance (see ``continuation.py``/``continuations/owner_input.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class FieldAuthority(str, Enum):
    SYSTEM = "system"
    OWNER = "owner"
    VERIFIER = "verifier"


@dataclass(frozen=True)
class InputField:
    name: str
    value_type: str
    authority: FieldAuthority
    required: bool = True
    allowed_values: tuple[str, ...] = ()
    description: str = ""
    secret: bool = False


@dataclass(frozen=True)
class ArtifactSpec:
    artifact_id: str
    template_path: str
    final_path: str
    fields: tuple[InputField, ...] = ()
    write_mode: str = "materialize"  # scaffold | materialize | append_only


@dataclass(frozen=True)
class InputContract:
    contract_ref: str
    version: int
    owner_role: str
    fields: tuple[InputField, ...]
    artifacts: tuple[ArtifactSpec, ...]
    resume_transition: str

    def field(self, name: str) -> InputField | None:
        return next((f for f in self.fields if f.name == name), None)

    def owner_fields(self) -> tuple[InputField, ...]:
        return tuple(f for f in self.fields if f.authority == FieldAuthority.OWNER)

    def validate_owner_submission(self, values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Validate a candidate owner submission against this contract.

        Returns ``(clean_values, errors)``. ``clean_values`` only contains
        accepted OWNER-authority fields; any error means the submission must
        be refused in full (the caller must not partially apply it).
        """
        errors: list[str] = []
        clean: dict[str, Any] = {}
        known = {f.name: f for f in self.fields}
        owner_field_names = {f.name for f in self.owner_fields()}

        for name, value in values.items():
            field_def = known.get(name)
            if field_def is None:
                errors.append(f"unknown_field:{name}")
                continue
            if field_def.authority != FieldAuthority.OWNER:
                errors.append(f"field_not_owner_authority:{name}")
                continue
            if field_def.allowed_values and value not in field_def.allowed_values:
                errors.append(f"value_not_allowed:{name}")
                continue
            clean[name] = value

        provided = set(values.keys())
        for name in owner_field_names:
            field_def = known[name]
            if field_def.required and name not in provided:
                errors.append(f"missing_required_owner_field:{name}")

        if errors:
            return {}, errors
        return clean, []
