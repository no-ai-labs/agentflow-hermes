import json

from agentflow_hermes.ack import parse_ack_block
from agentflow_hermes.states import JobStatus
from agentflow_hermes.store import AgentFlowStore, render_dispatch_prompt


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
