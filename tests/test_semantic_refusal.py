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
from agentflow_hermes.continuations.semantic_refusal import _stable_digest
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

    # Targeted trusted-origin wake: the source task's origin got a typed
    # wake-origin call, never the generic notify-subscribe path.
    assert adapter.subscriptions == []
    assert adapter.scheduled_origin_wakes == [("t_89e3c71f", "discord:#research")]
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
    assert adapter.subscriptions == []
    assert len(adapter.scheduled_origin_wakes) == 1
    instance_id = store.list_instances()[0]["id"]
    assert len(store.list_requirement_satisfactions(instance_id)) == 1

    # Duplicate replay/restart: cursor already at 7305 -> zero new work, still
    # exactly one instance, one wake-origin call, one ACK.
    second = _ingest(store, adapter, [_unsafe_event(run_metadata, summary)])
    assert second["processed"] in (0, 1)
    assert adapter.tasks == {}
    assert len(adapter.scheduled_origin_wakes) == 1
    assert len(store.list_instances()) == 1
    assert len(store.list_requirement_satisfactions(instance_id)) == 1


def test_semantic_refusal_no_prior_wake_zero_notify_subscribe_one_wake_origin(tmp_path):
    """Reviewer BLOCK (M30F): the no-prior-wake branch must never invoke the
    generic notify-subscribe path -- only a typed origin wake-origin call."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    label, run_metadata, summary = _UNSAFE_CASES[0]

    result = _ingest(store, adapter, [_unsafe_event(run_metadata, summary)])

    assert result["results"][0]["router_success"] is True
    assert adapter.subscriptions == []
    assert adapter.scheduled_origin_wakes == [("t_89e3c71f", "discord:#research")]


class _PendingWakeAdapter(FakeBoardAdapter):
    """schedule_origin_wake succeeds (scheduled) but the wake is not yet
    durably accepted -- mirrors the real CLI's fire-and-forget wake-origin
    command, where acceptance is a separate, later gateway write. Scheduling
    alone must never be treated as semantic completion (M30F)."""

    def schedule_origin_wake(self, task_id: str, endpoint: str) -> dict[str, Any]:
        pair = (task_id, endpoint)
        if pair not in self.scheduled_origin_wakes:
            self.scheduled_origin_wakes.append(pair)
        return {"success": True, "scheduled": True}


def test_semantic_refusal_scheduled_not_yet_accepted_leaves_cursor_retryable_then_accepted(tmp_path):
    """E2E scheduled->accepted transition: zero child tasks, exactly one
    wake-origin call across the whole retry lifecycle, one consumer ACK, and
    the cursor advances exactly once once the wake is durably accepted."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    label, run_metadata, summary = _UNSAFE_CASES[0]

    pending = _PendingWakeAdapter()
    first = _ingest(store, pending, [_unsafe_event(run_metadata, summary)])

    item = first["results"][0]
    assert item["action"] == "semantic_refusal_applied"
    assert item["router_success"] is False
    # Fail closed: cursor did not advance past the failed event.
    assert store.get_cursor("warroom-os", "warroom-os-db") == 0
    assert pending.tasks == {}
    assert len(pending.scheduled_origin_wakes) == 1
    instance = store.list_instances()[0]
    assert instance["state"] == "failed_retryable"
    assert store.list_requirement_satisfactions(instance["id"]) == []

    # The gateway durably accepts the wake out of band; a retry after the
    # durable backoff becomes due must not re-invoke wake-origin (the
    # schedule outbox op already applied) and must record exactly one ACK.
    pending.satisfied_origin_wakes.add(("t_89e3c71f", "discord:#research"))
    with store.connect() as con:
        con.execute("update board_outbox set next_attempt_at=0")
    second = _ingest(store, pending, [_unsafe_event(run_metadata, summary)])
    assert second["results"][0]["router_success"] is True
    assert pending.tasks == {}
    assert len(pending.scheduled_origin_wakes) == 1
    assert store.get_cursor("warroom-os", "warroom-os-db") == 7305
    assert len(store.list_instances()) == 1
    assert store.list_instances()[0]["state"] == "blocked_invalid"
    assert len(store.list_requirement_satisfactions(instance["id"])) == 1


def test_semantic_refusal_poisoned_applied_wake_row_rechecks_receipt_before_ack(tmp_path):
    """Defense in depth: a legacy reconcile bug may have marked a scheduled-only
    wake outbox row applied. The handler must still recheck the durable wake
    receipt before recording semantic_refusal ACK or advancing the cursor."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    label, run_metadata, summary = _UNSAFE_CASES[0]
    adapter = _PendingWakeAdapter()

    first = _ingest(store, adapter, [_unsafe_event(run_metadata, summary)])
    assert first["results"][0]["router_success"] is False
    assert store.get_cursor("warroom-os", "warroom-os-db") == 0
    instance = store.list_instances()[0]
    assert store.list_requirement_satisfactions(instance["id"]) == []

    # Simulate the poisoned pre-fix reconcile state: applied outbox without an
    # accepted/started/completed durable origin wake receipt.
    with store.connect() as con:
        con.execute("update board_outbox set state='applied', next_attempt_at=0, last_error=''")

    poisoned_retry = _ingest(store, adapter, [_unsafe_event(run_metadata, summary)])
    assert poisoned_retry["results"][0]["router_success"] is False
    assert store.get_cursor("warroom-os", "warroom-os-db") == 0
    assert store.list_requirement_satisfactions(instance["id"]) == []
    rows = store.list_outbox()
    assert rows[0]["state"] == "pending"
    assert rows[0]["last_error"] == "origin_wake_not_yet_accepted"

    adapter.satisfied_origin_wakes.add(("t_89e3c71f", "discord:#research"))
    with store.connect() as con:
        con.execute("update board_outbox set next_attempt_at=0")
    accepted_retry = _ingest(store, adapter, [_unsafe_event(run_metadata, summary)])
    assert accepted_retry["results"][0]["router_success"] is True
    assert store.get_cursor("warroom-os", "warroom-os-db") == 7305
    assert store.list_instances()[0]["state"] == "blocked_invalid"
    assert len(store.list_requirement_satisfactions(instance["id"])) == 1
    assert adapter.scheduled_origin_wakes == [("t_89e3c71f", "discord:#research")]


def test_semantic_refusal_prior_wake_receipt_skips_schedule_entirely(tmp_path):
    """A durable prior wake receipt short-circuits straight to the ACK --
    zero notify-subscribe and zero wake-origin calls."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    adapter = FakeBoardAdapter()
    adapter.satisfied_origin_wakes.add(("t_89e3c71f", "discord:#research"))
    label, run_metadata, summary = _UNSAFE_CASES[0]

    result = _ingest(store, adapter, [_unsafe_event(run_metadata, summary)])

    assert result["results"][0]["router_success"] is True
    assert adapter.subscriptions == []
    assert adapter.scheduled_origin_wakes == []
    instance = store.list_instances()[0]
    assert instance["state"] == "blocked_invalid"
    assert len(store.list_requirement_satisfactions(instance["id"])) == 1


def test_semantic_refusal_reinterprets_legacy_subscribe_outbox_row(tmp_path):
    """A live pre-fix semantic_refusal subscribe outbox row is migrated in
    place to schedule_origin_wake, preserving the row and avoiding
    notify-subscribe replay."""
    store = ContinuationStore(tmp_path / "agentflow.sqlite")
    creation = store.create_instance(
        board="warroom-os",
        source_task_id="t_89e3c71f",
        source_event_id="7305",
        source_graph_id="g_89",
        verdict="BLOCK",
        continuation_kind="semantic_refusal",
        origin_ref="discord:#research",
        return_to_ref="discord:#research",
    )
    instance = creation["instance"]
    digest = _stable_digest(instance)
    legacy = store.outbox_enqueue(
        instance["id"],
        step_id="0",
        operation="subscribe",
        payload={"task_id": "t_89e3c71f", "endpoint": "discord:#research"},
        idempotency_key=f"semantic_refusal_notify:{digest}:discord:#research",
    )["outbox"]
    adapter = FakeBoardAdapter()
    label, run_metadata, summary = _UNSAFE_CASES[0]

    result = _ingest(store, adapter, [_unsafe_event(run_metadata, summary)])

    assert result["results"][0]["router_success"] is True
    rows = store.list_outbox()
    assert len(rows) == 1
    assert rows[0]["id"] == legacy["id"]
    assert rows[0]["operation"] == "schedule_origin_wake"
    assert rows[0]["idempotency_key"].startswith("semantic_refusal_wake:")
    assert rows[0]["state"] == "applied"
    assert adapter.subscriptions == []
    assert adapter.scheduled_origin_wakes == [("t_89e3c71f", "discord:#research")]


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
