"""M13 durable maintenance-cycle receipts, idempotency, and failure-path state.

These tests exercise the standalone durable helpers directly (no runner
gating), proving: additive schema, atomic cross-process claim semantics,
per-day/interval counters, degraded/circuit-breaker state, and sanitized
deadletter/journal-ref fallback rows.
"""
from __future__ import annotations

from agentflow_hermes.maintenance import durable


def _db(tmp_path):
    return str(tmp_path / "maintenance.db")


def test_claim_cycle_is_atomic_across_fresh_calls(tmp_path):
    db_path = _db(tmp_path)
    key = durable.build_cycle_key(repo_id="repo1", target_unit="hermes-gateway.service", cycle_ref="sha1")

    claimed, row = durable.claim_cycle(
        db_path, idempotency_key=key, target_unit="hermes-gateway.service",
        repo_id="repo1", reason="eligible", dry_run=True, fake=True,
        source_ref="", policy_ref="maintenance.json", now=1000.0,
    )
    assert claimed is True
    assert row["status"] == "attempt"

    # A second, fresh "process" (new call) with the same key must not claim again.
    claimed2, row2 = durable.claim_cycle(
        db_path, idempotency_key=key, target_unit="hermes-gateway.service",
        repo_id="repo1", reason="eligible", dry_run=True, fake=True,
        source_ref="", policy_ref="maintenance.json", now=1001.0,
    )
    assert claimed2 is False
    assert row2["status"] == "attempt"
    assert row2["idempotency_key"] == key


def test_update_cycle_status_reconciles_to_single_terminal_state(tmp_path):
    db_path = _db(tmp_path)
    key = durable.build_cycle_key(repo_id="repo1", target_unit="hermes-gateway.service", cycle_ref="sha1")
    durable.claim_cycle(
        db_path, idempotency_key=key, target_unit="hermes-gateway.service",
        repo_id="repo1", reason="eligible", dry_run=True, fake=True,
        source_ref="", policy_ref="maintenance.json", now=1000.0,
    )
    durable.update_cycle_status(db_path, key, status="applied", reason="service_action_applied")

    row = durable.get_cycle(db_path, key)
    assert row["status"] == "applied"
    assert row["reason"] == "service_action_applied"

    # A second claim attempt against the same key after the terminal write still
    # returns the terminal row rather than claiming/executing again.
    claimed, row2 = durable.claim_cycle(
        db_path, idempotency_key=key, target_unit="hermes-gateway.service",
        repo_id="repo1", reason="eligible", dry_run=True, fake=True,
        source_ref="", policy_ref="maintenance.json", now=2000.0,
    )
    assert claimed is False
    assert row2["status"] == "applied"


def test_count_cycles_today_and_last_applied_at(tmp_path):
    db_path = _db(tmp_path)
    key1 = durable.build_cycle_key(repo_id="repo1", target_unit="unit.service", cycle_ref="sha1")
    key2 = durable.build_cycle_key(repo_id="repo1", target_unit="unit.service", cycle_ref="sha2")

    durable.claim_cycle(
        db_path, idempotency_key=key1, target_unit="unit.service", repo_id="repo1",
        reason="", dry_run=False, fake=True, source_ref="", policy_ref="", now=1000.0,
    )
    durable.update_cycle_status(db_path, key1, status="applied", reason="ok")

    durable.claim_cycle(
        db_path, idempotency_key=key2, target_unit="unit.service", repo_id="repo1",
        reason="", dry_run=False, fake=True, source_ref="", policy_ref="", now=2000.0,
    )
    durable.update_cycle_status(db_path, key2, status="applied", reason="ok")

    count = durable.count_cycles_today(
        db_path, repo_id="repo1", target_unit="unit.service", day_start=0.0, day_end=86400.0,
    )
    assert count == 2
    assert durable.last_applied_at(db_path, repo_id="repo1", target_unit="unit.service") == 2000.0


def test_degraded_state_roundtrip(tmp_path):
    db_path = _db(tmp_path)
    assert durable.is_degraded(db_path) is False
    durable.set_degraded(db_path, True)
    assert durable.is_degraded(db_path) is True
    durable.set_degraded(db_path, False)
    assert durable.is_degraded(db_path) is False


def test_deadletter_ref_is_sanitized(tmp_path):
    db_path = _db(tmp_path)
    durable.write_deadletter(
        db_path, reason="post_smoke_failed", target_unit="unit.service",
        idempotency_key="k1", ref="/home/alice/private/log TOKEN=abc123",
    )
    import sqlite3

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    row = con.execute("select * from maintenance_deadletter").fetchone()
    assert row["reason"] == "post_smoke_failed"
    assert "/home/alice" not in row["ref"]
    assert "TOKEN=abc123" not in row["ref"]
