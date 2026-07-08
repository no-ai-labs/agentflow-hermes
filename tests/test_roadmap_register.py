from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import agentflow_hermes.roadmap_cli as roadmap_cli
from agentflow_hermes.cli import main as cli_main
from agentflow_hermes.roadmap_config import load_repo_roadmap_config

from test_roadmap_autopromoter_watchdog import _RecordingCliRunner, _task, _go_summary


def _run(argv, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFLOW_HOME", str(tmp_path))
    captured = io.StringIO()
    with redirect_stdout(captured):
        rc = cli_main(argv)
    text = captured.getvalue()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {"raw": text}
    return rc, data


def _init_argv(output, **overrides):
    argv = [
        "roadmap", "init-config",
        "--output", str(output),
        "--board", overrides.get("board", "contextops"),
        "--origin", overrides.get("origin", "Discord Devhub / #contextops"),
        "--transition", overrides.get("transition", "m1->m2.impl_review_fanin"),
        "--from", overrides.get("from_slice", "m1"),
        "--to", overrides.get("to_slice", "m2"),
    ]
    return argv


def test_init_config_scaffolds_loadable_config(monkeypatch, tmp_path):
    output = tmp_path / "agentflow-roadmap.yaml"
    rc, data = _run(_init_argv(output), monkeypatch, tmp_path)

    assert rc == 0
    assert data["success"] is True
    assert output.exists()

    config = load_repo_roadmap_config(str(output))
    assert config.enabled is True
    # apply_mode is disabled by default: generated config proposes, never writes
    # a board unless the operator explicitly arms it.
    assert config.apply_mode is False
    assert config.board == "contextops"
    assert config.allowed_transitions == ("m1->m2.impl_review_fanin",)
    transition = config.transitions["m1->m2.impl_review_fanin"]
    assert transition.from_slice == "m1"
    assert transition.to_slice == "m2"
    assert transition.slice_template == ("impl", "review", "fanin")
    assert config.expected_origin == "Discord Devhub / #contextops"
    assert config.expected_return_to == "Discord Devhub / #contextops"


def test_init_config_apply_mode_flag_arms_board_writes(monkeypatch, tmp_path):
    output = tmp_path / "roadmap.yaml"
    rc, data = _run(_init_argv(output) + ["--apply-mode"], monkeypatch, tmp_path)
    assert rc == 0
    config = load_repo_roadmap_config(str(output))
    assert config.apply_mode is True


def test_init_config_refuses_to_overwrite_without_force(monkeypatch, tmp_path):
    output = tmp_path / "roadmap.yaml"
    _run(_init_argv(output), monkeypatch, tmp_path)
    rc, data = _run(_init_argv(output), monkeypatch, tmp_path)
    assert rc != 0
    assert data["success"] is False
    assert data["error"] == "output_exists"

    rc2, data2 = _run(_init_argv(output) + ["--force"], monkeypatch, tmp_path)
    assert rc2 == 0
    assert data2["success"] is True


def test_init_config_rejects_unsafe_origin(monkeypatch, tmp_path):
    output = tmp_path / "roadmap.yaml"
    rc, data = _run(_init_argv(output, origin='bad"origin\nnewline'), monkeypatch, tmp_path)
    assert rc != 0
    assert data["success"] is False
    assert data["error"] == "invalid_value"
    assert not output.exists()


def test_generated_config_runs_dry_run_propose(monkeypatch, tmp_path):
    output = tmp_path / "roadmap.yaml"
    _run(
        _init_argv(
            output,
            board="agentflow-hermes",
            origin="discord:#hermes-main",
            transition="m14->m15.impl_review_fanin",
            from_slice="m14",
            to_slice="m15",
        ),
        monkeypatch,
        tmp_path,
    )

    task = _task(
        result=_go_summary(
            transition="m14->m15.impl_review_fanin",
            next_slice="m15",
            origin="discord:#hermes-main",
            return_to="discord:#hermes-main",
        )
    )
    runner = _RecordingCliRunner(tasks={"t_final_1": task})
    monkeypatch.setattr(roadmap_cli, "resolve_kanban_board_client", lambda: runner)

    rc, data = _run(
        ["roadmap", "promote", "--config", str(output), "--task", "t_final_1"],
        monkeypatch,
        tmp_path,
    )
    assert rc == 0
    assert data["action"] == "stabilize"
    assert data["verdict"] == "GO"
    roadmap = data["receipt"]["decision_payload"]["roadmap_autopromote"]
    assert roadmap["action"] == "propose"
    assert roadmap["applied"] is False
    assert roadmap["created_task_ids"] == []
    # dry-run (no --apply): no board create call
    assert not any("create" in c for c in runner.calls)


def _register_argv(config_path, registry_path):
    return [
        "roadmap", "register-watchdog",
        "--config", str(config_path),
        "--registry", str(registry_path),
    ]


def _make_config(monkeypatch, tmp_path, name="agentflow-roadmap.yaml", **overrides):
    output = tmp_path / name
    _run(_init_argv(output, **overrides), monkeypatch, tmp_path)
    return output


def test_register_watchdog_creates_registry_entry(monkeypatch, tmp_path):
    config = _make_config(monkeypatch, tmp_path)
    registry = tmp_path / "reg" / "roadmap-watchdog-configs.json"

    rc, data = _run(_register_argv(config, registry), monkeypatch, tmp_path)

    assert rc == 0
    assert data["success"] is True
    assert data["registered"] is True
    assert data["already_registered"] is False
    assert registry.exists()

    payload = json.loads(registry.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert isinstance(payload["configs"], list)
    assert len(payload["configs"]) == 1
    entry = payload["configs"][0]
    # The no-agent cron script keys on item["config"], not "path".
    assert entry["config"] == str(config.resolve())
    assert "path" not in entry
    assert entry["workdir"] == str(config.resolve().parent)
    assert entry["board"] == "contextops"
    assert entry["enabled"] is True
    assert entry["receipts_file"].endswith(".json")


def test_register_watchdog_idempotent_no_duplicate(monkeypatch, tmp_path):
    config = _make_config(monkeypatch, tmp_path)
    registry = tmp_path / "roadmap-watchdog-configs.json"

    _run(_register_argv(config, registry), monkeypatch, tmp_path)
    rc, data = _run(_register_argv(config, registry), monkeypatch, tmp_path)

    assert rc == 0
    assert data["success"] is True
    assert data["registered"] is False
    assert data["already_registered"] is True

    payload = json.loads(registry.read_text(encoding="utf-8"))
    assert len(payload["configs"]) == 1


def test_register_watchdog_two_configs_coexist(monkeypatch, tmp_path):
    config_a = _make_config(monkeypatch, tmp_path, name="a.yaml", board="contextops")
    config_b = _make_config(monkeypatch, tmp_path, name="b.yaml", board="research")
    registry = tmp_path / "roadmap-watchdog-configs.json"

    _run(_register_argv(config_a, registry), monkeypatch, tmp_path)
    _run(_register_argv(config_b, registry), monkeypatch, tmp_path)

    payload = json.loads(registry.read_text(encoding="utf-8"))
    boards = sorted(e["board"] for e in payload["configs"])
    assert boards == ["contextops", "research"]


def test_register_idempotent_against_legacy_path_entry(monkeypatch, tmp_path):
    # A registry written by an earlier CLI draft keyed entries on "path".
    # Registering the same config must dedupe against it, not double-add.
    config = _make_config(monkeypatch, tmp_path)
    registry = tmp_path / "roadmap-watchdog-configs.json"
    registry.write_text(
        json.dumps({"version": 1, "configs": [{"path": str(config.resolve())}]}),
        encoding="utf-8",
    )

    rc, data = _run(_register_argv(config, registry), monkeypatch, tmp_path)
    assert rc == 0
    assert data["already_registered"] is True
    payload = json.loads(registry.read_text(encoding="utf-8"))
    assert len(payload["configs"]) == 1


def test_unregister_removes_legacy_path_entry(monkeypatch, tmp_path):
    config = _make_config(monkeypatch, tmp_path)
    registry = tmp_path / "roadmap-watchdog-configs.json"
    registry.write_text(
        json.dumps({"version": 1, "configs": [{"path": str(config.resolve())}]}),
        encoding="utf-8",
    )

    rc, data = _run(
        ["roadmap", "unregister-watchdog", "--config", str(config), "--registry", str(registry)],
        monkeypatch,
        tmp_path,
    )
    assert rc == 0
    assert data["removed"] is True
    payload = json.loads(registry.read_text(encoding="utf-8"))
    assert payload["configs"] == []


def test_register_rejects_unloadable_config(monkeypatch, tmp_path):
    bad = tmp_path / "broken.yaml"
    # A transition entry that is a scalar (not a mapping) makes the loader raise.
    bad.write_text("board: contextops\ntransitions:\n  m1->m2: notamapping\n", encoding="utf-8")
    registry = tmp_path / "roadmap-watchdog-configs.json"

    rc, data = _run(_register_argv(bad, registry), monkeypatch, tmp_path)

    assert rc != 0
    assert data["success"] is False
    assert data["error"] == "malformed_config"
    assert not registry.exists()


def test_register_rejects_missing_config(monkeypatch, tmp_path):
    registry = tmp_path / "roadmap-watchdog-configs.json"
    rc, data = _run(_register_argv(tmp_path / "nope.yaml", registry), monkeypatch, tmp_path)
    assert rc != 0
    assert data["success"] is False
    assert data["error"] == "malformed_config"


def test_unregister_watchdog_removes_entry(monkeypatch, tmp_path):
    config = _make_config(monkeypatch, tmp_path)
    registry = tmp_path / "roadmap-watchdog-configs.json"
    _run(_register_argv(config, registry), monkeypatch, tmp_path)

    rc, data = _run(
        ["roadmap", "unregister-watchdog", "--config", str(config), "--registry", str(registry)],
        monkeypatch,
        tmp_path,
    )

    assert rc == 0
    assert data["success"] is True
    assert data["removed"] is True
    payload = json.loads(registry.read_text(encoding="utf-8"))
    assert payload["configs"] == []


def test_unregister_watchdog_absent_entry_is_noop(monkeypatch, tmp_path):
    config = _make_config(monkeypatch, tmp_path)
    registry = tmp_path / "roadmap-watchdog-configs.json"
    _run(_register_argv(config, registry), monkeypatch, tmp_path)

    other = _make_config(monkeypatch, tmp_path, name="other.yaml")
    rc, data = _run(
        ["roadmap", "unregister-watchdog", "--config", str(other), "--registry", str(registry)],
        monkeypatch,
        tmp_path,
    )

    assert rc == 0
    assert data["success"] is True
    assert data["removed"] is False
    payload = json.loads(registry.read_text(encoding="utf-8"))
    assert len(payload["configs"]) == 1
