#!/usr/bin/env python3
"""M27 final remediation (blocker 4): quarantine the legacy continuation-store
incident rows left behind by the pre-fix dry-run leakage, with a full auditable
before/after receipt.

Background
----------
Before the side-effect-free dry-run fix, the retired cron watchdog
(``aa7adad4350b``) ran ``agentflow_needs_input_watchdog.py`` in dry-run against
the LEGACY continuation store
(``~/.hermes/state/agentflow_needs_input_continuations.sqlite``) and its writes
leaked durably. That produced three phantom ``continuation_instances`` (the
``t_m27live_*`` probe task ids, whose *legitimate* home is the canonical
daemon store ``agentflow-daemon.sqlite``) plus their ``board_outbox`` anchors/
subscriptions. Those legacy rows have no owner work behind them and must not be
mirrored anywhere but the canonical store.

What this does
--------------
Selects ONLY the incident instances — those whose ``source_task_id`` matches the
``t_m27live_`` probe prefix, i.e. rows that should live solely in the canonical
store — moves every incident row (the instances plus all rows in dependent
tables keyed by ``continuation_id``) into an in-DB ``quarantined_incident_rows``
audit table as JSON, then deletes them from the live tables. Legitimate legacy
instances (1-5) and every canonical row are never touched. Idempotent: a second
run finds nothing to quarantine.

Safety: read-only/plan by default. ``--apply`` performs the quarantine inside a
single transaction. Writes a JSON receipt under ``artifacts/m27-remediation/``.
No network, no board mutation, no secrets — refs/ids/states only.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_LEGACY_DB = Path("/home/duckran/.hermes/state/agentflow_needs_input_continuations.sqlite")
_DEFAULT_RECEIPT_DIR = _REPO / "artifacts" / "m27-remediation"

# The probe task-id prefix that identifies a dry-run-leaked incident row in the
# LEGACY store. These tasks are created by the live apply daemon and belong only
# in the canonical store; any copy in the legacy store is leakage.
_INCIDENT_TASK_PREFIX = "t_m27live_"

# Every table that references a continuation via ``continuation_id`` and must be
# quarantined alongside its instance.
_DEPENDENT_TABLES = (
    "board_outbox",
    "continuation_events",
    "continuation_steps",
    "owner_input_receipts",
    "requirement_satisfactions",
    "external_wait_conditions",
    "interaction_members",
)

_QUARANTINE_DDL = """
create table if not exists quarantined_incident_rows (
    id integer primary key autoincrement,
    source_table text not null,
    continuation_id integer,
    row_json text not null,
    reason text not null default '',
    quarantined_at real not null
);
"""


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "select 1 from sqlite_master where type='table' and name=?", (name,)
    ).fetchone() is not None


def _all_table_counts(con: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for (name,) in con.execute("select name from sqlite_master where type='table' order by name"):
        counts[name] = int(con.execute(f"select count(*) from {name}").fetchone()[0])
    return counts


def _incident_instance_ids(con: sqlite3.Connection) -> list[int]:
    rows = con.execute(
        "select id from continuation_instances where source_task_id like ? order by id",
        (_INCIDENT_TASK_PREFIX + "%",),
    ).fetchall()
    return [int(r[0]) for r in rows]


def quarantine(*, legacy_db: Path, apply: bool, receipt_dir: Path) -> dict:
    if not legacy_db.exists():
        return {"success": True, "quarantined": False, "reason": "legacy_db_missing", "legacy_db": str(legacy_db)}

    con = sqlite3.connect(legacy_db)
    con.row_factory = sqlite3.Row
    try:
        if not _table_exists(con, "continuation_instances"):
            return {"success": True, "quarantined": False, "reason": "no_continuation_tables", "legacy_db": str(legacy_db)}

        counts_before = _all_table_counts(con)
        incident_ids = _incident_instance_ids(con)

        # Gather full incident payloads for the receipt (and, on apply, the
        # in-DB quarantine table) before deleting anything.
        moved: dict[str, list[dict]] = {"continuation_instances": []}
        preserved_instances = [
            dict(r) for r in con.execute(
                "select id, board, source_task_id, state from continuation_instances "
                "where source_task_id not like ? order by id",
                (_INCIDENT_TASK_PREFIX + "%",),
            ).fetchall()
        ]
        if incident_ids:
            placeholders = ",".join("?" for _ in incident_ids)
            for r in con.execute(
                f"select * from continuation_instances where id in ({placeholders}) order by id", incident_ids
            ).fetchall():
                moved["continuation_instances"].append(dict(r))
            for table in _DEPENDENT_TABLES:
                if not _table_exists(con, table):
                    continue
                rows = con.execute(
                    f"select * from {table} where continuation_id in ({placeholders}) order by id", incident_ids
                ).fetchall()
                if rows:
                    moved[table] = [dict(r) for r in rows]

        if apply and incident_ids:
            now = time.time()
            placeholders = ",".join("?" for _ in incident_ids)
            con.executescript(_QUARANTINE_DDL)
            with con:
                for table, rows in moved.items():
                    for row in rows:
                        con.execute(
                            "insert into quarantined_incident_rows(source_table, continuation_id, row_json, reason, quarantined_at) "
                            "values(?,?,?,?,?)",
                            (
                                table,
                                row.get("continuation_id", row.get("id")) if table != "continuation_instances" else row.get("id"),
                                json.dumps(row, ensure_ascii=False, default=str),
                                "dry_run_leakage_incident_no_owner_work",
                                now,
                            ),
                        )
                for table in _DEPENDENT_TABLES:
                    if _table_exists(con, table):
                        con.execute(f"delete from {table} where continuation_id in ({placeholders})", incident_ids)
                con.execute(f"delete from continuation_instances where id in ({placeholders})", incident_ids)

        counts_after = _all_table_counts(con) if apply else counts_before

        receipt = {
            "success": True,
            "quarantined": bool(apply and incident_ids),
            "apply": apply,
            "legacy_db": str(legacy_db),
            "incident_task_prefix": _INCIDENT_TASK_PREFIX,
            "incident_instance_ids": incident_ids,
            "incident_outbox_ids": [r["id"] for r in moved.get("board_outbox", [])],
            "preserved_legit_instances": preserved_instances,
            "moved_rows": moved,
            "counts_before": counts_before,
            "counts_after": counts_after,
            "generated_at": time.time(),
        }
    finally:
        con.close()

    receipt_dir.mkdir(parents=True, exist_ok=True)
    tag = "applied" if (apply and incident_ids) else ("noop" if apply else "plan")
    receipt_path = receipt_dir / f"legacy_incident_quarantine_receipt.{tag}.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    receipt["receipt_path"] = str(receipt_path)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-db", default=str(_DEFAULT_LEGACY_DB))
    parser.add_argument("--receipt-dir", default=str(_DEFAULT_RECEIPT_DIR))
    parser.add_argument("--apply", action="store_true", help="Perform the quarantine (default: plan/receipt only).")
    args = parser.parse_args(argv)

    result = quarantine(legacy_db=Path(args.legacy_db), apply=args.apply, receipt_dir=Path(args.receipt_dir))
    print(json.dumps({k: v for k, v in result.items() if k != "moved_rows"}, indent=2, ensure_ascii=False, default=str))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
