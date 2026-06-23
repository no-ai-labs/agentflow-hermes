from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from .store import AgentFlowStore

_MARKER_RE = re.compile(
    r"\[AF-CRON\]\s+"
    r"kind=(?P<kind>\w+)\s+"
    r"ref=(?P<ref>\S+)\s+"
    r"hash=(?P<hash>\S+)\s+"
    r"summary=(?P<summary>.+?)(?:\s*$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CronMarker:
    kind: str
    ref: str
    hash: str
    summary: str


def classify_markers(text: str) -> list[CronMarker]:
    """Parse [AF-CRON] marker lines from cron output excerpt."""
    markers: list[CronMarker] = []
    for line in (text or "").splitlines():
        match = _MARKER_RE.search(line)
        if match:
            markers.append(
                CronMarker(
                    kind=match.group("kind").lower(),
                    ref=match.group("ref").strip(),
                    hash=match.group("hash").strip(),
                    summary=match.group("summary").strip(),
                )
            )
    return markers


def make_dedupe_key(source_kind: str, correlation_id: str, source_hash: str) -> str:
    """Compute a short dedupe key from source kind, correlation, and content hash."""
    payload = f"{source_kind}:{correlation_id}:{source_hash}".encode("utf-8")
    return sha256(payload).hexdigest()[:16]


def ingest_cron_output(
    store: AgentFlowStore,
    *,
    source_ref: str,
    source_hash: str,
    marker_text: str = "",
    source: str = "cron",
    correlation_id: str = "",
    target: str = "",
    origin_return: str = "",
    title: str = "",
) -> dict[str, Any]:
    """Dry-run cron bridge ingestion.

    Receives only source ref/hash and a small marker excerpt. Raw cron
    transcripts are never stored.
    """
    markers = classify_markers(marker_text)
    material = [m for m in markers if m.kind == "material"]
    noise = [m for m in markers if m.kind == "noise"]

    if not material:
        store.record_event(
            "",
            "cron_noise",
            payload={
                "source": source,
                "source_ref": source_ref,
                "source_hash": source_hash,
                "markers": len(noise),
            },
        )
        return {
            "success": True,
            "applied": False,
            "duplicate": False,
            "reason": "no_material_marker",
            "job_id": None,
        }

    marker = material[0]
    dedupe_key = make_dedupe_key(source, correlation_id or source_hash, source_hash)

    # Check for existing job with the same source_hash before attempting insert.
    existing = store.get_job_by_source_hash(source_hash)
    if existing:
        store.record_event(
            existing["id"],
            "cron_duplicate",
            payload={
                "source": source,
                "source_ref": source_ref,
                "source_hash": source_hash,
                "dedupe_key": dedupe_key,
            },
        )
        return {
            "success": True,
            "applied": False,
            "duplicate": True,
            "job_id": existing["id"],
        }

    result = store.enqueue(
        title=title or marker.summary or f"cron:{source_ref}",
        body=marker.summary,
        target=target,
        origin_return=origin_return,
        dedupe_key=dedupe_key,
        correlation_id=correlation_id,
        source_kind=source,
        source_id=source_ref,
        source_ref=source_ref,
        source_hash=source_hash,
    )

    if result.get("success"):
        store.record_event(
            result["job_id"],
            "cron_ingested",
            payload={
                "source": source,
                "source_ref": source_ref,
                "source_hash": source_hash,
                "dedupe_key": dedupe_key,
            },
        )
        return {
            "success": True,
            "applied": True,
            "duplicate": False,
            "job_id": result["job_id"],
        }

    return result
