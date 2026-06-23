import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentflow_hermes.bridges.kanban import (
    KanbanResolverError,
    _extract_verdict,
    _is_blocked,
    _mentions_blocked_card,
    _mentions_remediation,
    load_fixture,
    resolve_blocked_remediation,
)


def _fixture(tmp_path: Path, cards: list[dict]) -> Path:
    path = tmp_path / "kanban.json"
    path.write_text(json.dumps({"cards": cards}, ensure_ascii=False), encoding="utf-8")
    return path


def _blocked_downstream() -> dict:
    return {
        "id": "card_blocked_abc",
        "status": "blocked",
        "title": "Deploy fix to prod",
        "body": "Waiting on remediation review for original blocker card_blocker_xyz.",
        "comments": ["Blocked by remediation path from card_blocker_xyz."],
        "metadata": {"blocker_id": "card_blocker_xyz", " remediation_requested": True},
    }


def _original_blocker() -> dict:
    return {
        "id": "card_blocker_xyz",
        "status": "failed",
        "title": "Original review",
        "body": "[JOB ACK]\njob_id: job_old\nstatus: failed\nsummary: Verdict: BLOCK due to race",
    }


def _remediation_review_go() -> dict:
    return {
        "id": "card_review_def",
        "status": "succeeded",
        "title": "Remediation review",
        "body": "Reviewed fix for card_blocker_xyz. Verdict: GO.",
        "comments": ["Fix addresses race condition; card_blocked_abc can proceed."],
        "metadata": {"parent": "card_blocker_xyz", "downstream": "card_blocked_abc"},
    }


def test_extract_verdict_detects_go_and_block():
    assert _extract_verdict("Verdict: GO") == "GO"
    assert _extract_verdict("Verdict: BLOCK") == "BLOCK"
    assert _extract_verdict("Verdict: NEED_MORE") == "NEED_MORE"
    assert _extract_verdict("no verdict here") == ""


def test_is_blocked():
    assert _is_blocked({"status": "blocked"})
    assert not _is_blocked({"status": "done", "body": "This task is blocked by X"})
    assert not _is_blocked({"status": "done"})


def test_mentions_remediation():
    assert _mentions_remediation({"body": "needs remediation"})
    assert _mentions_remediation({"body": "fix the bug"})
    assert not _mentions_remediation({"body": "normal task"})


def test_mentions_blocked_card():
    blocked = {"id": "card_abc", "blocked_by": "card_xyz", "body": "blocked"}
    review = {"id": "card_def", "body": "card_abc can proceed"}
    assert _mentions_blocked_card(blocked, review)

    unrelated = {"id": "card_def", "body": "something else"}
    assert not _mentions_blocked_card(blocked, unrelated)


def test_resolve_blocked_remediation_success():
    result = resolve_blocked_remediation(_blocked_downstream(), _remediation_review_go())
    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["resolved"] is True
    assert result["blocked_card_id"] == "card_blocked_abc"
    assert result["remediation_review_id"] == "card_review_def"
    assert result["verdict"] == "GO"
    assert result["candidate"]["action"] == "unblock_and_dispatch"
    assert result["mutations"] == []


def test_refuses_when_blocked_card_not_blocked():
    card = _blocked_downstream()
    card["status"] = "done"
    card["body"] = "This task is complete."
    card["comments"] = []
    card["metadata"] = {}
    result = resolve_blocked_remediation(card, _remediation_review_go())
    assert result["success"] is False
    assert result["error"] == "blocked_card_not_blocked"


def test_refuses_when_blocked_card_lacks_remediation_reference():
    card = {
        "id": "card_blocked_abc",
        "status": "blocked",
        "title": "Deploy thing to prod",
        "body": "Just a normal stalled task.",
        "comments": [],
        "metadata": {},
    }
    result = resolve_blocked_remediation(card, _remediation_review_go())
    assert result["success"] is False
    assert result["error"] == "blocked_card_lacks_remediation_reference"


def test_refuses_missing_go_verdict():
    review = _remediation_review_go()
    review["body"] = "Reviewed fix. No verdict yet."
    result = resolve_blocked_remediation(_blocked_downstream(), review)
    assert result["success"] is False
    assert result["error"] == "missing_go_verdict"


def test_refuses_block_verdict():
    review = _remediation_review_go()
    review["body"] = "Verdict: BLOCK"
    result = resolve_blocked_remediation(_blocked_downstream(), review)
    assert result["success"] is False
    assert result["error"] == "remediation_review_has_block_verdict"
    assert result["unsafe"] is True


def test_refuses_need_more_verdict():
    review = _remediation_review_go()
    review["body"] = "Verdict: NEED_MORE"
    result = resolve_blocked_remediation(_blocked_downstream(), review)
    assert result["success"] is False
    assert result["error"] == "remediation_review_needs_more_work"


def test_refuses_unrelated_remediation_review():
    review = _remediation_review_go()
    review["body"] = "Verdict: GO for some other task"
    review["comments"] = []
    review["metadata"] = {}
    result = resolve_blocked_remediation(_blocked_downstream(), review)
    assert result["success"] is False
    assert result["error"] == "remediation_review_unrelated_to_blocked_card"


def test_refuses_ambiguous_multiple_blockers():
    card = _blocked_downstream()
    card["body"] = (
        "Waiting on remediation review for original blockers card_blocker_xyz "
        "and card_blocker_other."
    )
    card["metadata"] = {}
    review = _remediation_review_go()
    review["body"] = "Verdict: GO. card_blocked_abc can proceed if this was the right blocker."
    review["comments"] = []
    review["metadata"] = {}
    result = resolve_blocked_remediation(card, review)
    assert result["success"] is False
    assert result["error"] == "ambiguous_multiple_blockers"


def test_actual_m2_remediation_shape_prefers_structured_relation():
    blocked = {
        "id": "t_18032353",
        "status": "blocked",
        "title": "AgentFlow Hermes M2: cron bridge dry-run",
        "body": "Prereq blocked by t_a5016809. Fix card t_aafe72c5 and fix-review t_60d7a850 were added; after t_60d7a850 semantic GO, unblock/redispatch this M2 card.",
        "comments": [
            "Operator remediation: staying blocked because M1 review t_a5016809 returned BLOCK. Added fix t_aafe72c5 and fix-review t_60d7a850 as an additional parent gate.",
        ],
        "metadata": {"blocking_reviews": ["t_60d7a850"]},
    }
    review = {
        "id": "t_60d7a850",
        "status": "done",
        "title": "Review M1 ACK multiline artifacts parser fix",
        "summary": "Verdict: GO — t_a5016809 BLOCK is remediated; M2 card t_18032353 may be unblocked/redispatched.",
        "metadata": {
            "verdict": "GO",
            "remediates": "t_a5016809",
            "unblock_ok": ["t_18032353"],
        },
    }
    result = resolve_blocked_remediation(blocked, review)
    assert result["success"] is True
    assert result["candidate"]["action"] == "unblock_and_dispatch"
    assert result["mutations"] == []
    assert result["evidence"]["structured_relation"] is True


def test_refuses_missing_blocked_card_id():
    result = resolve_blocked_remediation({}, _remediation_review_go())
    assert result["success"] is False
    assert result["error"] == "missing_blocked_card_id"


def test_refuses_missing_remediation_review_id():
    result = resolve_blocked_remediation(_blocked_downstream(), {})
    assert result["success"] is False
    assert result["error"] == "missing_remediation_review_id"


def test_dry_run_required():
    result = resolve_blocked_remediation(
        _blocked_downstream(), _remediation_review_go(), dry_run=False
    )
    assert result["success"] is False
    assert result["error"] == "live_mutation_disabled"
    assert result["dry_run"] is False


def test_load_fixture(tmp_path: Path):
    path = _fixture(tmp_path, [_blocked_downstream(), _remediation_review_go()])
    data = load_fixture(path)
    assert len(data["cards"]) == 2


def test_cli_resolve_blocked_success(tmp_path: Path, monkeypatch):
    fixture = _fixture(
        tmp_path,
        [_original_blocker(), _blocked_downstream(), _remediation_review_go()],
    )
    env = {"PYTHONPATH": "src"}
    monkeypatch.setenv("PYTHONPATH", "src")
    output = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentflow_hermes.cli",
            "bridge",
            "kanban",
            "resolve-blocked",
            "--blocked-card",
            "card_blocked_abc",
            "--remediation-review",
            "card_review_def",
            "--input-file",
            str(fixture),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    result = json.loads(output.stdout)
    assert result["success"] is True
    assert result["resolved"] is True


def test_cli_resolve_blocked_refusal(tmp_path: Path, monkeypatch):
    fixture = _fixture(
        tmp_path,
        [_blocked_downstream(), {"id": "card_review_def", "status": "succeeded", "body": "Verdict: BLOCK"}],
    )
    env = {"PYTHONPATH": "src"}
    monkeypatch.setenv("PYTHONPATH", "src")
    output = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentflow_hermes.cli",
            "bridge",
            "kanban",
            "resolve-blocked",
            "--blocked-card",
            "card_blocked_abc",
            "--remediation-review",
            "card_review_def",
            "--input-file",
            str(fixture),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert output.returncode == 2
    result = json.loads(output.stdout)
    assert result["success"] is False
    assert result["error"] == "remediation_review_has_block_verdict"
