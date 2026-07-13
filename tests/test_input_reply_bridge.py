from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from agentflow_hermes.continuation_store import ContinuationState, ContinuationStore
from agentflow_hermes.continuations.owner_input import OwnerInputHandler
from agentflow_hermes.input_contract import ArtifactSpec, FieldAuthority, InputContract, InputField
from agentflow_hermes.interaction import InteractionInbox
from agentflow_hermes.outcome import ContinuationKind, OutcomeEnvelope, RequirementRef, Verdict
from agentflow_hermes.requirements import Requirement, RequirementKind

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "hermes-agentflow"
PLUGIN_FILE = PLUGIN_DIR / "__init__.py"


def _load_plugin(module_name: str = "hermes_agentflow_reply_bridge_test"):
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
    monkeypatch.setattr(plugin, "_engine_error", None)
    monkeypatch.setattr(plugin, "_run_cli", None)


@pytest.fixture
def hermes_ctx(tmp_path, monkeypatch):
    # ContinuationStore.canonical() resolves HERMES_CONTINUATION_DB first.
    monkeypatch.setenv("HERMES_CONTINUATION_DB", str(tmp_path / "agentflow.sqlite"))
    ctx = FakeHermesContext()
    plugin.register(ctx)
    return ctx


def _store(tmp_path) -> ContinuationStore:
    return ContinuationStore(tmp_path / "agentflow.sqlite")


def _outcome(**overrides) -> OutcomeEnvelope:
    kwargs = dict(
        schema_version=1,
        event_id="ev_1",
        board="warroom-os",
        source_task_id="t_1",
        source_graph_id="g_1",
        verdict=Verdict.BLOCK,
        continuation_kind=ContinuationKind.NEEDS_INPUT,
        contract_ref="generic.owner-input.v1",
        origin_ref="discord:#research",
        return_to_ref="discord:#research",
        requirements=(RequirementRef(name="result_url", authority="owner"),),
    )
    kwargs.update(overrides)
    return OutcomeEnvelope(**kwargs)


def _contract() -> InputContract:
    return InputContract(
        contract_ref="generic.owner-input.v1",
        version=1,
        owner_role="board-owner",
        fields=(InputField(name="result_url", value_type="text", authority=FieldAuthority.OWNER),),
        artifacts=(ArtifactSpec(artifact_id="evidence", template_path="t.json", final_path="f.json"),),
        resume_transition="generic.owner-input.resume",
    )


def _open_single_field_case(store: ContinuationStore) -> tuple[int, str]:
    handler = OwnerInputHandler()
    inbox = InteractionInbox(store=store)
    plan = handler.plan(
        _outcome(),
        store=store,
        adapter=None,
        contract=_contract(),
        interaction_inbox=inbox,
        unresolved_requirements=(Requirement(name="result_url", kind=RequirementKind.FACT, authority="owner", question="what's the result URL?"),),
        owner_ref="operator-main",
        project_scope="warroom-os",
    )
    case_id = store.list_interaction_cases()[0]["id"]
    return plan.instance_id, case_id


def test_register_includes_interaction_tools(hermes_ctx):
    names = {t["name"] for t in hermes_ctx.tools}
    assert {"agentflow_input_inbox", "agentflow_submit_input_text", "agentflow_input_status"} <= names


def test_plugin_canonical_store_uses_control_plane_default_and_sees_cases(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_CONTINUATION_DB", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    ctx = FakeHermesContext()
    plugin.register(ctx)
    store = ContinuationStore.canonical()
    assert store.path == tmp_path / ".hermes" / "agentflow" / "agentflow-control-plane.sqlite"
    _open_single_field_case(store)

    inbox_tool = _find_tool(ctx, "agentflow_input_inbox")
    result = json.loads(inbox_tool["handler"]({"endpoint": "discord:#research"}))

    assert result["success"] is True
    assert len(result["cases"]) == 1
    assert result["cases"][0]["endpoint"] == "discord:#research"


def test_input_inbox_lists_case_and_transitions_to_asked(tmp_path, hermes_ctx):
    store = _store(tmp_path)
    _open_single_field_case(store)

    inbox_tool = _find_tool(hermes_ctx, "agentflow_input_inbox")
    result = json.loads(inbox_tool["handler"]({"endpoint": "discord:#research"}))

    assert result["success"] is True
    assert len(result["cases"]) == 1
    case = result["cases"][0]
    assert case["state"] == "asked"
    assert case["effort"] == "H1"
    assert "Blocked on" in case["question"]
    assert "contract_ref" not in case["question"]
    assert "task:" not in case["question"]


def test_submit_input_text_resumes_single_field_case(tmp_path, hermes_ctx):
    store = _store(tmp_path)
    instance_id, case_id = _open_single_field_case(store)

    inbox_tool = _find_tool(hermes_ctx, "agentflow_input_inbox")
    json.loads(inbox_tool["handler"]({"case_id": case_id}))  # ask the question first

    submit_tool = _find_tool(hermes_ctx, "agentflow_submit_input_text")
    result = json.loads(
        submit_tool["handler"](
            {"case_id": case_id, "endpoint": "discord:#research", "text": "https://example.com/result", "owner_ref": "operator-main"}
        )
    )

    assert result["success"] is True
    assert result["status"] == "resumed"
    assert instance_id in result["resumed_continuation_ids"]

    instance = store.get_instance(instance_id)
    assert instance["state"] == ContinuationState.MATERIALIZING.value
    receipts = store.list_owner_receipts(instance_id)
    assert receipts[0]["fields"] == {"result_url": "https://example.com/result"}


def test_submit_input_text_never_persists_raw_text(tmp_path, hermes_ctx):
    store = _store(tmp_path)
    instance_id, case_id = _open_single_field_case(store)
    submit_tool = _find_tool(hermes_ctx, "agentflow_submit_input_text")
    secret_text = "https://example.com/result my-secret-token-xyz"

    json.loads(submit_tool["handler"]({"case_id": case_id, "endpoint": "discord:#research", "text": secret_text, "owner_ref": "operator-main"}))

    receipts = store.list_inbound_reply_receipts(case_id)
    assert len(receipts) == 1
    assert secret_text not in json.dumps(receipts[0])
    assert "my-secret-token-xyz" not in json.dumps(receipts[0])


def test_submit_input_text_incomplete_reply_stays_waiting(tmp_path, hermes_ctx):
    store = _store(tmp_path)
    instance_id, case_id = _open_single_field_case(store)
    submit_tool = _find_tool(hermes_ctx, "agentflow_submit_input_text")

    # No URL/allowed-value shape recognizable text still applies as the raw
    # value for a single free-text field — use an empty reply instead to
    # force the "nothing compiled" path.
    result = json.loads(submit_tool["handler"]({"case_id": case_id, "endpoint": "discord:#research", "text": "   ", "owner_ref": "operator-main"}))

    assert result["success"] is False
    assert result["status"] == "waiting for you"
    instance = store.get_instance(instance_id)
    assert instance["state"] == ContinuationState.WAITING_OWNER.value


def test_batched_reply_resumes_multiple_continuations(tmp_path, hermes_ctx):
    store = _store(tmp_path)
    handler = OwnerInputHandler()
    inbox = InteractionInbox(store=store)

    plan_1 = handler.plan(
        _outcome(event_id="ev_1", source_task_id="t_1"),
        store=store,
        adapter=None,
        contract=_contract(),
        interaction_inbox=inbox,
        unresolved_requirements=(Requirement(name="result_url", kind=RequirementKind.FACT, authority="owner", question="URL?"),),
        owner_ref="operator-main",
        project_scope="warroom-os",
    )
    plan_2 = handler.plan(
        _outcome(event_id="ev_2", source_task_id="t_2"),
        store=store,
        adapter=None,
        contract=_contract(),
        interaction_inbox=inbox,
        unresolved_requirements=(Requirement(name="approval_id", kind=RequirementKind.FACT, authority="owner", question="approval id?"),),
        owner_ref="operator-main",
        project_scope="warroom-os",
    )
    case_id = store.list_interaction_cases()[0]["id"]
    assert set(store.get_interaction_case(case_id) and [m["continuation_id"] for m in store.list_interaction_members(case_id)]) == {
        plan_1.instance_id,
        plan_2.instance_id,
    }

    submit_tool = _find_tool(hermes_ctx, "agentflow_submit_input_text")
    result = json.loads(
        submit_tool["handler"](
            {"case_id": case_id, "endpoint": "discord:#research", "text": "1 https://example.com/x, 2 recv_42", "owner_ref": "operator-main"}
        )
    )

    assert result["success"] is True
    assert set(result["resumed_continuation_ids"]) == {plan_1.instance_id, plan_2.instance_id}
    assert store.get_instance(plan_1.instance_id)["state"] == ContinuationState.MATERIALIZING.value
    assert store.get_instance(plan_2.instance_id)["state"] == ContinuationState.MATERIALIZING.value


def test_input_status_reports_waiting_then_resumed(tmp_path, hermes_ctx):
    store = _store(tmp_path)
    instance_id, case_id = _open_single_field_case(store)
    status_tool = _find_tool(hermes_ctx, "agentflow_input_status")

    before = json.loads(status_tool["handler"]({"case_id": case_id, "endpoint": "discord:#research"}))
    assert before["status"] == "waiting for you"

    submit_tool = _find_tool(hermes_ctx, "agentflow_submit_input_text")
    json.loads(
        submit_tool["handler"](
            {"case_id": case_id, "endpoint": "discord:#research", "text": "https://example.com/result", "owner_ref": "operator-main"}
        )
    )

    after = json.loads(status_tool["handler"]({"case_id": case_id, "endpoint": "discord:#research"}))
    assert after["status"] == "resumed"


def test_input_status_unknown_case_is_an_error(hermes_ctx):
    status_tool = _find_tool(hermes_ctx, "agentflow_input_status")
    result = json.loads(status_tool["handler"]({"case_id": "ic_does_not_exist", "endpoint": "discord:#research"}))
    assert result["success"] is False


def test_submit_input_text_wrong_lane_case_id_fails_closed(tmp_path, hermes_ctx):
    store = _store(tmp_path)
    instance_id, case_id = _open_single_field_case(store)

    submit_tool = _find_tool(hermes_ctx, "agentflow_submit_input_text")
    result = json.loads(
        submit_tool["handler"](
            {"case_id": case_id, "endpoint": "discord:#other-lane", "text": "https://example.com/result", "owner_ref": "operator-main"}
        )
    )

    assert result["success"] is False
    assert result["error"] == "origin_mismatch"
    instance = store.get_instance(instance_id)
    assert instance["state"] != ContinuationState.MATERIALIZING.value
    assert store.list_owner_receipts(instance_id) == []
    assert store.list_inbound_reply_receipts(case_id) == []


def test_submit_input_text_missing_endpoint_fails_closed(tmp_path, hermes_ctx):
    store = _store(tmp_path)
    instance_id, case_id = _open_single_field_case(store)

    submit_tool = _find_tool(hermes_ctx, "agentflow_submit_input_text")
    result = json.loads(
        submit_tool["handler"]({"case_id": case_id, "text": "https://example.com/result", "owner_ref": "operator-main"})
    )

    assert result["success"] is False
    assert result["error"] == "endpoint_required"
    instance = store.get_instance(instance_id)
    assert instance["state"] != ContinuationState.MATERIALIZING.value


def test_input_status_wrong_lane_case_id_fails_closed(tmp_path, hermes_ctx):
    store = _store(tmp_path)
    _instance_id, case_id = _open_single_field_case(store)
    status_tool = _find_tool(hermes_ctx, "agentflow_input_status")

    result = json.loads(status_tool["handler"]({"case_id": case_id, "endpoint": "discord:#other-lane"}))
    assert result["success"] is False
    assert result["error"] == "origin_mismatch"


def test_input_inbox_case_id_from_other_lane_fails_closed(tmp_path, hermes_ctx):
    store = _store(tmp_path)
    _open_single_field_case(store)
    case_id = store.list_interaction_cases()[0]["id"]

    inbox_tool = _find_tool(hermes_ctx, "agentflow_input_inbox")
    result = json.loads(inbox_tool["handler"]({"case_id": case_id, "endpoint": "discord:#other-lane"}))

    assert result["success"] is True
    assert result["cases"] == []
