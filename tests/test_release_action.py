from __future__ import annotations

import json

import pytest

from agentflow_hermes.release_action import (
    ReleaseActionConfig,
    evaluate_release_action,
    load_receipts_ledger,
    load_release_action_config,
    parse_release_directive,
    save_receipts_ledger,
)

GO_SUMMARY = """Verdict: GO
Release-Action: github-release
Release-Version: v1.2.3
Release-Approved: true
Release-Title: v1.2.3 release
Release-Notes: Bugfixes and docs.
"""


def _enabled_config(**overrides) -> ReleaseActionConfig:
    fields = dict(
        release_actions_enabled=True,
        apply_mode=True,
        allowed_actions=("github-release",),
    )
    fields.update(overrides)
    return ReleaseActionConfig(**fields)


class FakeRunner:
    """Injectable fake git/gh runner. Records every argv it was called with."""

    def __init__(self, *, existing_tags: tuple[str, ...] = (), existing_releases: tuple[str, ...] = (), fail_on: str = ""):
        self.calls: list[list[str]] = []
        self.existing_tags = set(existing_tags)
        self.existing_releases = set(existing_releases)
        self.fail_on = fail_on

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        if argv[:2] == ["git", "tag"] and "-l" in argv:
            version = argv[-1]
            out = version if version in self.existing_tags else ""
            return 0, out, ""
        if argv[:3] == ["gh", "release", "view"]:
            version = argv[-1]
            return (0, "{}", "") if version in self.existing_releases else (1, "", "not found")
        if argv[:2] == ["git", "tag"]:
            if self.fail_on == "tag":
                return 1, "", "tag failed"
            return 0, "", ""
        if argv[:2] == ["git", "push"]:
            if self.fail_on == "push":
                return 1, "", "push failed"
            return 0, "", ""
        if argv[:3] == ["gh", "release", "create"]:
            if self.fail_on == "release":
                return 1, "", "release create failed"
            return 0, "", ""
        raise AssertionError(f"unexpected argv: {argv}")


# ---------------------------------------------------------------------------
# parse_release_directive
# ---------------------------------------------------------------------------


def test_parse_release_directive_reads_explicit_markers():
    directive = parse_release_directive(GO_SUMMARY, source_ref="task:t1")
    assert directive.verdict == "GO"
    assert directive.action == "github-release"
    assert directive.version == "v1.2.3"
    assert directive.approved is True
    assert directive.title == "v1.2.3 release"
    assert directive.notes == "Bugfixes and docs."
    assert directive.confidence == "explicit"


def test_parse_release_directive_prose_only_is_not_explicit():
    directive = parse_release_directive("Verdict: GO\nWe should probably cut a release soon.")
    assert directive.confidence == "none"


def test_parse_release_directive_missing_approval_marker_is_not_explicit():
    text = "Verdict: GO\nRelease-Action: github-release\nRelease-Version: v1.0.0\n"
    directive = parse_release_directive(text)
    assert directive.confidence == "none"
    assert directive.approved is False


# ---------------------------------------------------------------------------
# evaluate_release_action: dry-run default
# ---------------------------------------------------------------------------


def test_dry_run_is_the_default_even_with_apply_true_if_config_apply_mode_false():
    config = ReleaseActionConfig(release_actions_enabled=True, apply_mode=False, allowed_actions=("github-release",))
    ledger: dict = {}
    result = evaluate_release_action(GO_SUMMARY, config, ledger, apply=True, runner=FakeRunner())
    assert result["decision"] == "propose"
    assert result["success"] is True
    assert result["mutations"] == [
        {
            "action": "github-release",
            "version": "v1.2.3",
            "title": "v1.2.3 release",
            "notes": "Bugfixes and docs.",
            "target": "",
        }
    ]
    assert ledger == {}


def test_dry_run_without_apply_flag_even_when_config_apply_mode_true():
    config = _enabled_config()
    ledger: dict = {}
    result = evaluate_release_action(GO_SUMMARY, config, ledger, apply=False, runner=FakeRunner())
    assert result["decision"] == "propose"
    assert ledger == {}


def test_release_actions_disabled_by_default_noops_on_arbitrary_go():
    config = ReleaseActionConfig()
    result = evaluate_release_action(GO_SUMMARY, config, {}, apply=True, runner=FakeRunner())
    assert result["decision"] == "noop"
    assert result["reason"] == "release_actions_disabled"
    assert result["success"] is False


# ---------------------------------------------------------------------------
# evaluate_release_action: missing approval
# ---------------------------------------------------------------------------


def test_missing_approval_marker_refuses():
    text = "Verdict: GO\nRelease-Action: github-release\nRelease-Version: v1.2.3\n"
    config = _enabled_config()
    result = evaluate_release_action(text, config, {}, apply=True, runner=FakeRunner())
    assert result["decision"] == "refuse"
    assert result["reason"] == "missing_directive"


def test_release_approved_false_refuses_with_missing_approval_reason():
    text = "Verdict: GO\nRelease-Action: github-release\nRelease-Version: v1.2.3\nRelease-Approved: false\n"
    config = _enabled_config()
    result = evaluate_release_action(text, config, {}, apply=True, runner=FakeRunner())
    assert result["decision"] == "refuse"
    assert result["reason"] == "missing_approval"


def test_verdict_block_never_publishes():
    text = "Verdict: BLOCK\nRelease-Action: github-release\nRelease-Version: v1.2.3\nRelease-Approved: true\n"
    config = _enabled_config()
    result = evaluate_release_action(text, config, {}, apply=True, runner=FakeRunner())
    assert result["decision"] == "refuse"
    assert result["reason"] == "not_go"


def test_unknown_action_refuses():
    text = "Verdict: GO\nRelease-Action: npm-publish\nRelease-Version: v1.2.3\nRelease-Approved: true\n"
    config = _enabled_config()
    result = evaluate_release_action(text, config, {}, apply=True, runner=FakeRunner())
    assert result["decision"] == "refuse"
    assert result["reason"] == "unknown_action"


def test_invalid_version_refuses():
    text = "Verdict: GO\nRelease-Action: github-release\nRelease-Version: not-a-version\nRelease-Approved: true\n"
    config = _enabled_config()
    result = evaluate_release_action(text, config, {}, apply=True, runner=FakeRunner())
    assert result["decision"] == "refuse"
    assert result["reason"] == "invalid_version"


def test_allowed_versions_explicit_list_restricts():
    config = _enabled_config(allowed_versions=("v9.9.9",))
    result = evaluate_release_action(GO_SUMMARY, config, {}, apply=True, runner=FakeRunner())
    assert result["decision"] == "refuse"
    assert result["reason"] == "invalid_version"


# ---------------------------------------------------------------------------
# evaluate_release_action: duplicate protection
# ---------------------------------------------------------------------------


def test_duplicate_existing_tag_refuses_without_pushing():
    config = _enabled_config()
    runner = FakeRunner(existing_tags=("v1.2.3",))
    result = evaluate_release_action(GO_SUMMARY, config, {}, apply=True, runner=runner)
    assert result["decision"] == "refuse"
    assert result["reason"] == "duplicate_tag"
    assert not any(argv[:2] == ["git", "push"] for argv in runner.calls)
    assert not any(argv[:3] == ["gh", "release", "create"] for argv in runner.calls)


def test_duplicate_existing_release_refuses_without_pushing():
    config = _enabled_config()
    runner = FakeRunner(existing_releases=("v1.2.3",))
    result = evaluate_release_action(GO_SUMMARY, config, {}, apply=True, runner=runner)
    assert result["decision"] == "refuse"
    assert result["reason"] == "duplicate_release"
    assert not any(argv[:2] == ["git", "push"] for argv in runner.calls)


def test_local_ledger_receipt_short_circuits_before_touching_runner():
    config = _enabled_config()
    ledger = {
        "release:github-release:v1.2.3": {
            "action": "github-release",
            "version": "v1.2.3",
            "idempotency_key": "release:github-release:v1.2.3",
            "result": "success",
        }
    }
    runner = FakeRunner()
    result = evaluate_release_action(GO_SUMMARY, config, ledger, apply=True, runner=runner)
    assert result["decision"] == "noop"
    assert result["reason"] == "duplicate_receipt"
    assert runner.calls == []


# ---------------------------------------------------------------------------
# evaluate_release_action: successful fake apply + receipt
# ---------------------------------------------------------------------------


def test_successful_apply_writes_receipt_and_calls_expected_argv_sequence():
    config = _enabled_config()
    ledger: dict = {}
    runner = FakeRunner()
    result = evaluate_release_action(GO_SUMMARY, config, ledger, apply=True, runner=runner)

    assert result["decision"] == "apply"
    assert result["success"] is True
    key = "release:github-release:v1.2.3"
    assert result["idempotency_key"] == key
    assert result["receipt"] == {
        "action": "github-release",
        "version": "v1.2.3",
        "idempotency_key": key,
        "result": "success",
    }
    assert ledger[key] == result["receipt"]

    kinds = [tuple(argv[:2]) for argv in runner.calls]
    assert ("git", "tag") in kinds
    assert ("git", "push") in kinds
    assert any(argv[:3] == ["gh", "release", "create"] for argv in runner.calls)


def test_repeat_apply_run_with_persisted_ledger_does_not_double_publish():
    config = _enabled_config()
    ledger: dict = {}
    runner1 = FakeRunner()
    first = evaluate_release_action(GO_SUMMARY, config, ledger, apply=True, runner=runner1)
    assert first["decision"] == "apply"

    runner2 = FakeRunner()
    second = evaluate_release_action(GO_SUMMARY, config, ledger, apply=True, runner=runner2)
    assert second["decision"] == "noop"
    assert second["reason"] == "duplicate_receipt"
    assert runner2.calls == []


# ---------------------------------------------------------------------------
# evaluate_release_action: failure before receipt
# ---------------------------------------------------------------------------


def test_tag_failure_writes_no_receipt():
    config = _enabled_config()
    ledger: dict = {}
    runner = FakeRunner(fail_on="tag")
    result = evaluate_release_action(GO_SUMMARY, config, ledger, apply=True, runner=runner)
    assert result["decision"] == "refuse"
    assert result["reason"] == "tag_failed"
    assert result["success"] is False
    assert ledger == {}
    assert not any(argv[:2] == ["git", "push"] for argv in runner.calls)


def test_push_failure_writes_no_receipt():
    config = _enabled_config()
    ledger: dict = {}
    runner = FakeRunner(fail_on="push")
    result = evaluate_release_action(GO_SUMMARY, config, ledger, apply=True, runner=runner)
    assert result["decision"] == "refuse"
    assert result["reason"] == "push_failed"
    assert ledger == {}
    assert not any(argv[:3] == ["gh", "release", "create"] for argv in runner.calls)


def test_release_create_failure_writes_no_receipt():
    config = _enabled_config()
    ledger: dict = {}
    runner = FakeRunner(fail_on="release")
    result = evaluate_release_action(GO_SUMMARY, config, ledger, apply=True, runner=runner)
    assert result["decision"] == "refuse"
    assert result["reason"] == "release_create_failed"
    assert ledger == {}


def test_apply_without_runner_refuses():
    config = _enabled_config()
    result = evaluate_release_action(GO_SUMMARY, config, {}, apply=True, runner=None)
    assert result["decision"] == "refuse"
    assert result["reason"] == "no_runner"


# ---------------------------------------------------------------------------
# config loading
# ---------------------------------------------------------------------------


def test_load_release_action_config_json(tmp_path):
    path = tmp_path / "release_config.json"
    path.write_text(json.dumps({
        "release_actions_enabled": True,
        "apply_mode": True,
        "allowed_actions": ["github-release"],
        "allowed_version_pattern": r"^v\d+\.\d+\.\d+$",
    }))
    config = load_release_action_config(path)
    assert config.release_actions_enabled is True
    assert config.apply_mode is True
    assert config.allowed_actions == ("github-release",)
    assert config.allowed_version_pattern == r"^v\d+\.\d+\.\d+$"


def test_load_release_action_config_defaults_are_disabled(tmp_path):
    path = tmp_path / "release_config.json"
    path.write_text("{}")
    config = load_release_action_config(path)
    assert config.release_actions_enabled is False
    assert config.apply_mode is False
    assert config.allowed_actions == ()


def test_load_release_action_config_malformed_root_raises(tmp_path):
    path = tmp_path / "release_config.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(ValueError):
        load_release_action_config(path)


# ---------------------------------------------------------------------------
# receipts ledger file round-trip
# ---------------------------------------------------------------------------


def test_receipts_ledger_round_trip(tmp_path):
    path = tmp_path / "receipts.json"
    assert load_receipts_ledger(path) == {}
    save_receipts_ledger(path, {"release:github-release:v1.0.0": {"result": "success"}})
    assert load_receipts_ledger(path) == {"release:github-release:v1.0.0": {"result": "success"}}


def test_receipts_ledger_tolerates_missing_file(tmp_path):
    assert load_receipts_ledger(tmp_path / "missing.json") == {}


def test_receipts_ledger_tolerates_malformed_file(tmp_path):
    path = tmp_path / "receipts.json"
    path.write_text("not json")
    assert load_receipts_ledger(path) == {}
