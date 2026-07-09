"""Scaffold + registry UX for the roadmap GO autopromoter watchdog (M18).

Two dependency-free, side-effect-light helpers layered on top of the existing
M17 `roadmap promote|watch` path:

- ``roadmap init-config`` writes a committed repo config (the dependency-free
  YAML subset understood by :mod:`agentflow_hermes.roadmap_config`) from a few
  flags, so an operator can stand up a new board/channel without hand-editing.
- ``roadmap register-watchdog`` / ``roadmap unregister-watchdog`` idempotently
  record config paths in a JSON registry that the existing no-agent cron script
  consumes. The registry is a plain list of config paths plus metadata; it does
  not change the cron script's contract (iterate configs, run ``roadmap watch``).

Neither helper touches the gateway, systemctl, cron itself, live board state, or
any cross-board surface. They only read/write local files, emit JSON like the
rest of the CLI, and fail closed on malformed input.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from .live.sanitize import sanitize_string
from .roadmap_config import load_repo_roadmap_config
from .roadmap_templates import LEGACY_PRESET, preset_names, resolve_template

REGISTRY_VERSION = 1

# Free-text-ish values (origin/return_to) may contain spaces, `#`, `/`, `:`,
# etc., but never quotes, backslashes, or control characters — those could break
# the YAML subset emitter or smuggle a second document line. Fail closed instead.
_ORIGIN_RE = re.compile(r"^[^\"'\\\r\n\t]{1,240}$")
# Identifier-like values (board, assignees, transition ids, slices) are stricter.
_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:>#/+-]{0,120}$")


class ConfigValueError(ValueError):
    """Raised when an init-config flag value fails validation (fail closed)."""


def _safe_origin(value: str) -> str:
    text = str(value or "").strip()
    if not _ORIGIN_RE.fullmatch(text):
        raise ConfigValueError("invalid_value")
    return text


def _safe_ident(value: str) -> str:
    text = str(value or "").strip()
    if not _IDENT_RE.fullmatch(text):
        raise ConfigValueError("invalid_value")
    return text


# ---------------------------------------------------------------------------
# init-config
# ---------------------------------------------------------------------------


def add_roadmap_init_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", required=True)
    parser.add_argument("--board", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--transition", required=True)
    parser.add_argument("--from", dest="from_slice", required=True)
    parser.add_argument("--to", dest="to_slice", required=True)
    parser.add_argument("--return-to", default="", help="defaults to --origin")
    parser.add_argument("--impl-assignee", default="ccsupervisor")
    parser.add_argument("--review-assignee", default="ccreviewer")
    parser.add_argument("--roadmap-id", default="", help="defaults to <board>.roadmap")
    parser.add_argument("--template-preset", default="",
                        help=f"roadmap task template preset (default: {LEGACY_PRESET}; choices: {', '.join(preset_names())})")
    parser.add_argument("--goal-anchor", default="", help="optional lane standing goal anchor for generated task bodies")
    parser.add_argument("--apply-mode", action="store_true", default=False,
                        help="arm board writes in the generated config (still needs --apply at run time)")
    parser.add_argument("--force", action="store_true", default=False,
                        help="overwrite an existing --output file")


def render_roadmap_config(
    *,
    board: str,
    origin: str,
    transition: str,
    from_slice: str,
    to_slice: str,
    return_to: str = "",
    impl_assignee: str = "ccsupervisor",
    review_assignee: str = "ccreviewer",
    roadmap_id: str = "",
    template_preset: str = "",
    goal_anchor: str = "",
    apply_mode: bool = False,
) -> str:
    """Render a committed roadmap config in the dependency-free YAML subset.

    All caller-supplied values are validated (fail closed) and free-text values
    are double-quoted so the minimal YAML parser round-trips them exactly.
    """

    board = _safe_ident(board)
    transition = _safe_ident(transition)
    from_slice = _safe_ident(from_slice)
    to_slice = _safe_ident(to_slice)
    impl_assignee = _safe_ident(impl_assignee)
    review_assignee = _safe_ident(review_assignee)
    origin = _safe_origin(origin)
    return_to = _safe_origin(return_to) if return_to else origin
    roadmap_id = _safe_ident(roadmap_id) if roadmap_id else f"{board}.roadmap"
    template_preset = _safe_ident(template_preset) if template_preset else ""
    goal_anchor = _safe_origin(goal_anchor) if goal_anchor else ""
    resolved_template = resolve_template(
        template_preset=template_preset,
        slice_template=(),
        goal_anchor=goal_anchor,
    )
    sequence = resolved_template.slice_template

    apply_mode_str = "true" if apply_mode else "false"
    lines = [
        "# agentflow-hermes repo-config roadmap GO autopromoter.",
        "#",
        "# Consumed by `agentflow-hermes roadmap promote|watch --config <this file>`.",
        "# `enabled: false` is the kill switch: no board read or write happens when",
        "# it is false, regardless of --apply.",
        "enabled: true",
        "kill_switch: false",
        "",
        "# same_board_only: true means promote/watch only ever read/write this board.",
        f"board: {board}",
        "same_board_only: true",
        "",
        "# apply_mode is the repo-level arm for board writes. A real create also",
        "# requires the operator to pass --apply on the CLI; both gates must be open.",
        f"apply_mode: {apply_mode_str}",
        "",
        f'expected_origin: "{origin}"',
        f'expected_return_to: "{return_to}"',
        "",
        f"impl_assignee: {impl_assignee}",
        f"review_assignee: {review_assignee}",
        "# true means derive the ack agent from review_assignee/trusted_assignees.",
        "ack_trigger_agent: true",
        "trusted_assignees:",
        f"  - {review_assignee}",
        "",
        "allowed_transitions:",
        f"  - {transition}",
        "",
        "max_chain_depth: 3",
        "max_promotions_per_roadmap: 6",
        "promote_cooldown_seconds: 900",
        "require_review_edge: true",
        "require_ack_edge: true",
        "require_trusted_assignee: true",
        "require_origin_match: true",
        "require_policy_resolution: true",
        "",
        "transitions:",
        f"  {transition}:",
        f"    roadmap_id: {roadmap_id}",
        f"    from_slice: {from_slice}",
        f"    to_slice: {to_slice}",
        *([f"    template_preset: {template_preset}"] if template_preset else []),
        *([f'    goal_anchor: "{goal_anchor}"'] if goal_anchor else []),
        "    slice_template:",
        *[f"      - {kind}" for kind in sequence],
        "    policy_refs:",
        "      - design_opus",
        "      - implementation_default",
        "    max_chain_depth: 3",
        f"    version: {'template-v2' if template_preset else 'template-v1'}",
        "",
    ]
    return "\n".join(lines)


def run_roadmap_init_config(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    output = Path(args.output)
    if output.exists() and not args.force:
        return 2, {"success": False, "error": "output_exists", "detail": "pass --force to overwrite"}

    try:
        content = render_roadmap_config(
            board=args.board,
            origin=args.origin,
            transition=args.transition,
            from_slice=args.from_slice,
            to_slice=args.to_slice,
            return_to=args.return_to,
            impl_assignee=args.impl_assignee,
            review_assignee=args.review_assignee,
            roadmap_id=args.roadmap_id,
            template_preset=args.template_preset,
            goal_anchor=args.goal_anchor,
            apply_mode=args.apply_mode,
        )
    except (ConfigValueError, ValueError) as exc:
        return 2, {"success": False, "error": str(exc)}

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        # Validate the file we just wrote actually loads as a config.
        config = load_repo_roadmap_config(str(output))
    except (OSError, ValueError) as exc:
        return 2, {"success": False, "error": "write_failed", "detail": sanitize_string(str(exc))}

    return 0, {
        "success": True,
        "output": str(output),
        "board": config.board,
        "transition": args.transition,
        "apply_mode": config.apply_mode,
        "enabled": config.enabled,
    }


# ---------------------------------------------------------------------------
# register-watchdog / unregister-watchdog
# ---------------------------------------------------------------------------


def add_roadmap_register_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--receipts-file", default="",
                        help="defaults to <config-dir>/.agentflow-roadmap-receipts.json")


def add_roadmap_unregister_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True)
    parser.add_argument("--registry", required=True)


def _registry_path(raw: str) -> Path:
    return Path(raw).expanduser()


def load_registry(path: Path) -> dict[str, Any]:
    """Load the watchdog registry, tolerating absence and legacy shapes.

    Returns a normalized ``{"version": N, "configs": [...]}`` mapping. A missing
    file yields an empty registry. A malformed file raises ``ValueError`` so the
    caller can fail closed rather than silently clobbering operator data.
    """

    if not path.exists():
        return {"version": REGISTRY_VERSION, "configs": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"malformed registry: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("registry root must be a mapping")
    configs = data.get("configs")
    if not isinstance(configs, list):
        raise ValueError("registry.configs must be a list")
    normalized: list[dict[str, Any]] = []
    for entry in configs:
        if isinstance(entry, str):
            # The no-agent cron script accepts a bare string as {"config": item}.
            normalized.append({"config": entry})
        elif isinstance(entry, dict) and _entry_config(entry):
            normalized.append(dict(entry))
        else:
            raise ValueError("registry.configs entries must be paths or mappings")
    version = data.get("version")
    return {"version": version if isinstance(version, int) else REGISTRY_VERSION, "configs": normalized}


def _entry_config(entry: dict[str, Any]) -> str:
    """Return an entry's config path, tolerating the legacy ``path`` key.

    The cron script keys on ``config``; earlier drafts of this CLI wrote
    ``path``. Read both so an old registry keeps working.
    """

    value = entry.get("config") or entry.get("path")
    return str(value) if isinstance(value, str) else ""


def _write_registry(path: Path, registry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _default_receipts_file(config_path: Path) -> str:
    return str(config_path.parent / ".agentflow-roadmap-receipts.json")


def run_roadmap_register(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    config_path = Path(args.config)
    # Validate the config loads before registering; a broken config must not
    # enter the registry the cron script blindly iterates.
    try:
        config = load_repo_roadmap_config(str(config_path))
    except (OSError, ValueError) as exc:
        return 2, {"success": False, "error": "malformed_config", "detail": sanitize_string(str(exc))}

    resolved = str(config_path.resolve())
    registry_path = _registry_path(args.registry)
    try:
        registry = load_registry(registry_path)
    except ValueError as exc:
        return 2, {"success": False, "error": "malformed_registry", "detail": sanitize_string(str(exc))}

    for entry in registry["configs"]:
        if _entry_config(entry) == resolved:
            return 0, {
                "success": True,
                "registered": False,
                "already_registered": True,
                "registry": str(registry_path),
                "config": resolved,
            }

    workdir = str(config_path.resolve().parent)
    receipts_file = args.receipts_file or _default_receipts_file(config_path)
    # Key on "config" to match the no-agent cron script's item["config"] contract.
    # name/workdir/receipts_file are read by the script; board/enabled are extra
    # metadata it ignores harmlessly.
    entry = {
        "name": config.board or config_path.stem,
        "config": resolved,
        "workdir": workdir,
        "receipts_file": receipts_file,
        "board": config.board,
        "enabled": config.enabled,
    }
    registry["configs"].append(entry)
    registry["version"] = REGISTRY_VERSION
    try:
        _write_registry(registry_path, registry)
    except OSError as exc:
        return 2, {"success": False, "error": "write_failed", "detail": sanitize_string(str(exc))}

    return 0, {
        "success": True,
        "registered": True,
        "already_registered": False,
        "registry": str(registry_path),
        "entry": entry,
    }


def run_roadmap_unregister(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    config_path = Path(args.config)
    resolved = str(config_path.resolve())
    registry_path = _registry_path(args.registry)
    try:
        registry = load_registry(registry_path)
    except ValueError as exc:
        return 2, {"success": False, "error": "malformed_registry", "detail": sanitize_string(str(exc))}

    before = len(registry["configs"])
    registry["configs"] = [e for e in registry["configs"] if _entry_config(e) != resolved]
    removed = len(registry["configs"]) != before
    registry["version"] = REGISTRY_VERSION
    if removed:
        try:
            _write_registry(registry_path, registry)
        except OSError as exc:
            return 2, {"success": False, "error": "write_failed", "detail": sanitize_string(str(exc))}

    return 0, {
        "success": True,
        "removed": removed,
        "registry": str(registry_path),
        "config": resolved,
    }
