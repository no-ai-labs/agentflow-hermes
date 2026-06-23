from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..store import AgentFlowStore

_AF_CRON_RE = re.compile(
    r"\[AF-CRON\]\s+"
    r"kind=(?P<kind>\w+)\s+"
    r"ref=(?P<ref>\S+)\s+"
    r"hash=(?P<hash>\S+)\s+"
    r"summary=(?P<summary>.+?)(?:\s*$)",
    re.IGNORECASE,
)
_ACTIVE_WAKE_PREFIX = "HERMES_ACTIVE_WAKE "


@dataclass(frozen=True)
class CronMarker:
    kind: str
    ref: str
    hash: str
    summary: str
    metadata: dict[str, Any]


def _short_text(value: Any, *, max_len: int = 240) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text[:max_len]


def _safe_active_wake_metadata(raw: dict[str, Any]) -> dict[str, str]:
    """Return compact material-event metadata safe for durable storage."""
    allowed = ("event_key", "status", "summary", "target", "job_id", "run_id")
    return {key: _short_text(raw.get(key)) for key in allowed if raw.get(key) not in (None, "")}


def output_hash(text: str) -> str:
    return sha256((text or "").encode("utf-8")).hexdigest()


def file_hash(path: str | Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_markers(text: str, *, default_ref: str = "", default_hash: str = "") -> list[CronMarker]:
    """Parse safe material/noise markers from a cron output excerpt.

    Supported formats:
    - ``HERMES_ACTIVE_WAKE {json}`` at the start of a line. This is treated as
      material metadata only; no active wake is dispatched here.
    - Legacy ``[AF-CRON] kind=<material|noise> ref=<ref> hash=<hash> summary=...``.
    """
    markers: list[CronMarker] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(_ACTIVE_WAKE_PREFIX):
            payload_text = stripped[len(_ACTIVE_WAKE_PREFIX) :].strip()
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                markers.append(
                    CronMarker(
                        kind="noise",
                        ref=default_ref,
                        hash=default_hash,
                        summary="malformed active wake marker",
                        metadata={"marker": "active_wake", "malformed": "true"},
                    )
                )
                continue
            if not isinstance(payload, dict):
                payload = {}
            metadata = _safe_active_wake_metadata(payload)
            summary = metadata.get("summary") or metadata.get("status") or metadata.get("event_key") or "cron material event"
            markers.append(
                CronMarker(
                    kind="material",
                    ref=default_ref,
                    hash=default_hash,
                    summary=_short_text(summary),
                    metadata={"marker": "active_wake", **metadata, "live_wake_disabled": "true"},
                )
            )
            continue

        match = _AF_CRON_RE.search(stripped)
        if match:
            markers.append(
                CronMarker(
                    kind=match.group("kind").lower(),
                    ref=match.group("ref").strip(),
                    hash=match.group("hash").strip(),
                    summary=_short_text(match.group("summary")),
                    metadata={"marker": "af_cron"},
                )
            )
    return markers


def make_dedupe_key(
    source_kind: str = "cron",
    correlation_id: str = "",
    source_hash: str = "",
    *,
    job_id: str = "",
    run_id: str = "",
    target: str = "",
) -> str:
    """Return stable human-readable cron dedupe key.

    Shape: ``cron:<job_id>:<run_id-or-output_hash>:<target>``. The positional
    ``correlation_id`` argument is kept for M1 compatibility and is used as the
    job id fallback.
    """
    prefix = source_kind or "cron"
    stable_job = _short_text(job_id or correlation_id or "unknown", max_len=96)
    stable_run = _short_text(run_id or source_hash or "unknown", max_len=96)
    stable_target = _short_text(target or "default", max_len=96)
    return f"{prefix}:{stable_job}:{stable_run}:{stable_target}"


def _get_job_by_dedupe_key(store: AgentFlowStore, dedupe_key: str) -> dict[str, Any] | None:
    if not dedupe_key:
        return None
    store.init()
    with store.connect() as con:
        row = con.execute("select * from jobs where dedupe_key=? limit 1", (dedupe_key,)).fetchone()
    return dict(row) if row else None


def _record_duplicate(store: AgentFlowStore, job: dict[str, Any], payload: dict[str, Any]) -> None:
    store.record_event(job["id"], "cron_duplicate", payload=payload)


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
    job_id: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    """Dry-run cron bridge ingestion by ref/hash/marker metadata.

    Raw cron transcripts are not stored. ``marker_text`` may contain only the
    small marker excerpt needed to classify material/no-change output.
    """
    source_hash = source_hash or output_hash(marker_text)
    markers = classify_markers(marker_text, default_ref=source_ref, default_hash=source_hash)
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
                "raw_output_stored": False,
            },
        )
        return {"success": True, "applied": False, "duplicate": False, "reason": "no_material_marker", "job_id": None}

    marker = material[0]
    effective_ref = marker.ref or source_ref
    effective_hash = marker.hash or source_hash
    effective_job_id = job_id or marker.metadata.get("job_id") or correlation_id or source_ref or "cron"
    effective_run_id = run_id or marker.metadata.get("run_id") or effective_hash
    dedupe_key = make_dedupe_key(source, correlation_id, effective_hash, job_id=effective_job_id, run_id=effective_run_id, target=target)

    existing = _get_job_by_dedupe_key(store, dedupe_key) or store.get_job_by_source_hash(effective_hash)
    duplicate_payload = {
        "source": source,
        "source_ref": effective_ref,
        "source_hash": effective_hash,
        "dedupe_key": dedupe_key,
        "marker": marker.metadata.get("marker", ""),
        "raw_output_stored": False,
    }
    if existing:
        _record_duplicate(store, existing, duplicate_payload)
        return {"success": True, "applied": False, "duplicate": True, "job_id": existing["id"], "dedupe_key": dedupe_key}

    body = (
        "Cron material event detected for AgentFlow dry-run dispatch.\n"
        f"source_ref: {effective_ref}\n"
        f"source_hash: {effective_hash}\n"
        f"summary: {marker.summary}\n"
        "live_wake_dispatch: disabled"
    )
    result = store.enqueue(
        title=title or marker.summary or f"cron:{effective_ref}",
        body=body,
        target=target or marker.metadata.get("target", ""),
        origin_return=origin_return,
        dedupe_key=dedupe_key,
        correlation_id=correlation_id or effective_job_id,
        source_kind=source,
        source_id=effective_job_id,
        source_ref=effective_ref,
        source_hash=effective_hash,
    )
    if result.get("success"):
        event_payload = {
            "source": source,
            "source_ref": effective_ref,
            "source_hash": effective_hash,
            "dedupe_key": dedupe_key,
            "marker": marker.metadata.get("marker", ""),
            "material_event": marker.metadata,
            "raw_output_stored": False,
            "live_wake_disabled": True,
        }
        store.record_event(result["job_id"], "cron_ingested", payload=event_payload)
        return {"success": True, "applied": True, "duplicate": False, "job_id": result["job_id"], "dedupe_key": dedupe_key}
    return result


def scan_cron_output(
    store: AgentFlowStore,
    *,
    output_file: str | Path,
    source_ref: str = "",
    source_hash: str = "",
    source: str = "cron",
    job_id: str = "",
    run_id: str = "",
    target: str = "",
    origin_return: str = "",
    title: str = "",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Scan a cron output artifact and enqueue a dry-run AgentFlow job if material."""
    if not dry_run:
        return {"success": False, "error": "live_dispatch_disabled", "dry_run": False}
    path = Path(output_file)
    digest = source_hash or file_hash(path)
    # The output file is read for classification only. The store only receives
    # marker metadata, ref, hash, and summary.
    marker_text = path.read_text(encoding="utf-8", errors="replace")
    ref = source_ref or f"file://{path.name}"
    return ingest_cron_output(
        store,
        source_ref=ref,
        source_hash=digest,
        marker_text=marker_text,
        source=source,
        correlation_id=job_id,
        target=target,
        origin_return=origin_return,
        title=title,
        job_id=job_id,
        run_id=run_id,
    )
