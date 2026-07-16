"""agentflowd: the unified event-driven continuation runtime (plan section 9).

This module is the "one runtime, many handlers" implementation: board
discovery, a real Linux inotify primary wake source watching every
discovered board's ``kanban.db`` (plus its WAL/journal/shm siblings —
``inotify_watch.py``) with the short poll-interval loop kept as the
fallback wake source when inotify is unavailable (non-Linux, sandboxed, or
the syscall setup fails for any reason), and a durable outbox/cursor
reconciliation pass that also runs on every wake.
``AgentflowDaemon.tick()`` is the synchronous core — the async loop in
``run()`` is a thin wrapper around it so tests can exercise routing/latency
without any real asyncio sleeping.

The per-kind unified handler router itself (GO/CODE_FIX/NEEDS_INPUT/
APPROVAL_REQUIRED/EXTERNAL_WAIT/UNKNOWN) lives in ``continuation_engine.py``
(``ingest_board_once``/``ingest_all_boards``) — this module is a thin caller
of that one implementation rather than a parallel copy (plan 14, commit 7
item 1). ``route_board_events`` below only adds the "seed a never-before-seen
board's cursor" step ``ingest_all_boards`` already does internally, exposed
per-board so tests and the async loop can drive one board at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .board_adapter import FakeBoardAdapter
from .board_events import BoardRegistryEntry, load_board_registry
from .continuation_config import ContractRegistry, load_contract_registry
from .continuation_engine import (
    ExternalWaitChecker,
    ingest_board_once,
    load_roadmap_apply_ledger,
    live_source_factory,
    poll_external_wait_conditions,
    real_adapter_factory,
    reconcile_outbox,
)
from .continuation_store import ContinuationStore
from .graph_creator import RealKanbanGraphAdapter, propose_next_slice_graph, propose_remediation_graph
from .interaction import InteractionInbox
from .roadmap_config import load_repo_roadmap_config

DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_RECONCILE_INTERVAL_SECONDS = 300.0


def default_boards_root() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "kanban" / "boards"


def default_runtime_dir() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "agentflow" / "run"


# -- board discovery (plan 9.1) --------------------------------------------


def discover_boards(
    *,
    boards_root: Path | None = None,
    overrides_path: Path | str | None = None,
) -> dict[str, BoardRegistryEntry]:
    """Scan ``boards_root``/*/kanban.db for every enrolled board. Every
    discovered board is auto-enrolled unless an override in
    ``config/boards.yaml`` (or an equivalent path) explicitly disables it.
    ``config/boards.yaml`` is consulted per-board as an override catalog
    (disable/endpoint/contract override) — it is no longer a gate on
    discovery itself."""
    root = boards_root if boards_root is not None else default_boards_root()
    overrides: dict[str, BoardRegistryEntry] = {}
    if overrides_path is not None and Path(overrides_path).exists():
        overrides = load_board_registry(overrides_path)

    registry: dict[str, BoardRegistryEntry] = {}
    if root.exists() and root.is_dir():
        for board_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            db_path = board_dir / "kanban.db"
            if not db_path.exists():
                continue
            board = board_dir.name
            override = overrides.get(board)
            if override is not None and not override.enabled:
                continue
            registry[board] = BoardRegistryEntry(
                board=board,
                db_identity=(override.db_identity if override else "") or board,
                outcome_handlers=override.outcome_handlers if override else (),
                enabled=True,
                default_endpoint=(override.default_endpoint if override else ""),
                db_path=(override.db_path if override and override.db_path else "") or str(db_path),
                roadmap_config_path=(override.roadmap_config_path if override else ""),
                roadmap_receipts_file=(override.roadmap_receipts_file if override else ""),
            )
    return registry


# -- runtime doctor/status surface (plan/M30A item 5) -----------------------


# The return-trip transport every semantic handler uses: a durable Kanban
# notify + active-wake subscription on the generated graph, never a direct
# AgentFlow/Discord send.
CALLBACK_TRANSPORT = "kanban_notify_wake"


def _board_table_columns(db_path: str | Path, table: str) -> set[str]:
    path = Path(db_path)
    if not path.exists():
        return set()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def _board_has_notify_rows(db_path: str | Path) -> bool:
    path = Path(db_path)
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("select 1 from kanban_notify_subs limit 1").fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


# Cursor columns this surface knows how to read, most canonical first. The
# canonical schema is board_cursors(board, db_identity, last_event_id,
# updated_at); anything else must fail closed rather than be guessed at.
_KNOWN_CURSOR_COLUMNS = ("last_event_id",)


def _store_cursor_status(store: ContinuationStore, board: str, db_identity: str) -> dict[str, Any]:
    """Read cursor state without initializing or migrating the canonical store.

    Returns the exact cursor when the known schema is present. A cursor that
    cannot be read truthfully (unreadable DB, missing/unknown cursor schema) is
    reported as an explicit error with ``cursor``/``cursor_seeded`` left unset:
    doctor and live status must never report a malformed store as a healthy
    ``cursor=0, cursor_seeded=false`` board.
    """
    if not store.path.exists():
        # The daemon has simply never run: no cursor exists yet, and saying so
        # is truthful rather than a swallowed error.
        return {"status": "uninitialized", "cursor": 0, "cursor_seeded": False, "error": "", "warning": ""}
    try:
        conn = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    except sqlite3.Error:
        return _cursor_error("cursor_db_unavailable")
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(board_cursors)")}
        if not columns:
            return _cursor_error("cursor_table_missing")
        column = next((c for c in _KNOWN_CURSOR_COLUMNS if c in columns), "")
        if not column:
            return _cursor_error("cursor_column_unknown")
        row = conn.execute(
            f"select {column} from board_cursors where board=? and db_identity=?",
            (board, db_identity),
        ).fetchone()
    except sqlite3.Error:
        return _cursor_error("cursor_read_failed")
    finally:
        conn.close()
    if row is None:
        # A board discovered before its first cursor write: genuinely unseeded.
        return {"status": "ok", "cursor": 0, "cursor_seeded": False, "error": "", "warning": ""}
    try:
        cursor = int(row[0] or 0)
    except (TypeError, ValueError):
        return _cursor_error("cursor_value_malformed")
    return {"status": "ok", "cursor": cursor, "cursor_seeded": True, "error": "", "warning": ""}


def _cursor_error(error: str) -> dict[str, Any]:
    return {
        "status": "error",
        "cursor": None,
        "cursor_seeded": None,
        "error": error,
        "warning": "cursor_status_unavailable",
    }


def _board_protection_verdict(entry: BoardRegistryEntry, *, apply: bool) -> dict[str, Any]:
    """Read-only per-board semantic protection verdict for doctor/live status.

    The global daemon can discover every board, but an operator still needs to
    see whether terminal semantic events have an effective return path: a typed
    origin/default endpoint, a notify+wake-capable board schema, and live apply
    mode. This deliberately does not expand any legacy direct-send allowlist.
    """
    notify_cols = _board_table_columns(entry.db_path, "kanban_notify_subs")
    legacy_ack_cols = _board_table_columns(entry.db_path, "ack_subscription")
    legacy_wake_cols = _board_table_columns(entry.db_path, "ack_active_wake")
    has_typed_rows = _board_has_notify_rows(entry.db_path)
    typed_origin_available = bool(entry.default_endpoint or has_typed_rows)
    canonical_notify_wake_available = "delivery_mode" in notify_cols
    legacy_active_wake_available = bool(legacy_ack_cols and legacy_wake_cols)
    notify_wake_available = canonical_notify_wake_available or legacy_active_wake_available
    effective = bool(apply and typed_origin_available and notify_wake_available)

    missing: list[str] = []
    if not apply:
        missing.append("apply_disabled")
    if not typed_origin_available:
        missing.append("typed_origin_missing")
    if not notify_wake_available:
        missing.append("notify_wake_unavailable")

    return {
        "effective_semantic_protection": effective,
        "typed_origin_available": typed_origin_available,
        "typed_origin_source": "default_endpoint" if entry.default_endpoint else ("kanban_notify_subs" if has_typed_rows else ""),
        "notify_wake_available": notify_wake_available,
        "canonical_notify_wake_available": canonical_notify_wake_available,
        "legacy_active_wake_available": legacy_active_wake_available,
        "apply_available": bool(apply),
        "warning": "" if effective else "board_lacks_effective_semantic_protection",
        "missing": missing,
    }


def runtime_report(
    *,
    boards_root: Path,
    overrides_path: Path | None = None,
    store: ContinuationStore | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Describe the global continuation daemon's live surface so an operator
    can tell it apart from the legacy direct-dispatch canary.

    Reports discovered/enrolled boards, ``--apply`` status, the registered
    semantic continuation handlers, the canonical store path and per-board
    cursors, and the callback transport (Kanban notify+wake). Crucially it
    reports continuation coverage per board independently of whether any
    legacy *direct AgentFlow live-send* remains canary-only: a board covered
    by the global daemon is protected even if direct live-send is not enabled
    for it, so this surface never claims such a board is "unprotected"."""
    from .continuation import _HANDLERS

    registry = discover_boards(boards_root=boards_root, overrides_path=overrides_path)
    handlers = sorted(kind.value for kind in _HANDLERS)

    boards: list[dict[str, Any]] = []
    for board, entry in registry.items():
        db_identity = entry.db_identity or board
        board_row: dict[str, Any] = {
            "board": board,
            "enrolled": True,
            "db_identity": db_identity,
            "default_endpoint": entry.default_endpoint,
            "db_path": entry.db_path,
            # Global-by-discovery: coverage comes from the daemon observing the
            # board, not from any per-channel allowlist or direct-send canary.
            "continuation_protected": True,
            "protection": "global_continuation_daemon",
        }
        board_row["protection_verdict"] = _board_protection_verdict(entry, apply=apply)
        board_row["effective_semantic_protection"] = board_row["protection_verdict"][
            "effective_semantic_protection"
        ]
        if store is not None:
            cursor_status = _store_cursor_status(store, board, db_identity)
            board_row["cursor"] = cursor_status["cursor"]
            board_row["cursor_seeded"] = cursor_status["cursor_seeded"]
            board_row["cursor_status"] = cursor_status
        boards.append(board_row)

    warnings = [
        {"board": b["board"], "warning": b["protection_verdict"]["warning"], "missing": b["protection_verdict"]["missing"]}
        for b in boards
        if b["protection_verdict"]["warning"]
    ]
    warnings.extend(
        {"board": b["board"], "warning": b["cursor_status"]["warning"], "missing": [b["cursor_status"]["error"]]}
        for b in boards
        if b.get("cursor_status", {}).get("warning")
    )

    continuation_runtime = {
        "runtime": "global_continuation_daemon",
        "apply": apply,
        "discovered_boards": len(boards),
        "enrolled_boards": [b["board"] for b in boards],
        "boards": boards,
        "semantic_handlers": handlers,
        "callback_transport": CALLBACK_TRANSPORT,
        "canonical_db": str(store.path) if store is not None else "",
        "warnings": warnings,
    }

    return {
        "runtime": "global_continuation_daemon",
        "apply": apply,
        "discovered_boards": len(boards),
        "enrolled_boards": [b["board"] for b in boards],
        "boards": boards,
        "semantic_handlers": handlers,
        "callback_transport": CALLBACK_TRANSPORT,
        "canonical_db": str(store.path) if store is not None else "",
        # Legacy direct-dispatch AgentFlow live-send is a *separate*, canary-only
        # policy. Its scope never implies a board observed by this daemon is
        # unprotected — continuation coverage is global-by-discovery.
        "legacy_direct_dispatch": "canary_only_independent_of_continuation_coverage",
        "direct_dispatch_policy": {"scope": "legacy_canary_only", "separate_from_continuation_runtime": True},
        "continuation_runtime": continuation_runtime,
        "warnings": warnings,
    }


# -- per-board wrapper over continuation_engine's router ---------------------


def route_board_events(
    *,
    board: str,
    entry: BoardRegistryEntry,
    store: ContinuationStore,
    contract_registry: ContractRegistry,
    adapter: Any,
    roadmap_router: Callable[..., dict[str, Any]] | None = None,
    code_fix_router: Callable[..., dict[str, Any]] = propose_remediation_graph,
    interaction_inbox: InteractionInbox | None = None,
    source_factory: Callable[[str, BoardRegistryEntry], Any] = live_source_factory,
    roadmap_config: Any = None,
    roadmap_apply_ledger: Any = None,
    roadmap_receipts_file: str = "",
    roadmap_graph_adapter: Any = None,
    apply: bool = False,
) -> dict[str, Any]:
    """One board's worth of the unified handler router. Seeds a
    never-before-seen board's cursor to its current max event id (no
    historical replay, plan 2.6/9.1) exactly like
    ``continuation_engine.ingest_all_boards`` does for its whole registry;
    everything else is delegated straight to ``ingest_board_once``."""
    source = source_factory(board, entry)
    db_identity = source.db_identity()

    if not store.cursor_exists(board, db_identity):
        seed = source.current_max_seq()
        store.advance_cursor(board, db_identity, seed)
        return {"success": True, "board": board, "processed": 0, "seeded_cursor": seed, "results": [], "cursor": seed}

    return ingest_board_once(
        board=board,
        source=source,
        store=store,
        contract_registry=contract_registry,
        adapter=adapter,
        roadmap_router=roadmap_router,
        code_fix_router=code_fix_router,
        default_endpoint=entry.default_endpoint,
        interaction_inbox=interaction_inbox,
        roadmap_config=roadmap_config,
        roadmap_apply_ledger=roadmap_apply_ledger,
        roadmap_receipts_file=roadmap_receipts_file,
        roadmap_graph_adapter=roadmap_graph_adapter,
        apply=apply,
    )


# -- single-instance lock ----------------------------------------------------


class SingleInstanceLock:
    """A pidfile+flock lock in the runtime dir. Best-effort on non-POSIX
    platforms (``fcntl`` import failure never crashes the daemon; it just
    can't guarantee exclusivity there)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:
            return True
        fh = open(self.path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            import fcntl
            fcntl.flock(self._fh, fcntl.LOCK_UN)
        except ImportError:
            pass
        finally:
            self._fh.close()
            self._fh = None


# -- daemon -------------------------------------------------------------------


@dataclass
class DaemonConfig:
    store: ContinuationStore
    boards_root: Path
    overrides_path: Path | None = None
    contracts_dir: Path | None = None
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    reconcile_interval_seconds: float = DEFAULT_RECONCILE_INTERVAL_SECONDS
    apply: bool = False
    external_wait_checker: ExternalWaitChecker | None = None
    source_factory: Callable[[str, BoardRegistryEntry], Any] = live_source_factory
    adapter_factory: Callable[[str, BoardRegistryEntry], Any] | None = None


def _fake_adapter_factory(board: str, entry: BoardRegistryEntry) -> FakeBoardAdapter:
    return FakeBoardAdapter()


@dataclass
class AgentflowDaemon:
    config: DaemonConfig
    _contracts: ContractRegistry | None = field(default=None, init=False, repr=False)

    def _store(self) -> ContinuationStore:
        """The store every wake cycle reads/writes: always the configured store.

        The daemon persists to exactly the store it was handed — this is what
        the restart/exactly-once guarantees (plan 13.7) and the zero-ceremony
        e2e harness depend on. Strictly side-effect-free ``apply=false``
        dry-runs against the *durable/canonical* ledger are enforced one level
        up, at the CLI entrypoint (``scripts/agentflowd.py``), which hands this
        daemon an isolated preview copy instead of the durable store when
        ``--apply`` was not passed (plan M27 blocker 1). Keeping the boundary
        there means a test/e2e daemon given its own throwaway store still
        persists normally, while the production dry-run never touches the
        canonical file."""
        return self.config.store

    def _contract_registry(self) -> ContractRegistry:
        if self._contracts is None:
            contracts_dir = self.config.contracts_dir
            paths = sorted(contracts_dir.glob("*.yaml")) if contracts_dir else []
            self._contracts = load_contract_registry(paths)
        return self._contracts

    def _adapter_factory(self) -> Callable[[str, BoardRegistryEntry], Any]:
        if self.config.adapter_factory is not None:
            return self.config.adapter_factory
        return real_adapter_factory if self.config.apply else _fake_adapter_factory

    def tick(self) -> dict[str, Any]:
        """One synchronous wake cycle: discover boards, route every new
        event, poll due external-wait conditions. This is the function every
        test exercises directly — the async loop below only decides *when*
        to call it."""
        store = self._store()
        registry = discover_boards(boards_root=self.config.boards_root, overrides_path=self.config.overrides_path)
        contracts = self._contract_registry()
        adapter_factory = self._adapter_factory()
        interaction_inbox = InteractionInbox(store=store)

        board_reports = []
        for board, entry in registry.items():
            adapter = adapter_factory(board, entry)
            roadmap_config = None
            roadmap_apply_ledger = None
            roadmap_graph_adapter = None
            if entry.roadmap_config_path:
                with contextlib.suppress(OSError, ValueError):
                    roadmap_config = load_repo_roadmap_config(entry.roadmap_config_path)
                roadmap_apply_ledger = load_roadmap_apply_ledger(entry.roadmap_receipts_file)
                if self.config.apply and roadmap_config is not None:
                    roadmap_graph_adapter = RealKanbanGraphAdapter(
                        board=board,
                        source_task_id="agentflowd",
                        subscription_endpoint=entry.default_endpoint,
                    )
            board_reports.append(
                route_board_events(
                    board=board,
                    entry=entry,
                    store=store,
                    contract_registry=contracts,
                    adapter=adapter,
                    interaction_inbox=interaction_inbox,
                    source_factory=self.config.source_factory,
                    roadmap_config=roadmap_config,
                    roadmap_apply_ledger=roadmap_apply_ledger,
                    roadmap_receipts_file=entry.roadmap_receipts_file,
                    roadmap_graph_adapter=roadmap_graph_adapter,
                    apply=self.config.apply,
                )
            )
        wait_report = poll_external_wait_conditions(store, checker=self.config.external_wait_checker)
        return {"success": True, "boards": board_reports, "external_wait": wait_report, "ts": time.time()}

    def runtime_report(self) -> dict[str, Any]:
        """Operator-facing status surface for this daemon (plan/M30A item 5)."""
        return runtime_report(
            boards_root=self.config.boards_root,
            overrides_path=self.config.overrides_path,
            store=self._store(),
            apply=self.config.apply,
        )

    def reconcile(self) -> dict[str, Any]:
        """Reconciliation pass (plan 2.6/9.5): identical event routing plus
        durable outbox replay. This is the quiet recovery path, never the
        primary path."""
        report = self.tick()
        registry = discover_boards(boards_root=self.config.boards_root, overrides_path=self.config.overrides_path)
        adapter_factory = self._adapter_factory()
        adapter_by_board = {board: adapter_factory(board, entry) for board, entry in registry.items()}
        report["outbox"] = reconcile_outbox(self._store(), adapter_by_board=adapter_by_board)
        return report

    def _watch_targets(self) -> list[Path]:
        """Every path an inotify watch should cover this tick: the boards
        root itself (so a newly created board's directory triggers a
        re-scan next loop) plus each currently discovered board's
        kanban.db."""
        targets = [self.config.boards_root]
        registry = discover_boards(boards_root=self.config.boards_root, overrides_path=self.config.overrides_path)
        for entry in registry.values():
            targets.append(Path(entry.db_path))
        return targets

    def _start_watcher(self, loop: asyncio.AbstractEventLoop, wake_event: asyncio.Event):
        """Best-effort real inotify primary wake source. Returns None (and
        the caller falls back to poll-interval-only wake, exactly the prior
        behavior) on any platform/syscall failure."""
        try:
            from .inotify_watch import InotifyUnavailable, InotifyWatcher

            watcher = InotifyWatcher()
        except Exception:
            return None

        def _on_readable() -> None:
            if watcher.drain():
                wake_event.set()

        try:
            loop.add_reader(watcher.fd, _on_readable)
        except (NotImplementedError, OSError):
            watcher.close()
            return None
        return watcher

    def _refresh_watches(self, watcher) -> None:
        if watcher is None:
            return
        watcher.watch(self.config.boards_root)
        for target in self._watch_targets()[1:]:
            watcher.watch_board_db(target)

    async def run(self, *, stop_event: asyncio.Event | None = None, max_ticks: int | None = None) -> None:
        """Async wake loop: a real Linux inotify primary wake source
        (``inotify_watch.py``) watching every discovered board's kanban.db
        for a write, with the short coalescing poll kept as the fallback
        wake source (plan 9.3) whenever inotify is unavailable or simply
        hasn't fired within one poll interval, plus a periodic
        reconciliation pass. Stops on ``stop_event``, SIGTERM/SIGINT, or
        after ``max_ticks`` (test-only escape hatch)."""
        stop_event = stop_event or asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, stop_event.set)

        wake_event = asyncio.Event()
        watcher = self._start_watcher(loop, wake_event)

        last_reconcile = 0.0
        ticks = 0
        try:
            while not stop_event.is_set():
                self._refresh_watches(watcher)
                self.tick()
                ticks += 1
                now = time.time()
                if now - last_reconcile >= self.config.reconcile_interval_seconds:
                    self.reconcile()
                    last_reconcile = now
                if max_ticks is not None and ticks >= max_ticks:
                    return
                wake_event.clear()
                stop_wait = asyncio.ensure_future(stop_event.wait())
                wake_wait = asyncio.ensure_future(wake_event.wait())
                try:
                    await asyncio.wait(
                        {stop_wait, wake_wait},
                        timeout=self.config.poll_interval_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for pending in (stop_wait, wake_wait):
                        if not pending.done():
                            pending.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await pending
        finally:
            if watcher is not None:
                with contextlib.suppress(Exception):
                    loop.remove_reader(watcher.fd)
                watcher.close()
