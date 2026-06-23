import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

# The plugin adapter lives under a hyphenated plugin directory, so load it
# directly from its __init__.py instead of relying on a package import name.
PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "hermes-agentflow"
PLUGIN_FILE = PLUGIN_DIR / "__init__.py"


def _load_plugin(module_name: str = "hermes_agentflow_test"):
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


class FakeHermesContext:
    def __init__(self):
        self.tools = []

    def register_tool(self, name, namespace, schema, handler, emoji=None):
        self.tools.append({"name": name, "namespace": namespace, "schema": schema, "handler": handler, "emoji": emoji})


def _find_tool(ctx, name):
    for tool in ctx.tools:
        if tool["name"] == name:
            return tool
    raise AssertionError(f"tool {name!r} not registered")


@pytest.fixture(autouse=True)
def _reset_plugin_engine_state(monkeypatch):
    """Each test starts with a clean plugin engine-load state."""
    monkeypatch.setattr(plugin, "_engine_error", None)
    monkeypatch.setattr(plugin, "_run_cli", None)


@pytest.fixture
def hermes_ctx():
    return FakeHermesContext()


def test_register_loads_tools_without_crashing(hermes_ctx):
    plugin.register(hermes_ctx)
    names = {t["name"] for t in hermes_ctx.tools}
    assert names == {
        "agentflow_enqueue",
        "agentflow_status",
        "agentflow_dispatch_dry_run",
        "agentflow_ack_ingest",
        "agentflow_doctor",
        "agentflow_bridge_cron",
    }


def test_plugin_has_no_path_parents_repo_layout_assumption():
    """Guard: the adapter must not climb out of its own directory to find src."""
    source = Path(PLUGIN_DIR / "__init__.py").read_text()
    assert "parents[2]" not in source
    assert "PYTHONPATH" not in source
    assert "subprocess" not in source


def test_doctor_returns_importable_success(hermes_ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    plugin.register(hermes_ctx)
    doctor = _find_tool(hermes_ctx, "agentflow_doctor")
    payload = json.loads(doctor["handler"]({}))
    assert payload["success"] is True
    assert payload.get("engine_importable") is True
    assert payload.get("mode") == "dry-run-first"
    assert "schema_version" in payload


def test_enqueue_then_status_roundtrip(hermes_ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    plugin.register(hermes_ctx)
    enqueue = _find_tool(hermes_ctx, "agentflow_enqueue")
    status = _find_tool(hermes_ctx, "agentflow_status")

    result = json.loads(enqueue["handler"]({"title": "test job", "target": "t", "origin_return": "o"}))
    assert result["success"] is True
    job_id = result["job_id"]

    listed = json.loads(status["handler"]({"limit": 10}))
    assert listed["success"] is True
    assert any(j["id"] == job_id for j in listed["jobs"])


def test_dispatch_dry_run_returns_prompt(hermes_ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    plugin.register(hermes_ctx)
    enqueue = _find_tool(hermes_ctx, "agentflow_enqueue")
    dispatch = _find_tool(hermes_ctx, "agentflow_dispatch_dry_run")

    result = json.loads(enqueue["handler"]({"title": "dispatch me"}))
    job_id = result["job_id"]

    dispatched = json.loads(dispatch["handler"]({"job_id": job_id}))
    assert dispatched["success"] is True
    assert "[JOB]" in dispatched["output"]
    assert "[JOB ACK]" in dispatched["output"]


def test_bridge_cron_ingest_roundtrip(hermes_ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    plugin.register(hermes_ctx)
    bridge = _find_tool(hermes_ctx, "agentflow_bridge_cron")
    result = json.loads(bridge["handler"]({
        "ref": "r1",
        "hash": "h1",
        "title": "cron job",
        "marker_text": "[AF-CRON] kind=material ref=r1 hash=h1 summary=cron job",
    }))
    assert result["success"] is True
    assert result["job_id"].startswith("job_")


def test_missing_engine_degrades_to_doctor_guidance(hermes_ctx, monkeypatch):
    """If the engine package cannot be imported, register still succeeds and doctor returns actionable JSON."""
    # Block the engine package by injecting a failing module.
    fake_failing = types.ModuleType("agentflow_hermes")
    fake_failing.__path__ = []

    def always_fail(*args, **kwargs):
        raise ImportError("engine not installed")

    fake_failing.__getattr__ = lambda name: always_fail()
    fake_failing.__loader__ = None
    monkeypatch.setitem(sys.modules, "agentflow_hermes", fake_failing)

    # Load a fresh plugin module from disk so it tries to import the blocked engine.
    sys.modules.pop("agentflow_hermes.cli", None)
    fresh_plugin = _load_plugin("hermes_agentflow_missing_engine_test")

    fresh_plugin.register(hermes_ctx)
    doctor = _find_tool(hermes_ctx, "agentflow_doctor")
    payload = json.loads(doctor["handler"]({}))
    assert payload["success"] is False
    assert payload["engine_importable"] is False
    assert payload["mode"] == "dry-run-first"
    assert "agentflow-hermes" in payload.get("installation", "")

    # Other tools also degrade gracefully.
    enqueue = _find_tool(hermes_ctx, "agentflow_enqueue")
    enq_payload = json.loads(enqueue["handler"]({"title": "t"}))
    assert enq_payload["success"] is False
    assert "engine not importable" in enq_payload["error"]


def test_in_process_run_cli_captures_stdout(hermes_ctx, tmp_path, monkeypatch):
    """Handlers call the engine in-process and capture JSON printed to stdout."""
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    plugin.register(hermes_ctx)
    enqueue = _find_tool(hermes_ctx, "agentflow_enqueue")
    result = json.loads(enqueue["handler"]({"title": "stdout captured"}))
    assert result["success"] is True
    assert result["status"] == "queued"
