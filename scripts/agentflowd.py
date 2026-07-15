#!/usr/bin/env python3
"""agentflowd: the long-lived event-driven AgentFlow continuation runtime
(plan section 9). Discovers every board under ``--boards-root`` (default
``~/.hermes/kanban/boards``), routes terminal events through the unified
handler router in ``agentflow_hermes.daemon``, and runs a periodic
reconciliation pass.

Wake source: a real Linux inotify watch (hand-rolled ctypes syscall
bindings in ``agentflow_hermes.inotify_watch`` — no non-stdlib dependency)
on every discovered board's ``kanban.db`` (and its WAL/journal/shm
siblings) is the primary wake source; ``AgentflowDaemon.run`` wakes and
ticks as soon as any board file is written to. The short poll interval
(``--poll-interval-seconds``, default 0.5s) remains the fallback wake
source per plan section 9.3, used whenever inotify is unavailable (non-
Linux, sandboxed) or simply hasn't fired within one interval. The
five-minute reconciliation timer (``--reconcile-interval-seconds``) is the
real recovery path per plan section 2.6, independent of either wake path.

Safety: dry-run (in-memory FakeBoardAdapter) by default; ``--apply`` switches
to the real gated CLI adapter that mutates the shared Kanban boards.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from pathlib import Path

_DEFAULT_REPO = Path(os.environ.get("AGENTFLOW_HERMES_REPO", "/home/duckran/dev/agentflow-hermes"))
_REPO_ROOT = _DEFAULT_REPO if _DEFAULT_REPO.exists() else Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agentflow_hermes.daemon import (  # noqa: E402
    AgentflowDaemon,
    DaemonConfig,
    SingleInstanceLock,
    default_boards_root,
    default_runtime_dir,
)
from agentflow_hermes.continuation_store import ContinuationStore, isolated_preview_store  # noqa: E402
from agentflow_hermes import service_install  # noqa: E402

_DEFAULT_OVERRIDES = _REPO_ROOT / "config" / "boards.yaml"
_DEFAULT_CONTRACTS_DIR = _REPO_ROOT / "contracts"


def _configured_store(args: argparse.Namespace) -> ContinuationStore:
    return ContinuationStore(Path(args.db)) if args.db else ContinuationStore.canonical()


@contextlib.contextmanager
def effective_store(args: argparse.Namespace):
    """Yield the store the daemon should run against for this invocation.

    ``--apply`` (the live systemd service) uses the configured durable/canonical
    store directly. Without ``--apply`` the run/tick/reconcile paths must be
    strictly side-effect-free against that durable store (plan M27 blocker 1):
    the daemon is handed an isolated throwaway copy instead, so a dry-run
    diagnostic never advances a cursor or leaks a continuation/outbox row into
    the canonical ledger (the exact failure that produced the legacy incident
    rows). The copy is deleted when the invocation ends."""
    configured = _configured_store(args)
    if args.apply:
        yield configured
    else:
        with isolated_preview_store(configured.path) as preview:
            yield preview


def build_daemon(args: argparse.Namespace, store: ContinuationStore | None = None) -> AgentflowDaemon:
    store = store if store is not None else _configured_store(args)
    config = DaemonConfig(
        store=store,
        boards_root=Path(args.boards_root),
        overrides_path=Path(args.overrides) if args.overrides else None,
        contracts_dir=Path(args.contracts_dir),
        poll_interval_seconds=args.poll_interval_seconds,
        reconcile_interval_seconds=args.reconcile_interval_seconds,
        apply=args.apply,
    )
    return AgentflowDaemon(config)


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--boards-root", default=str(default_boards_root()))
    parser.add_argument("--overrides", default=str(_DEFAULT_OVERRIDES))
    parser.add_argument("--contracts-dir", default=str(_DEFAULT_CONTRACTS_DIR))
    parser.add_argument("--db", default="")
    parser.add_argument("--apply", action="store_true", help="Mutate the real shared boards (default: dry-run).")
    parser.add_argument("--poll-interval-seconds", type=float, default=0.5)
    parser.add_argument("--reconcile-interval-seconds", type=float, default=300.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run the long-lived daemon loop.")
    _add_common_args(run_p)
    run_p.add_argument("--lock-file", default=str(default_runtime_dir() / "agentflowd.pid"))
    run_p.add_argument("--max-ticks", type=int, default=None, help="Stop after N ticks (test/debug only).")

    tick_p = sub.add_parser("tick", help="Run exactly one wake cycle and exit.")
    _add_common_args(tick_p)

    reconcile_p = sub.add_parser("reconcile", help="Run one reconciliation pass and exit.")
    _add_common_args(reconcile_p)

    doctor_p = sub.add_parser(
        "doctor", help="Print the global continuation daemon's runtime surface (boards, handlers, transport)."
    )
    _add_common_args(doctor_p)

    _default_unit_dir = str(Path.home() / ".config" / "systemd" / "user")
    service_p = sub.add_parser("service", help="Install/enable the one user-level agentflowd systemd units.")
    service_sub = service_p.add_subparsers(dest="service_cmd", required=True)

    install_p = service_sub.add_parser("install", help="Render (and optionally write) the unit files.")
    install_p.add_argument("--script", default=str(Path(__file__).resolve()))
    install_p.add_argument("--unit-dir", default=_default_unit_dir)
    install_p.add_argument("--write-files", action="store_true", help="Write unit files to --unit-dir (default: render only).")
    install_p.add_argument("--extra-args", default="")

    enable_p = service_sub.add_parser("enable", help="systemctl --user daemon-reload + enable the units.")
    enable_p.add_argument("--unit-dir", default=_default_unit_dir)
    enable_p.add_argument("--apply", action="store_true", help="Actually run systemctl (default: print the commands only).")
    enable_p.add_argument("--now", action="store_true", help="Also start the units immediately (systemctl --now).")

    status_p = service_sub.add_parser("status", help="File-presence status of the unit files in --unit-dir.")
    status_p.add_argument("--unit-dir", default=_default_unit_dir)

    args = parser.parse_args(argv)

    if args.cmd == "run":
        lock = SingleInstanceLock(Path(args.lock_file))
        if not lock.acquire():
            print("agentflowd: another instance already holds the lock", file=sys.stderr)
            return 2
        try:
            with effective_store(args) as store:
                daemon = build_daemon(args, store)
                asyncio.run(daemon.run(max_ticks=args.max_ticks))
        finally:
            lock.release()
        return 0

    if args.cmd == "tick":
        with effective_store(args) as store:
            report = build_daemon(args, store).tick()
        print(report)
        return 0

    if args.cmd == "reconcile":
        with effective_store(args) as store:
            report = build_daemon(args, store).reconcile()
        print(report)
        return 0

    if args.cmd == "doctor":
        # Read-only status surface: report against the configured/canonical
        # store directly (never a throwaway preview copy) so the cursors shown
        # are the real ones, but never mutate anything.
        report = build_daemon(args, _configured_store(args)).runtime_report()
        print(report)
        return 0

    if args.cmd == "service":
        if args.service_cmd == "install":
            plan = service_install.install(
                args.script, unit_dir=args.unit_dir, write_files=args.write_files, extra_args=args.extra_args
            )
            print(plan)
            return 0
        if args.service_cmd == "enable":
            result = service_install.enable(unit_dir=args.unit_dir, apply=args.apply, now=args.now)
            print(result)
            return 0 if result["success"] else 1
        if args.service_cmd == "status":
            print(service_install.status(args.unit_dir))
            return 0
        raise AssertionError(args.service_cmd)

    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
