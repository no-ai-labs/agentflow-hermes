"""Production needs_input watchdog: registry-driven, silent when no events,
material line only on real owner-input creation. Never mutates a real board
(dry-run FakeBoardAdapter)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "agentflow_needs_input_watchdog.py"


def _load_watchdog():
    spec = importlib.util.spec_from_file_location("agentflow_needs_input_watchdog", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_watchdog_seeds_then_silent_on_no_new_events(tmp_path, capsys):
    wd = _load_watchdog()
    db = tmp_path / "agentflow.sqlite"

    # First cadence: every real board is seen for the first time -> seeded to
    # current max, no replay, no material creation -> silent.
    code1, out1 = wd.run_once(registry_path=wd._DEFAULT_REGISTRY, db_path=db, apply=False, all_kinds=False)
    assert code1 == 0
    assert out1 == ""

    # Second cadence: no genuinely new terminal events beyond the seed in this
    # test window -> still silent.
    code2, out2 = wd.run_once(registry_path=wd._DEFAULT_REGISTRY, db_path=db, apply=False, all_kinds=False)
    assert code2 == 0
    assert out2 == ""


def test_watchdog_blocks_on_empty_registry(tmp_path):
    wd = _load_watchdog()
    empty = tmp_path / "empty.yaml"
    empty.write_text("boards: {}\n", encoding="utf-8")
    code, out = wd.run_once(registry_path=empty, db_path=tmp_path / "db.sqlite", apply=False, all_kinds=False)
    assert code == 2
    assert "empty_or_unreadable_board_registry" in out


def test_watchdog_main_smoke_exit_zero(tmp_path):
    wd = _load_watchdog()
    db = tmp_path / "agentflow.sqlite"
    rc = wd.main(["--registry", str(wd._DEFAULT_REGISTRY), "--db", str(db)])
    assert rc == 0
