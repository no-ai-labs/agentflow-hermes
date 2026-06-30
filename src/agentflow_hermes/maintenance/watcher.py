from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

from agentflow_hermes.live.sanitize import safe_event_payload, short_text
from agentflow_hermes.maintenance.gitprobe import GitProbeResult

_OPEN_STATUSES = {"ready", "running", "blocked", "todo", "pending", "queued", "open", "new"}


@dataclass(frozen=True)
class SyncProposal:
    action: str
    dedupe_key: str
    title: str
    metadata: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return safe_event_payload({
            "action": self.action,
            "dedupe_key": self.dedupe_key,
            "title": self.title,
            "metadata": self.metadata,
            "mutations": [],
        })


def propose_sync_graph(
    probe: GitProbeResult,
    *,
    existing_cards: Iterable[dict[str, Any]] = (),
    behind_threshold: int = 1,
    origin: str = "discord:#hermes-main",
) -> dict[str, Any]:
    """Return a read-only sync graph proposal or no-op.

    This function never creates Kanban cards. It only reports the proposal shape and
    dedupe key so an operator/reviewer path can decide whether to apply later.
    """
    if probe.behind < behind_threshold:
        return {"success": True, "action": "noop", "reason": "behind_threshold_not_met", "mutations": []}
    dedupe_key = sync_dedupe_key(probe.repo_id, probe.upstream_sha)
    if _existing_sync_graph(dedupe_key, existing_cards):
        return {"success": True, "action": "noop", "reason": "existing_sync_graph", "dedupe_key": dedupe_key, "mutations": []}
    metadata = {
        "repo_id": probe.repo_id,
        "upstream_sha": probe.upstream_sha,
        "behind": probe.behind,
        "ahead": probe.ahead,
        "dirty": probe.dirty,
        "local_carried": probe.local_carried,
        "ff_eligible": probe.ff_eligible,
        "cycle_safe_now": False,
        "origin_channel": short_text(origin),
        "return_target": short_text(origin),
        "wants_subscription": True,
        "intent_kind": "remediation",
        "dry_run_only": True,
    }
    proposal = SyncProposal(
        action="create_sync_graph_proposal",
        dedupe_key=dedupe_key,
        title=f"Maintenance sync proposal {probe.upstream_sha[:12]}",
        metadata=metadata,
    )
    return {"success": True, "proposal": proposal.as_dict(), "mutations": []}


def sync_dedupe_key(repo_id: str, upstream_sha: str) -> str:
    digest = hashlib.sha256(f"{repo_id}:{upstream_sha}".encode("utf-8")).hexdigest()[:16]
    return f"maint:sync:{digest}"


def _existing_sync_graph(dedupe_key: str, cards: Iterable[dict[str, Any]]) -> bool:
    for card in cards:
        status = str(card.get("status") or "").lower()
        raw_metadata = card.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        title = str(card.get("title") or "")
        if status and status not in _OPEN_STATUSES:
            continue
        if metadata.get("dedupe_key") == dedupe_key or card.get("dedupe_key") == dedupe_key:
            return True
        if dedupe_key in title:
            return True
    return False
