"""Schema-level sanity checks for the hermes-agentflow plugin tool
registrations. Behavioral coverage for individual tools lives in
test_plugin_adapter.py (existing tools) and test_input_reply_bridge.py
(the M27 interaction-inbox natural-language tools)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "hermes-agentflow"
PLUGIN_FILE = PLUGIN_DIR / "__init__.py"


def _load_plugin(module_name: str = "hermes_agentflow_schema_test"):
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


_NEW_INTERACTION_TOOLS = ("agentflow_input_inbox", "agentflow_submit_input_text", "agentflow_input_status")


def test_all_tools_are_namespaced_agentflow_with_a_schema_and_handler():
    ctx = FakeHermesContext()
    plugin.register(ctx)
    assert len(ctx.tools) >= len(_NEW_INTERACTION_TOOLS)
    for tool in ctx.tools:
        assert tool["namespace"] == "agentflow"
        assert callable(tool["handler"])
        assert tool["schema"]["type"] == "function"
        assert tool["schema"]["function"]["name"] == tool["name"]


def test_interaction_tools_are_registered_alongside_existing_tools():
    ctx = FakeHermesContext()
    plugin.register(ctx)
    names = {t["name"] for t in ctx.tools}
    for name in _NEW_INTERACTION_TOOLS:
        assert name in names


def test_submit_input_text_schema_requires_case_id_text_and_endpoint():
    ctx = FakeHermesContext()
    plugin.register(ctx)
    tool = next(t for t in ctx.tools if t["name"] == "agentflow_submit_input_text")
    required = tool["schema"]["function"]["parameters"]["required"]
    assert set(required) == {"case_id", "text", "endpoint"}


def test_input_status_schema_requires_case_id_and_endpoint():
    ctx = FakeHermesContext()
    plugin.register(ctx)
    tool = next(t for t in ctx.tools if t["name"] == "agentflow_input_status")
    required = tool["schema"]["function"]["parameters"]["required"]
    assert set(required) == {"case_id", "endpoint"}


def test_input_inbox_schema_has_no_required_fields():
    """endpoint/case_id are both optional — the plan requires endpoint to be
    inferred from the active gateway session, not demanded from the user."""
    ctx = FakeHermesContext()
    plugin.register(ctx)
    tool = next(t for t in ctx.tools if t["name"] == "agentflow_input_inbox")
    assert "required" not in tool["schema"]["function"]["parameters"]
