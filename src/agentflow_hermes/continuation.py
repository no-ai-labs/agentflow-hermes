"""Central continuation-handler registry.

``continuation_store.py`` owns the durable state machine (``ContinuationState``,
legal transitions, persistence). This module owns the router-facing registry
that maps a ``ContinuationKind`` to the handler responsible for it, so
``continuation_engine.py`` (board-aware event ingestion) never hardcodes
per-kind branching.
"""

from __future__ import annotations

from .continuations.code_fix import CodeFixHandler
from .continuations.owner_input import OwnerInputHandler
from .outcome import ContinuationKind

_HANDLERS = {
    ContinuationKind.NEEDS_INPUT: OwnerInputHandler(),
    ContinuationKind.CODE_FIX: CodeFixHandler(),
}


def get_handler(kind: ContinuationKind):
    return _HANDLERS.get(kind)
