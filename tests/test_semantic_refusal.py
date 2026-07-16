"""M30A remediation: an unsafe reviewer BLOCK/NEED_MORE (credentials, secrets,
destructive/data-loss, owner-only proof/user input, live-money/financial
approval) must NOT auto-apply as a CODE_FIX graph. It fails closed to an
explicit SEMANTIC_REFUSAL that is durably quarantined, produces a targeted
trusted-origin notify+wake plus a durable refusal ACK (distinct from passive
delivery), and creates zero fix/review/activation tasks.

Reuses the t_89e3c71f incident shape (flat ``{verdict, blockers}`` metadata with
a bare ``Verdict: BLOCK`` summary), only now the blocker names an unsafe
category.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentflow_hermes.board_adapter import FakeBoardAdapter
from agentflow_hermes.board_events import BoardEvent, FakeBoardEventSource
from agentflow_hermes.continuation_config import load_contract_registry
from agentflow_hermes.continuation_engine import ingest_board_once
from agentflow_hermes.continuation_store import ContinuationStore
from pathlib import Path

_G421_YAML = Path(__file__).resolve().parents[1] / "contracts" / "warroom.g421.exposure-resolution.v1.yaml"


def _contracts():
    return load_contract_registry([_G421_YAML])


_UNSAFE_CASES = [
    ("credentials", {"verdict": "BLOCK", "blockers": ["rotate the leaked database credentials"]}, "Verdict: BLOCK"),
    ("secret_token", {"verdict": "BLOCK", "blockers": ["the API token is hardcoded in the repo"]}, "Verdict: BLOCK"),
    ("destructive", {"verdict": "BLOCK", "blockers": ["drop the production users table"]}, "Verdict: BLOCK"),
    ("owner_proof", None, "Verdict: BLOCK\nBlockers: needs the owner to provide sign-off proof"),
    ("live_money", None, "Verdict: BLOCK\nBlockers: requires financial approval to release the payment"),
]


def _unsafe_event(run_metadata, summary, event_id="7305", event_seq=7305, source_task_id="t_89e3c71f") -> BoardEvent:
    return BoardEvent(
        event_id=event_id,
        event_seq=event_seq,
        source_task_id=source_task_id,
        source_graph_id="g_89",
        event_kind="completed",
        summary=summary,
        run_metadata=run_metadata,
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


@pytest.mark.parametrize("label,run_metadata,summary", _UNSAFE_CASES, ids=[c[0] for c in _UNSAFE_CASES])
def test_unsafe_block_fails_closed_zero_tasks_notify_wake_and_durable_ack(label, run_metadata, summary, tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()

    result = _ingest(store, adapter, [_unsafe_event(run_metadata, summary)])

    item = result["results"][0]
    assert item["action"] == "semantic_refusal_applied"
    assert item["router_success"] is True

    # Zero fix/review/activation tasks — nothing auto-remediated.
    assert adapter.tasks == {}

    # Targeted trusted-origin notify+wake: the source task subscribed to origin.
    assert adapter.subscriptions == [("t_89e3c71f", "discord:#research")]
    # No direct AgentFlow/Discord send stand-in.
    assert adapter.comments == []
    assert adapter.completed == []

    # Durable quarantine: exactly one instance parked in the refusal terminal
    # state, recorded as a semantic_refusal continuation.
    instances = store.list_instances()
    assert len(instances) == 1
    instance = instances[0]
    assert instance["continuation_kind"] == "semantic_refusal"
    assert instance["state"] == "blocked_invalid"

    # Explicit durable refusal ACK distinct from the passive subscription.
    sats = store.list_requirement_satisfactions(instance["id"])
    ack = next(s for s in sats if s["source_kind"] == "semantic_refusal_ack")
    assert ack["value"]["categories"]  # non-empty unsafe categories
    assert item["refusal"]["refusal_ack"] is True
    assert item["refusal"]["subscribed"] is True

    # Cursor advanced past the refused event (it was handled, not dropped).
    assert store.get_cursor("warroom-os", "warroom-os-db") == 7305


def test_semantic_refusal_replay_creates_zero_duplicate_tasks_or_acks(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    label, run_metadata, summary = _UNSAFE_CASES[0]

    first = _ingest(store, adapter, [_unsafe_event(run_metadata, summary)])
    assert first["results"][0]["router_success"] is True
    assert adapter.tasks == {}
    assert len(adapter.subscriptions) == 1
    instance_id = store.list_instances()[0]["id"]
    assert len(store.list_requirement_satisfactions(instance_id)) == 1

    # Duplicate replay/restart: cursor already at 7305 -> zero new work, still
    # exactly one instance, one subscription, one ACK.
    second = _ingest(store, adapter, [_unsafe_event(run_metadata, summary)])
    assert second["processed"] in (0, 1)
    assert adapter.tasks == {}
    assert len(adapter.subscriptions) == 1
    assert len(store.list_instances()) == 1
    assert len(store.list_requirement_satisfactions(instance_id)) == 1


class _FailingSubscribeAdapter(FakeBoardAdapter):
    """notify-subscribe fails, like a real CLI subscribe that errored."""

    def subscribe(self, task_id: str, endpoint: str) -> dict[str, Any]:
        return {"success": False, "error": "cli_subscribe_failed"}


def test_semantic_refusal_notify_failure_leaves_cursor_retryable_then_retry(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    label, run_metadata, summary = _UNSAFE_CASES[0]

    failing = _FailingSubscribeAdapter()
    first = _ingest(store, failing, [_unsafe_event(run_metadata, summary)])

    item = first["results"][0]
    assert item["action"] == "semantic_refusal_applied"
    assert item["router_success"] is False
    # Fail closed: cursor did not advance past the failed event.
    assert store.get_cursor("warroom-os", "warroom-os-db") == 0
    assert failing.tasks == {}
    instance = store.list_instances()[0]
    assert instance["state"] == "failed_retryable"
    assert store.list_requirement_satisfactions(instance["id"]) == []

    # Retry with a working adapter after the durable backoff becomes due:
    # refusal completes, cursor advances, one ACK, one subscription, no duplicate instance.
    with store.connect() as con:
        con.execute("update board_outbox set next_attempt_at=0")
    working = FakeBoardAdapter()
    second = _ingest(store, working, [_unsafe_event(run_metadata, summary)])
    assert second["results"][0]["router_success"] is True
    assert working.tasks == {}
    assert len(working.subscriptions) == 1
    assert store.get_cursor("warroom-os", "warroom-os-db") == 7305
    assert len(store.list_instances()) == 1
    assert store.list_instances()[0]["state"] == "blocked_invalid"


class _NestedAckFailureSubscribeAdapter(FakeBoardAdapter):
    """Mirrors RealBoardAdapter.subscribe returning top-level success=True
    with a failed nested ACK/active-wake repair (ack_schema_missing/
    ack_ensure_failed) -- the exact M30C incident shape."""

    def subscribe(self, task_id: str, endpoint: str) -> dict[str, Any]:
        pair = (task_id, endpoint)
        if pair not in self.subscriptions:
            self.subscriptions.append(pair)
        return {"success": True, "ack": {"success": False, "error": "ack_schema_missing"}}


def test_semantic_refusal_nested_ack_failure_fails_closed_then_retry_succeeds_once(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    label, run_metadata, summary = _UNSAFE_CASES[0]

    failing = _NestedAckFailureSubscribeAdapter()
    first = _ingest(store, failing, [_unsafe_event(run_metadata, summary)])

    item = first["results"][0]
    assert item["action"] == "semantic_refusal_applied"
    assert item["router_success"] is False
    # Fail closed: cursor did not advance, and no false semantic success or
    # quarantine transition happened despite the adapter's top-level success.
    assert store.get_cursor("warroom-os", "warroom-os-db") == 0
    instance = store.list_instances()[0]
    assert instance["state"] == "failed_retryable"
    assert store.list_requirement_satisfactions(instance["id"]) == []

    # Retry with a working adapter after the durable backoff becomes due:
    # exactly one wake/refusal receipt, zero duplicate tasks/wakes, and the
    # retry advances exactly once.
    with store.connect() as con:
        con.execute("update board_outbox set next_attempt_at=0")
    working = FakeBoardAdapter()
    second = _ingest(store, working, [_unsafe_event(run_metadata, summary)])
    assert second["results"][0]["router_success"] is True
    assert working.tasks == {}
    assert len(working.subscriptions) == 1
    assert store.get_cursor("warroom-os", "warroom-os-db") == 7305
    assert len(store.list_instances()) == 1
    assert store.list_instances()[0]["state"] == "blocked_invalid"


def test_dry_run_semantic_refusal_is_proposal_only_no_board_writes(tmp_path):
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    label, run_metadata, summary = _UNSAFE_CASES[0]

    result = _ingest(store, adapter, [_unsafe_event(run_metadata, summary)], apply=False)

    item = result["results"][0]
    assert item["action"] == "semantic_refusal_detected"
    assert item["refusal_categories"]
    # No board mutation and no durable instance in dry-run; cursor still advances.
    assert adapter.tasks == {}
    assert adapter.subscriptions == []
    assert store.list_instances() == []
    assert store.get_cursor("warroom-os", "warroom-os-db") == 7305
