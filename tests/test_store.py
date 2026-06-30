import json

from agentflow_hermes.ack import parse_ack_block
from agentflow_hermes.states import JobStatus
from agentflow_hermes.store import AgentFlowStore, render_dispatch_prompt


def _events(store, job_id, kind=None):
    with store.connect() as con:
        if kind:
            rows = con.execute(
                "select * from job_events where job_id=? and kind=? order by seq",
                (job_id, kind),
            ).fetchall()
        else:
            rows = con.execute("select * from job_events where job_id=? order by seq", (job_id,)).fetchall()
    return [dict(r) for r in rows]


def test_store_enqueue_and_dispatch_prompt(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="Do thing", body="Body", target="discord:#work", origin_return="discord:#home")
    assert created["success"] is True
    job = store.get_job(created["job_id"])
    assert job is not None
    assert job["correlation_id"] == job["id"]
    assert job["causation_id"] == ""
    assert job["source_kind"] == "manual"
    assert job["source_id"] == ""
    prompt = render_dispatch_prompt(job)
    assert "[JOB ACK]" in prompt
    assert created["job_id"] in prompt
    assert "source_kind:" in prompt
    assert "source_id:" in prompt
    assert "correlation_id:" in prompt
    assert "causation_id:" in prompt


def test_ack_ingest_updates_status(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="Do thing")
    fields = parse_ack_block(f"""[JOB ACK]
job_id: {created['job_id']}
status: succeeded
summary: done
""")
    result = store.ack(job_id=fields["job_id"], status=fields["status"], summary=fields["summary"], payload=fields)
    assert result["success"] is True
    assert result["applied"] is True
    job = store.get_job(created["job_id"])
    assert job is not None
    assert job["status"] == "succeeded"
    assert job["final_at"] is not None


def test_status_json_shape(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    store.enqueue(title="A")
    payload = {"success": True, "jobs": store.list_jobs()}
    encoded = json.dumps(payload)
    assert "A" in encoded


def test_event_seq_is_monotonic(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="T")
    job_id = created["job_id"]
    store.ack(job_id=job_id, status=JobStatus.SUCCEEDED, summary="done")
    store.ack(job_id=job_id, status=JobStatus.SUCCEEDED, summary="again")
    with store.connect() as con:
        rows = con.execute("select seq, kind, prev_status, new_status from job_events where job_id=? order by seq", (job_id,)).fetchall()
    seqs = [r["seq"] for r in rows]
    assert seqs == [1, 2, 3]
    assert rows[1]["kind"] == "ack_applied"
    assert rows[1]["prev_status"] == "queued"
    assert rows[1]["new_status"] == "succeeded"
    assert rows[2]["kind"] == "duplicate_ack"


def test_duplicate_ack_is_idempotent(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="T")
    job_id = created["job_id"]
    r1 = store.ack(job_id=job_id, status=JobStatus.SUCCEEDED, summary="done")
    r2 = store.ack(job_id=job_id, status=JobStatus.SUCCEEDED, summary="done")
    assert r1["applied"] is True
    assert r2["applied"] is False
    assert r2["duplicate"] is True


def test_final_state_guard(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="T")
    job_id = created["job_id"]
    store.ack(job_id=job_id, status=JobStatus.SUCCEEDED, summary="done")
    result = store.ack(job_id=job_id, status=JobStatus.FAILED, summary="too late")
    assert result["success"] is False
    assert result["error"] == "already_final"
    with store.connect() as con:
        dl = con.execute("select * from deadletter where job_id=?", (job_id,)).fetchall()
    assert len(dl) == 1
    assert dl[0]["reason"] == "already_final"


def test_illegal_transition(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="T")
    job_id = created["job_id"]
    result = store.ack(job_id=job_id, status=JobStatus.WAITING_USER, summary="skip")
    assert result["success"] is False
    assert result["error"] == "illegal_transition"


def test_unknown_job_deadletters(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    result = store.ack(job_id="job_noexist", status=JobStatus.SUCCEEDED, summary="nope")
    assert result["success"] is False
    assert result["error"] == "unknown_job"
    with store.connect() as con:
        dl = con.execute("select * from deadletter where job_id=?", ("job_noexist",)).fetchall()
    assert len(dl) == 1


def test_render_dispatch_prompt_compatible(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="T", body="B", target="t", origin_return="o")
    job = store.get_job(created["job_id"])
    prompt = render_dispatch_prompt(job)
    assert "[JOB ACK FORMAT]" in prompt
    assert "status: succeeded|failed|waiting_review|waiting_user" in prompt


def test_enqueue_does_not_persist_token_or_private_path(tmp_path):
    """Regression for t_a637e8a5: raw secrets/private paths must not reach jobs/events rows."""
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(
        title="Deploy with TOKEN",
        body="Read payload from /home/operator/private/agentflow/task.json\nTOKEN",
        target="discord:#ops",
        origin_return="discord:#home",
        source_ref="/home/operator/private/agentflow/task.json",
        source_hash="abc123",
        dedupe_key="cron:job-1:run-1:discord:#ops",
    )
    job = store.get_job(created["job_id"])
    assert job is not None
    durable = json.dumps(job, ensure_ascii=False)
    assert "TOKEN" not in durable
    assert "/home/operator" not in durable
    assert job["source_ref"].startswith("ref:sha256:")

    events = _events(store, created["job_id"], "enqueued")
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    event_durable = json.dumps(payload, ensure_ascii=False)
    assert "TOKEN" not in event_durable
    assert "/home/operator" not in event_durable
    assert payload["source_ref"].startswith("ref:sha256:")
    assert payload["source_ref_redacted"] is True
    assert payload["title"] == "title:redacted"
    assert payload["body"] == "body:redacted"


def test_enqueue_preserves_safe_short_fields(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(
        title="Deploy app",
        body="ready",
        target="discord:#ops",
        origin_return="discord:#home",
        source_ref="build/123",
        source_hash="abc123",
        source_kind="cron",
        source_id="cron-job",
    )
    job = store.get_job(created["job_id"])
    assert job["title"] == "Deploy app"
    assert job["body"] == "ready"
    assert job["target"] == "discord:#ops"
    assert job["origin_return"] == "discord:#home"
    assert job["source_ref"] == "build/123"
    assert job["source_hash"] == "abc123"
    assert job["source_kind"] == "cron"
    assert job["source_id"] == "cron-job"


def test_record_event_sanitizes_nested_sensitive_payloads(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="Deploy app")
    store.record_event(
        created["job_id"],
        "reviewer_repro",
        payload={
            "summary": "TOKEN",
            "source_ref": "/home/operator/private/agentflow/task.json",
            "nested": {"raw_ref": "/Users/operator/private/transcript.txt"},
        },
    )
    events = _events(store, created["job_id"], "reviewer_repro")
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    durable = json.dumps(payload, ensure_ascii=False)
    assert "TOKEN" not in durable
    assert "/home/operator" not in durable
    assert "/Users/operator" not in durable
    assert payload["summary"] == "summary:redacted"
    assert payload["source_ref"].startswith("ref:sha256:")
    assert payload["nested"]["raw_ref"].startswith("ref:sha256:")
