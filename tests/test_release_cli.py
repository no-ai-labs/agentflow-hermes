from __future__ import annotations

import argparse
import json

from agentflow_hermes import release_cli
from agentflow_hermes.release_cli import add_release_github_args, run_release_github

GO_SUMMARY = """Verdict: GO
Release-Action: github-release
Release-Version: v2.0.0
Release-Approved: true
"""


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_release_github_args(parser)
    return parser.parse_args(argv)


def _write_config(tmp_path, **overrides):
    payload = {
        "release_actions_enabled": True,
        "apply_mode": True,
        "allowed_actions": ["github-release"],
    }
    payload.update(overrides)
    path = tmp_path / "release_config.json"
    path.write_text(json.dumps(payload))
    return path


def test_no_config_is_a_safe_noop(tmp_path):
    summary_file = tmp_path / "summary.txt"
    summary_file.write_text(GO_SUMMARY)
    args = _parse_args(["--summary-file", str(summary_file), "--apply"])
    rc, report = run_release_github(args)
    assert rc == 0
    assert report["decision"] == "noop"
    assert report["reason"] == "release_actions_disabled"


def test_dry_run_via_cli_never_calls_real_runner(tmp_path, monkeypatch):
    summary_file = tmp_path / "summary.txt"
    summary_file.write_text(GO_SUMMARY)
    config_path = _write_config(tmp_path)

    called = []

    def _boom(argv):
        called.append(argv)
        raise AssertionError("real runner must not be invoked in dry-run")

    monkeypatch.setattr(release_cli, "default_release_cli_runner", _boom)

    args = _parse_args(["--summary-file", str(summary_file), "--config", str(config_path)])
    rc, report = run_release_github(args)
    assert rc == 0
    assert report["decision"] == "propose"
    assert called == []


def test_apply_via_cli_uses_injected_runner_and_persists_receipt(tmp_path, monkeypatch):
    summary_file = tmp_path / "summary.txt"
    summary_file.write_text(GO_SUMMARY)
    config_path = _write_config(tmp_path)
    receipts_path = tmp_path / "receipts.json"

    calls = []

    def _fake_runner(argv):
        calls.append(argv)
        if argv[:2] == ["git", "tag"] and "-l" in argv:
            return 0, "", ""
        if argv[:3] == ["gh", "release", "view"]:
            return 1, "", "not found"
        return 0, "", ""

    monkeypatch.setattr(release_cli, "default_release_cli_runner", _fake_runner)

    args = _parse_args([
        "--summary-file", str(summary_file),
        "--config", str(config_path),
        "--receipts-file", str(receipts_path),
        "--apply",
    ])
    rc, report = run_release_github(args)
    assert rc == 0
    assert report["decision"] == "apply"
    assert calls, "expected the fake runner to be invoked"

    persisted = json.loads(receipts_path.read_text())
    assert persisted["release:github-release:v2.0.0"]["result"] == "success"

    # Re-run: must not call the runner again, since the receipt already exists.
    calls.clear()
    rc2, report2 = run_release_github(args)
    assert rc2 == 0
    assert report2["decision"] == "noop"
    assert report2["reason"] == "duplicate_receipt"
    assert calls == []


def test_malformed_config_fails_closed(tmp_path):
    summary_file = tmp_path / "summary.txt"
    summary_file.write_text(GO_SUMMARY)
    config_path = tmp_path / "bad.json"
    config_path.write_text("[1, 2]")

    args = _parse_args(["--summary-file", str(summary_file), "--config", str(config_path)])
    rc, report = run_release_github(args)
    assert rc == 2
    assert report["error"] == "malformed_config"


def test_missing_summary_file_fails_closed(tmp_path):
    args = _parse_args(["--summary-file", str(tmp_path / "missing.txt")])
    rc, report = run_release_github(args)
    assert rc == 2
    assert report["error"] == "summary_read_failed"
