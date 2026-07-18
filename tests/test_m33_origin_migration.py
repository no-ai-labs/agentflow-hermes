"""M33: Discord origin canonicalization + legacy callback repair (t_e9f64682).

All fixtures are temp SQLite DBs with exact real-shaped schemas. No live DB is
touched and no live ``--apply`` runs; the ACK runner is a fake that writes into
the temp board DB so read-back proof is exercised deterministically.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agentflow_hermes.board_adapter import RealBoardAdapter
from agentflow_hermes.board_events import load_board_registry
from agentflow_hermes.continuation_store import ContinuationStore
from agentflow_hermes import origin_migration as om
from agentflow_hermes.cli import main as cli_main

FROM = "research"
FROM_HASH = "#research"
FROM_OLD = "#research-old"
TO = "1499390151393284106"

# Real per-board Kanban shape (only the columns the migration/adapter read).
_BOARD_SCHEMA = """
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
create table kanban_task_origin (
    task_id text primary key, platform text not null, chat_id text not null,
    thread_id text not null default '', user_id text, notifier_profile text,
    chat_type text, created_at integer not null, updated_at integer not null
);
create table kanban_notify_receipts (
    task_id text not null, platform text not null, chat_id text not null,
    thread_id text not null default '',
    notify_delivery_status text, notify_delivery_at integer,
    active_wake_status text, active_wake_at integer,
    consumer_ack_status text, consumer_ack_at integer,
    updated_at integer not null,
    primary key(task_id, platform, chat_id, thread_id)
);
create table kanban_notify_subs (
    task_id text not null, platform text not null, chat_id text not null,
    thread_id text not null default '', created_at integer not null,
    primary key(task_id, platform, chat_id, thread_id)
);
"""


def _origin(con, task_id, chat_id, thread_id=""):
    con.execute(
        "insert into kanban_task_origin(task_id, platform, chat_id, thread_id, notifier_profile, chat_type, created_at, updated_at)"
        " values(?, 'discord', ?, ?, 'default', 'channel', 1, 1)",
        (task_id, chat_id, thread_id),
    )


def _receipt(con, task_id, chat_id, *, notify=None, wake=None, ack=None, thread_id="", updated_at=1):
    con.execute(
        "insert into kanban_notify_receipts(task_id, platform, chat_id, thread_id,"
        " notify_delivery_status, notify_delivery_at, active_wake_status, active_wake_at,"
        " consumer_ack_status, consumer_ack_at, updated_at) values(?, 'discord', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, chat_id, thread_id, notify, updated_at, wake, updated_at, ack, updated_at, updated_at),
    )


def _sub(con, task_id, chat_id, thread_id=""):
    con.execute(
        "insert into kanban_notify_subs(task_id, platform, chat_id, thread_id, created_at) values(?, 'discord', ?, ?, 1)",
        (task_id, chat_id, thread_id),
    )


def build_board_db(tmp_path: Path) -> Path:
    """Board with exact bare/literal aliases, a non-alias symbolic control,
    numeric controls, and collision rows. Only ``research``/``#research`` are
    in the bounded migration alias set; ``#research-old`` must stay untouched."""
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.executescript(_BOARD_SCHEMA)
    # Task rows carry a secret in body/summary to prove they are never surfaced.
    for tid in ("t_plain", "t_collide", "t_hash_plain", "t_hash_collide", "t_old", "t_numeric"):
        con.execute(
            "insert into tasks(id, title, body, assignee, status, workspace_path, workflow_template_id)"
            " values(?, 'title', 'API_KEY=sk-secret-do-not-print', 'worker', 'blocked', '/tmp/ws', 'g')",
            (tid,),
        )
        con.execute(
            "insert into task_runs(id, task_id, step_key, status, summary, metadata) values(?, ?, 'impl', 'blocked', 'SECRET summary text', '{}')",
            (hash(tid) & 0xFFFF, tid, ),
        )

    _origin(con, "t_plain", FROM)
    _receipt(con, "t_plain", FROM, notify="delivered", wake="accepted", ack=None)
    _sub(con, "t_plain", FROM)

    _origin(con, "t_collide", FROM)
    # symbolic side: strong notify+wake, no ack
    _receipt(con, "t_collide", FROM, notify="delivered", wake="accepted", ack=None, updated_at=5)
    # numeric side already present: weak notify/wake but an ACK
    _receipt(con, "t_collide", TO, notify="pending", wake="pending", ack="acked", updated_at=9)
    _sub(con, "t_collide", FROM)

    _origin(con, "t_hash_plain", FROM_HASH)
    _receipt(con, "t_hash_plain", FROM_HASH, notify="delivered", wake="accepted", ack=None)
    _sub(con, "t_hash_plain", FROM_HASH)

    _origin(con, "t_hash_collide", FROM_HASH)
    _receipt(con, "t_hash_collide", FROM_HASH, notify="delivered", wake="accepted", ack=None, updated_at=6)
    _receipt(con, "t_hash_collide", TO, notify="pending", wake="pending", ack="acked", updated_at=10)
    _sub(con, "t_hash_collide", FROM_HASH)

    _origin(con, "t_old", FROM_OLD)
    _receipt(con, "t_old", FROM_OLD, notify="delivered", wake="accepted", ack="acked")
    _sub(con, "t_old", FROM_OLD)

    _origin(con, "t_numeric", TO)
    _receipt(con, "t_numeric", TO, notify="delivered", wake="completed", ack="acked")
    con.commit()
    con.close()
    return db


def build_store(tmp_path: Path, *, board="warroom-os") -> ContinuationStore:
    store = ContinuationStore(tmp_path / "control-plane.sqlite")
    store.init()
    return store


def enqueue_deadletter(store, *, board, task_id, endpoint, status="acked", idem):
    """Create an instance + a callback_deadletter outbox row for record_consumer_ack."""
    inst = store.create_instance(
        board=board, source_task_id=task_id, source_event_id=f"evt:{task_id}",
        contract_ref="semantic_refusal", origin_ref=endpoint, return_to_ref=endpoint,
    )["instance"]
    ob = store.outbox_enqueue(
        inst["id"], step_id="", operation="record_consumer_ack",
        payload={"task_id": task_id, "endpoint": endpoint, "status": status}, idempotency_key=idem,
    )["outbox"]
    store.outbox_mark(ob["id"], state="callback_deadletter", last_error="callback_ack_symbolic")
    return ob["id"]


def ack_writer_runner(db: Path, *, calls: list, fail=False, malformed=False):
    """Fake Hermes CLI runner. On consumer-ack-origin it writes a numeric ack
    receipt into the board DB (so read-back proof passes) and returns JSON."""
    def runner(argv):
        calls.append(argv)
        if "--help" in argv:
            return 0, "usage", ""
        if "consumer-ack-origin" in argv:
            if fail:
                return 1, "", "boom"
            if malformed:
                return 0, "not json{", ""
            # extract chat id + status + task id from argv
            task_id = argv[argv.index("consumer-ack-origin") + 1]
            status = argv[argv.index("--status") + 1]
            chat_id = argv[argv.index("--chat-id") + 1]
            con = sqlite3.connect(db)
            con.execute(
                "insert into kanban_notify_receipts(task_id, platform, chat_id, thread_id,"
                " active_wake_status, consumer_ack_status, consumer_ack_at, updated_at)"
                " values(?, 'discord', ?, '', 'accepted', ?, 2, 2)"
                " on conflict(task_id, platform, chat_id, thread_id) do update set"
                " consumer_ack_status=excluded.consumer_ack_status, consumer_ack_at=2, updated_at=2",
                (task_id, chat_id, status),
            )
            con.commit()
            con.close()
            return 0, json.dumps({"success": True, "task_id": task_id}), ""
        return 0, "{}", ""
    return runner


# --------------------------------------------------------------------------- #
# input validation
# --------------------------------------------------------------------------- #

def test_numeric_validation():
    assert om.validate_migration_inputs("#research", TO) is None
    assert om.validate_migration_inputs("research", "not-numeric") == "discord_target_not_numeric"
    assert om.validate_migration_inputs("", TO) == "discord_source_empty"
    assert om.validate_migration_inputs("#", TO) == "discord_source_empty"
    assert om.validate_migration_inputs(TO, TO) == "discord_source_equals_target"


# --------------------------------------------------------------------------- #
# audit / list
# --------------------------------------------------------------------------- #

def test_list_symbolic_origins(tmp_path):
    db = build_board_db(tmp_path)
    rows = om.list_symbolic_discord_origins(db)
    ids = sorted(r["task_id"] for r in rows)
    assert ids == ["t_collide", "t_hash_collide", "t_hash_plain", "t_old", "t_plain"]  # t_numeric excluded

    filtered = om.list_symbolic_discord_origins(db, from_id="#research")
    assert sorted(r["task_id"] for r in filtered) == ["t_collide", "t_hash_collide", "t_hash_plain", "t_plain"]
    assert {r["chat_id"] for r in filtered} == {FROM, FROM_HASH}


# --------------------------------------------------------------------------- #
# dry-run: deterministic report, byte + state equality, zero writes
# --------------------------------------------------------------------------- #

def test_dry_run_report_shape(tmp_path):
    db = build_board_db(tmp_path)
    store = build_store(tmp_path)
    enqueue_deadletter(store, board="warroom-os", task_id="t_plain", endpoint="discord:research", idem="dl:1")
    report = om.plan_discord_migration(board="warroom-os", from_id="#research", to_id=TO, board_db=db, store=store)
    assert report["success"] and report["writes"] == 0 and report["created_tasks"] == 0
    assert report["cursor_rewrites"] == 0
    assert report["from"] == FROM_HASH
    assert report["from_aliases"] == [FROM_HASH, FROM]
    assert report["source_token_counts"][FROM_HASH] == {"origins": 2, "receipts": 2, "subscriptions": 2, "total": 6}
    assert report["source_token_counts"][FROM] == {"origins": 2, "receipts": 2, "subscriptions": 2, "total": 6}
    assert report["source_total_counts"] == {"origins": 4, "receipts": 4, "subscriptions": 4, "total": 12}
    assert sorted(r["task_id"] for r in report["task_origin_rows"]) == ["t_collide", "t_hash_collide", "t_hash_plain", "t_plain"]
    assert sorted(c["task_id"] for c in report["collisions"]) == ["t_collide", "t_hash_collide"]
    merged = {m["task_id"]: m for m in report["merged_receipts"]}["t_collide"]
    assert merged["notify_delivery_status"] == "delivered"
    assert merged["active_wake_status"] == "accepted"
    assert merged["consumer_ack_status"] == "acked"
    assert sorted(s["task_id"] for s in report["alias_subs_to_delete"]) == ["t_collide", "t_hash_collide", "t_hash_plain", "t_plain"]
    assert [d["outbox_id"] for d in report["affected_deadletters"]]
    assert "<timestamp>" in report["planned_backups"]["board_db"]


def test_dry_run_byte_and_state_equality(tmp_path):
    db = build_board_db(tmp_path)
    store = build_store(tmp_path)

    def board_snapshot():
        con = sqlite3.connect(db)
        rows = con.execute("select * from kanban_notify_receipts order by task_id, chat_id").fetchall()
        origins = con.execute("select * from kanban_task_origin order by task_id").fetchall()
        subs = con.execute("select * from kanban_notify_subs order by task_id, chat_id").fetchall()
        con.close()
        return (rows, origins, subs)

    before = board_snapshot()
    r1 = json.dumps(om.plan_discord_migration(board="warroom-os", from_id="research", to_id=TO, board_db=db, store=store), sort_keys=True)
    r2 = json.dumps(om.plan_discord_migration(board="warroom-os", from_id="research", to_id=TO, board_db=db, store=store), sort_keys=True)
    after = board_snapshot()
    assert r1 == r2  # byte-identical dry runs
    assert before == after  # zero writes
    assert json.loads(r1)["source_total_counts"] == {"origins": 4, "receipts": 4, "subscriptions": 4, "total": 12}


# --------------------------------------------------------------------------- #
# apply: migration + merge + backups
# --------------------------------------------------------------------------- #

def test_apply_migrates_origins_and_merges_receipts(tmp_path):
    db = build_board_db(tmp_path)
    store = build_store(tmp_path)
    report = om.apply_discord_migration(
        board="warroom-os", from_id="#research", to_id=TO, board_db=db, store=store, now=lambda: 1000.0,
    )
    assert report["success"], report
    m = report["board_migration"]
    assert m["origins_migrated"] == 4
    assert m["receipts_rekeyed"] == 2  # t_plain + t_hash_plain
    assert m["receipts_merged"] == 2   # t_collide + t_hash_collide
    assert m["alias_subs_deleted"] == 4

    con = sqlite3.connect(db)
    # all symbolic gone
    assert con.execute("select count(*) from kanban_task_origin where chat_id=?", (FROM,)).fetchone()[0] == 0
    assert con.execute("select count(*) from kanban_notify_receipts where chat_id=?", (FROM,)).fetchone()[0] == 0
    assert con.execute("select count(*) from kanban_notify_subs where chat_id=?", (FROM,)).fetchone()[0] == 0
    assert con.execute("select count(*) from kanban_task_origin where chat_id=?", (FROM_HASH,)).fetchone()[0] == 0
    assert con.execute("select count(*) from kanban_notify_receipts where chat_id=?", (FROM_HASH,)).fetchone()[0] == 0
    assert con.execute("select count(*) from kanban_notify_subs where chat_id=?", (FROM_HASH,)).fetchone()[0] == 0
    assert con.execute("select count(*) from kanban_task_origin where chat_id=?", (FROM_OLD,)).fetchone()[0] == 1
    assert con.execute("select count(*) from kanban_notify_receipts where chat_id=?", (FROM_OLD,)).fetchone()[0] == 1
    assert con.execute("select count(*) from kanban_notify_subs where chat_id=?", (FROM_OLD,)).fetchone()[0] == 1
    # merged numeric receipt preserves strongest facts
    row = con.execute(
        "select notify_delivery_status, active_wake_status, consumer_ack_status from kanban_notify_receipts"
        " where task_id='t_collide' and chat_id=?", (TO,)
    ).fetchone()
    assert row == ("delivered", "accepted", "acked")
    row = con.execute(
        "select notify_delivery_status, active_wake_status, consumer_ack_status from kanban_notify_receipts"
        " where task_id='t_hash_collide' and chat_id=?", (TO,)
    ).fetchone()
    assert row == ("delivered", "accepted", "acked")
    # numeric-only control untouched (single row)
    assert con.execute("select count(*) from kanban_notify_receipts where task_id='t_numeric'").fetchone()[0] == 1
    con.close()


def test_backup_existence_and_integrity(tmp_path):
    db = build_board_db(tmp_path)
    store = build_store(tmp_path)
    report = om.apply_discord_migration(
        board="warroom-os", from_id="research", to_id=TO, board_db=db, store=store, now=lambda: 4242.0,
    )
    backups = report["backups"]
    board_bak = Path(backups["board_db"]["path"])
    canon_bak = Path(backups["canonical_db"]["path"])
    assert board_bak.exists() and canon_bak.exists()
    assert backups["board_db"]["integrity_ok"] and backups["canonical_db"]["integrity_ok"]
    assert "4242" in board_bak.name
    # backup is a real openable sqlite with the pre-migration symbolic rows
    con = sqlite3.connect(f"file:{board_bak}?mode=ro", uri=True)
    assert con.execute("select count(*) from kanban_task_origin where chat_id=?", (FROM,)).fetchone()[0] == 2
    con.close()


def test_ambiguous_receipt_merge_refused(tmp_path):
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.executescript(_BOARD_SCHEMA)
    _origin(con, "t_amb", FROM)
    _receipt(con, "t_amb", FROM, notify="delivered", wake="accepted", ack="acked")
    _receipt(con, "t_amb", TO, notify="delivered", wake="accepted", ack="refused")
    con.commit()
    con.close()
    store = build_store(tmp_path)
    report = om.plan_discord_migration(board="warroom-os", from_id="research", to_id=TO, board_db=db, store=store)
    assert not report["success"] and report["error"] == "ambiguous_receipt_merge"
    # apply refuses too, before any write
    ap = om.apply_discord_migration(board="warroom-os", from_id="research", to_id=TO, board_db=db, store=store)
    assert not ap["success"] and ap["error"] == "ambiguous_receipt_merge"
    con = sqlite3.connect(db)
    assert con.execute("select count(*) from kanban_task_origin where chat_id=?", (FROM,)).fetchone()[0] == 1
    con.close()


# --------------------------------------------------------------------------- #
# missing DB / schema
# --------------------------------------------------------------------------- #

def test_missing_board_db_refused(tmp_path):
    store = build_store(tmp_path)
    report = om.plan_discord_migration(board="warroom-os", from_id="research", to_id=TO, board_db=tmp_path / "nope.db", store=store)
    assert not report["success"] and report["error"] == "board_db_missing"


def test_missing_schema_refused(tmp_path):
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.executescript("create table tasks(id text primary key);")
    con.commit()
    con.close()
    store = build_store(tmp_path)
    report = om.plan_discord_migration(board="warroom-os", from_id="research", to_id=TO, board_db=db, store=store)
    assert not report["success"] and report["error"] == "board_schema_missing"


def test_dry_run_missing_canonical_store_stays_absent(tmp_path):
    """Regression: a dry-run's deadletter selection must be genuinely
    read-only. It must not call ContinuationStore.init() or otherwise
    create/migrate a missing canonical continuation DB just to report
    writes=0 -- the store path must remain absent afterward."""
    db = build_board_db(tmp_path)
    missing_store_path = tmp_path / "control-plane.sqlite"
    assert not missing_store_path.exists()
    store = ContinuationStore(missing_store_path)
    report = om.plan_discord_migration(board="warroom-os", from_id="research", to_id=TO, board_db=db, store=store)
    assert report["success"] and report["writes"] == 0
    assert report["affected_deadletters"] == []
    assert not missing_store_path.exists()  # never created/migrated by a dry run


def test_apply_missing_canonical_db_refused_before_any_write(tmp_path):
    """Regression: apply must fail closed before store.init()/build_plan when
    the canonical AgentFlow DB path (e.g. a typo/wrong --db) does not already
    exist. It must not create/back up an empty DB or mutate the board DB."""
    db = build_board_db(tmp_path)
    missing_store_path = tmp_path / "typo-control-plane.sqlite"
    assert not missing_store_path.exists()
    store = ContinuationStore(missing_store_path)
    before = sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
        "select count(*) from kanban_task_origin where chat_id=?", (FROM,)
    ).fetchone()[0]

    report = om.apply_discord_migration(board="warroom-os", from_id="research", to_id=TO, board_db=db, store=store, now=lambda: 1.0)

    assert not report["success"]
    assert report["error"] == "canonical_db_missing"
    assert report["writes"] == 0
    assert not missing_store_path.exists()  # never created/migrated
    assert not any(tmp_path.glob("*.bak"))  # no backups taken
    after = sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
        "select count(*) from kanban_task_origin where chat_id=?", (FROM,)
    ).fetchone()[0]
    assert before == after


# --------------------------------------------------------------------------- #
# active writers refusal + coordinated contract
# --------------------------------------------------------------------------- #

def test_active_writers_refused_without_contract(tmp_path):
    db = build_board_db(tmp_path)
    store = build_store(tmp_path)
    blocker = sqlite3.connect(db, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # hold a competing write lock
    blocker.execute("update kanban_task_origin set updated_at=2 where task_id='t_numeric'")
    try:
        report = om.apply_discord_migration(board="warroom-os", from_id="research", to_id=TO, board_db=db, store=store)
        assert not report["success"] and report["error"] == "active_writers_present"
        # origin migration NOT applied
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        assert con.execute("select count(*) from kanban_task_origin where chat_id=?", (FROM,)).fetchone()[0] == 2
        con.close()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


# --------------------------------------------------------------------------- #
# deadletter callback repair: success / failure / malformed / read-back proof
# --------------------------------------------------------------------------- #

def _apply_with_repair(tmp_path, *, runner_kwargs=None, endpoint="discord:research", **overrides):
    db = build_board_db(tmp_path)
    store = build_store(tmp_path)
    oid = enqueue_deadletter(store, board="warroom-os", task_id="t_plain", endpoint=endpoint, idem="dl:1")
    calls: list = []
    runner = ack_writer_runner(db, calls=calls, **(runner_kwargs or {}))
    adapter = RealBoardAdapter(runner=runner, board="warroom-os", board_db_path=db)
    report = om.apply_discord_migration(
        board="warroom-os", from_id="research", to_id=TO, board_db=db, store=store,
        adapter=adapter, now=lambda: 1.0, **overrides,
    )
    return db, store, oid, report, calls


def test_deadletter_repair_success_and_readback(tmp_path):
    db, store, oid, report, calls = _apply_with_repair(tmp_path)
    assert report["success"], report
    repair = report["callback_repair"]
    assert [r["outbox_id"] for r in repair["repaired"]] == [oid]
    assert repair["partial"] == []
    # ACK recorded against MIGRATED numeric channel, not symbolic
    ack_argv = [a for a in calls if "consumer-ack-origin" in a][0]
    assert TO in ack_argv and FROM not in ack_argv
    # outbox transitioned callback_deadletter -> applied, error cleared
    with store.connect() as con:
        row = con.execute("select state, last_error from board_outbox where id=?", (oid,)).fetchone()
    assert row["state"] == "applied" and row["last_error"] == ""


def test_deadletter_repair_literal_hash_endpoint_targets_numeric_channel(tmp_path):
    """Regression: a stored literal ``discord:#research`` deadletter must ACK
    the numeric Discord destination, never the symbolic fallback route."""
    db, store, oid, report, calls = _apply_with_repair(tmp_path, endpoint="discord:#research")
    assert report["success"], report
    assert [r["outbox_id"] for r in report["callback_repair"]["repaired"]] == [oid]
    ack_argv = [a for a in calls if "consumer-ack-origin" in a][0]
    assert ack_argv[ack_argv.index("--chat-id") + 1] == TO
    assert "#research" not in ack_argv
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert con.execute(
            "select consumer_ack_status from kanban_notify_receipts where task_id='t_plain' and platform='discord' and chat_id=?",
            (TO,),
        ).fetchone()[0] == "acked"
        assert con.execute(
            "select count(*) from kanban_notify_receipts where task_id='t_plain' and platform='discord' and chat_id=?",
            (FROM_HASH,),
        ).fetchone()[0] == 0
    finally:
        con.close()


def test_deadletter_repair_failure_is_partial(tmp_path):
    db, store, oid, report, calls = _apply_with_repair(tmp_path, runner_kwargs={"fail": True})
    assert report["success"]  # origin migration succeeded
    repair = report["callback_repair"]
    assert repair["repaired"] == []
    assert [p["outbox_id"] for p in repair["partial"]] == [oid]
    # deadletter left as-is, origin migration NOT rolled back
    with store.connect() as con:
        assert con.execute("select state from board_outbox where id=?", (oid,)).fetchone()["state"] == "callback_deadletter"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    assert con.execute("select count(*) from kanban_task_origin where chat_id=?", (FROM,)).fetchone()[0] == 0
    con.close()


def test_deadletter_repair_malformed_json_is_partial(tmp_path):
    db, store, oid, report, calls = _apply_with_repair(tmp_path, runner_kwargs={"malformed": True})
    assert report["success"]
    assert [p["outbox_id"] for p in report["callback_repair"]["partial"]] == [oid]
    with store.connect() as con:
        assert con.execute("select state from board_outbox where id=?", (oid,)).fetchone()["state"] == "callback_deadletter"


# --------------------------------------------------------------------------- #
# wrong-board / cross-task refusal
# --------------------------------------------------------------------------- #

def test_wrong_board_deadletter_refused(tmp_path):
    db = build_board_db(tmp_path)
    store = build_store(tmp_path)
    oid = enqueue_deadletter(store, board="other-board", task_id="t_plain", endpoint="discord:research", idem="dl:x")
    report = om.plan_discord_migration(
        board="warroom-os", from_id="research", to_id=TO, board_db=db, store=store, deadletter_ids=[oid],
    )
    assert not report["success"] and report["error"] == "deadletter_board_task_mismatch"


def test_cross_channel_deadletter_refused(tmp_path):
    db = build_board_db(tmp_path)
    store = build_store(tmp_path)
    oid = enqueue_deadletter(store, board="warroom-os", task_id="t_plain", endpoint="discord:9999999999", idem="dl:y")
    report = om.plan_discord_migration(
        board="warroom-os", from_id="research", to_id=TO, board_db=db, store=store, deadletter_ids=[oid],
    )
    assert not report["success"] and report["error"] == "deadletter_board_task_mismatch"


# --------------------------------------------------------------------------- #
# idempotency: rerun / restart / reconcile => zero further changes
# --------------------------------------------------------------------------- #

def test_idempotent_rerun_zero_changes(tmp_path):
    db, store, oid, report1, _calls = _apply_with_repair(tmp_path)
    assert report1["success"]

    def snapshot():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        s = con.execute("select * from kanban_notify_receipts order by task_id, chat_id").fetchall()
        o = con.execute("select * from kanban_task_origin order by task_id").fetchall()
        con.close()
        with store.connect() as c:
            ob = c.execute("select id, state from board_outbox order by id").fetchall()
        return (s, o, [tuple(r) for r in ob])

    before = snapshot()
    calls2: list = []
    adapter2 = RealBoardAdapter(runner=ack_writer_runner(db, calls=calls2), board="warroom-os", board_db_path=db)
    report2 = om.apply_discord_migration(
        board="warroom-os", from_id="research", to_id=TO, board_db=db, store=store, adapter=adapter2, now=lambda: 2.0,
    )
    after = snapshot()
    assert report2["success"]
    assert report2["board_migration"] == {"origins_migrated": 0, "receipts_rekeyed": 0, "receipts_merged": 0, "alias_subs_deleted": 0}
    assert report2["callback_repair"]["attempted"] == 0  # already applied, no longer a deadletter
    assert report2["created_tasks"] == 0
    assert before == after  # zero further changes
    assert calls2 == []  # no ACK CLI re-invoked


def test_cursor_unchanged(tmp_path):
    db = build_board_db(tmp_path)
    store = build_store(tmp_path)
    store.advance_cursor("warroom-os", "warroom-os", 8523)
    om.apply_discord_migration(board="warroom-os", from_id="research", to_id=TO, board_db=db, store=store, now=lambda: 1.0)
    assert store.get_cursor("warroom-os", "warroom-os") == 8523


# --------------------------------------------------------------------------- #
# prevention at the generated-task boundary
# --------------------------------------------------------------------------- #

def test_reject_symbolic_discord_endpoint():
    assert om.reject_symbolic_discord_endpoint("discord:#research") == "symbolic_discord_chat_id_rejected"
    assert om.reject_symbolic_discord_endpoint("discord:research") == "symbolic_discord_chat_id_rejected"
    assert om.reject_symbolic_discord_endpoint(f"discord:{TO}") is None
    assert om.reject_symbolic_discord_endpoint("slack:#general") is None  # generic platform unchanged
    assert om.reject_symbolic_discord_endpoint("telegram:#chan") is None


def test_prevention_rejects_symbolic_board_default_endpoint(tmp_path):
    """A board config whose generated Discord default_endpoint is symbolic is
    rejected at load time, before any task/outbox is materialized from it."""
    reg = tmp_path / "boards.yaml"
    reg.write_text("boards:\n  warroom-os:\n    default_endpoint: discord:#research\n", encoding="utf-8")
    with pytest.raises(ValueError) as ei:
        load_board_registry(reg)
    assert "symbolic_discord_chat_id_rejected" in str(ei.value)


def test_prevention_accepts_numeric_and_generic_default_endpoint(tmp_path):
    reg = tmp_path / "boards.yaml"
    reg.write_text(
        "boards:\n"
        f"  warroom-os:\n    default_endpoint: discord:{TO}\n"
        "  slackboard:\n    default_endpoint: slack:#general\n",
        encoding="utf-8",
    )
    registry = load_board_registry(reg)  # must not raise
    assert registry["warroom-os"].default_endpoint == f"discord:{TO}"
    assert registry["slackboard"].default_endpoint == "slack:#general"  # generic unchanged


# --------------------------------------------------------------------------- #
# CLI help / JSON redaction
# --------------------------------------------------------------------------- #

def test_cli_dry_run_json_no_secrets(tmp_path, capsys):
    db = build_board_db(tmp_path)
    store = build_store(tmp_path)
    rc = cli_main([
        "origin", "migrate-discord", "--board", "warroom-os",
        "--from", "#research", "--to", TO,
        "--board-db", str(db), "--db", str(store.path),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["success"] and payload["writes"] == 0
    assert "sk-secret" not in out and "SECRET summary" not in out and "API_KEY" not in out


def test_cli_list_json(tmp_path, capsys):
    db = build_board_db(tmp_path)
    rc = cli_main(["origin", "list", "--board", "warroom-os", "--board-db", str(db)])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["count"] == 5
    assert sorted(r["task_id"] for r in payload["symbolic_origins"]) == ["t_collide", "t_hash_collide", "t_hash_plain", "t_old", "t_plain"]


def test_cli_help_runs(capsys):
    with pytest.raises(SystemExit) as ei:
        cli_main(["origin", "migrate-discord", "--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "--apply" in out and "--from" in out and "--to" in out
