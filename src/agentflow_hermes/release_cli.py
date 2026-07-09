"""CLI for the bounded GitHub release publish trigger (M20).

Thin orchestration only: reads a final GO summary file and an optional repo
config, then delegates entirely to :func:`evaluate_release_action`. This
module never runs git/gh itself except through the injected runner, and the
injected runner is only ever the real subprocess one
(:func:`default_release_cli_runner`) when both ``config.apply_mode`` and
``--apply`` are set — the same double-gate the M17 roadmap watchdog CLI uses.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .live.sanitize import sanitize_string
from .release_action import (
    ReleaseActionConfig,
    default_release_cli_runner,
    evaluate_release_action,
    load_receipts_ledger,
    load_release_action_config,
    save_receipts_ledger,
)


def add_release_github_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--summary-file", required=True)
    parser.add_argument("--config", default="", help="path to a release-action config; omitted = release actions disabled")
    parser.add_argument("--receipts-file", default="")
    parser.add_argument("--apply", action="store_true", default=False)


def run_release_github(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    try:
        summary = Path(args.summary_file).read_text(encoding="utf-8")
    except OSError as exc:
        return 2, {"success": False, "error": "summary_read_failed", "detail": sanitize_string(str(exc))}

    if args.config:
        try:
            config = load_release_action_config(args.config)
        except (OSError, ValueError) as exc:
            return 2, {"success": False, "error": "malformed_config", "detail": sanitize_string(str(exc))}
    else:
        # No config supplied: release actions stay off (release_actions_enabled
        # defaults to False), so the run is always a safe noop.
        config = ReleaseActionConfig()

    receipts_path = args.receipts_file or ""
    ledger = load_receipts_ledger(receipts_path) if receipts_path else {}

    apply_armed = bool(args.apply and config.apply_mode)
    runner = default_release_cli_runner if apply_armed else None

    try:
        result = evaluate_release_action(
            summary,
            config,
            ledger,
            apply=args.apply,
            runner=runner,
            source_ref=args.summary_file,
        )
    except Exception as exc:  # fail closed: never leak a raw traceback to CLI output
        return 2, {"success": False, "error": "evaluation_failed", "detail": sanitize_string(str(exc))}

    if receipts_path:
        save_receipts_ledger(receipts_path, ledger)

    rc = 0 if result.get("decision") in {"propose", "apply", "noop"} else 2
    return rc, result
