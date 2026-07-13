"""Acceptance for plan section 10 / 14 commit 8: canonical control-plane
store migration. Every legacy path is injected via env var or an explicit
tmp_path argument — this suite never touches the real ``~/.hermes`` or
``~/.agentflow`` home directories.
"""
from __future__ import annotations

from pathlib import Path

from agentflow_hermes.continuation_store import (
    ContinuationState,
    ContinuationStore,
    default_continuation_db_path,
    default_legacy_continuation_db_paths,
    legacy_needs_input_db_path,
    legacy_residue_report,
    migrate_all_legacy_stores,
    migrate_legacy_store,
)


def test_default_continuation_db_path_is_canonical(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_CONTINUATION_DB", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    path = default_continuation_db_path()

    assert path == tmp_path / ".hermes" / "agentflow" / "agentflow.sqlite"


def test_legacy_needs_input_db_path_respects_env_and_default(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTFLOW_NEEDS_INPUT_DB", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    assert legacy_needs_input_db_path() == tmp_path / ".hermes" / "state" / "agentflow_needs_input_continuations.sqlite"

    monkeypatch.setenv("AGENTFLOW_NEEDS_INPUT_DB", str(tmp_path / "explicit.sqlite"))
    assert legacy_needs_input_db_path() == tmp_path / "explicit.sqlite"


def test_default_legacy_paths_exclude_canonical_and_dedupe(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path / ".agentflow"))
    monkeypatch.delenv("AGENTFLOW_NEEDS_INPUT_DB", raising=False)
    monkeypatch.delenv("HERMES_CONTINUATION_DB", raising=False)

    paths = default_legacy_continuation_db_paths()

    assert default_continuation_db_path() not in paths
    assert len(paths) == len(set(paths))


def _seed_legacy_store(path: Path) -> ContinuationStore:
    legacy = ContinuationStore(path)
    created = legacy.create_instance(
        board="warroom-os", source_task_id="t_legacy_1", source_event_id="ev_legacy_1",
        source_graph_id="g_1", contract_ref="generic.owner-input.v1", verdict="BLOCK",
        continuation_kind="needs_input", origin_ref="discord:#research", return_to_ref="discord:#research",
    )
    instance_id = created["instance"]["id"]
    legacy.transition(instance_id, ContinuationState.WAITING_OWNER, reason="seed")
    legacy.add_step(instance_id, step_kind="owner_anchor", idempotency_key="anchor:1", board_task_id="bt_1")
    legacy.add_owner_receipt(instance_id, owner_ref="operator-main", fields={"result_url": "https://example.com/x"})
    legacy.advance_cursor("warroom-os", "warroom-os-db", 42)
    legacy.outbox_enqueue(
        instance_id, step_id="1", operation="create_task", payload={"title": "x"}, idempotency_key="ob:1"
    )
    return legacy


def test_migrate_legacy_store_copies_rows_with_source_ids_preserved(tmp_path):
    legacy_path = tmp_path / "legacy.sqlite"
    legacy = _seed_legacy_store(legacy_path)
    legacy_instance = legacy.list_instances()[0]

    canonical = ContinuationStore(tmp_path / "canonical.sqlite")
    report = migrate_legacy_store(canonical=canonical, legacy_path=legacy_path)

    assert report["success"] is True
    assert report["migrated"] is True
    assert report["counts"] == {"instances": 1, "steps": 1, "receipts": 1, "events": report["counts"]["events"], "cursors": 1, "outbox": 1}
    assert report["counts"]["events"] > 0
    assert report["verification"]["ok"] is True

    migrated = canonical.list_instances()
    assert len(migrated) == 1
    assert migrated[0]["source_task_id"] == legacy_instance["source_task_id"]
    assert migrated[0]["source_event_id"] == legacy_instance["source_event_id"]
    assert migrated[0]["state"] == legacy_instance["state"]

    steps = canonical.list_steps(migrated[0]["id"])
    assert len(steps) == 1
    assert steps[0]["board_task_id"] == "bt_1"

    receipts = canonical.list_owner_receipts(migrated[0]["id"])
    assert receipts[0]["fields"] == {"result_url": "https://example.com/x"}

    assert canonical.get_cursor("warroom-os", "warroom-os-db") == 42
    assert len(canonical.list_outbox()) == 1


def test_migrate_legacy_store_is_idempotent(tmp_path):
    legacy_path = tmp_path / "legacy.sqlite"
    _seed_legacy_store(legacy_path)
    canonical = ContinuationStore(tmp_path / "canonical.sqlite")

    first = migrate_legacy_store(canonical=canonical, legacy_path=legacy_path)
    second = migrate_legacy_store(canonical=canonical, legacy_path=legacy_path)

    assert first["success"] is True and second["success"] is True
    assert len(canonical.list_instances()) == 1
    assert len(canonical.list_steps(canonical.list_instances()[0]["id"])) == 1
    assert len(canonical.list_owner_receipts(canonical.list_instances()[0]["id"])) == 1
    assert len(canonical.list_outbox()) == 1
    # Second run copies zero new rows — nothing left to migrate.
    assert second["counts"]["instances"] == 0
    assert second["counts"]["steps"] == 0
    assert second["counts"]["receipts"] == 0
    assert second["counts"]["outbox"] == 0


def test_migrate_legacy_store_writes_a_migration_receipt(tmp_path):
    legacy_path = tmp_path / "legacy.sqlite"
    _seed_legacy_store(legacy_path)
    canonical = ContinuationStore(tmp_path / "canonical.sqlite")

    migrate_legacy_store(canonical=canonical, legacy_path=legacy_path)

    receipts = canonical.list_migration_receipts()
    assert len(receipts) == 1
    assert receipts[0]["legacy_path"] == str(legacy_path)
    assert receipts[0]["counts"]["instances"] == 1
    assert receipts[0]["verification"]["ok"] is True


def test_migrate_legacy_store_missing_path_is_a_noop(tmp_path):
    canonical = ContinuationStore(tmp_path / "canonical.sqlite")

    report = migrate_legacy_store(canonical=canonical, legacy_path=tmp_path / "does-not-exist.sqlite")

    assert report["success"] is True
    assert report["migrated"] is False
    assert report["reason"] == "legacy_path_missing"
    assert canonical.list_instances() == []


def test_migrate_legacy_store_refuses_to_self_migrate(tmp_path):
    canonical_path = tmp_path / "canonical.sqlite"
    canonical = ContinuationStore(canonical_path)
    canonical.init()

    report = migrate_legacy_store(canonical=canonical, legacy_path=canonical_path)

    assert report["migrated"] is False
    assert report["reason"] == "legacy_path_is_canonical"


def test_migrate_all_legacy_stores_covers_every_known_path(tmp_path):
    legacy_a = tmp_path / "legacy_a.sqlite"
    legacy_b = tmp_path / "legacy_b.sqlite"
    _seed_legacy_store(legacy_a)
    _seed_legacy_store(legacy_b)
    canonical = ContinuationStore(tmp_path / "canonical.sqlite")

    report = migrate_all_legacy_stores(canonical=canonical, legacy_paths=(legacy_a, legacy_b))

    assert report["success"] is True
    assert len(report["results"]) == 2
    # Both legacy stores describe the same board/task/event tuple, so the
    # source-tuple unique key on continuation_instances dedupes the second
    # store's instance into the first's row rather than creating a second.
    assert len(canonical.list_instances()) == 1


def test_legacy_residue_report_reports_active_rows_without_mutating_legacy(tmp_path):
    legacy_path = tmp_path / "legacy.sqlite"
    _seed_legacy_store(legacy_path)

    report = legacy_residue_report(paths=(legacy_path,))

    assert report == [{"path": str(legacy_path), "total_rows": 1, "active_rows": 1}]

    # Migration must never delete/mutate the legacy DB (plan: "leave old DB
    # read-only for one release") — residue is still reported afterward.
    canonical = ContinuationStore(tmp_path / "canonical.sqlite")
    migrate_legacy_store(canonical=canonical, legacy_path=legacy_path)
    report_after = legacy_residue_report(paths=(legacy_path,))
    assert report_after == [{"path": str(legacy_path), "total_rows": 1, "active_rows": 1}]


def test_legacy_residue_report_skips_missing_and_non_continuation_dbs(tmp_path):
    missing = tmp_path / "missing.sqlite"
    import sqlite3

    empty_db = tmp_path / "empty.sqlite"
    sqlite3.connect(empty_db).close()

    assert legacy_residue_report(paths=(missing, empty_db)) == []
