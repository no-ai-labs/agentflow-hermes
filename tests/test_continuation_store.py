from __future__ import annotations

from agentflow_hermes.continuation_store import (
    ContinuationState,
    ContinuationStore,
    doctor_store_selection,
)


def _store(tmp_path):
    return ContinuationStore(tmp_path / "agentflow.sqlite")


def _make_instance(store, **overrides):
    kwargs = dict(
        board="warroom-os",
        source_task_id="t_ab93a206",
        source_event_id="ev_1",
        source_graph_id="g_1",
        contract_ref="warroom.g421.exposure-resolution.v1",
        verdict="BLOCK",
        continuation_kind="needs_input",
        origin_ref="discord:#research",
        return_to_ref="discord:#research",
    )
    kwargs.update(overrides)
    return store.create_instance(**kwargs)


def test_create_instance_is_idempotent_for_same_source_tuple(tmp_path):
    store = _store(tmp_path)
    first = _make_instance(store)
    second = _make_instance(store)
    assert first["created"] is True
    assert second["created"] is False
    assert first["instance"]["id"] == second["instance"]["id"]
    assert len(store.list_instances()) == 1


def test_create_instance_distinct_source_event_creates_new_instance(tmp_path):
    store = _store(tmp_path)
    first = _make_instance(store, source_event_id="ev_1")
    second = _make_instance(store, source_event_id="ev_2")
    assert first["instance"]["id"] != second["instance"]["id"]
    assert len(store.list_instances()) == 2


def test_new_instance_starts_detected(tmp_path):
    store = _store(tmp_path)
    result = _make_instance(store)
    assert result["instance"]["state"] == ContinuationState.DETECTED.value


def test_legal_transition_applies(tmp_path):
    store = _store(tmp_path)
    instance_id = _make_instance(store)["instance"]["id"]
    result = store.transition(instance_id, ContinuationState.WAITING_OWNER)
    assert result["success"] is True
    assert result["applied"] is True
    assert store.get_instance(instance_id)["state"] == ContinuationState.WAITING_OWNER.value


def test_illegal_transition_is_rejected(tmp_path):
    store = _store(tmp_path)
    instance_id = _make_instance(store)["instance"]["id"]
    # DETECTED -> RESUMED is not a legal direct transition.
    result = store.transition(instance_id, ContinuationState.RESUMED)
    assert result["success"] is False
    assert result["error"] == "illegal_transition"
    assert store.get_instance(instance_id)["state"] == ContinuationState.DETECTED.value


def test_any_state_can_move_to_blocked_invalid(tmp_path):
    store = _store(tmp_path)
    instance_id = _make_instance(store)["instance"]["id"]
    store.transition(instance_id, ContinuationState.WAITING_OWNER)
    result = store.transition(instance_id, ContinuationState.BLOCKED_INVALID)
    assert result["success"] is True


def test_terminal_state_rejects_further_transitions(tmp_path):
    store = _store(tmp_path)
    instance_id = _make_instance(store)["instance"]["id"]
    store.transition(instance_id, ContinuationState.WAITING_OWNER)
    store.transition(instance_id, ContinuationState.BLOCKED_INVALID)
    result = store.transition(instance_id, ContinuationState.WAITING_OWNER)
    assert result["success"] is False
    assert result["error"] == "already_terminal"


def test_owner_receipts_are_append_only_and_versioned(tmp_path):
    store = _store(tmp_path)
    instance_id = _make_instance(store)["instance"]["id"]
    r1 = store.add_owner_receipt(instance_id, owner_ref="operator-main", fields={"a": "1"})
    r2 = store.add_owner_receipt(instance_id, owner_ref="operator-main", fields={"a": "2"}, supersedes_receipt_id=r1["id"])
    assert r1["version"] == 1
    assert r2["version"] == 2
    receipts = store.list_owner_receipts(instance_id)
    assert [r["version"] for r in receipts] == [1, 2]
    latest = store.latest_owner_receipt(instance_id)
    assert latest["version"] == 2
    assert latest["fields"]["a"] == "2"


def test_steps_are_idempotent_by_key(tmp_path):
    store = _store(tmp_path)
    instance_id = _make_instance(store)["instance"]["id"]
    s1 = store.add_step(instance_id, step_kind="owner_anchor", idempotency_key="anchor:1")
    s2 = store.add_step(instance_id, step_kind="owner_anchor", idempotency_key="anchor:1")
    assert s1["created"] is True
    assert s2["created"] is False
    assert s1["step"]["id"] == s2["step"]["id"]
    assert store.count_steps(instance_id) == 1
    assert store.count_steps(instance_id, step_kind="materialization") == 0


def test_mark_step_updates_state_and_board_task_id(tmp_path):
    store = _store(tmp_path)
    instance_id = _make_instance(store)["instance"]["id"]
    step = store.add_step(instance_id, step_kind="owner_anchor", idempotency_key="anchor:1")["step"]
    store.mark_step(step["id"], state="applied", board_task_id="task:xyz")
    updated = store.list_steps(instance_id)[0]
    assert updated["state"] == "applied"
    assert updated["board_task_id"] == "task:xyz"


def test_outbox_enqueue_is_idempotent_and_reconciles(tmp_path):
    store = _store(tmp_path)
    instance_id = _make_instance(store)["instance"]["id"]
    op1 = store.outbox_enqueue(instance_id, step_id="", operation="create_task", payload={"x": 1}, idempotency_key="op:1")
    op2 = store.outbox_enqueue(instance_id, step_id="", operation="create_task", payload={"x": 1}, idempotency_key="op:1")
    assert op1["created"] is True
    assert op2["created"] is False
    assert len(store.list_outbox()) == 1
    store.outbox_mark(op1["outbox"]["id"], state="applied", board_task_id="task:abc")
    row = store.list_outbox()[0]
    assert row["state"] == "applied"
    assert row["board_task_id"] == "task:abc"


def test_board_cursor_scoped_per_board_and_db_identity(tmp_path):
    store = _store(tmp_path)
    store.advance_cursor("warroom-os", "db-a", 100)
    store.advance_cursor("oracle-lab", "db-b", 100)  # overlapping event id across boards is valid
    assert store.get_cursor("warroom-os", "db-a") == 100
    assert store.get_cursor("oracle-lab", "db-b") == 100
    assert store.get_cursor("warroom-os", "db-c") == 0


def test_advance_cursor_never_decreases(tmp_path):
    store = _store(tmp_path)
    store.advance_cursor("warroom-os", "db-a", 50)
    store.advance_cursor("warroom-os", "db-a", 10)
    assert store.get_cursor("warroom-os", "db-a") == 50


def test_doctor_reports_no_split_brain_for_single_active_store(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical.sqlite"
    fallback = tmp_path / "fallback.db"
    monkeypatch.setenv("HERMES_CONTINUATION_DB", str(canonical))
    store = ContinuationStore(canonical)
    _make_instance(store)
    result = doctor_store_selection(canonical_path=canonical, fallback_path=fallback)
    assert result["success"] is True
    assert result["split_brain"] is False
    assert result["selected"] == str(canonical)


def test_doctor_blocks_on_split_brain_both_active(tmp_path):
    canonical = tmp_path / "canonical.sqlite"
    fallback = tmp_path / "fallback.db"
    _make_instance(ContinuationStore(canonical))
    _make_instance(ContinuationStore(fallback))
    result = doctor_store_selection(canonical_path=canonical, fallback_path=fallback)
    assert result["success"] is False
    assert result["error"] == "split_store_both_active"


def test_doctor_ignores_jobs_only_fallback_collision(tmp_path):
    import sqlite3

    canonical = tmp_path / "canonical.sqlite"
    fallback = tmp_path / "fallback.db"
    _make_instance(ContinuationStore(canonical))
    with sqlite3.connect(fallback) as con:
        con.executescript(
            """
            create table jobs(id text primary key, status text not null);
            insert into jobs(id, status) values('job_collision', 'queued');
            """
        )

    result = doctor_store_selection(canonical_path=canonical, fallback_path=fallback)

    assert result["success"] is True
    assert result["split_brain"] is False
    assert result["selected"] == str(canonical)
    assert result["fallback_active"] is False
