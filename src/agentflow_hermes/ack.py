from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .states import JobStatus, normalize_status

_ACK_RE = re.compile(r"\[JOB ACK\](?P<body>.*)", re.IGNORECASE | re.DOTALL)
_FIELD_RE = re.compile(r"^(?P<key>[a-zA-Z_][\w-]*):\s*(?P<value>.*)$")
_CONTINUATION_RE = re.compile(r"^(?:\s+|[-*+]\s+|\d+[.)]\s+).*$")


class AckError(ValueError):
    """Raised when an ACK block is invalid or cannot be accepted."""

    def __init__(self, reason: str, deadletter: bool = True, payload: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.deadletter = deadletter
        self.payload = dict(payload or {})


@dataclass(frozen=True)
class AckPayload:
    job_id: str
    status: JobStatus
    summary: str
    artifacts: str
    blockers: str
    raw_fields: dict[str, str]


def parse_ack_block(text: str) -> dict[str, str]:
    """Extract key:value fields from the first [JOB ACK] block in *text*.

    The dispatch prompt allows fields such as ``artifacts`` and ``blockers`` to
    be emitted as Markdown-style multiline lists::

        artifacts:
        - ref://artifact

    Preserve those continuation lines as part of the preceding field instead of
    dropping them.
    """
    match = _ACK_RE.search(text or "")
    if not match:
        raise AckError("missing [JOB ACK] block", deadletter=False)
    fields: dict[str, list[str]] = {}
    current_key = ""
    for raw in match.group("body").splitlines():
        m = _FIELD_RE.match(raw.strip())
        if m:
            current_key = m.group("key").replace("-", "_").lower()
            fields[current_key] = [m.group("value").strip()]
        elif current_key and (not raw.strip() or _CONTINUATION_RE.match(raw)):
            fields[current_key].append(raw.rstrip())
    return {key: "\n".join(value).strip() for key, value in fields.items()}


def validate_ack(fields: Mapping[str, str]) -> AckPayload:
    """Validate extracted ACK fields and return a structured payload.

    Raises AckError for missing/invalid values. Invalid status values are
    deadlettered because they reference a concrete job_id.
    """
    job_id = fields.get("job_id", "").strip()
    if not job_id:
        raise AckError("missing_job_id", deadletter=False)

    status_raw = fields.get("status", "").strip()
    if not status_raw:
        raise AckError("missing_status", deadletter=False)
    try:
        status = normalize_status(status_raw)
    except ValueError as exc:
        raise AckError(
            "invalid_status",
            deadletter=True,
            payload={"job_id": job_id, "status": status_raw},
        ) from exc

    if status == JobStatus.DISPATCHED:
        raise AckError(
            "invalid_status",
            deadletter=True,
            payload={"job_id": job_id, "status": status_raw},
        )

    return AckPayload(
        job_id=job_id,
        status=status,
        summary=fields.get("summary", "").strip(),
        artifacts=fields.get("artifacts", "").strip(),
        blockers=fields.get("blockers", "").strip(),
        raw_fields=dict(fields),
    )
