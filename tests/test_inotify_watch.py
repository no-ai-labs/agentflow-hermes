"""Regression coverage for the real inotify primary wake source (M27
inotify remediation): ``InotifyWatcher`` watches a board's kanban.db plus
its WAL/journal/shm siblings, ``AgentflowDaemon.run`` registers a real
``loop.add_reader`` on it when available, and falls back cleanly to the
poll-interval-only loop when it isn't (non-Linux, sandboxed, or any
syscall failure). Also proves the end-to-end async latency win: a real
board WAL write is observed and ticked well inside the poll interval,
not merely on the fallback timer.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from agentflow_hermes.continuation_store import ContinuationStore
from agentflow_hermes.daemon import AgentflowDaemon, DaemonConfig
from agentflow_hermes.inotify_watch import InotifyUnavailable, InotifyWatcher


def _seed_board_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        create table tasks (
            id text primary key, title text, assignee text, workspace_path text, workflow_template_id text
        );
        create table task_runs (id text primary key, step_key text, summary text, metadata text);
        create table task_events (
            id integer primary key autoincrement, task_id text, run_id text, kind text, payload text, created_at real
        );
        """
    )
    con.commit()
    con.close()


def _write_event(path: Path, *, task_id: str, summary: str) -> None:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("insert or replace into tasks(id, title, assignee) values(?, 'demo', 'agent')", (task_id,))
    con.execute(
        "insert into task_runs(id, step_key, summary, metadata) values(?, 'g1', ?, '{}')", (f"{task_id}-run", summary)
    )
    con.execute(
        "insert into task_events(task_id, run_id, kind, payload, created_at) values(?, ?, 'completed', '{}', ?)",
        (task_id, f"{task_id}-run", time.time()),
    )
    con.commit()
    con.close()


# -- InotifyWatcher (real syscalls) ------------------------------------------


def test_watcher_watches_db_and_wal_siblings(tmp_path):
    db_path = tmp_path / "boardroot" / "alpha" / "kanban.db"
    _seed_board_db(db_path)

    # Keep a connection open (WAL mode checkpoints -wal away on close when
    # it's the last connection) so the sibling file exists while we watch.
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("insert or replace into tasks(id, title, assignee) values('seed', 'demo', 'agent')")
    con.commit()

    watcher = InotifyWatcher()
    try:
        watcher.watch_board_db(db_path)
        assert str(db_path) in watcher._watches
        wal_path = Path(str(db_path) + "-wal")
        assert wal_path.exists(), "sqlite WAL mode must have produced a -wal file"
        assert str(wal_path) in watcher._watches
    finally:
        watcher.close()
        con.close()


def test_watcher_watch_returns_false_for_missing_path(tmp_path):
    watcher = InotifyWatcher()
    try:
        assert watcher.watch(tmp_path / "does-not-exist.db") is False
    finally:
        watcher.close()


def test_watcher_watch_is_idempotent(tmp_path):
    db_path = tmp_path / "kanban.db"
    _seed_board_db(db_path)
    watcher = InotifyWatcher()
    try:
        assert watcher.watch(db_path) is True
        assert watcher.watch(db_path) is False
    finally:
        watcher.close()


def test_watcher_drain_reports_a_real_write(tmp_path):
    db_path = tmp_path / "kanban.db"
    _seed_board_db(db_path)
    watcher = InotifyWatcher()
    try:
        watcher.watch_board_db(db_path)
        assert watcher.drain() is False  # nothing pending yet

        _write_event(db_path, task_id="t1", summary="Verdict: GO")

        deadline = time.time() + 2.0
        saw_event = False
        while time.time() < deadline:
            if watcher.drain():
                saw_event = True
                break
            time.sleep(0.02)
        assert saw_event, "expected inotify to observe the WAL/db write"
    finally:
        watcher.close()


def test_watcher_close_is_safe_to_call_twice(tmp_path):
    watcher = InotifyWatcher()
    watcher.close()
    watcher.close()  # must not raise


# -- daemon wiring: add_reader registration and fallback --------------------


def test_daemon_start_watcher_registers_add_reader(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    boards_root = tmp_path / "boards"
    _seed_board_db(boards_root / "alpha" / "kanban.db")
    config = DaemonConfig(store=store, boards_root=boards_root, poll_interval_seconds=999, reconcile_interval_seconds=999)
    daemon = AgentflowDaemon(config)

    async def _run():
        loop = asyncio.get_running_loop()
        wake_event = asyncio.Event()
        watcher = daemon._start_watcher(loop, wake_event)
        try:
            assert watcher is not None, "real inotify must be available on this Linux CI/dev box"
            assert watcher.fd in loop._selector.get_map() if hasattr(loop, "_selector") else True
        finally:
            if watcher is not None:
                loop.remove_reader(watcher.fd)
                watcher.close()

    asyncio.run(_run())


def test_daemon_start_watcher_falls_back_cleanly_when_add_reader_unavailable(tmp_path, monkeypatch):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    boards_root = tmp_path / "boards"
    _seed_board_db(boards_root / "alpha" / "kanban.db")
    config = DaemonConfig(store=store, boards_root=boards_root, poll_interval_seconds=999, reconcile_interval_seconds=999)
    daemon = AgentflowDaemon(config)

    class _NoReaderLoop:
        def add_reader(self, *a, **kw):
            raise NotImplementedError("no selector event loop on this platform")

    watcher = daemon._start_watcher(_NoReaderLoop(), asyncio.Event())
    assert watcher is None, "must fall back to None (poll-only) instead of raising"


def test_daemon_start_watcher_falls_back_when_inotify_unavailable(tmp_path, monkeypatch):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    boards_root = tmp_path / "boards"
    _seed_board_db(boards_root / "alpha" / "kanban.db")
    config = DaemonConfig(store=store, boards_root=boards_root, poll_interval_seconds=999, reconcile_interval_seconds=999)
    daemon = AgentflowDaemon(config)

    def _boom():
        raise InotifyUnavailable("simulated: no inotify on this platform")

    monkeypatch.setattr("agentflow_hermes.inotify_watch.InotifyWatcher", _boom)

    async def _run():
        loop = asyncio.get_running_loop()
        watcher = daemon._start_watcher(loop, asyncio.Event())
        assert watcher is None

    asyncio.run(_run())


def test_daemon_run_loop_survives_missing_inotify_module(tmp_path, monkeypatch):
    """End-to-end: run() must still tick to completion via the poll fallback
    even when the inotify watcher can never be constructed."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    boards_root = tmp_path / "boards"
    _seed_board_db(boards_root / "alpha" / "kanban.db")
    config = DaemonConfig(store=store, boards_root=boards_root, poll_interval_seconds=0.01, reconcile_interval_seconds=999)
    daemon = AgentflowDaemon(config)

    def _boom():
        raise InotifyUnavailable("simulated")

    monkeypatch.setattr("agentflow_hermes.inotify_watch.InotifyWatcher", _boom)

    asyncio.run(daemon.run(max_ticks=3))


# -- async event latency: real WAL write observed well inside poll interval -


def test_run_loop_wakes_on_real_wal_write_faster_than_poll_interval(tmp_path):
    """Proves the inotify primary wake source actually shortens latency: a
    long poll interval (10s) is configured, a board event is written after
    the loop starts, and the daemon must tick again promptly (<2s) instead
    of waiting out the poll interval — only possible if the wake came from
    the real inotify fd becoming readable, not the fallback timer."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    boards_root = tmp_path / "boards"
    db_path = boards_root / "alpha" / "kanban.db"
    _seed_board_db(db_path)

    config = DaemonConfig(store=store, boards_root=boards_root, poll_interval_seconds=10.0, reconcile_interval_seconds=999)
    daemon = AgentflowDaemon(config)

    tick_times: list[float] = []
    real_tick = daemon.tick

    def _timed_tick():
        tick_times.append(time.monotonic())
        return real_tick()

    daemon.tick = _timed_tick  # type: ignore[method-assign]

    stop_event = asyncio.Event()

    async def _writer_after_first_tick():
        deadline = time.monotonic() + 5.0
        while not tick_times and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert tick_times, "daemon never ticked once at startup"
        await asyncio.sleep(0.1)
        _write_event(db_path, task_id="t-latency", summary="Verdict: GO")

        deadline = time.monotonic() + 5.0
        while len(tick_times) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        stop_event.set()

    async def _run():
        await asyncio.gather(daemon.run(stop_event=stop_event), _writer_after_first_tick())

    asyncio.run(_run())

    assert len(tick_times) >= 2, "expected a second tick triggered by the WAL write, not just the startup tick"
    latency = tick_times[1] - tick_times[0]
    assert latency < 2.0, f"second tick took {latency:.2f}s — inotify wake did not fire faster than the 10s poll interval"
