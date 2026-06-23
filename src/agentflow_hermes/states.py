from __future__ import annotations

from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    WAITING_REVIEW = "waiting_review"
    WAITING_USER = "waiting_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


FINAL_STATES = {JobStatus.SUCCEEDED, JobStatus.FAILED}

ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    # M0 compatibility: legacy callers may enqueue then ACK directly without a
    # separate dispatch mutation. dispatch-dry-run records DISPATCHED for the
    # richer M1 lifecycle, but direct terminal success/failure ACKs remain valid.
    JobStatus.QUEUED: {
        JobStatus.DISPATCHED,
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
    },
    JobStatus.DISPATCHED: {
        JobStatus.WAITING_REVIEW,
        JobStatus.WAITING_USER,
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
    },
    JobStatus.WAITING_REVIEW: {
        JobStatus.DISPATCHED,
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
    },
    JobStatus.WAITING_USER: {
        JobStatus.DISPATCHED,
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
    },
    JobStatus.SUCCEEDED: set(),
    JobStatus.FAILED: set(),
}


def normalize_status(value: str) -> JobStatus:
    """Return a JobStatus enum member for a string value.

    Raises ValueError for unknown statuses.
    """
    try:
        return JobStatus(value.lower())
    except ValueError as exc:
        raise ValueError(f"invalid status: {value!r}") from exc
