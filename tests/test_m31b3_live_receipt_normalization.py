"""M31B3: operator resolution receipts over the REAL LiveBoardEventSource path.

Exercises the sanitized exact offline fixture copied read-only from three real
warroom-os terminal events (8488 blocked t_fc21ca00, 8517 completed t_0e72730a
with structured BLOCK run metadata, 8523 blocked t_b556f023) through
``LiveBoardEventSource`` — not the synthetic ``_t89`` BoardEvent shape.

Proves the two M31B3 reviewer blockers:
  1. numeric ``8488``, canonical ``kanban-event-8488`` and ``event_seq=8488``
     all resolve to the SAME canonical live board event, the durable receipt
     key uses canonical board event identity, and invalid/mismatched
     board/id fails closed.
  2. receipts/backfill run against real board-event rows, yielding
     superseded_by_operator with zero created tasks and no duplicate graph.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agentflow_hermes.board_adapter import FakeBoardAdapter
from agentflow_hermes.board_events import LiveBoardEventSource
from agentflow_hermes.continuation_engine import record_operator_resolution_receipt
from agentflow_hermes.continuation_store import ContinuationStore

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "warroom_m31b3_live_events.json"

# Real per-board Kanban shape (columns the live source actually reads).
_SCHEMA = """
create table tasks (
    id text primary key, title text not null, body text, assignee text,
    status text not null, workspace_path text, workflow_template_id text
);
create table task_runs (
    id integer primary key, task_id text not null, step_key text,
    status text not null, summary text, metadata text
);
create table task_events (
    id integer primary key, task_id text not null, run_id integer,
    kind text not null, payload text, created_at integer not null
);
create table kanban_notify_subs (
    task_id text not null, platform text not null, chat_id text not null,
    thread_id text not null default '', created_at integer not null,
    primary key (task_id, platform, chat_id, thread_id)
);
"""


def _fixture_payload() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _live_db(tmp_path: Path) -> Path:
    """Materialize the sanitized offline fixture into a real-shaped kanban.db."""
    payload = _fixture_payload()
    db = tmp_path / "kanban.db"
    if db.exists():
        # Restart/reconcile cases re-open the SAME board DB.
        return db
    conn = sqlite3.connect(db)
    try:
        conn.executescript(_SCHEMA)
        for t in payload["tasks"]:
            conn.execute(
                "insert into tasks(id, title, assignee, status, workspace_path, workflow_template_id)"
                " values(?,?,?,?,?,?)",
                (t["id"], t["title"], t["assignee"], t["status"], t["workspace_path"],
                 t["workflow_template_id"]),
            )
        for r in payload["task_runs"]:
            conn.execute(
                "insert into task_runs(id, task_id, step_key, status, summary, metadata)"
                " values(?,?,?,?,?,?)",
                (r["id"], r["task_id"], r["step_key"], r["status"], r["summary"], r["metadata"]),
            )
        for e in payload["task_events"]:
            conn.execute(
                "insert into task_events(id, task_id, run_id, kind, payload, created_at)"
                " values(?,?,?,?,?,?)",
                (e["id"], e["task_id"], e["run_id"], e["kind"], e["payload"], e["created_at"]),
            )
        conn.commit()
    finally:
        conn.close()
    return db


def _source(tmp_path: Path) -> LiveBoardEventSource:
    return LiveBoardEventSource(
        board="warroom-os",
        db_path=_live_db(tmp_path),
        db_identity="warroom-os",
        default_endpoint="discord:000000000000000000",
    )


# --- fixture integrity: the exact real events, through the live source --------


def test_fixture_exposes_the_three_exact_real_events_via_live_source(tmp_path):
    events = _source(tmp_path).fetch_events_since(0)

    assert [(e.event_seq, e.event_kind, e.source_task_id) for e in events] == [
        (8488, "blocked", "t_fc21ca00"),
        (8517, "completed", "t_0e72730a"),
        (8523, "blocked", "t_b556f023"),
    ]
    # canonical live identity, not the synthetic _t89 shape
    assert [e.event_id for e in events] == [
        "kanban-event-8488",
        "kanban-event-8517",
        "kanban-event-8523",
    ]
    # 8517 carries the structured BLOCK run metadata exactly.
    ev8517 = events[1]
    assert ev8517.run_metadata["verdict"] == "BLOCK"
    assert ev8517.run_metadata["findings"]
    assert "Verdict: BLOCK" in ev8517.summary


# --- blocker 1: canonical identity normalization -----------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"event_id": "8488"}, id="numeric-string"),
        pytest.param({"event_id": "kanban-event-8488"}, id="canonical-id"),
        pytest.param({"event_seq": 8488}, id="event-seq"),
        pytest.param({"event_id": 8488}, id="numeric-int"),
        pytest.param({"event_id": "kanban-event-8488", "event_seq": 8488}, id="both-agreeing"),
    ],
)
def test_all_identity_forms_target_the_same_canonical_live_board_event(tmp_path, kwargs):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")

    report = record_operator_resolution_receipt(
        board="warroom-os", source=_source(tmp_path), store=store,
        operator_receipt_ref="operator:m31b3:8488", **kwargs,
    )

    assert report["success"] is True
    assert report["action"] == "superseded_by_operator"
    assert report["created_tasks"] == 0
    assert report["state"] == "resumed"
    # durable receipt key uses CANONICAL board event identity, never the raw input
    assert report["receipt_key"] == "warroom-os:kanban-event-8488"
    assert report["event_id"] == "kanban-event-8488"
    assert report["event_seq"] == 8488

    sats = store.list_requirement_satisfactions(report["instance_id"])
    assert sats[0]["source_kind"] == "operator_resolution_receipt"
    assert sats[0]["value"]["source_event_id"] == "kanban-event-8488"
    assert sats[0]["source_ref"] == "warroom-os:kanban-event-8488"


def test_mixed_identity_forms_converge_on_one_instance_no_duplicate_graph(tmp_path):
    """numeric -> canonical -> event_seq across restarts must NOT fork a graph."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()

    first = record_operator_resolution_receipt(
        board="warroom-os", source=_source(tmp_path), store=store, event_id="8488",
        operator_receipt_ref="operator:m31b3:8488",
    )
    # restart/reconcile with a different spelling of the same event
    second = record_operator_resolution_receipt(
        board="warroom-os", source=_source(tmp_path), store=store, event_id="kanban-event-8488",
        operator_receipt_ref="operator:m31b3:8488",
    )
    third = record_operator_resolution_receipt(
        board="warroom-os", source=_source(tmp_path), store=store, event_seq=8488,
        operator_receipt_ref="operator:m31b3:8488",
    )

    assert first["created"] is True
    assert second["created"] is False and third["created"] is False
    assert second["instance_id"] == first["instance_id"] == third["instance_id"]
    assert len(store.list_instances()) == 1
    assert len(store.list_requirement_satisfactions(first["instance_id"])) == 1
    assert adapter.tasks == {}


@pytest.mark.parametrize(
    "seq, task_id",
    [(8488, "t_fc21ca00"), (8517, "t_0e72730a"), (8523, "t_b556f023")],
)
def test_each_exact_event_receipt_creates_zero_tasks(tmp_path, seq, task_id):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()

    report = record_operator_resolution_receipt(
        board="warroom-os", source=_source(tmp_path), store=store, event_id=str(seq),
        operator_receipt_ref=f"operator:m31b3:{seq}",
    )

    assert report["success"] is True
    assert report["action"] == "superseded_by_operator"
    assert report["created_tasks"] == 0
    assert report["receipt_key"] == f"warroom-os:kanban-event-{seq}"
    instance = store.get_instance(report["instance_id"])
    assert instance is not None
    assert instance["source_task_id"] == task_id
    assert instance["source_event_id"] == f"kanban-event-{seq}"
    # zero board mutation: no tasks, no wakes
    assert adapter.tasks == {}
    assert adapter.subscriptions == []


# --- blocker 1: fail closed --------------------------------------------------


def test_mismatched_event_id_and_event_seq_fails_closed(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")

    report = record_operator_resolution_receipt(
        board="warroom-os", source=_source(tmp_path), store=store,
        event_id="kanban-event-8488", event_seq=8517,
    )

    assert report["success"] is False
    assert report["error"] == "event_reference_mismatch"
    assert store.list_instances() == []


def test_mismatched_board_fails_closed(tmp_path):
    """The receipt board must match the source's board; no cross-board receipts."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")

    report = record_operator_resolution_receipt(
        board="oracle-lab", source=_source(tmp_path), store=store, event_id="8488",
    )

    assert report["success"] is False
    assert report["error"] == "board_mismatch"
    assert store.list_instances() == []


@pytest.mark.parametrize(
    "event_id",
    [
        "",
        "not-an-event",
        "kanban-event-",
        "kanban-event-abc",
        "9999",
        "kanban-event-9999",
        "84 88",
        "kanban-event- 8488",
    ],
)
def test_invalid_or_unknown_event_reference_fails_closed(tmp_path, event_id):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")

    report = record_operator_resolution_receipt(
        board="warroom-os", source=_source(tmp_path), store=store, event_id=event_id,
    )

    assert report["success"] is False
    assert report["error"] in {"event_not_found", "event_reference_required", "event_reference_invalid"}
    assert store.list_instances() == []


def test_receipt_without_any_event_reference_fails_closed(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")

    report = record_operator_resolution_receipt(
        board="warroom-os", source=_source(tmp_path), store=store,
    )

    assert report["success"] is False
    assert report["error"] == "event_reference_required"
    assert store.list_instances() == []


def test_wrong_board_id_does_not_match_by_numeric_coincidence(tmp_path):
    """A canonical id whose numeric part matches but whose prefix is foreign
    must not silently resolve."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")

    report = record_operator_resolution_receipt(
        board="warroom-os", source=_source(tmp_path), store=store,
        event_id="oracle-lab-event-8488",
    )

    assert report["success"] is False
    assert store.list_instances() == []


# --- cursor progression only after durable receipt ---------------------------


def test_cursor_advances_only_after_durable_receipt(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    source = _source(tmp_path)

    # A failed (fail-closed) receipt must not move the cursor.
    assert store.get_cursor("warroom-os", "warroom-os") == 0
    failed = record_operator_resolution_receipt(
        board="warroom-os", source=source, store=store, event_id="9999",
    )
    assert failed["success"] is False
    assert store.get_cursor("warroom-os", "warroom-os") == 0

    # The receipt itself does not implicitly advance the cursor...
    report = record_operator_resolution_receipt(
        board="warroom-os", source=source, store=store, event_id="8488",
        operator_receipt_ref="operator:m31b3:8488",
    )
    assert report["success"] is True
    assert store.get_cursor("warroom-os", "warroom-os") == 0

    # ...the durable receipt is what licenses the caller to advance it.
    store.advance_cursor("warroom-os", "warroom-os", report["event_seq"])
    assert store.get_cursor("warroom-os", "warroom-os") == 8488

    # Restart/reconcile after cursor advance stays idempotent.
    again = record_operator_resolution_receipt(
        board="warroom-os", source=source, store=store, event_id="kanban-event-8488",
        operator_receipt_ref="operator:m31b3:8488",
    )
    assert again["created"] is False
    assert again["instance_id"] == report["instance_id"]
    assert len(store.list_instances()) == 1
    assert store.get_cursor("warroom-os", "warroom-os") == 8488
