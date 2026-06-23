from agentflow_hermes.cron_bridge import classify_markers, ingest_cron_output, make_dedupe_key
from agentflow_hermes.store import AgentFlowStore


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


def test_make_dedupe_key_is_stable():
    assert make_dedupe_key("cron", "corr", "hash") == make_dedupe_key("cron", "corr", "hash")


def test_ingest_material_enqueues_job(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    result = ingest_cron_output(
        store,
        source_ref="build/123",
        source_hash="abc123",
        marker_text="[AF-CRON] kind=material ref=build/123 hash=abc123 summary=Deploy app",
        target="discord:#work",
        origin_return="discord:#home",
    )
    assert result["success"] is True
    assert result["applied"] is True
    job = store.get_job(result["job_id"])
    assert job["source_kind"] == "cron"
    assert job["source_ref"] == "build/123"
    assert job["source_hash"] == "abc123"
    assert job["target"] == "discord:#work"


def test_ingest_same_hash_is_duplicate(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    marker = "[AF-CRON] kind=material ref=build/123 hash=abc123 summary=Deploy app"
    r1 = ingest_cron_output(store, source_ref="build/123", source_hash="abc123", marker_text=marker)
    r2 = ingest_cron_output(store, source_ref="build/123", source_hash="abc123", marker_text=marker)
    assert r1["applied"] is True
    assert r2["applied"] is False
    assert r2["duplicate"] is True


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


def test_dispatch_prompt_includes_source_provenance(tmp_path):
    store = AgentFlowStore(tmp_path / "agentflow.db")
    marker = "[AF-CRON] kind=material ref=build/123 hash=abc123 summary=Deploy app"
    result = ingest_cron_output(store, source_ref="build/123", source_hash="abc123", marker_text=marker)
    job = store.get_job(result["job_id"])
    from agentflow_hermes.store import render_dispatch_prompt

    prompt = render_dispatch_prompt(job)
    assert "source_kind: cron" in prompt
    assert "source_ref: build/123" in prompt
    assert "source_hash: abc123" in prompt
