from __future__ import annotations

import json

import pytest

from agentflow_hermes import cli
from agentflow_hermes.continuation_store import ContinuationStore


def _events_file(tmp_path, events):
    path = tmp_path / "events.json"
    path.write_text(json.dumps(events), encoding="utf-8")
    return path


def _needs_input_event(event_id="ev1", event_seq=1):
    return {
        "event_id": event_id,
        "event_seq": event_seq,
        "source_task_id": "t_ab93a206",
        "source_graph_id": "g_1",
        "origin_ref": "discord:#research",
        "return_to_ref": "discord:#research",
        "run_metadata": {
            "agentflow_outcome": {
                "schema_version": 1,
                "verdict": "BLOCK",
                "continuation_kind": "needs_input",
                "contract_ref": "warroom.g421.exposure-resolution.v1",
                "required_inputs": [
                    {"name": "approval_receipt_id", "authority": "owner"},
                    {"name": "resolution_basis", "authority": "owner"},
                    {"name": "owner_confirmation", "authority": "owner"},
                ],
                "resume_transition": "warroom.g421.packet-rerun",
            }
        },
    }


def _run(argv, tmp_path):
    db_path = tmp_path / "continuation.sqlite"
    full_argv = [*argv, "--db", str(db_path)]
    rc = cli.main(full_argv)
    return rc


def _run_capture(argv, tmp_path, capsys):
    rc = _run(argv, tmp_path)
    out = capsys.readouterr().out
    return rc, json.loads(out.strip().splitlines()[-1])


def test_ingest_list_show_submit_vertical(tmp_path, capsys):
    events_path = _events_file(tmp_path, [_needs_input_event()])

    rc, report = _run_capture(
        ["continuation", "ingest", "--board", "warroom-os", "--board-db-identity", "warroom-os-db", "--events-file", str(events_path)],
        tmp_path, capsys,
    )
    assert rc == 0
    assert report["processed"] == 1

    rc, listing = _run_capture(["continuation", "list", "--state", "waiting_owner"], tmp_path, capsys)
    assert rc == 0
    assert len(listing["instances"]) == 1
    instance_id = listing["instances"][0]["id"]

    rc, shown = _run_capture(["continuation", "show", str(instance_id)], tmp_path, capsys)
    assert rc == 0
    assert shown["instance"]["state"] == "waiting_owner"
    assert "resolution_basis" in shown["required_owner_fields"]
    assert shown["downstream_will_not"]

    submission_path = tmp_path / "owner-input.json"
    submission_path.write_text(json.dumps({
        "owner_ref": "operator-main",
        "fields": {"resolution_basis": "target_never_submitted", "approval_receipt_id": "recv_1"},
    }), encoding="utf-8")

    rc, refused = _run_capture(
        ["continuation", "submit", str(instance_id), "--input-file", str(submission_path)], tmp_path, capsys
    )
    assert rc == 2
    assert refused["success"] is False
    assert any("owner_confirmation" in e for e in refused["errors"])

    submission_path.write_text(json.dumps({
        "owner_ref": "operator-main",
        "fields": {
            "resolution_basis": "target_never_submitted",
            "approval_receipt_id": "recv_1",
            "owner_confirmation": True,
        },
    }), encoding="utf-8")

    rc, accepted = _run_capture(
        ["continuation", "submit", str(instance_id), "--input-file", str(submission_path)], tmp_path, capsys
    )
    assert rc == 0
    assert accepted["success"] is True
    assert accepted["state"] == "materializing"

    rc, shown_after = _run_capture(["continuation", "show", str(instance_id)], tmp_path, capsys)
    assert shown_after["instance"]["state"] == "materializing"

    # duplicate submit of same fixture is refused (no longer waiting_owner) and never
    # invents omitted owner fields.
    rc, dup = _run_capture(
        ["continuation", "submit", str(instance_id), "--input-file", str(submission_path)], tmp_path, capsys
    )
    assert rc == 2
    assert dup["success"] is False

    # duplicate ingest of the same fixture creates zero duplicate cards.
    rc, dup_ingest = _run_capture(
        ["continuation", "ingest", "--board", "warroom-os", "--board-db-identity", "warroom-os-db", "--events-file", str(events_path)],
        tmp_path, capsys,
    )
    assert dup_ingest["processed"] == 0


def test_doctor_reports_selected_store(tmp_path, capsys):
    rc, report = _run_capture(["continuation", "doctor"], tmp_path, capsys)
    assert rc == 0
    assert report["success"] is True
    assert "selected" in report
    assert "legacy_residue" in report


def test_migrate_store_cli_copies_from_explicit_legacy_db(tmp_path, capsys):
    legacy_path = tmp_path / "legacy.sqlite"
    legacy_store = ContinuationStore(legacy_path)
    legacy_store.create_instance(
        board="warroom-os", source_task_id="t_legacy", source_event_id="ev_legacy",
        contract_ref="generic.owner-input.v1", continuation_kind="needs_input",
    )

    canonical_db = tmp_path / "canonical.sqlite"
    rc = cli.main([
        "continuation", "migrate-store", "--db", str(canonical_db), "--legacy-db", str(legacy_path),
    ])
    out = capsys.readouterr().out
    report = json.loads(out.strip().splitlines()[-1])

    assert rc == 0
    assert report["success"] is True
    assert len(report["results"]) == 1
    assert report["results"][0]["counts"]["instances"] == 1

    canonical = ContinuationStore(canonical_db)
    assert len(canonical.list_instances()) == 1


def test_migrate_store_cli_is_idempotent(tmp_path, capsys):
    legacy_path = tmp_path / "legacy.sqlite"
    ContinuationStore(legacy_path).create_instance(
        board="warroom-os", source_task_id="t_legacy", source_event_id="ev_legacy",
        contract_ref="generic.owner-input.v1", continuation_kind="needs_input",
    )
    canonical_db = tmp_path / "canonical.sqlite"
    argv = ["continuation", "migrate-store", "--db", str(canonical_db), "--legacy-db", str(legacy_path)]

    assert cli.main(argv) == 0
    capsys.readouterr()
    assert cli.main(argv) == 0
    capsys.readouterr()

    canonical = ContinuationStore(canonical_db)
    assert len(canonical.list_instances()) == 1


def test_retry_reconciles_pending_outbox_create_and_subscribe(tmp_path, capsys):
    db_path = tmp_path / "continuation.sqlite"
    store = ContinuationStore(db_path)
    instance = store.create_instance(
        board="warroom-os",
        source_task_id="t_source",
        source_event_id="ev_retry",
        contract_ref="warroom.g421.exposure-resolution.v1",
        continuation_kind="needs_input",
    )["instance"]
    step = store.add_step(instance["id"], step_kind="owner_anchor", idempotency_key="owner_anchor:retry")["step"]
    store.outbox_enqueue(
        instance["id"],
        step_id=str(step["id"]),
        operation="create_task",
        payload={"title": "retry owner anchor", "idempotency_key": "owner_anchor:retry"},
        idempotency_key="owner_anchor:retry",
    )
    store.outbox_enqueue(
        instance["id"],
        step_id=str(step["id"]),
        operation="subscribe",
        payload={"task_id": "task:owner", "endpoint": "discord:#research"},
        idempotency_key="subscribe:retry",
    )

    rc = cli.main(["continuation", "retry", str(instance["id"]), "--db", str(db_path)])
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert rc == 0
    assert report["pending_before"] == 2
    rows = store.list_outbox()
    assert [(r["operation"], r["state"], r["attempts"]) for r in rows] == [
        ("create_task", "applied", 1),
        ("subscribe", "applied", 1),
    ]
