from agentflow_hermes.migrations import SCHEMA_VERSION
from agentflow_hermes.store import AgentFlowStore




V1_SCHEMA = """
create table if not exists jobs (
    id text primary key,
    title text not null,
    body text not null default '',
    target text not null default '',
    origin_return text not null default '',
    dedupe_key text not null default '',
    status text not null default 'queued',
    created_at real not null,
    updated_at real not null
);
create table if not exists job_events (
    id integer primary key autoincrement,
    job_id text not null,
    kind text not null,
    payload_json text not null default '{}',
    created_at real not null,
    foreign key(job_id) references jobs(id)
);
create index if not exists idx_jobs_status_updated on jobs(status, updated_at);
create index if not exists idx_events_job_id on job_events(job_id, id);
"""


def test_fresh_db_initializes_at_schema_version(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    store.init()
    with store.connect() as con:
        version = con.execute("pragma user_version").fetchone()[0]
    assert version == SCHEMA_VERSION == 3


def test_v1_db_upgrades_without_data_loss(tmp_path):
    db_path = tmp_path / "agentflow.db"
    import sqlite3

    con = sqlite3.connect(db_path)
    con.executescript(V1_SCHEMA)
    con.execute("insert into jobs(id,title,status,created_at,updated_at) values('j1','Old','queued',1,1)")
    con.execute("insert into job_events(job_id,kind,payload_json,created_at) values('j1','enqueued','{}',1)")
    con.commit()
    con.close()

    store = AgentFlowStore(db_path)
    store.init()
    with store.connect() as con:
        version = con.execute("pragma user_version").fetchone()[0]
        cols = {r[1] for r in con.execute("pragma table_info(jobs)").fetchall()}
        dead_cols = {r[1] for r in con.execute("pragma table_info(deadletter)").fetchall()}
        receipt_cols = {r[1] for r in con.execute("pragma table_info(operator_receipts)").fetchall()}
        job = con.execute("select * from jobs where id='j1'").fetchone()

    assert version == SCHEMA_VERSION == 3
    assert "correlation_id" in cols
    assert "causation_id" in cols
    assert "source_id" in cols
    assert "source_hash" in cols
    assert "attempt" in cols
    assert "final_at" in cols
    assert "live_delivered_at" in cols
    assert "live_delivery_ref" in cols
    assert "seq" in {r[1] for r in con.execute("pragma table_info(job_events)").fetchall()}
    assert dead_cols
    assert receipt_cols
    assert job["title"] == "Old"
    assert job["status"] == "queued"


def test_indexes_exist(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    store.init()
    with store.connect() as con:
        indexes = {r["name"] for r in con.execute("select name from sqlite_master where type='index'").fetchall()}
    assert "idx_jobs_correlation" in indexes
    assert "idx_jobs_source" in indexes
    assert "idx_deadletter_job" in indexes
    assert "uniq_events_job_seq" in indexes
    assert "uniq_jobs_source_hash" in indexes
    assert "idx_receipts_job" in indexes
    assert "idx_receipts_channel" in indexes
