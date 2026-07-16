"""M30A: semantic BLOCK (flat reviewer metadata) -> typed CODE_FIX -> exactly
one runnable-fix/independent-review graph -> targeted notify+wake return trip.

Reproduces the t_89e3c71f incident shape: a ``done`` terminal event whose run
metadata is flat ``{verdict:'BLOCK', blockers:[...]}`` with no
``agentflow_outcome`` envelope and whose summary only says ``Verdict: BLOCK``.
"""

from __future__ import annotations

from typing import Any

from agentflow_hermes.board_adapter import FakeBoardAdapter
from agentflow_hermes.board_events import BoardEvent, FakeBoardEventSource
from agentflow_hermes.continuation_config import load_contract_registry
from agentflow_hermes.continuation_engine import backfill_code_fix_event, ingest_board_once
from agentflow_hermes.continuation_store import ContinuationStore
from pathlib import Path

_G421_YAML = Path(__file__).resolve().parents[1] / "contracts" / "warroom.g421.exposure-resolution.v1.yaml"


def _contracts():
    return load_contract_registry([_G421_YAML])


def _t89_event(event_id="7305", event_seq=7305, source_task_id="t_89e3c71f") -> BoardEvent:
    """done + flat BLOCK metadata, exactly like the incident."""
    return BoardEvent(
        event_id=event_id,
        event_seq=event_seq,
        source_task_id=source_task_id,
        source_graph_id="g_89",
        event_kind="completed",
        summary="Verdict: BLOCK",
        run_metadata={"verdict": "BLOCK", "blockers": ["packet rerun url never posted", "stale review edge"]},
        origin_ref="discord:#research",
        return_to_ref="discord:#research",
    )


def _ingest(store, adapter, events, *, apply=True):
    source = FakeBoardEventSource(db_identity="warroom-os-db", events=events)
    store.advance_cursor("warroom-os", "warroom-os-db", 0)
    return ingest_board_once(
        board="warroom-os", source=source, store=store, contract_registry=_contracts(),
        adapter=adapter, apply=apply,
    )


def test_flat_block_becomes_code_fix_graph_with_notify_wake(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()

    result = _ingest(store, adapter, [_t89_event()])

    item = result["results"][0]
    assert item["action"] == "code_fix_applied"
    assert item["router_success"] is True

    # Exactly one fix + one review task, no more.
    assert len(adapter.tasks) == 2
    kinds = sorted(t["kind"] for t in adapter.tasks.values())
    assert kinds == ["code_fix", "review"]

    # The return trip is a Kanban notify+wake subscription of the review task
    # to the source's trusted origin — never a direct AgentFlow/Discord send.
    fix = next(t for t in adapter.tasks.values() if t["kind"] == "code_fix")
    review = next(t for t in adapter.tasks.values() if t["kind"] == "review")
    assert adapter.subscriptions == [(review["task_id"], "discord:#research")]
    # Review is independent and linked to the fix, not the fix itself.
    assert review["parent_task_id"] == fix["task_id"]

    # Durable instance/steps reflect the materialized graph.
    instances = store.list_instances()
    assert len(instances) == 1
    assert instances[0]["continuation_kind"] == "code_fix"
    assert instances[0]["verdict"] == "BLOCK"
    assert instances[0]["state"] == "waiting_review"
    assert item["code_fix"]["subscribed"] is True

    # Cursor advanced past the incident event.
    assert store.get_cursor("warroom-os", "warroom-os-db") == 7305


def test_code_fix_replay_creates_zero_duplicate_tasks_or_wakes(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()

    first = _ingest(store, adapter, [_t89_event()])
    assert first["results"][0]["router_success"] is True
    assert len(adapter.tasks) == 2
    assert len(adapter.subscriptions) == 1

    # Duplicate replay/restart against the same board: zero new tasks, zero new
    # subscriptions, still exactly one instance.
    second = _ingest(store, adapter, [_t89_event()])
    assert second["processed"] in (0, 1)  # cursor already at 7305 -> zero new events
    assert len(adapter.tasks) == 2
    assert len(adapter.subscriptions) == 1
    assert len(store.list_instances()) == 1


class _FailingBoardAdapter(FakeBoardAdapter):
    """A board whose create_task always fails, like a real CLI create that
    errored."""

    def create_task(self, intent: dict[str, Any]) -> dict[str, Any]:
        return {"success": False, "error": "cli_create_failed"}


def test_code_fix_apply_failure_leaves_cursor_retryable_then_retry_dedupes(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")

    failing = _FailingBoardAdapter()
    first = _ingest(store, failing, [_t89_event()])

    item = first["results"][0]
    assert item["action"] == "code_fix_applied"
    assert item["router_success"] is False
    # Fail closed: cursor did not advance past the failed event.
    assert store.get_cursor("warroom-os", "warroom-os-db") == 0
    # No board tasks were created; the instance is parked retryable.
    assert failing.tasks == {}
    instance = store.list_instances()[0]
    assert instance["state"] == "failed_retryable"

    # Retry the same event with a working adapter: exactly one fix/review graph,
    # cursor advances, and no phantom duplicate from the failed attempt.
    working = FakeBoardAdapter()
    second = _ingest(store, working, [_t89_event()])
    assert second["results"][0]["router_success"] is True
    assert len(working.tasks) == 2
    assert len(working.subscriptions) == 1
    assert store.get_cursor("warroom-os", "warroom-os-db") == 7305
    assert len(store.list_instances()) == 1
    # A retry that replays from FAILED_RETRYABLE must still materialize the
    # graph all the way to WAITING_REVIEW, not get stuck re-normalizing only
    # from DETECTED (M30C item 4).
    assert store.list_instances()[0]["state"] == "waiting_review"


class _NestedAckFailureSubscribeAdapter(FakeBoardAdapter):
    """Mirrors RealBoardAdapter.subscribe returning top-level success=True
    with a failed nested ACK/active-wake repair -- the exact M30C incident
    shape, applied to the code-fix review subscription."""

    def subscribe(self, task_id: str, endpoint: str) -> dict[str, Any]:
        pair = (task_id, endpoint)
        if pair not in self.subscriptions:
            self.subscriptions.append(pair)
        return {"success": True, "ack": {"success": False, "error": "ack_ensure_failed"}}


def test_code_fix_nested_ack_failure_on_review_subscribe_fails_closed_then_retry_succeeds_once(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")

    failing = _NestedAckFailureSubscribeAdapter()
    first = _ingest(store, failing, [_t89_event()])

    item = first["results"][0]
    assert item["action"] == "code_fix_applied"
    assert item["router_success"] is False
    # Fail closed: cursor did not advance despite the fix/review tasks having
    # been created and the adapter reporting top-level subscribe success.
    assert store.get_cursor("warroom-os", "warroom-os-db") == 0
    instance = store.list_instances()[0]
    assert instance["state"] == "failed_retryable"
    # The fix/review tasks were already durably created and applied on the
    # first attempt -- only the review subscribe/ACK repair failed closed.
    assert len(failing.tasks) == 2
    assert len(failing.subscriptions) == 1

    # Retry with a working adapter: the already-applied create_task steps
    # dedupe locally (zero duplicate tasks against the new adapter), exactly
    # one new review subscription/wake succeeds, and the retry advances
    # exactly once.
    working = FakeBoardAdapter()
    second = _ingest(store, working, [_t89_event()])
    assert second["results"][0]["router_success"] is True
    assert working.tasks == {}
    assert len(working.subscriptions) == 1
    assert store.get_cursor("warroom-os", "warroom-os-db") == 7305
    assert len(store.list_instances()) == 1
    assert store.list_instances()[0]["state"] == "waiting_review"


def test_delivered_subscription_is_the_semantic_return_not_a_direct_send(tmp_path):
    """A delivered board notification alone is not a semantic ACK: the semantic
    return path is a durable subscription of the *review* task to the origin, so
    the origin only ever hears back through the reviewed graph, never a raw send
    from the code-fix materialization step."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()

    _ingest(store, adapter, [_t89_event()])

    # No comments / completions were emitted as a stand-in for a real ACK; the
    # only return-trip artifact is the review subscription.
    assert adapter.comments == []
    assert adapter.completed == []
    assert len(adapter.subscriptions) == 1
    review = next(t for t in adapter.tasks.values() if t["kind"] == "review")
    assert adapter.subscriptions[0][0] == review["task_id"]


def test_backfill_missed_event_detects_manual_remediation_and_does_not_duplicate(tmp_path):
    """M30A item 4: backfilling missed event 7305 when remediation tasks
    t_566a2b1b/t_706d3754 were already created manually must record the linked
    manual-remediation satisfaction and create zero new board tasks/wakes,
    without resetting unrelated cursor history."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    # Prime an unrelated cursor so we can prove backfill never resets it.
    store.advance_cursor("warroom-os", "warroom-os-db", 7400)
    source = FakeBoardEventSource(db_identity="warroom-os-db", events=[_t89_event()])

    report = backfill_code_fix_event(
        board="warroom-os", source=source, store=store, event_id="7305", adapter=adapter,
        existing_remediation_ids=["t_566a2b1b", "t_706d3754"],
    )

    assert report["success"] is True
    assert report["action"] == "linked_manual_remediation"
    assert report["manual_remediation_ids"] == ["t_566a2b1b", "t_706d3754"]
    assert report["state"] == "resumed"
    # Zero duplicate board tasks/wakes.
    assert adapter.tasks == {}
    assert adapter.subscriptions == []
    # Manual-remediation satisfaction is durably recorded.
    instance_id = report["instance_id"]
    sats = store.list_requirement_satisfactions(instance_id)
    assert sats[0]["source_kind"] == "manual_remediation"
    assert sats[0]["value"] == ["t_566a2b1b", "t_706d3754"]
    # Unrelated cursor history untouched.
    assert store.get_cursor("warroom-os", "warroom-os-db") == 7400

    # Idempotent replay of the backfill: still one instance, still zero tasks.
    again = backfill_code_fix_event(
        board="warroom-os", source=source, store=store, event_id="7305", adapter=adapter,
        existing_remediation_ids=["t_566a2b1b", "t_706d3754"],
    )
    assert again["created"] is False
    assert len(store.list_instances()) == 1
    assert adapter.tasks == {}


def test_backfill_without_manual_remediation_materializes_graph(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    source = FakeBoardEventSource(db_identity="warroom-os-db", events=[_t89_event()])

    report = backfill_code_fix_event(
        board="warroom-os", source=source, store=store, event_seq=7305, adapter=adapter,
    )

    assert report["success"] is True
    assert report["action"] == "code_fix_backfilled"
    assert len(adapter.tasks) == 2
    assert len(adapter.subscriptions) == 1


def test_backfill_unknown_event_is_a_safe_noop(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    source = FakeBoardEventSource(db_identity="warroom-os-db", events=[_t89_event()])

    report = backfill_code_fix_event(
        board="warroom-os", source=source, store=store, event_id="does-not-exist", adapter=adapter,
    )
    assert report["success"] is False
    assert report["error"] == "event_not_found"
    assert store.list_instances() == []


def test_dry_run_code_fix_is_proposal_only_no_board_writes(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()

    result = _ingest(store, adapter, [_t89_event()], apply=False)

    # Dry-run: request-only proposal, no board mutation, cursor still advances.
    assert result["results"][0]["action"] == "code_fix_routed"
    assert adapter.tasks == {}
    assert adapter.subscriptions == []
    assert store.list_instances() == []
