from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .live.policy import default_home
from .live.sanitize import contains_sensitive_text, safe_durable_ref, safe_event_payload, short_text


class PolicyRefError(ValueError):
    """PolicyRef preflight failed closed."""


@dataclass(frozen=True)
class PolicyRef:
    key: str
    required: bool = True


@dataclass(frozen=True)
class RoutePolicy:
    provider: str
    model: str
    command_family: str
    source: str = "central"

    @classmethod
    def from_raw(cls, raw: Any, *, key: str) -> "RoutePolicy":
        if not isinstance(raw, dict):
            raise PolicyRefError(f"policy route {key} must be an object")
        provider = short_text(raw.get("provider"))
        model = short_text(raw.get("model"))
        command_family = short_text(raw.get("command_family") or raw.get("command"))
        source = short_text(raw.get("source") or "central")
        if not provider or not model or not command_family:
            raise PolicyRefError(f"policy route {key} is incomplete")
        return cls(provider=provider, model=model, command_family=command_family, source=source)


@dataclass(frozen=True)
class PolicyDocument:
    version: str
    routes: dict[str, RoutePolicy]
    source_ref: str
    content_hash: str


@dataclass(frozen=True)
class PolicyResolution:
    key: str
    policy_version: str
    provider: str
    model_class: str
    command_family: str
    source: str
    evidence: dict[str, Any]
    resolved_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "policy_version": self.policy_version,
            "provider": self.provider,
            "model_class": self.model_class,
            "command_family": self.command_family,
            "source": self.source,
            "evidence": safe_event_payload(self.evidence),
            "resolved_at": self.resolved_at,
        }


@dataclass(frozen=True)
class InlinePolicyFinding:
    blocker: str
    snippet: str
    policy_ref: str
    conflict_class: str
    pattern: str

    def as_dict(self) -> dict[str, str]:
        return safe_event_payload({
            "blocker": self.blocker,
            "snippet": self.snippet,
            "policy_ref": self.policy_ref,
            "conflict_class": self.conflict_class,
            "pattern": self.pattern,
        })


def default_model_policy_path() -> Path:
    return default_home() / "model_policy.json"


def load_policy_document(path: str | Path | None = None) -> PolicyDocument:
    """Load central model policy and fail closed on malformed values."""
    policy_path = Path(path) if path is not None else default_model_policy_path()
    try:
        raw_text = policy_path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyRefError("malformed_policy") from exc
    if not isinstance(raw, dict):
        raise PolicyRefError("malformed_policy")
    routes_raw = raw.get("routes")
    if not isinstance(routes_raw, dict) or not routes_raw:
        raise PolicyRefError("malformed_policy")
    routes: dict[str, RoutePolicy] = {}
    for key, value in routes_raw.items():
        safe_key = _normalize_key(str(key))
        routes[safe_key] = RoutePolicy.from_raw(value, key=safe_key)
    raw_version = raw.get("version") or raw.get("policy_version") or ""
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16]
    version = short_text(raw_version or content_hash)
    source_ref, _ = safe_durable_ref(str(policy_path), field="policy_path")
    return PolicyDocument(version=version, routes=routes, source_ref=source_ref, content_hash=content_hash)


def default_policy_document() -> PolicyDocument:
    """Safe built-in defaults used by tests and dry-run tools when explicitly requested."""
    routes = {
        "design_opus": RoutePolicy(
            provider="anthropic-native",
            model="claude-opus-4-8",
            command_family="claude-code-opus",
            source="built-in-default",
        ),
        "implementation_default": RoutePolicy(
            provider="anthropic-native",
            model="claude-sonnet-4-6",
            command_family="claude-code-sonnet",
            source="built-in-default",
        ),
    }
    return PolicyDocument(version="built-in-default", routes=routes, source_ref="policy:built-in", content_hash="built-in")


def resolve_policy_ref(ref: str | PolicyRef, policy: PolicyDocument, *, now: float | None = None) -> PolicyResolution:
    key = _normalize_key(ref.key if isinstance(ref, PolicyRef) else str(ref))
    route = policy.routes.get(key)
    if route is None:
        raise PolicyRefError(f"unknown_policy_ref:{key}")
    evidence = {
        "policy_ref": key,
        "policy_version": policy.version,
        "policy_hash": policy.content_hash,
        "provider": route.provider,
        "model_class": _model_class(route.model),
        "command_family": route.command_family,
        "source": route.source,
        "policy_source_ref": policy.source_ref,
    }
    return PolicyResolution(
        key=key,
        policy_version=policy.version,
        provider=route.provider,
        model_class=_model_class(route.model),
        command_family=route.command_family,
        source=route.source,
        evidence=safe_event_payload(evidence),
        resolved_at=now if now is not None else time.time(),
    )


def preflight_task_body(
    body: str,
    *,
    policy: PolicyDocument | None = None,
    policy_path: str | Path | None = None,
    refs: tuple[str | PolicyRef, ...] = ("design_opus", "implementation_default"),
) -> dict[str, Any]:
    """Resolve binding PolicyRef values and scan stale inline route snippets.

    A malformed central policy fails closed; inline stale policy without a structured
    override is returned as a BLOCK candidate rather than being acted on.
    """
    try:
        doc = policy or load_policy_document(policy_path)
    except PolicyRefError as exc:
        return {"success": False, "error": "malformed_policy", "conflict_class": "unverifiable", "resolutions": []}
    resolutions: list[dict[str, Any]] = []
    findings = [finding.as_dict() for finding in detect_stale_inline_policy(body, policy=doc)]
    # Every requested required ref must resolve or produce an explicit fail-closed
    # finding. Never silently drop an unknown/missing ref from the resolution set.
    for ref in refs:
        key, required = _ref_key_required(ref)
        if key in doc.routes:
            resolutions.append(resolve_policy_ref(ref, doc).as_dict())
        elif required:
            findings.append(InlinePolicyFinding(
                blocker="unknown_policy_ref",
                snippet=short_text(key, max_len=80),
                policy_ref=key,
                conflict_class="unverifiable",
                pattern="unknown_policy_ref",
            ).as_dict())
    has_unknown = any(f["blocker"] == "unknown_policy_ref" for f in findings)
    has_contradicted = any(f["conflict_class"] == "contradicted" for f in findings)
    if any(f["blocker"] == "stale_inline_route" for f in findings):
        error = "stale_inline_route"
    elif has_unknown:
        error = "unknown_policy_ref"
    else:
        error = ""
    return {
        "success": not has_contradicted and not has_unknown,
        "error": error,
        "resolutions": resolutions,
        "findings": findings,
        "policy_version": doc.version,
    }


_STALE_ROUTE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("claude-openrouter-opus", re.compile(r"claude-openrouter-opus", re.I)),
    ("claude-venice-opus", re.compile(r"claude-venice-opus", re.I)),
    ("openrouter,anthropic", re.compile(r"openrouter\s*,\s*anthropic", re.I)),
    ("moonshot", re.compile(r"\b(?:moonshot|kimi)\b", re.I)),
)
_BINDING_CONTEXT_RE = re.compile(r"\b(?:route|use|via|default|implementation|model|provider|worker|delegate)\b", re.I)
_STRUCTURED_OVERRIDE_RE = re.compile(r"\b(?:PolicyOverride|policy_override|structured override|override:)\b", re.I)


def detect_stale_inline_policy(body: str, *, policy: PolicyDocument | None = None) -> list[InlinePolicyFinding]:
    text = body or ""
    findings: list[InlinePolicyFinding] = []
    has_override = bool(_STRUCTURED_OVERRIDE_RE.search(text))
    for label, pattern in _STALE_ROUTE_PATTERNS:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - 80): match.end() + 80]
            if not _BINDING_CONTEXT_RE.search(window) and "route" not in label:
                continue
            policy_ref = "implementation_default" if label == "moonshot" else "design_opus"
            conflict_class = "explicit_override_candidate" if has_override else "contradicted"
            snippet = short_text(match.group(0), max_len=80)
            if contains_sensitive_text(snippet):
                snippet = "snippet:redacted"
            findings.append(InlinePolicyFinding(
                blocker="explicit_override_candidate" if has_override else "stale_inline_route",
                snippet=snippet,
                policy_ref=policy_ref,
                conflict_class=conflict_class,
                pattern=label,
            ))
    return findings


def parse_policy_refs(body: str) -> list[PolicyRef]:
    refs: list[PolicyRef] = []
    for match in re.finditer(r"policy:(?:model\.)?([A-Za-z0-9_.-]+)", body or ""):
        refs.append(PolicyRef(_normalize_key(match.group(1))))
    return refs


def _ref_key_required(ref: str | PolicyRef) -> tuple[str, bool]:
    if isinstance(ref, PolicyRef):
        return _normalize_key(ref.key), ref.required
    return _normalize_key(str(ref)), True


def _normalize_key(value: str) -> str:
    key = value.strip().removeprefix("policy:").removeprefix("model.")
    if key == "impl":
        return "implementation_default"
    if key == "design":
        return "design_opus"
    return key


def _model_class(model: str) -> str:
    lowered = model.lower()
    if "opus" in lowered:
        return "opus"
    if "sonnet" in lowered:
        return "sonnet"
    if "haiku" in lowered:
        return "haiku"
    return short_text(model, max_len=80)
