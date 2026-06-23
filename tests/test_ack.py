import pytest

from agentflow_hermes.ack import AckError, parse_ack_block, validate_ack
from agentflow_hermes.states import JobStatus


def test_parse_ack_block_extracts_fields():
    text = """[JOB ACK]
job_id: job_abc
status: succeeded
summary: done
artifacts: file.txt
blockers: none
"""
    fields = parse_ack_block(text)
    assert fields["job_id"] == "job_abc"
    assert fields["status"] == "succeeded"
    assert fields["summary"] == "done"


def test_parse_ack_block_preserves_multiline_prompt_artifacts_and_blockers():
    text = """Worker output before ack.

[JOB ACK]
job_id: job_abc
status: succeeded
summary: M1 store/ACK implementation complete; pytest passed.
artifacts:
- src/agentflow_hermes/ack.py
- tests/test_ack.py
- uv run pytest -q -> 27 passed
blockers:
- none
"""
    fields = parse_ack_block(text)
    payload = validate_ack(fields)

    assert payload.job_id == "job_abc"
    assert payload.status == JobStatus.SUCCEEDED
    assert payload.summary == "M1 store/ACK implementation complete; pytest passed."
    assert payload.artifacts == "\n".join(
        [
            "- src/agentflow_hermes/ack.py",
            "- tests/test_ack.py",
            "- uv run pytest -q -> 27 passed",
        ]
    )
    assert payload.blockers == "- none"
    assert payload.raw_fields["artifacts"] == payload.artifacts
    assert payload.raw_fields["blockers"] == payload.blockers


def test_parse_ack_block_ignores_unmarked_trailing_prose():
    text = """[JOB ACK]
job_id: job_abc
status: succeeded
summary: done
blockers: none
This sentence is outside the structured ACK payload.
"""
    fields = parse_ack_block(text)

    assert fields["blockers"] == "none"


def test_parse_ack_block_missing_block():
    with pytest.raises(AckError) as exc:
        parse_ack_block("no block here")
    assert exc.value.reason == "missing [JOB ACK] block"


def test_validate_ack_ok():
    payload = validate_ack({"job_id": "j1", "status": "succeeded", "summary": "done"})
    assert payload.job_id == "j1"
    assert payload.status == JobStatus.SUCCEEDED


def test_validate_ack_missing_job_id():
    with pytest.raises(AckError) as exc:
        validate_ack({"status": "succeeded"})
    assert exc.value.reason == "missing_job_id"


def test_validate_ack_missing_status():
    with pytest.raises(AckError) as exc:
        validate_ack({"job_id": "j1"})
    assert exc.value.reason == "missing_status"


def test_validate_ack_invalid_status():
    with pytest.raises(AckError) as exc:
        validate_ack({"job_id": "j1", "status": "banana"})
    assert exc.value.reason == "invalid_status"
    assert exc.value.deadletter is True


def test_validate_ack_dispatched_not_allowed():
    with pytest.raises(AckError) as exc:
        validate_ack({"job_id": "j1", "status": "dispatched"})
    assert exc.value.reason == "invalid_status"
