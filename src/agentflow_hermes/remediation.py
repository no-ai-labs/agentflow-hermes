from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from .live.sanitize import safe_durable_ref, safe_event_payload, short_text

_VERDICT_RE = re.compile(r"\bVerdict\s*:\s*(GO|BLOCK|NEED_MORE)\b", re.I)

_BLOCKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("stale_inline_route", re.compile(r"\bstale[_ -]?inline[_ -]?route\b|claude-openrouter-opus|claude-venice-opus|openrouter\s*,\s*anthropic|\b(?:Moonshot|Kimi)\b", re.I)),
    ("stale_trust_grant_wording", re.compile(r"\bstale[_ -]?trust[_ -]?grant[_ -]?wording\b|trust[- ]grant.*wording|wording.*trust[- ]grant", re.I)),
    ("missing_subscription", re.compile(r"\bmissing[_ -]?subscription\b|subscription edge|never reliably subscribed", re.I)),
    ("stale_final_fanin", re.compile(r"\bstale[_ -]?final[_ -]?fanin\b|stale final|final fan[- ]?in", re.I)),
)


@dataclass(frozen=True)
class ParsedVerdict:
    verdict: str
    confidence: str
    blockers: tuple[str, ...]
    source_ref: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "blockers": list(self.blockers),
            "source_ref": safe_durable_ref(self.source_ref, field="source_ref")[0],
        }


@dataclass(frozen=True)
class RemediationProposal:
    kind: str
    blocker: str
    title: str
    action: str
    idempotency_key: str
    metadata: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return safe_event_payload({
            "kind": self.kind,
            "blocker": self.blocker,
            "title": self.title,
            "action": self.action,
            "idempotency_key": self.idempotency_key,
            "metadata": self.metadata,
        })


def parse_verdict_summary(text: str, *, source_ref: str = "") -> ParsedVerdict:
    match = _VERDICT_RE.search(text or "")
    verdict = match.group(1).upper() if match else "UNKNOWN"
    blockers = tuple(_detect_blockers(text)) if verdict in {"BLOCK", "NEED_MORE"} else ()
    return ParsedVerdict(verdict=verdict, confidence="explicit" if match else "none", blockers=blockers, source_ref=source_ref)


def plan_remediation(
    summary: str,
    *,
    source_ref: str = "",
    prior_blockers: list[str] | tuple[str, ...] = (),
    dry_run: bool = True,
) -> dict[str, Any]:
    parsed = parse_verdict_summary(summary, source_ref=source_ref)
    if not dry_run:
        return {"success": False, "error": "live_mutation_disabled", "mutations": []}
    if parsed.verdict not in {"BLOCK", "NEED_MORE"} or parsed.confidence != "explicit":
        return {"success": False, "error": "no_blocking_verdict", "verdict": parsed.as_dict(), "mutations": []}
    if not parsed.blockers:
        return {"success": False, "error": "block_has_no_named_blocker", "verdict": parsed.as_dict(), "mutations": []}
    proposals = [_proposal_for_blocker(blocker, summary, source_ref=source_ref, repeat_count=prior_blockers.count(blocker) + 1) for blocker in parsed.blockers]
    return {
        "success": True,
        "dry_run": True,
        "verdict": parsed.as_dict(),
        "proposals": [p.as_dict() for p in proposals],
        "mutations": [],
    }


def _detect_blockers(text: str) -> list[str]:
    found: list[str] = []
    for blocker, pattern in _BLOCKER_PATTERNS:
        if pattern.search(text or "") and blocker not in found:
            found.append(blocker)
    return found


def _proposal_for_blocker(blocker: str, summary: str, *, source_ref: str, repeat_count: int) -> RemediationProposal:
    action_by_blocker = {
        "stale_inline_route": "append_policy_refs_and_migrate_inline_route_preview",
        "stale_trust_grant_wording": "narrow_doc_fix_trust_grant_wording",
        "missing_subscription": "propose_subscription_edge_review",
        "stale_final_fanin": "propose_final_v2_supersession_review",
    }
    title_by_blocker = {
        "stale_inline_route": "PolicyRef migration for stale inline route",
        "stale_trust_grant_wording": "Fix stale trust-grant wording",
        "missing_subscription": "Add missing ACK subscription edge",
        "stale_final_fanin": "Create final-v2 supersession proposal",
    }
    idem = _idempotency_key(blocker, source_ref or summary)
    metadata = {
        "dedupe_key": idem,
        "same_blocker_repeat_count": repeat_count,
        "source_ref": short_text(source_ref or "summary"),
        "candidate_sequence": _candidate_sequence(blocker),
        "dry_run_only": True,
    }
    return RemediationProposal(
        kind="remediation_proposal",
        blocker=blocker,
        title=title_by_blocker[blocker],
        action=action_by_blocker[blocker],
        idempotency_key=idem,
        metadata=metadata,
    )


def _candidate_sequence(blocker: str) -> list[str]:
    if blocker == "stale_trust_grant_wording":
        return ["fix", "review"]
    return ["fix", "review", "final-vN"]


def _idempotency_key(blocker: str, source: str) -> str:
    digest = hashlib.sha256(f"{blocker}:{source}".encode("utf-8")).hexdigest()[:16]
    return f"remediation:{blocker}:{digest}"
