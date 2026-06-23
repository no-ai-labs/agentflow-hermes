import json

from agentflow_hermes.cli import parse_ack_block
from agentflow_hermes.store import AgentFlowStore, render_dispatch_prompt


def test_store_enqueue_and_dispatch_prompt(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    created = store.enqueue(title="Do thing", body="Body", target="discord:#work", origin_return="discord:#home")
    assert created["success"] is True
    job = store.get_job(created["job_id"])
    assert job is not None
    prompt = render_dispatch_prompt(job)
    assert "[JOB ACK]" in prompt
    assert created["job_id"] in prompt


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
    job = store.get_job(created["job_id"])
    assert job is not None
    assert job["status"] == "succeeded"


def test_status_json_shape(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    store.enqueue(title="A")
    payload = {"success": True, "jobs": store.list_jobs()}
    encoded = json.dumps(payload)
    assert "A" in encoded
