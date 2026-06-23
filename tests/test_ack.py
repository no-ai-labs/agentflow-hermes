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
