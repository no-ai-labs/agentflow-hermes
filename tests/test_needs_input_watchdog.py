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

    # apply=false is a strictly side-effect-free preview (plan M27 blocker 1):
    # every real board's dry-run runs against an isolated throwaway copy, so a
    # first-sight board previews nothing (seeded-only) and stays silent...
    code1, out1 = wd.run_once(registry_path=wd._DEFAULT_REGISTRY, db_path=db, apply=False, all_kinds=False)
    assert code1 == 0
    assert out1 == ""

    # ...and the durable db_path is never even created by a dry-run.
    assert not db.exists()

    # Second cadence: still silent, still no durable side effects.
    code2, out2 = wd.run_once(registry_path=wd._DEFAULT_REGISTRY, db_path=db, apply=False, all_kinds=False)
    assert code2 == 0
    assert out2 == ""
    assert not db.exists()


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
