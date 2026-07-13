"""Acceptance criterion 13.1: p95 event -> continuation action under 5s.

Drives the real async wake loop (``AgentflowDaemon.run``) against a short
poll interval and a live sqlite board DB, writing a fresh terminal event
after the loop starts and measuring wall-clock time until the router has
produced a continuation instance for it. Intervals here are deliberately
tiny (0.02-0.2s) so the suite stays fast; production defaults live in
``scripts/agentflowd.py`` (0.5s).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest

from pathlib import Path

from agentflow_hermes.continuation_store import ContinuationStore
from agentflow_hermes.daemon import AgentflowDaemon, DaemonConfig

_CONTRACTS_DIR = Path(__file__).resolve().parents[1] / "contracts"


def _init_board_db(path):
    con = sqlite3.connect(path)
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


# GO/CODE_FIX route straight through graph_creator with no continuation_store
# row of their own (same as pre-existing continuation_engine.ingest_board_once
# behavior), so a needs_input outcome — which DOES create a durable
# continuation_instances row via OwnerInputHandler — is the observable signal
# this test measures latency against.
_NEEDS_INPUT_METADATA = json.dumps(
    {
        "agentflow_outcome": {
            "schema_version": 1,
            "verdict": "BLOCK",
            "continuation_kind": "needs_input",
            "required_inputs": [{"name": "result_url", "authority": "owner"}],
        }
    }
)


def _write_terminal_event(path, *, task_id: str, metadata: str = _NEEDS_INPUT_METADATA) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "insert or replace into tasks(id, title, assignee) values(?, ?, ?)", (task_id, "demo", "agent")
    )
    con.execute(
        "insert into task_runs(id, step_key, summary, metadata) values(?, ?, ?, ?)",
        (f"{task_id}-run", "g1", "BLOCK", metadata),
    )
    con.execute(
        "insert into task_events(task_id, run_id, kind, payload, created_at) values(?, ?, 'completed', '{}', ?)",
        (task_id, f"{task_id}-run", time.time()),
    )
    con.commit()
    con.close()


@pytest.mark.parametrize("poll_interval_seconds", [0.02, 0.05, 0.2])
def test_event_to_action_latency_under_5s(tmp_path, poll_interval_seconds):
    boards_root = tmp_path / "boards"
    (boards_root / "alpha").mkdir(parents=True)
    db_path = boards_root / "alpha" / "kanban.db"
    _init_board_db(db_path)

    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    config = DaemonConfig(
        store=store,
        boards_root=boards_root,
        contracts_dir=_CONTRACTS_DIR,
        poll_interval_seconds=poll_interval_seconds,
        reconcile_interval_seconds=999,
    )
    daemon = AgentflowDaemon(config)

    async def scenario():
        loop_task = asyncio.ensure_future(daemon.run())
        # Let the daemon seed its cursor (no historical replay) before the
        # "real" event is written, mirroring a live board's first-seen wake.
        await asyncio.sleep(poll_interval_seconds * 3)

        write_time = time.time()
        _write_terminal_event(db_path, task_id="t_latency")

        deadline = write_time + 5.0
        detected_at = None
        while time.time() < deadline:
            if store.list_instances():
                detected_at = time.time()
                break
            await asyncio.sleep(poll_interval_seconds / 2)

        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task
        return write_time, detected_at

    write_time, detected_at = asyncio.run(scenario())

    assert detected_at is not None, "continuation was never created within the 5s acceptance budget"
    latency = detected_at - write_time
    assert latency < 5.0
