from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agentflow_hermes.board_adapter import RealBoardAdapter
from agentflow_hermes.board_events import BoardEvent, FakeBoardEventSource
from agentflow_hermes.continuation_config import load_contract_registry
from agentflow_hermes.continuation_engine import ingest_board_once
from agentflow_hermes.continuation_store import ContinuationStore

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
    assert rows[0]["state"] == "pending"
    assert rows[0]["attempts"] == 1
    assert rows[0]["last_error"] == "origin_wake_not_yet_accepted"
    assert rows[0]["next_attempt_at"] > rows[0]["updated_at"]
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
    final_rows = store.list_outbox()
    assert final_rows[0]["state"] == "applied"
    assert final_rows[0]["attempts"] == 2
    # The now-present receipt satisfies the durable check directly -- no
    # second wake-origin call was needed.
    assert len([c for c in calls if "wake-origin" in c]) == 1


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
