from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from agentflow_hermes.board_adapter import RealBoardAdapter
from agentflow_hermes.board_events import BoardEvent, FakeBoardEventSource
from agentflow_hermes.continuation_config import load_contract_registry
from agentflow_hermes.continuation_engine import ingest_board_once
from agentflow_hermes.continuation_store import ContinuationState, ContinuationStore
from agentflow_hermes.continuations.semantic_refusal import SemanticRefusalHandler, _stable_digest
from agentflow_hermes.outcome import ContinuationKind, OutcomeEnvelope, Verdict

_REPO = Path(__file__).resolve().parents[1]
_GENERIC_CONTRACT_YAML = _REPO / "contracts" / "generic.owner-input.v1.yaml"


def _contracts():
    return load_contract_registry([_GENERIC_CONTRACT_YAML])


def _create_current_hermes_notify_schema(db: Path) -> None:
    con = sqlite3.connect(db)
    con.executescript(
        """
        create table kanban_notify_subs (
            task_id text not null,
            platform text not null,
            chat_id text not null,
            thread_id text not null default '',
            user_id text,
            notifier_profile text,
            trigger_agent integer not null default 0,
            created_at integer not null,
            last_event_id integer not null default 0,
            delivery_mode text not null default 'notify',
            chat_type text not null default 'dm',
            user_id_alt text,
            primary key(task_id, platform, chat_id, thread_id)
        );
        create table kanban_task_origin (
            task_id text primary key,
            platform text not null,
            chat_id text not null,
            thread_id text not null default '',
            user_id text,
            user_id_alt text,
            notifier_profile text,
            chat_type text,
            session_key text,
            created_at integer not null,
            updated_at integer not null
        );
        create table kanban_notify_receipts (
            task_id text not null,
            platform text not null,
            chat_id text not null,
            thread_id text not null default '',
            notify_delivery_status text,
            notify_delivery_at integer,
            active_wake_status text,
            active_wake_at integer,
            consumer_ack_status text,
            consumer_ack_at integer,
            updated_at integer not null,
            primary key(task_id, platform, chat_id, thread_id)
        );
        """
    )
    con.close()


def _insert_delivered_one_shot_receipt(db: Path, task_id: str = "t_source") -> None:
    con = sqlite3.connect(db)
    con.execute(
        """
        insert into kanban_task_origin(
            task_id, platform, chat_id, thread_id, notifier_profile, chat_type, created_at, updated_at
        ) values (?, 'discord', '1497895797579190357', '', 'default', 'group', 1, 1)
        """,
        (task_id,),
    )
    con.execute(
        """
        insert into kanban_notify_receipts(
            task_id, platform, chat_id, thread_id,
            notify_delivery_status, notify_delivery_at, active_wake_status, active_wake_at,
            consumer_ack_status, updated_at
        ) values (?, 'discord', '1497895797579190357', '', 'delivered', 2, 'accepted', 2, NULL, 2)
        """,
        (task_id,),
    )
    con.commit()
    con.close()


def _semantic_refusal_event() -> BoardEvent:
    return BoardEvent(
        event_id="kanban-event-3968",
        event_seq=3968,
        source_task_id="t_source",
        source_graph_id="graph:t_source",
        origin_ref="discord:1497895797579190357",
        return_to_ref="discord:1497895797579190357",
        run_metadata={
            "agentflow_outcome": {
                "schema_version": 1,
                "verdict": "BLOCK",
                "continuation_kind": "semantic_refusal",
                "refusal_categories": ["credentials"],
                "blockers": ["credentials required from user"],
            }
        },
        summary="Verdict: BLOCK\nBlockers: credentials required from user",
    )


def _adapter(db: Path, calls: list[list[str]]) -> RealBoardAdapter:
    def runner(argv):
        calls.append(argv)
        if "--help" in argv:
            return 0, "usage: hermes kanban notify-subscribe --delivery-mode notify+wake --chat-type channel", ""
        if "wake-origin" in argv:
            con = sqlite3.connect(db)
            con.execute(
                """
                insert into kanban_task_origin(
                    task_id, platform, chat_id, thread_id, notifier_profile, chat_type, created_at, updated_at
                ) values (?, 'discord', '1497895797579190357', '', 'default', 'group', 1, 1)
                on conflict(task_id) do update set updated_at=excluded.updated_at
                """,
                ("t_source",),
            )
            con.execute(
                """
                insert into kanban_notify_receipts(
                    task_id, platform, chat_id, thread_id,
                    active_wake_status, active_wake_at, consumer_ack_status, updated_at
                ) values (?, 'discord', '1497895797579190357', '', 'scheduled', 2, NULL, 2)
                on conflict(task_id, platform, chat_id, thread_id) do update set
                    active_wake_status='scheduled', active_wake_at=2, updated_at=2
                """,
                ("t_source",),
            )
            con.commit()
            con.close()
            return 0, json.dumps({"task_id": "t_source", "active_wake_status": "scheduled"}), ""
        if "consumer-ack-origin" in argv:
            con = sqlite3.connect(db)
            con.execute(
                """
                update kanban_notify_receipts
                set consumer_ack_status='semantic_refusal_ack', consumer_ack_at=4, updated_at=4
                where task_id='t_source' and platform='discord' and chat_id='1497895797579190357' and thread_id=''
                """
            )
            con.commit()
            con.close()
            return 0, json.dumps({"task_id": "t_source", "consumer_ack_status": "semantic_refusal_ack"}), ""
        return 0, "Subscribed", ""

    return RealBoardAdapter(runner=runner, board="agentflow-hermes", board_db_path=db)


def _route(store: ContinuationStore, db: Path, calls: list[list[str]]):
    source = FakeBoardEventSource(db_identity="agentflow-hermes", events=[_semantic_refusal_event()])
    return ingest_board_once(
        board="agentflow-hermes",
        source=source,
        store=store,
        contract_registry=_contracts(),
        adapter=_adapter(db, calls),
        default_endpoint="discord:1497895797579190357",
        apply=True,
    )


def test_semantic_refusal_cli_failure_backoff_bounds_events_and_clears_on_convergence(tmp_path):
    """M30I: scans while outbox is in backoff must not append state
    transitions per scan. Applied rows clear stale retry/error metadata."""
    board_db = tmp_path / "kanban.db"
    _create_current_hermes_notify_schema(board_db)
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    store.advance_cursor("agentflow-hermes", "agentflow-hermes", 0)
    calls: list[list[str]] = []
    mode = {"value": "fail"}

    def runner(argv):
        calls.append(argv)
        if "--help" in argv:
            return 0, "usage: hermes kanban notify-subscribe --delivery-mode notify+wake --chat-type channel", ""
        if "wake-origin" in argv:
            if mode["value"] == "fail":
                return 1, "", "hermes missing from systemd PATH"
            con = sqlite3.connect(board_db)
            con.execute(
                """
                insert into kanban_task_origin(
                    task_id, platform, chat_id, thread_id, notifier_profile, chat_type, created_at, updated_at
                ) values ('t_source', 'discord', '1497895797579190357', '', 'default', 'group', 1, 1)
                on conflict(task_id) do update set updated_at=excluded.updated_at
                """
            )
            con.execute(
                """
                insert into kanban_notify_receipts(
                    task_id, platform, chat_id, thread_id, active_wake_status, active_wake_at, consumer_ack_status, updated_at
                ) values ('t_source', 'discord', '1497895797579190357', '', 'accepted', 2, NULL, 2)
                on conflict(task_id, platform, chat_id, thread_id) do update set
                    active_wake_status='accepted', active_wake_at=2, updated_at=2
                """
            )
            con.commit()
            con.close()
            return 0, json.dumps({"task_id": "t_source", "active_wake_status": "accepted"}), ""
        if "consumer-ack-origin" in argv:
            con = sqlite3.connect(board_db)
            con.execute(
                """
                update kanban_notify_receipts
                set consumer_ack_status='semantic_refusal_ack', consumer_ack_at=4, updated_at=4
                where task_id='t_source' and platform='discord' and chat_id='1497895797579190357' and thread_id=''
                """
            )
            con.commit()
            con.close()
            return 0, json.dumps({"task_id": "t_source", "consumer_ack_status": "semantic_refusal_ack"}), ""
        return 0, "", ""

    def route():
        source = FakeBoardEventSource(db_identity="agentflow-hermes", events=[_semantic_refusal_event()])
        return ingest_board_once(
            board="agentflow-hermes",
            source=source,
            store=store,
            contract_registry=_contracts(),
            adapter=RealBoardAdapter(runner=runner, board="agentflow-hermes", board_db_path=board_db),
            default_endpoint="discord:1497895797579190357",
            apply=True,
        )

    first = route()
    assert first["cursor"] == 0
    assert first["results"][0]["router_success"] is False
    instance = store.list_instances()[0]
    event_count_after_attempt = len(store.list_events(instance["id"]))
    row_after_attempt = store.list_outbox()[0]
    assert row_after_attempt["attempts"] == 1
    assert row_after_attempt["last_error"] == "cli_runner_failed"
    assert row_after_attempt["next_attempt_at"] > row_after_attempt["updated_at"]

    for _ in range(5):
        replay = route()
        assert replay["cursor"] == 0
        assert replay["results"][0]["router_success"] is False

    assert len(store.list_events(instance["id"])) == event_count_after_attempt
    assert store.list_outbox()[0]["attempts"] == 1
    assert len([c for c in calls if "wake-origin" in c]) == 1

    mode["value"] = "recover"
    with store.connect() as con:
        con.execute("update board_outbox set next_attempt_at=0")

    recovered = route()
    assert recovered["cursor"] == 3968
    assert recovered["results"][0]["router_success"] is True
    rows = {row["operation"]: row for row in store.list_outbox()}
    assert rows["schedule_origin_wake"]["state"] == "applied"
    assert rows["schedule_origin_wake"]["last_error"] == ""
    assert rows["schedule_origin_wake"]["next_attempt_at"] == 0
    assert rows["record_consumer_ack"]["state"] == "applied"
    assert rows["record_consumer_ack"]["last_error"] == ""
    assert rows["record_consumer_ack"]["next_attempt_at"] == 0
    assert len([c for c in calls if "wake-origin" in c]) == 2
    assert len([c for c in calls if "consumer-ack-origin" in c]) == 1

    terminal_event_count = len(store.list_events(instance["id"]))
    outcome = OutcomeEnvelope(
        schema_version=1,
        event_id="kanban-event-3968",
        board="agentflow-hermes",
        source_task_id="t_source",
        source_graph_id="graph:t_source",
        verdict=Verdict.BLOCK,
        continuation_kind=ContinuationKind.SEMANTIC_REFUSAL,
        origin_ref="discord:1497895797579190357",
        return_to_ref="discord:1497895797579190357",
    )
    adapter = RealBoardAdapter(runner=runner, board="agentflow-hermes", board_db_path=board_db)
    for _ in range(3):
        result = SemanticRefusalHandler().materialize(
            outcome,
            store=store,
            adapter=adapter,
            blockers=("credentials required from user",),
            refusal_categories=("credentials",),
        )
        assert result.success is True
        assert result.state == "blocked_invalid"
    assert len(store.list_events(instance["id"])) == terminal_event_count


def test_semantic_refusal_accepts_existing_one_shot_receipt_and_backs_off_retry(tmp_path):
    """M30 live canary shape: canonical notify_sub row was cleaned up, but
    typed origin + delivered/active-wake receipt remain authoritative. The first
    failed verification backs off durably; immediate replay does not hot-loop;
    once the authoritative rows exist, replay records exactly one semantic ACK,
    advances cursor, applies outbox, and creates zero child tasks.
    """
    board_db = tmp_path / "kanban.db"
    _create_current_hermes_notify_schema(board_db)
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    store.advance_cursor("agentflow-hermes", "agentflow-hermes", 0)
    calls: list[list[str]] = []

    first = _route(store, board_db, calls)

    assert first["cursor"] == 0
    assert first["results"][0]["router_success"] is False
    rows = store.list_outbox()
    assert len(rows) == 1
    wake_row = rows[0]
    assert wake_row["state"] == "pending"
    assert wake_row["operation"] == "schedule_origin_wake"
    assert wake_row["attempts"] == 1
    assert wake_row["last_error"] == "origin_wake_not_yet_accepted"
    assert wake_row["next_attempt_at"] > wake_row["updated_at"]
    # Never the generic notify-subscribe path -- only a typed wake-origin call.
    assert [c for c in calls if "notify-subscribe" in c and "--help" not in c] == []
    assert len([c for c in calls if "wake-origin" in c]) == 1

    call_count_after_first = len([c for c in calls if "wake-origin" in c])
    second = _route(store, board_db, calls)
    rows = store.list_outbox()
    assert second["cursor"] == 0
    assert second["results"][0]["router_success"] is False
    assert rows[0]["attempts"] == 1
    assert len([c for c in calls if "wake-origin" in c]) == call_count_after_first

    con = sqlite3.connect(board_db)
    con.execute(
        """
        update kanban_notify_receipts
        set active_wake_status='accepted', active_wake_at=3, updated_at=3
        where task_id='t_source'
        """
    )
    con.commit()
    con.close()
    con = sqlite3.connect(store.path)
    con.execute("update board_outbox set next_attempt_at=0")
    con.commit()
    con.close()

    third = _route(store, board_db, calls)

    assert third["cursor"] == 3968
    assert third["results"][0]["router_success"] is True
    instance = store.list_instances()[0]
    assert instance["state"] == "blocked_invalid"
    assert store.list_requirement_satisfactions(instance["id"])[0]["field_name"] == "semantic_refusal"
    assert store.count_steps(instance["id"]) == 0
    final_rows = {row["operation"]: row for row in store.list_outbox()}
    assert final_rows["schedule_origin_wake"]["state"] == "applied"
    assert final_rows["schedule_origin_wake"]["attempts"] == 2
    assert final_rows["record_consumer_ack"]["state"] == "applied"
    assert final_rows["record_consumer_ack"]["attempts"] == 1
    con = sqlite3.connect(board_db)
    receipt = con.execute(
        "select notify_delivery_status, active_wake_status, consumer_ack_status from kanban_notify_receipts where task_id='t_source'"
    ).fetchone()
    con.close()
    assert tuple(receipt) == (None, "accepted", "semantic_refusal_ack")
    # The now-present receipt satisfies the durable check directly -- no
    # second wake-origin call was needed.
    assert len([c for c in calls if "wake-origin" in c]) == 1
    assert len([c for c in calls if "consumer-ack-origin" in c]) == 1


def test_real_adapter_verifies_existing_one_shot_receipt_without_consumer_ack(tmp_path):
    db = tmp_path / "kanban.db"
    _create_current_hermes_notify_schema(db)
    _insert_delivered_one_shot_receipt(db, task_id="t_owner")
    calls: list[list[str]] = []

    result = _adapter(db, calls).subscribe("t_owner", "discord:1497895797579190357")

    assert result["success"] is True
    assert result["ack"]["source"] == "existing_notify_wake_receipt"
    assert result["ack"]["consumer_ack_status"] == ""
    assert [c for c in calls if "notify-subscribe" in c and "--help" not in c] == []


def test_recovery_of_terminal_instance_missing_consumer_ack_enqueues_only_that(tmp_path):
    """Production-shaped recovery: a live continuation instance is already
    terminal (blocked_invalid) with the internal semantic_refusal_ack
    requirement satisfaction and an applied+accepted wake outbox row -- a
    shape that could exist from before the Hermes consumer-ack boundary
    existed. Re-running the handler (the recovery path) must enqueue and
    apply only the missing record_consumer_ack outbox operation: it must not
    replay the wake, must not touch the cursor, must not create a second
    instance/requirement satisfaction, and must not recreate the wake outbox
    evidence."""
    board_db = tmp_path / "kanban.db"
    _create_current_hermes_notify_schema(board_db)
    # Origin wake already durably accepted; consumer ack column still NULL --
    # the exact pre-fix production shape.
    con = sqlite3.connect(board_db)
    con.execute(
        """
        insert into kanban_task_origin(
            task_id, platform, chat_id, thread_id, notifier_profile, chat_type, created_at, updated_at
        ) values ('t_source', 'discord', '1497895797579190357', '', 'default', 'group', 1, 1)
        """
    )
    con.execute(
        """
        insert into kanban_notify_receipts(
            task_id, platform, chat_id, thread_id,
            active_wake_status, active_wake_at, consumer_ack_status, updated_at
        ) values ('t_source', 'discord', '1497895797579190357', '', 'accepted', 2, NULL, 2)
        """
    )
    con.commit()
    con.close()

    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    creation = store.create_instance(
        board="agentflow-hermes",
        source_task_id="t_source",
        source_event_id="kanban-event-3968",
        source_graph_id="graph:t_source",
        verdict="BLOCK",
        continuation_kind="semantic_refusal",
        origin_ref="discord:1497895797579190357",
        return_to_ref="discord:1497895797579190357",
    )
    instance = creation["instance"]
    instance_id = instance["id"]
    digest = _stable_digest(instance)
    endpoint = "discord:1497895797579190357"

    wake_row = store.outbox_enqueue(
        instance_id,
        step_id="0",
        operation="schedule_origin_wake",
        payload={"task_id": "t_source", "endpoint": endpoint},
        idempotency_key=f"semantic_refusal_wake:{digest}:{endpoint}",
    )["outbox"]
    store.outbox_mark(wake_row["id"], state="applied")
    store.record_requirement_satisfaction(
        instance_id,
        field_name="semantic_refusal",
        value={"categories": ["credentials"], "blockers": ["credentials required from user"]},
        source_kind="semantic_refusal_ack",
        source_ref="credentials",
    )
    store.transition(instance_id, ContinuationState.BLOCKED_INVALID, reason="pre_fix_quarantine")
    store.advance_cursor("agentflow-hermes", "agentflow-hermes-db", 3968)

    cursor_before = store.get_cursor("agentflow-hermes", "agentflow-hermes-db")
    wake_row_before = {row["id"]: row for row in store.list_outbox()}[wake_row["id"]]

    calls: list[list[str]] = []
    adapter = _adapter(board_db, calls)
    outcome = OutcomeEnvelope(
        schema_version=1,
        event_id="kanban-event-3968",
        board="agentflow-hermes",
        source_task_id="t_source",
        source_graph_id="graph:t_source",
        verdict=Verdict.BLOCK,
        continuation_kind=ContinuationKind.SEMANTIC_REFUSAL,
        origin_ref=endpoint,
        return_to_ref=endpoint,
    )

    result = SemanticRefusalHandler().materialize(
        outcome,
        store=store,
        adapter=adapter,
        blockers=("credentials required from user",),
        refusal_categories=("credentials",),
    )

    assert result.success is True
    assert result.state == "blocked_invalid"

    # Exactly one instance, one requirement satisfaction -- no duplicate
    # created by re-running the handler against a terminal instance.
    assert len(store.list_instances()) == 1
    assert store.list_instances()[0]["id"] == instance_id
    assert len(store.list_requirement_satisfactions(instance_id)) == 1

    # Wake outbox evidence untouched -- no replayed wake-origin call.
    wake_row_after = {row["id"]: row for row in store.list_outbox()}[wake_row["id"]]
    assert wake_row_after["state"] == "applied"
    assert wake_row_after["attempts"] == wake_row_before["attempts"]
    assert [c for c in calls if "wake-origin" in c] == []

    # Only the missing consumer-ack operation was enqueued and applied.
    rows = {row["operation"]: row for row in store.list_outbox()}
    assert set(rows) == {"schedule_origin_wake", "record_consumer_ack"}
    assert rows["record_consumer_ack"]["state"] == "applied"
    assert rows["record_consumer_ack"]["attempts"] == 1
    assert len([c for c in calls if "consumer-ack-origin" in c]) == 1

    # Cursor untouched by the recovery call (this handler call does not own
    # cursor advancement -- ingest_board_once does).
    assert store.get_cursor("agentflow-hermes", "agentflow-hermes-db") == cursor_before

    con = sqlite3.connect(board_db)
    receipt = con.execute(
        "select active_wake_status, consumer_ack_status from kanban_notify_receipts where task_id='t_source'"
    ).fetchone()
    origin_count = con.execute("select count(*) from kanban_task_origin where task_id='t_source'").fetchone()[0]
    con.close()
    assert tuple(receipt) == ("accepted", "semantic_refusal_ack")
    assert origin_count == 1


def test_consumer_ack_cli_timeout_persists_bounded_error_and_non_due_retry_is_quiet(tmp_path):
    board_db = tmp_path / "kanban.db"
    _create_current_hermes_notify_schema(board_db)
    _insert_delivered_one_shot_receipt(board_db, task_id="t_source")
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    store.advance_cursor("agentflow-hermes", "agentflow-hermes", 0)
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(argv)
        if "consumer-ack-origin" in argv:
            raise subprocess.TimeoutExpired(argv, timeout=30)
        return 0, "{}", ""

    adapter = RealBoardAdapter(runner=runner, board="agentflow-hermes", board_db_path=board_db)
    source = FakeBoardEventSource(db_identity="agentflow-hermes", events=[_semantic_refusal_event()])

    first = ingest_board_once(
        board="agentflow-hermes",
        source=source,
        store=store,
        contract_registry=_contracts(),
        adapter=adapter,
        default_endpoint="discord:1497895797579190357",
        apply=True,
    )

    assert first["cursor"] == 0
    assert first["results"][0]["router_success"] is False
    rows = {row["operation"]: row for row in store.list_outbox()}
    assert rows["record_consumer_ack"]["state"] == "pending"
    assert rows["record_consumer_ack"]["last_error"] == "cli_runner_timeout"
    instance = store.list_instances()[0]
    transition_count = len([e for e in store.list_events(instance["id"]) if e["kind"] == "state_transition"])

    # Immediate replay sees the pending outbox backoff and must not append the
    # failed_retryable -> materializing -> failed_retryable storm observed live.
    second = ingest_board_once(
        board="agentflow-hermes",
        source=source,
        store=store,
        contract_registry=_contracts(),
        adapter=adapter,
        default_endpoint="discord:1497895797579190357",
        apply=True,
    )

    assert second["cursor"] == 0
    assert second["results"][0]["router_success"] is False
    assert len([c for c in calls if "consumer-ack-origin" in c]) == 1
    assert len([e for e in store.list_events(instance["id"]) if e["kind"] == "state_transition"]) == transition_count


def test_consumer_ack_cli_lock_error_is_sanitized(tmp_path):
    board_db = tmp_path / "kanban.db"
    _create_current_hermes_notify_schema(board_db)
    _insert_delivered_one_shot_receipt(board_db, task_id="t_source")
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    store.advance_cursor("agentflow-hermes", "agentflow-hermes", 0)

    def runner(argv):
        if "consumer-ack-origin" in argv:
            # Some Hermes CLI initialization-lock failures have historically
            # printed a lock error on stderr without propagating a non-zero rc.
            # The adapter still must persist the bounded class, not raw stderr
            # and not a misleading cli_invalid_json.
            return 0, "", "sqlite3.OperationalError: database is locked: /private/path/kanban.db"
        return 0, "{}", ""

    result = ingest_board_once(
        board="agentflow-hermes",
        source=FakeBoardEventSource(db_identity="agentflow-hermes", events=[_semantic_refusal_event()]),
        store=store,
        contract_registry=_contracts(),
        adapter=RealBoardAdapter(runner=runner, board="agentflow-hermes", board_db_path=board_db),
        default_endpoint="discord:1497895797579190357",
        apply=True,
    )

    assert result["cursor"] == 0
    rows = {row["operation"]: row for row in store.list_outbox()}
    assert rows["record_consumer_ack"]["last_error"] == "cli_runner_db_locked"
