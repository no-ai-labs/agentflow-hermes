#!/usr/bin/env python3
"""Global, registry-driven needs_input continuation watchdog.

M27 (plan section 9/14 commit 7): ``agentflowd`` is now the primary,
event-driven path for every continuation kind (GO/CODE_FIX/NEEDS_INPUT/
APPROVAL_REQUIRED/EXTERNAL_WAIT), reacting within seconds via
``agentflow_hermes.daemon``. This script is kept as a thin, still-fully-
functional COMPATIBILITY SHIM and reconciliation-only cadence: its job is to
catch up the durable board cursor/outbox if agentflowd was ever down, not to
be anyone's primary path. It intentionally still calls the exact same
underlying router agentflowd uses — ``continuation_engine.ingest_all_boards``
/ ``ingest_board_once`` — so there is exactly one router implementation
behind both entrypoints (no parallel/duplicate routing logic). Each cadence it:

  1. loads the declarative board registry + versioned InputContracts,
  2. runs the single board-aware scan loop (``ingest_all_boards``) over a
     read-only live per-board Kanban sqlite source,
  3. seeds a never-before-seen board's cursor to its current max event (no
     historical replay), and
  4. plans owner-input continuations for needs_input outcomes (or, with
     ``--all-kinds``, every kind the unified router understands).

Enrolling a future board is purely additive: add it to ``config/boards.yaml``,
or — since M27 commit 6 — do nothing at all, since agentflowd auto-discovers
every board under ``~/.hermes/kanban/boards``.

Output discipline: stdout ONLY for material owner-input/GO/BLOCK creation.
When a cadence produces nothing new the watchdog is silent (exit 0, no output),
so it is safe to run on a tight cron/timer without log spam.

Safety: dry-run by default (in-memory FakeBoardAdapter, no board mutation).
``--apply`` switches to the real gated CLI adapter that mutates the shared
boards; it must be passed explicitly.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_DEFAULT_REPO = Path(os.environ.get("AGENTFLOW_HERMES_REPO", "/home/duckran/dev/agentflow-hermes"))
_REPO_ROOT = _DEFAULT_REPO if _DEFAULT_REPO.exists() else Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agentflow_hermes.board_adapter import FakeBoardAdapter
from agentflow_hermes.board_events import BoardRegistryEntry, load_board_registry
from agentflow_hermes.continuation_config import load_contract_registry
from agentflow_hermes.continuation_engine import (
    ingest_all_boards,
    live_source_factory,
    real_adapter_factory,
)
from agentflow_hermes.continuation_store import ContinuationStore
from agentflow_hermes.outcome import ContinuationKind

_DEFAULT_REGISTRY = _REPO_ROOT / "config" / "boards.yaml"
_DEFAULT_CONTRACTS_DIR = _REPO_ROOT / "contracts"
_DEFAULT_DB = Path(os.environ.get("AGENTFLOW_NEEDS_INPUT_DB", "/home/duckran/.hermes/state/agentflow_needs_input_continuations.sqlite"))


def _contract_registry():
    paths = sorted(_DEFAULT_CONTRACTS_DIR.glob("*.yaml"))
    return load_contract_registry(paths)


def _fake_adapter_factory(board: str, entry: BoardRegistryEntry) -> FakeBoardAdapter:
    return FakeBoardAdapter()


def _material_lines(result: dict) -> list[str]:
    """Only material creations — quiet on seeds, noops, and empty scans."""
    lines: list[str] = []
    for board_result in result.get("boards", []):
        board = board_result.get("board", "")
        for item in board_result.get("results", []):
            action = item.get("action", "")
            if action == "owner_input_planned" and item.get("created"):
                lines.append(
                    f"OWNER-INPUT board={board} event={item.get('event_id')} "
                    f"instance={item.get('instance_id')} state={item.get('state')}"
                )
            elif action == "roadmap_routed" and item.get("router_success"):
                lines.append(f"GO board={board} event={item.get('event_id')} roadmap_routed")
            elif action == "code_fix_routed" and item.get("router_success"):
                lines.append(f"BLOCK board={board} event={item.get('event_id')} code_fix_routed")
    return lines


def run_once(
    *,
    registry_path: Path,
    db_path: Path,
    apply: bool,
    all_kinds: bool,
) -> tuple[int, str]:
    registry = load_board_registry(registry_path)
    if not registry:
        return 2, "BLOCK empty_or_unreadable_board_registry"

    store = ContinuationStore(db_path)
    contracts = _contract_registry()
    adapter_factory = real_adapter_factory if apply else _fake_adapter_factory
    handle_kinds = None if all_kinds else (ContinuationKind.NEEDS_INPUT,)

    result = ingest_all_boards(
        registry=registry,
        store=store,
        contract_registry=contracts,
        source_factory=live_source_factory,
        adapter_factory=adapter_factory,
        handle_kinds=handle_kinds,
    )

    lines = _material_lines(result)
    return 0, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default=str(_DEFAULT_REGISTRY))
    parser.add_argument("--db", default=str(_DEFAULT_DB))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Mutate the real shared boards via the gated CLI adapter (default: dry-run).",
    )
    parser.add_argument(
        "--all-kinds",
        action="store_true",
        help="Also route GO/roadmap and code-fix BLOCK outcomes (default: needs_input only).",
    )
    args = parser.parse_args(argv)

    code, output = run_once(
        registry_path=Path(args.registry),
        db_path=Path(args.db),
        apply=args.apply,
        all_kinds=args.all_kinds,
    )
    if output:
        print(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
