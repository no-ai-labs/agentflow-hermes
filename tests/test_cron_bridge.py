import json

from agentflow_hermes.cron_bridge import classify_markers, ingest_cron_output, make_dedupe_key, scan_cron_output
from agentflow_hermes.store import AgentFlowStore


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


def test_classify_material_marker():
    markers = classify_markers("[AF-CRON] kind=material ref=build/123 hash=abc123 summary=Deploy app")
    assert len(markers) == 1
    assert markers[0].kind == "material"
    assert markers[0].ref == "build/123"
    assert markers[0].hash == "abc123"


def test_classify_noise_marker():
    markers = classify_markers("[AF-CRON] kind=noise ref=build/123 hash=abc123 summary=nothing")
    assert len(markers) == 1
    assert markers[0].kind == "noise"


def test_classify_active_wake_marker_as_material_metadata():
    marker = 'HERMES_ACTIVE_WAKE {"event_key":"pr-123","status":"ci_failed","summary":"CI failed","secret":"do-not-store"}'
    markers = classify_markers(marker, default_ref="ref://cron/out", default_hash="hash123")
    assert len(markers) == 1
    assert markers[0].kind == "material"
    assert markers[0].metadata["marker"] == "active_wake"
    assert markers[0].metadata["event_key"] == "pr-123"
    assert markers[0].metadata["status"] == "ci_failed"
    assert "secret" not in markers[0].metadata
    assert markers[0].metadata["live_wake_disabled"] == "true"


def test_make_dedupe_key_is_stable_and_human_readable():
    assert make_dedupe_key("cron", "job-1", "hash", run_id="run-1", target="discord:#ops") == "cron:job-1:run-1:discord:#ops"
    assert make_dedupe_key("cron", "job-1", "hash") == make_dedupe_key("cron", "job-1", "hash")


def test_ingest_material_enqueues_job(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    result = ingest_cron_output(
        store,
        source_ref="build/123",
        source_hash="abc123",
        marker_text="[AF-CRON] kind=material ref=build/123 hash=abc123 summary=Deploy app",
        target="discord:#work",
        origin_return="discord:#home",
        job_id="cron-job",
        run_id="run-1",
    )
    assert result["success"] is True
    assert result["applied"] is True
    assert result["dedupe_key"] == "cron:cron-job:run-1:discord:#work"
    job = store.get_job(result["job_id"])
    assert job["source_kind"] == "cron"
    assert job["source_id"] == "cron-job"
    assert job["source_ref"] == "build/123"
    assert job["source_hash"] == "abc123"
    assert job["target"] == "discord:#work"
    assert job["dedupe_key"] == "cron:cron-job:run-1:discord:#work"


def test_ingest_active_wake_enqueues_without_raw_output_or_secret(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    marker = 'noise before\nHERMES_ACTIVE_WAKE {"event_key":"evt-1","status":"ci_failed","summary":"CI failed","secret":"TOKEN"}\nprivate transcript after'
    result = ingest_cron_output(
        store,
        source_ref="ref://cron/run-1",
        source_hash="hash-active",
        marker_text=marker,
        target="discord:#alerts",
        origin_return="discord:#home",
        job_id="cron-job",
        run_id="run-1",
    )
    assert result["applied"] is True
    job = store.get_job(result["job_id"])
    durable = json.dumps(job, ensure_ascii=False)
    assert "TOKEN" not in durable
    assert "private transcript" not in durable
    assert "CI failed" in durable
    events = _events(store, result["job_id"], "cron_ingested")
    event_payload = json.loads(events[0]["payload_json"])
    assert event_payload["live_wake_disabled"] is True
    assert event_payload["raw_output_stored"] is False
    assert event_payload["material_event"]["event_key"] == "evt-1"
    assert "secret" not in event_payload["material_event"]


def test_ingest_same_dedupe_key_is_duplicate(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    marker = "[AF-CRON] kind=material ref=build/123 hash=abc123 summary=Deploy app"
    r1 = ingest_cron_output(store, source_ref="build/123", source_hash="abc123", marker_text=marker, job_id="job", run_id="run", target="target")
    r2 = ingest_cron_output(store, source_ref="build/123", source_hash="abc123", marker_text=marker, job_id="job", run_id="run", target="target")
    assert r1["applied"] is True
    assert r2["applied"] is False
    assert r2["duplicate"] is True
    jobs = store.list_jobs()
    assert len(jobs) == 1


def test_ingest_noise_does_not_enqueue(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    result = ingest_cron_output(
        store,
        source_ref="build/123",
        source_hash="abc123",
        marker_text="[AF-CRON] kind=noise ref=build/123 hash=abc123 summary=nothing",
    )
    assert result["reason"] == "no_material_marker"
    assert result["job_id"] is None
    assert store.list_jobs() == []


def test_empty_no_change_does_not_enqueue(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    result = ingest_cron_output(store, source_ref="ref://empty", source_hash="emptyhash", marker_text="")
    assert result["applied"] is False
    assert result["reason"] == "no_material_marker"
    assert store.list_jobs() == []


def test_dispatch_prompt_includes_source_provenance_and_ack_contract(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    marker = 'HERMES_ACTIVE_WAKE {"event_key":"evt-1","status":"changed","summary":"new event"}'
    result = ingest_cron_output(store, source_ref="ref://cron/run-1", source_hash="abc123", marker_text=marker)
    job = store.get_job(result["job_id"])
    from agentflow_hermes.store import render_dispatch_prompt

    prompt = render_dispatch_prompt(job)
    assert "source_kind: cron" in prompt
    assert "source_ref: ref://cron/run-1" in prompt
    assert "source_hash: abc123" in prompt
    assert "[JOB ACK FORMAT]" in prompt
    assert "live_wake_dispatch: disabled" in prompt


def test_scan_cron_output_file_fixture(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    fixture = tmp_path / "cron.out"
    fixture.write_text('private header\nHERMES_ACTIVE_WAKE {"event_key":"evt-2","status":"ready","summary":"Ready"}\nprivate trailer', encoding="utf-8")
    result = scan_cron_output(
        store,
        output_file=fixture,
        source_ref="ref://fixture",
        job_id="cron-job",
        run_id="run-2",
        target="discord:#ops",
        origin_return="discord:#home",
    )
    assert result["applied"] is True
    job = store.get_job(result["job_id"])
    assert job["source_ref"] == "ref://fixture"
    assert job["dedupe_key"] == "cron:cron-job:run-2:discord:#ops"
    durable = json.dumps(job, ensure_ascii=False)
    assert "private header" not in durable
    assert "private trailer" not in durable
