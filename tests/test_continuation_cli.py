from __future__ import annotations

import json

import pytest

from agentflow_hermes import cli


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
