"""Repo-config loader for the roadmap GO autopromoter watchdog (M17).

Converts a committed repo config file (JSON, or a minimal YAML subset) into the
existing M14/M15/M16 primitives (`LoopPolicy`, `RoadmapTransitionRegistry`) so
the watchdog CLI never reimplements promotion/apply logic — it only builds
config objects that feed `evaluate_loop_event`.

Dependency-free by design: pyproject has no YAML dependency, so this module
implements a small indentation-based parser covering exactly the shapes this
config needs (nested mappings, lists of scalars, and scalars). It is not a
general YAML parser.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .roadmap import RoadmapTransition, RoadmapTransitionRegistry

_INT_RE = re.compile(r"-?\d+")
_FLOAT_RE = re.compile(r"-?\d+\.\d+")
_TRUE_WORDS = {"true", "yes"}
_FALSE_WORDS = {"false", "no"}
_NULL_WORDS = {"null", "~", ""}


@dataclass(frozen=True)
class RepoRoadmapConfig:
    enabled: bool = False
    kill_switch: bool = False
    board: str = ""
    apply_mode: bool = False
    same_board_only: bool = True
    allowed_transitions: tuple[str, ...] = ()
    transitions: dict[str, RoadmapTransition] = field(default_factory=dict)
    impl_assignee: str = ""
    review_assignee: str = ""
    ack_trigger_agent: str = ""
    trusted_assignees: tuple[str, ...] = ()
    expected_origin: str = ""
    expected_return_to: str = ""
    max_chain_depth: int = 3
    max_promotions_per_roadmap: int = 6
    promote_cooldown_seconds: int = 900
    require_review_edge: bool = True
    require_ack_edge: bool = True
    require_trusted_assignee: bool = True
    require_origin_match: bool = True
    require_policy_resolution: bool = True
    source_ref: str = ""


def load_repo_roadmap_config(path: str | Path) -> RepoRoadmapConfig:
    payload = _load_config_payload(path)

    trusted_assignees = _str_tuple(payload.get("trusted_assignees"))
    review_assignee = str(payload.get("review_assignee") or "")
    raw_ack = payload.get("ack_trigger_agent")
    if raw_ack is True:
        ack_trigger_agent = review_assignee or (trusted_assignees[0] if trusted_assignees else "")
    elif isinstance(raw_ack, str):
        ack_trigger_agent = raw_ack
    else:
        ack_trigger_agent = ""

    transitions = _load_transitions(payload.get("transitions"))

    return RepoRoadmapConfig(
        enabled=_bool(payload.get("enabled"), False),
        kill_switch=_bool(payload.get("kill_switch"), False),
        board=str(payload.get("board") or ""),
        apply_mode=_bool(payload.get("apply_mode"), False),
        same_board_only=_bool(payload.get("same_board_only"), True),
        allowed_transitions=_str_tuple(payload.get("allowed_transitions")),
        transitions=transitions,
        impl_assignee=str(payload.get("impl_assignee") or ""),
        review_assignee=review_assignee,
        ack_trigger_agent=ack_trigger_agent,
        trusted_assignees=trusted_assignees,
        expected_origin=str(payload.get("expected_origin") or ""),
        expected_return_to=str(payload.get("expected_return_to") or ""),
        max_chain_depth=_int(payload.get("max_chain_depth"), 3),
        max_promotions_per_roadmap=_int(payload.get("max_promotions_per_roadmap"), 6),
        promote_cooldown_seconds=_int(payload.get("promote_cooldown_seconds"), 900),
        require_review_edge=_bool(payload.get("require_review_edge"), True),
        require_ack_edge=_bool(payload.get("require_ack_edge"), True),
        require_trusted_assignee=_bool(payload.get("require_trusted_assignee"), True),
        require_origin_match=_bool(payload.get("require_origin_match"), True),
        require_policy_resolution=_bool(payload.get("require_policy_resolution"), True),
        source_ref=str(path),
    )


def build_registry(config: RepoRoadmapConfig) -> RoadmapTransitionRegistry:
    return RoadmapTransitionRegistry(
        version="repo-config",
        transitions=dict(config.transitions),
        source_ref=config.source_ref,
    )


def _load_transitions(raw: Any) -> dict[str, RoadmapTransition]:
    if not isinstance(raw, dict):
        return {}
    transitions: dict[str, RoadmapTransition] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError(f"invalid transition entry: {key}")
        transitions[str(key)] = RoadmapTransition(
            transition_id=str(value.get("transition_id") or key),
            roadmap_id=str(value.get("roadmap_id") or ""),
            from_slice=str(value.get("from_slice") or ""),
            to_slice=str(value.get("to_slice") or ""),
            slice_template=tuple(str(x) for x in (value.get("slice_template") or ())),
            policy_refs=tuple(str(x) for x in (value.get("policy_refs") or ())),
            max_chain_depth=_int(value.get("max_chain_depth"), 3),
            version=str(value.get("version") or ""),
        )
    return transitions


def _load_config_payload(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        payload = parse_minimal_yaml(text)
    if not isinstance(payload, dict):
        raise ValueError("roadmap config root must be a mapping")
    return payload


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(x) for x in value)
    return ()


# ---------------------------------------------------------------------------
# Minimal YAML subset parser.
#
# Supports exactly what a roadmap repo config needs: nested mappings via
# indentation, lists of scalars via "- item" lines, and scalar leaves (quoted
# strings, bare words, bools, ints, floats, null). Any JSON document is also
# accepted, since it is parsed first via json.loads. Comments ("#" to end of
# line outside quotes) and blank lines are ignored.
# ---------------------------------------------------------------------------


def parse_minimal_yaml(text: str) -> Any:
    lines = _clean_lines(text)
    if not lines:
        return {}
    value, _ = _parse_block(lines, 0, _indent_of(lines[0]))
    return value


def _clean_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        in_squote = in_dquote = False
        cut = len(line)
        for i, ch in enumerate(line):
            if ch == "'" and not in_dquote:
                in_squote = not in_squote
            elif ch == '"' and not in_squote:
                in_dquote = not in_dquote
            elif ch == "#" and not in_squote and not in_dquote and (i == 0 or line[i - 1] == " "):
                cut = i
                break
        line = line[:cut].rstrip()
        if line.strip():
            lines.append(line)
    return lines


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_block(lines: list[str], idx: int, indent: int) -> tuple[Any, int]:
    if idx >= len(lines) or _indent_of(lines[idx]) < indent:
        return {}, idx
    first_indent = _indent_of(lines[idx])
    if lines[idx].strip().startswith("- "):
        return _parse_list(lines, idx, first_indent)
    return _parse_mapping(lines, idx, first_indent)


def _parse_list(lines: list[str], idx: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while idx < len(lines):
        line = lines[idx]
        cur_indent = _indent_of(line)
        if cur_indent != indent:
            break
        stripped = line.strip()
        if not stripped.startswith("- "):
            raise ValueError(f"expected list item: {line}")
        content = stripped[2:].strip()
        result.append(_parse_scalar(content))
        idx += 1
    return result, idx


def _parse_mapping(lines: list[str], idx: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while idx < len(lines):
        line = lines[idx]
        cur_indent = _indent_of(line)
        if cur_indent != indent:
            break
        content = line.strip()
        if ":" not in content:
            raise ValueError(f"expected mapping entry: {content}")
        key, _, rest = content.partition(":")
        key = key.strip().strip("\"'")
        rest = rest.strip()
        idx += 1
        if rest:
            result[key] = _parse_scalar(rest)
        elif idx < len(lines) and _indent_of(lines[idx]) > cur_indent:
            value, idx = _parse_block(lines, idx, _indent_of(lines[idx]))
            result[key] = value
        else:
            result[key] = None
    return result, idx


def _parse_scalar(text: str) -> Any:
    text = text.strip()
    if len(text) >= 2 and ((text[0] == '"' and text[-1] == '"') or (text[0] == "'" and text[-1] == "'")):
        return text[1:-1]
    low = text.lower()
    if low in _TRUE_WORDS:
        return True
    if low in _FALSE_WORDS:
        return False
    if low in _NULL_WORDS:
        return None
    if _INT_RE.fullmatch(text):
        return int(text)
    if _FLOAT_RE.fullmatch(text):
        return float(text)
    return text
