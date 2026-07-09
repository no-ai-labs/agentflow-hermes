"""Bounded GitHub release publish trigger after a reviewed GO (M20).

Turns a final review GO summary carrying explicit release markers into a
GitHub release action (`git tag`, `git push` of the tag, `gh release
create`), gated behind:

- a repo-owned :class:`ReleaseActionConfig` — ``release_actions_enabled`` and
  ``apply_mode`` are both false by default, the kill switch/arm pair;
- an allowlist of release actions and an allowed-version pattern/explicit
  list, so an arbitrary GO cannot name an unreviewed action or version;
- an injectable CLI runner, so dry-run and tests never shell out for real;
- a receipt ledger keyed by ``release:<action>:<version>`` so a repeated run
  cannot push/tag/release the same version twice; and
- a live duplicate check (``git tag -l``, ``gh release view``) immediately
  before any apply mutation, in case the local receipt ledger is missing,
  stale, or was never persisted (e.g. a prior run crashed after the tag/push
  but before the receipt was written).

Dry-run (``propose``) is the only outcome unless ``config.apply_mode`` is
true AND the caller passes ``apply=True``. This module never runs git/gh
itself outside an injected runner — see :func:`default_release_cli_runner`,
which is only ever wired in by the CLI when both apply gates are open.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .live.sanitize import short_text
from .roadmap_config import parse_minimal_yaml

ReleaseCliRunner = Callable[[list[str]], tuple[int, str, str]]

_VERDICT_RE = re.compile(r"\bVerdict\s*:\s*(GO|BLOCK|NEED_MORE)\b", re.I)
_DEFAULT_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_TRUE_WORDS = {"true", "yes", "1"}


@dataclass(frozen=True)
class ReleaseDirective:
    verdict: str
    action: str
    version: str
    approved: bool
    title: str = ""
    notes: str = ""
    target: str = ""
    confidence: str = "none"
    source_ref: str = ""


@dataclass(frozen=True)
class ReleaseActionConfig:
    release_actions_enabled: bool = False
    apply_mode: bool = False
    allowed_actions: tuple[str, ...] = ()
    allowed_versions: tuple[str, ...] = ()
    allowed_version_pattern: str = ""
    source_ref: str = ""


def load_release_action_config(path: str | Path) -> ReleaseActionConfig:
    """Load a repo-owned release-action config (JSON, or the minimal YAML subset).

    Fails closed: malformed JSON/YAML or a non-mapping root raises
    ``ValueError`` rather than falling back to a permissive default.
    """

    text = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        payload = parse_minimal_yaml(text)
    if not isinstance(payload, dict):
        raise ValueError("release action config root must be a mapping")
    return ReleaseActionConfig(
        release_actions_enabled=_bool(payload.get("release_actions_enabled"), False),
        apply_mode=_bool(payload.get("apply_mode"), False),
        allowed_actions=_str_tuple(payload.get("allowed_actions")),
        allowed_versions=_str_tuple(payload.get("allowed_versions")),
        allowed_version_pattern=str(payload.get("allowed_version_pattern") or ""),
        source_ref=str(path),
    )


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(x) for x in value)
    return ()


def _marker(text: str, name: str) -> str:
    wanted = name.strip().lower()
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == wanted:
            return value.strip()
    return ""


def parse_release_directive(text: str, *, source_ref: str = "") -> ReleaseDirective:
    """Parse the structured release markers from a final GO summary.

    Only explicit ``Name: value`` marker lines are read — free-text prose
    ("we should probably release this") never yields a directive. Missing
    ``Verdict: GO``, ``Release-Action:``, ``Release-Version:``, or
    ``Release-Approved:`` markers force ``confidence="none"``, which the
    evaluator treats as a refuse.
    """

    verdict_match = _VERDICT_RE.search(text or "")
    verdict = verdict_match.group(1).upper() if verdict_match else "UNKNOWN"
    action = _marker(text, "Release-Action")
    version = _marker(text, "Release-Version")
    approved_marker = _marker(text, "Release-Approved")
    approved = approved_marker.strip().lower() in _TRUE_WORDS
    title = short_text(_marker(text, "Release-Title"), max_len=200)
    notes = short_text(_marker(text, "Release-Notes"), max_len=2000)
    target = short_text(_marker(text, "Release-Target"), max_len=200)
    confidence = "explicit" if (verdict_match and action and version and approved_marker) else "none"
    return ReleaseDirective(
        verdict=verdict,
        action=action,
        version=version,
        approved=approved,
        title=title,
        notes=notes,
        target=target,
        confidence=confidence,
        source_ref=source_ref,
    )


def _idempotency_key(action: str, version: str) -> str:
    return f"release:{action}:{version}"


def _version_allowed(version: str, config: ReleaseActionConfig) -> bool:
    if config.allowed_versions:
        return version in config.allowed_versions
    if config.allowed_version_pattern:
        try:
            pattern = re.compile(config.allowed_version_pattern)
        except re.error:
            return False
        return bool(pattern.fullmatch(version))
    return bool(_DEFAULT_VERSION_RE.fullmatch(version))


def _base_result(directive: ReleaseDirective, decision: str, reason: str, **extra: Any) -> dict[str, Any]:
    result = {
        "success": decision in {"propose", "apply"},
        "decision": decision,
        "reason": reason,
        "action": directive.action,
        "version": directive.version,
        "idempotency_key": "",
        "mutations": [],
        "receipt": None,
    }
    result.update(extra)
    return result


def _safe_run(runner: ReleaseCliRunner, argv: list[str]) -> tuple[int, str, str] | None:
    try:
        return runner(argv)
    except Exception:
        return None


def evaluate_release_action(
    summary: str,
    config: ReleaseActionConfig,
    ledger: dict[str, Any],
    *,
    apply: bool,
    runner: ReleaseCliRunner | None = None,
    now: float | None = None,
    source_ref: str = "",
) -> dict[str, Any]:
    """Evaluate one final GO summary against the release-action gates.

    ``ledger`` is a plain ``idempotency_key -> receipt`` dict, mutated in
    place on a successful apply so the caller can persist it. Nothing is
    written to ``ledger`` on propose/refuse/noop.
    """

    directive = parse_release_directive(summary, source_ref=source_ref)

    if not config.release_actions_enabled:
        return _base_result(directive, "noop", "release_actions_disabled")
    if directive.verdict != "GO":
        return _base_result(directive, "refuse", "not_go")
    if directive.confidence != "explicit":
        return _base_result(directive, "refuse", "missing_directive")
    if directive.action not in config.allowed_actions:
        return _base_result(directive, "refuse", "unknown_action")
    if not directive.approved:
        return _base_result(directive, "refuse", "missing_approval")
    if not _version_allowed(directive.version, config):
        return _base_result(directive, "refuse", "invalid_version")

    key = _idempotency_key(directive.action, directive.version)
    existing = ledger.get(key)
    if isinstance(existing, dict) and existing.get("result") == "success":
        return _base_result(directive, "noop", "duplicate_receipt", idempotency_key=key, receipt=existing)

    if not apply or not config.apply_mode:
        plan = {
            "action": directive.action,
            "version": directive.version,
            "title": directive.title,
            "notes": directive.notes,
            "target": directive.target,
        }
        return _base_result(directive, "propose", "dry_run", idempotency_key=key, mutations=[plan])

    if runner is None:
        return _base_result(directive, "refuse", "no_runner", idempotency_key=key)

    # Live duplicate check immediately before any mutation, independent of
    # the local receipt ledger (which may be missing/stale/never persisted).
    tag_check = _safe_run(runner, ["git", "tag", "-l", directive.version])
    if tag_check is None:
        return _base_result(directive, "refuse", "runner_error", idempotency_key=key)
    tag_rc, tag_out, _tag_err = tag_check
    if tag_rc == 0 and directive.version in tag_out.split():
        return _base_result(directive, "refuse", "duplicate_tag", idempotency_key=key)

    release_check = _safe_run(runner, ["gh", "release", "view", directive.version])
    if release_check is None:
        return _base_result(directive, "refuse", "runner_error", idempotency_key=key)
    release_rc, _release_out, _release_err = release_check
    if release_rc == 0:
        return _base_result(directive, "refuse", "duplicate_release", idempotency_key=key)

    tag_result = _safe_run(runner, ["git", "tag", "-a", directive.version, "-m", directive.title or directive.version])
    if tag_result is None or tag_result[0] != 0:
        return _base_result(directive, "refuse", "tag_failed", idempotency_key=key)

    push_result = _safe_run(runner, ["git", "push", "origin", directive.version])
    if push_result is None or push_result[0] != 0:
        return _base_result(directive, "refuse", "push_failed", idempotency_key=key)

    release_argv = [
        "gh", "release", "create", directive.version,
        "--title", directive.title or directive.version,
        "--notes", directive.notes,
    ]
    if directive.target:
        release_argv += ["--target", directive.target]
    release_result = _safe_run(runner, release_argv)
    if release_result is None or release_result[0] != 0:
        return _base_result(directive, "refuse", "release_create_failed", idempotency_key=key)

    receipt = {
        "action": directive.action,
        "version": directive.version,
        "idempotency_key": key,
        "result": "success",
    }
    ledger[key] = receipt
    return _base_result(
        directive,
        "apply",
        "applied",
        idempotency_key=key,
        mutations=[{"kind": "tag"}, {"kind": "push"}, {"kind": "gh_release"}],
        receipt=receipt,
    )


def load_receipts_ledger(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_receipts_ledger(path: str | Path, ledger: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def default_release_cli_runner(argv: list[str]) -> tuple[int, str, str]:
    """Invoke the real `git`/`gh` CLI as a subprocess. Never used unless armed."""
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    return proc.returncode, proc.stdout, proc.stderr


__all__ = [
    "ReleaseActionConfig",
    "ReleaseCliRunner",
    "ReleaseDirective",
    "default_release_cli_runner",
    "evaluate_release_action",
    "load_receipts_ledger",
    "load_release_action_config",
    "parse_release_directive",
    "save_receipts_ledger",
]
