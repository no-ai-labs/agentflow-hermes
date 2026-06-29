from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_PRIVATE_PATH_RE = re.compile(r"(?:file://)?/(?:home|Users|var/folders|tmp|private|mnt|media)/\S+", re.IGNORECASE)
_WINDOWS_PRIVATE_PATH_RE = re.compile(r"\b[A-Za-z]:\\\\(?:Users|Documents and Settings)\\\\\S+", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(?:"
    r"\b(?:TOKEN|API[_-]?KEY|SECRET|PASSWORD|PASSWD|AUTHORIZATION|BEARER|SESSION|COOKIE)\b\s*[:=]\s*\S+"
    r"|\bBearer\s+\S+"
    r"|\b(?:sk|ghp|gho|github_pat|xox[baprs])-[-_A-Za-z0-9]{12,}"
    r")",
    re.IGNORECASE,
)
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@+=-]{0,239}$")


def short_text(value: Any, *, max_len: int = 240) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text[:max_len]


def contains_sensitive_text(value: Any) -> bool:
    text = "" if value is None else str(value)
    return bool(_SECRET_RE.search(text) or _PRIVATE_PATH_RE.search(text) or _WINDOWS_PRIVATE_PATH_RE.search(text))


def sanitize_summary(value: Any, *, fallback: str = "material event") -> tuple[str, bool]:
    text = short_text(value)
    if not text:
        return fallback, False
    if contains_sensitive_text(text):
        return fallback, True
    return text, False


def safe_source_ref(value: Any, *, source_hash: str = "") -> tuple[str, bool]:
    ref = short_text(value, max_len=240)
    if not ref:
        return "", False
    if contains_sensitive_text(ref) or not _SAFE_REF_RE.fullmatch(ref):
        digest = _sha256(ref)[:16]
        return f"ref:sha256:{digest}", True
    return ref, False


def safe_body_for_delivery(body: str, *, job_id: str = "", source_ref: str = "", source_hash: str = "") -> str:
    """Return a scrubbed, bounded body suitable for gateway delivery.

    Removes secrets, private paths, and caps length. The result is explicitly
    metadata/refs, never a raw transcript.
    """
    text = short_text(body, max_len=2000)
    lines = text.splitlines()
    safe_lines: list[str] = []
    for line in lines:
        if contains_sensitive_text(line):
            continue
        safe_lines.append(line)
    safe = "\n".join(safe_lines)
    if not safe.strip():
        safe = "AgentFlow dispatch notification"
    safe_ref, _ = safe_source_ref(source_ref, source_hash=source_hash)
    header = f"job_id: {short_text(job_id)}\nsource_ref: {safe_ref}\nsource_hash: {short_text(source_hash)}\n"
    return header + safe


def policy_snapshot(policy: Any) -> str:
    """Serialize a compact, safe policy snapshot for receipts."""
    if hasattr(policy, "as_dict"):
        snapshot = policy.as_dict()
    else:
        snapshot = dict(policy) if isinstance(policy, dict) else {}
    # Drop any accidental secret/absolute-path values.
    cleaned: dict[str, Any] = {}
    for k, v in snapshot.items():
        if isinstance(v, str):
            cleaned[k] = "redacted" if contains_sensitive_text(v) else short_text(v)
        elif isinstance(v, (list, tuple)):
            cleaned[k] = ["redacted" if contains_sensitive_text(x) else short_text(x) for x in v]
        else:
            cleaned[k] = v
    return json.dumps(cleaned, ensure_ascii=False)


def _sha256(text: str) -> str:
    from hashlib import sha256

    return sha256((text or "").encode("utf-8")).hexdigest()
