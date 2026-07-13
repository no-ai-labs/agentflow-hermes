"""Minimal ctypes-based Linux inotify wrapper.

``agentflowd``'s primary wake source (plan section 9.3 originally shipped
only the poll-interval fallback; this closes that gap with a real syscall
binding instead of adding a non-stdlib dependency). Watches each board's
``kanban.db`` file plus its ``-wal``/``-journal`` siblings — SQLite in
WAL mode (the mode ``hermes kanban`` uses) commits land in the ``-wal``
file first, so that sibling must be watched directly or a commit can be
missed until the next poll tick — and the boards root directory itself so
a newly created board picks up its own watches on the next re-scan.

Fails soft everywhere: any import/syscall failure raises
``InotifyUnavailable`` once, the caller (``daemon.AgentflowDaemon.run``)
catches it and falls back to poll-interval-only wake, exactly the prior
behavior. Linux-only; no-op class is not provided here because the caller
already has a working fallback path without one.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from pathlib import Path

IN_MODIFY = 0x00000002
IN_CREATE = 0x00000100
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_ATTRIB = 0x00000004
WATCH_MASK = IN_MODIFY | IN_CREATE | IN_CLOSE_WRITE | IN_MOVED_TO | IN_ATTRIB

_IN_NONBLOCK = 0o4000

# Sibling suffixes SQLite may write commits into before/instead of the main
# file, depending on journal mode.
_DB_SIBLING_SUFFIXES = ("-wal", "-journal", "-shm")


class InotifyUnavailable(RuntimeError):
    """Raised when the inotify syscalls are not usable on this platform."""


class InotifyWatcher:
    """Thin ctypes wrapper over inotify_init1/inotify_add_watch/read.

    Only the fd-is-readable signal is used by callers (via
    ``asyncio loop.add_reader``); the actual event payloads are drained and
    discarded by :meth:`drain` since the daemon always re-ticks from
    durable cursor state, not from event content.
    """

    def __init__(self) -> None:
        libc_name = ctypes.util.find_library("c")
        if not libc_name:
            raise InotifyUnavailable("libc not found")
        self._libc = ctypes.CDLL(libc_name, use_errno=True)

        fd = self._libc.inotify_init1(_IN_NONBLOCK)
        if fd < 0:
            errno = ctypes.get_errno()
            raise InotifyUnavailable(f"inotify_init1 failed: errno={errno}")
        self.fd = fd
        self._watches: dict[str, int] = {}

    def watch(self, path: Path) -> bool:
        """Add a watch for ``path`` if it exists and isn't already watched.
        Returns True if a new watch was installed."""
        key = str(path)
        if key in self._watches:
            return False
        if not path.exists():
            return False
        wd = self._libc.inotify_add_watch(self.fd, key.encode("utf-8"), WATCH_MASK)
        if wd < 0:
            return False
        self._watches[key] = wd
        return True

    def watch_board_db(self, db_path: Path) -> None:
        """Watch a board's kanban.db plus its WAL/journal/shm siblings."""
        self.watch(db_path)
        for suffix in _DB_SIBLING_SUFFIXES:
            self.watch(Path(str(db_path) + suffix))

    def drain(self) -> bool:
        """Non-blocking read of any pending events. Returns True if at
        least one byte was read (i.e. a real wake, not a spurious call)."""
        got = False
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            except OSError:
                break
            if not data:
                break
            got = True
        return got

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass
