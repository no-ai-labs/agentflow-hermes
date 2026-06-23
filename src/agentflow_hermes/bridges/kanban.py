from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_VERDICT_GO_RE = re.compile(r"\bVerdict\s*:\s*GO\b", re.IGNORECASE)
_VERDICT_BLOCK_RE = re.compile(r"\bVerdict\s*:\s*BLOCK\b", re.IGNORECASE)
_VERDICT_NEED_MORE_RE = re.compile(r"\bVerdict\s*:\s*NEED_MORE\b", re.IGNORECASE)
_REMEDIATION_RE = re.compile(r"\bremediation\b", re.IGNORECASE)
_FIX_RE = re.compile(r"\bfix\b|\bfix(?:ed|es)\b", re.IGNORECASE)
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@+=-]{0,239}$")
_CARD_REF_RE = re.compile(r"\b(?:t_[0-9a-fA-F]{8}|card_[A-Za-z0-9_]+)\b")
_MAX_FIELD_LEN = 480


class KanbanResolverError(Exception):
    def __init__(self, reason: str, unsafe: bool = True, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.unsafe = unsafe
        self.details = dict(details or {})


def _short_text(value: Any, *, max_len: int = _MAX_FIELD_LEN) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text[:max_len]


def _has_word(text: str, pattern: re.Pattern[str]) -> bool:
    return bool(pattern.search(text or ""))


def _extract_verdict(text: str) -> str:
    if _VERDICT_BLOCK_RE.search(text or ""):
        return "BLOCK"
    if _VERDICT_NEED_MORE_RE.search(text or ""):
        return "NEED_MORE"
    if _VERDICT_GO_RE.search(text or ""):
        return "GO"
    return ""


def _contains_ref(text: str, ref: str) -> bool:
    if not ref or not text:
        return False
    if ref in text:
        return True
    # Allow matching hyphenated / compact ids, e.g. card_abc123 and abc123.
    compact = ref.split("_", 1)[-1] if "_" in ref else ref
    if compact and compact in text and len(compact) >= 6:
        return True
    return False


def _card_text(card: dict[str, Any]) -> str:
    parts = [
        card.get("title", ""),
        card.get("body", ""),
        card.get("summary", ""),
        card.get("result", ""),
    ]
    comments = card.get("comments") or []
    if isinstance(comments, list):
        parts.extend(str(c) for c in comments)
    metadata = card.get("metadata") or {}
    if isinstance(metadata, dict):
        parts.extend(f"{k}: {v}" for k, v in metadata.items())
    return "\n".join(parts)


def _is_blocked(card: dict[str, Any]) -> bool:
    status = str(card.get("status") or "").lower()
    if status in {"blocked", "waiting", "waiting_review", "on_hold", "hold"}:
        return True
    return False


def _mentions_remediation(card: dict[str, Any]) -> bool:
    text = _card_text(card)
    return _has_word(text, _REMEDIATION_RE) or _has_word(text, _FIX_RE)


def _mentions_blocked_card(blocked_card: dict[str, Any], other: dict[str, Any]) -> bool:
    blocked_id = str(blocked_card.get("id") or "")
    blocked_text = _card_text(blocked_card)
    other_text = _card_text(other)
    if _contains_ref(other_text, blocked_id):
        return True
    blocker_id = str(blocked_card.get("blocked_by") or blocked_card.get("blocker_id") or "")
    if blocker_id and _contains_ref(other_text, blocker_id):
        return True
    other_id = str(other.get("id") or "")
    if other_id and (_contains_ref(blocked_text, other_id) or _contains_ref(other_text, other_id)):
        return True
    return False


def _safe_card_ref(card: dict[str, Any]) -> str:
    ref = _short_text(card.get("id") or "")
    if not ref:
        return ""
    if _SAFE_REF_RE.fullmatch(ref):
        return ref
    return "card:redacted"


def _safe_summary(card: dict[str, Any]) -> str:
    return _short_text(card.get("title") or card.get("summary") or "card")


def _values_as_refs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        refs: list[str] = []
        for item in value:
            refs.extend(_values_as_refs(item))
        return refs
    if isinstance(value, dict):
        refs: list[str] = []
        for item in value.values():
            refs.extend(_values_as_refs(item))
        return refs
    text = _short_text(value)
    if not text:
        return []
    found = _CARD_REF_RE.findall(text)
    return found or [text]


def _metadata_refs(card: dict[str, Any], keys: tuple[str, ...]) -> set[str]:
    metadata = card.get("metadata") or {}
    if not isinstance(metadata, dict):
        return set()
    refs: set[str] = set()
    for key in keys:
        refs.update(_values_as_refs(metadata.get(key)))
    return {ref for ref in refs if ref}


def _has_structured_remediation_relation(
    blocked_card: dict[str, Any], remediation_review: dict[str, Any]
) -> bool:
    """Return True when explicit metadata selects this review/remediation path.

    Text can legitimately mention the original blocker, fix card, and fix review.
    Do not treat that as ambiguous when structured metadata names this review or
    when the review metadata explicitly says it remediates the blocker and
    unblocks this blocked card.
    """
    blocked_id = str(blocked_card.get("id") or "")
    review_id = str(remediation_review.get("id") or "")
    if not blocked_id or not review_id:
        return False

    blocked_review_refs = _metadata_refs(
        blocked_card,
        (
            "remediation_review",
            "remediation_reviews",
            "blocking_review",
            "blocking_reviews",
            "review_id",
            "review_ids",
            "unblock_review",
            "unblock_reviews",
        ),
    )
    if review_id in blocked_review_refs:
        return True

    review_unblocks = _metadata_refs(
        remediation_review,
        ("unblock_ok", "unblocks", "downstream", "blocked_card", "blocked_cards"),
    )
    if blocked_id in review_unblocks:
        return True

    blocker_refs = _metadata_refs(
        blocked_card,
        ("blocked_by", "blocker_id", "blockers", "blocking_review", "blocking_reviews"),
    )
    remediates = _metadata_refs(
        remediation_review,
        ("remediates", "remediate", "remediated", "remediated_blocker", "blocker_id"),
    )
    if blocker_refs and remediates.intersection(blocker_refs):
        return True

    blocked_text = _card_text(blocked_card)
    if any(_contains_ref(blocked_text, ref) for ref in remediates):
        return True

    return False


def _ambiguous_blockers(blocked_card: dict[str, Any]) -> list[str]:
    metadata = blocked_card.get("metadata") or {}
    if isinstance(metadata, dict):
        for key in ("blocked_by", "blocker_id", "blockers", "blocking_reviews"):
            value = metadata.get(key)
            if isinstance(value, list):
                refs = sorted({_short_text(item) for item in value if _short_text(item)})
                if len(refs) > 1:
                    return refs

    text = _card_text(blocked_card)
    refs = sorted({ref for ref in _CARD_REF_RE.findall(text) if ref != blocked_card.get("id")})
    blockerish = any(word in text.lower() for word in ("blocked by", "blocker", "blocking review"))
    return refs if blockerish and len(refs) > 1 else []


def _validate_input(blocked_card: dict[str, Any] | None, remediation_review: dict[str, Any] | None) -> None:
    if not blocked_card or not str(blocked_card.get("id") or "").strip():
        raise KanbanResolverError("missing_blocked_card_id", unsafe=True)
    if not remediation_review or not str(remediation_review.get("id") or "").strip():
        raise KanbanResolverError("missing_remediation_review_id", unsafe=True)


def _resolve_candidate(
    blocked_card: dict[str, Any],
    remediation_review: dict[str, Any],
    *,
    required_go: bool = True,
    require_provenance: bool = True,
) -> dict[str, Any]:
    _validate_input(blocked_card, remediation_review)

    if not _is_blocked(blocked_card):
        raise KanbanResolverError(
            "blocked_card_not_blocked",
            unsafe=False,
            details={
                "blocked_card_id": _safe_card_ref(blocked_card),
                "status": str(blocked_card.get("status") or ""),
            },
        )

    if not _mentions_remediation(blocked_card):
        raise KanbanResolverError(
            "blocked_card_lacks_remediation_reference",
            unsafe=False,
            details={"blocked_card_id": _safe_card_ref(blocked_card)},
        )

    structured_relation = _has_structured_remediation_relation(blocked_card, remediation_review)
    ambiguous_refs = [] if structured_relation else _ambiguous_blockers(blocked_card)
    if ambiguous_refs:
        raise KanbanResolverError(
            "ambiguous_multiple_blockers",
            unsafe=True,
            details={
                "blocked_card_id": _safe_card_ref(blocked_card),
                "blocker_refs": ambiguous_refs[:5],
            },
        )

    review_text = _card_text(remediation_review)
    verdict = _extract_verdict(review_text)

    if verdict == "BLOCK":
        raise KanbanResolverError(
            "remediation_review_has_block_verdict",
            unsafe=True,
            details={
                "remediation_review_id": _safe_card_ref(remediation_review),
                "verdict": verdict,
            },
        )
    if verdict == "NEED_MORE":
        raise KanbanResolverError(
            "remediation_review_needs_more_work",
            unsafe=True,
            details={
                "remediation_review_id": _safe_card_ref(remediation_review),
                "verdict": verdict,
            },
        )
    if required_go and verdict != "GO":
        raise KanbanResolverError(
            "missing_go_verdict",
            unsafe=True,
            details={
                "remediation_review_id": _safe_card_ref(remediation_review),
                "verdict": verdict or "none",
            },
        )

    if require_provenance and not _mentions_blocked_card(blocked_card, remediation_review):
        raise KanbanResolverError(
            "remediation_review_unrelated_to_blocked_card",
            unsafe=True,
            details={
                "blocked_card_id": _safe_card_ref(blocked_card),
                "remediation_review_id": _safe_card_ref(remediation_review),
            },
        )

    blocked_ref = _safe_card_ref(blocked_card)
    review_ref = _safe_card_ref(remediation_review)

    return {
        "success": True,
        "dry_run": True,
        "resolved": True,
        "blocked_card_id": blocked_ref,
        "remediation_review_id": review_ref,
        "provenance_ok": True,
        "verdict": verdict,
        "evidence": {
            "blocked_status": str(blocked_card.get("status") or ""),
            "blocked_summary": _safe_summary(blocked_card),
            "review_summary": _safe_summary(remediation_review),
            "references_remediation": True,
            "structured_relation": structured_relation,
        },
        "candidate": {
            "action": "unblock_and_dispatch",
            "blocked_card_id": blocked_ref,
            "remediation_review_id": review_ref,
            "reason": f"remediation review {review_ref} has explicit Verdict: GO and correlates to blocked card {blocked_ref}",
        },
        "mutations": [],
    }


def resolve_blocked_remediation(
    blocked_card: dict[str, Any] | None,
    remediation_review: dict[str, Any] | None,
    *,
    dry_run: bool = True,
    required_go: bool = True,
    require_provenance: bool = True,
) -> dict[str, Any]:
    """Dry-run resolver for a single blocked card with a single remediation review.

    No Hermes Kanban mutation, no dispatch, and no active wake. Returns a
    structured candidate with evidence or a fail-closed refusal.
    """
    if not dry_run:
        return {
            "success": False,
            "resolved": False,
            "error": "live_mutation_disabled",
            "dry_run": False,
        }

    try:
        return _resolve_candidate(
            blocked_card,
            remediation_review,
            required_go=required_go,
            require_provenance=require_provenance,
        )
    except KanbanResolverError as exc:
        return {
            "success": False,
            "resolved": False,
            "error": exc.reason,
            "unsafe": exc.unsafe,
            "dry_run": True,
            "details": exc.details,
        }


def load_fixture(path: str | Path) -> dict[str, Any]:
    if str(path) == "-":
        import sys

        data = sys.stdin.read()
    else:
        data = Path(path).read_text(encoding="utf-8")
    return json.loads(data)
