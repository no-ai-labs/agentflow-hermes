from __future__ import annotations

# Compatibility shim for the original M1/M2 import path.
from .bridges.cron import (  # noqa: F401
    CronMarker,
    classify_markers,
    file_hash,
    ingest_cron_output,
    make_dedupe_key,
    output_hash,
    scan_cron_output,
)
