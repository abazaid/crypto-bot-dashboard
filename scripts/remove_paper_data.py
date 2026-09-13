#!/usr/bin/env python3
"""Remove all paper-trading data from the shared trading database.

Manual maintenance tool — NEVER runs automatically at startup.
Run it ONCE, while the application is stopped, after deploying the live-only code.

The database file keeps its historical name (paper_trading_v2.db) because the
production Docker volume mounts it by that path. This script only removes rows,
never renames or moves the file.

Usage:
    python scripts/remove_paper_data.py                  # dry-run (default, safe)
    python scripts/remove_paper_data.py --apply --yes    # actually delete
    python scripts/remove_paper_data.py --apply --yes --keep-smart-logs
    python scripts/remove_paper_data.py --db /data/db/paper_trading_v2.db

A timestamped backup is taken before any deletion; the script aborts if the
backup fails. Live rows (mode='live', LIVE_*/SYSTEM logs) are never touched.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("remove_paper_data")

# (label, table, WHERE clause) — executed in this exact child-first order.
# Paper campaigns own positions/rules/dca-states, so those delete via subquery.
PAPER_ROW_DELETES: list[tuple[str, str, str]] = [
    (
        "position_dca_states (paper campaigns)",
        "position_dca_states",
        "position_id IN (SELECT p.id FROM positions p "
        "JOIN campaigns c ON p.campaign_id = c.id WHERE c.mode = 'paper')",
    ),
    (
        "positions (paper campaigns)",
        "positions",
        "campaign_id IN (SELECT id FROM campaigns WHERE mode = 'paper')",
    ),
    (
        "dca_rules (paper campaigns)",
        "dca_rules",
        "campaign_id IN (SELECT id FROM campaigns WHERE mode = 'paper')",
    ),
    ("smart_runtime_states (paper)", "smart_runtime_states", "mode = 'paper'"),
    ("market_snapshots (paper)", "market_snapshots", "mode = 'paper'"),
    ("campaigns (paper)", "campaigns", "mode = 'paper'"),
    (
        "accumulation_trades (paper plans)",
        "accumulation_trades",
        "plan_id IN (SELECT id FROM accumulation_plans WHERE mode = 'paper')",
    ),
    ("accumulation_plans (paper)", "accumulation_plans", "mode = 'paper'"),
    (
        "grid_trades (paper bots)",
        "grid_trades",
        "bot_id IN (SELECT id FROM grid_bots WHERE mode = 'paper')",
    ),
    ("grid_bots (paper)", "grid_bots", "mode = 'paper'"),
    ("app_settings (paper keys)", "app_settings", "key LIKE 'paper%'"),
]

# Legacy paper-only smart campaign tables (dropped whole, not row-filtered).
PAPER_DROP_TABLES: list[str] = ["smart_positions", "smart_campaigns"]

# Live counters printed before/after to prove nothing live was touched.
LIVE_COUNT_CHECKS: list[tuple[str, str, Any]] = [
    ("campaigns (live)", "SELECT COUNT(*) FROM campaigns WHERE mode = 'live'", ()),
    (
        "positions (live campaigns)",
        "SELECT COUNT(*) FROM positions WHERE campaign_id IN "
        "(SELECT id FROM campaigns WHERE mode = 'live')",
        (),
    ),
    ("accumulation_plans (live)", "SELECT COUNT(*) FROM accumulation_plans WHERE mode = 'live'", ()),
    ("grid_bots (live)", "SELECT COUNT(*) FROM grid_bots WHERE mode = 'live'", ()),
    ("activity_logs (LIVE_*)", "SELECT COUNT(*) FROM activity_logs WHERE event_type LIKE 'LIVE_%'", ()),
    ("activity_logs (SYSTEM)", "SELECT COUNT(*) FROM activity_logs WHERE event_type = 'SYSTEM'", ()),
]


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def default_db_path() -> Path:
    """Resolve the default DB path from app settings without importing FastAPI app."""
    try:
        repo_root = str(Path(__file__).resolve().parents[1])
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from app.core.config import settings

        url = str(settings.database_url)
    except Exception as exc:  # pragma: no cover - offline fallback
        logger.warning("Could not load app settings (%s); falling back to local DB file.", exc)
        url = "sqlite:///./paper_trading_v2.db"
    if url.startswith("sqlite:///"):
        url = url[len("sqlite:///") :]
    return Path(url)


def table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    row = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def activity_delete_clause(keep_smart_logs: bool) -> tuple[str, str, str]:
    where = "event_type NOT LIKE 'LIVE_%' AND event_type <> 'SYSTEM'"
    if keep_smart_logs:
        where += " AND event_type NOT LIKE 'SMART_%'"
    return ("activity_logs (non-live)", "activity_logs", where)


def live_counts(cur: sqlite3.Cursor) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, sql, params in LIVE_COUNT_CHECKS:
        table = sql.split("FROM ")[1].split(" ")[0]
        if table_exists(cur, table):
            counts[label] = int(cur.execute(sql, params).fetchone()[0])
    return counts


def backup_database(db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.backup-{stamp}")
    shutil.copy2(db_path, backup_path)
    if not backup_path.exists() or backup_path.stat().st_size == 0:
        raise RuntimeError(f"Backup verification failed: {backup_path}")
    return backup_path


def plan_deletes(cur: sqlite3.Cursor, keep_smart_logs: bool) -> list[tuple[str, str, str, int]]:
    """Count rows each step would delete, skipping tables that do not exist."""
    plan: list[tuple[str, str, str, int]] = []
    steps = PAPER_ROW_DELETES + [activity_delete_clause(keep_smart_logs)]
    for label, table, where in steps:
        if not table_exists(cur, table):
            logger.info("SKIP  %-42s table '%s' does not exist", label, table)
            continue
        count = int(cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0])
        plan.append((label, table, where, count))
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=None, help="Path to the SQLite DB file")
    parser.add_argument("--dry-run", action="store_true", help="Default mode; prints plan only")
    parser.add_argument("--apply", action="store_true", help="Actually delete data")
    parser.add_argument("--yes", action="store_true", help="Non-interactive confirmation for --apply")
    parser.add_argument(
        "--keep-smart-logs",
        action="store_true",
        help="Keep SMART_* activity logs (mixed paper/live provenance)",
    )
    args = parser.parse_args(argv)

    configure_logging()
    db_path = args.db if args.db is not None else default_db_path()
    if not db_path.exists():
        logger.error("Database file not found: %s", db_path)
        return 2

    logger.info("Database: %s", db_path)
    logger.info("Mode: %s", "APPLY (will delete)" if args.apply else "DRY-RUN (no changes)")
    if args.keep_smart_logs:
        logger.info("SMART_* activity logs will be KEPT (--keep-smart-logs)")

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    try:
        size_before = db_path.stat().st_size
        before = live_counts(cur)
        plan = plan_deletes(cur, args.keep_smart_logs)

        drops = [t for t in PAPER_DROP_TABLES if table_exists(cur, t)]
        drop_counts = {
            t: int(cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in drops
        }
        smart_log_rows = int(
            cur.execute("SELECT COUNT(*) FROM activity_logs WHERE event_type LIKE 'SMART_%'")
            .fetchone()[0]
        ) if table_exists(cur, "activity_logs") else 0

        logger.info("")
        logger.info("Planned deletions:")
        total = 0
        for label, _table, _where, count in plan:
            logger.info("  %-46s %6d rows", label, count)
            total += count
        for t in drops:
            logger.info("  DROP TABLE %-37s %6d rows", t, drop_counts[t])
            total += drop_counts[t]
        logger.info("  %-46s %6d rows total", "TOTAL", total)
        if not args.keep_smart_logs and smart_log_rows:
            logger.info(
                "  NOTE: %d SMART_* log rows are included above; pass --keep-smart-logs to keep them.",
                smart_log_rows,
            )

        logger.info("")
        logger.info("Live data (must be identical after the run):")
        for label, count in before.items():
            logger.info("  %-46s %6d rows", label, count)

        if not args.apply:
            logger.info("")
            logger.info("DRY-RUN complete. Re-run with --apply --yes to perform the deletion.")
            return 0

        if not args.yes:
            logger.error("--apply requires --yes (destructive, one-shot operation).")
            return 2

        backup_path = backup_database(db_path)
        logger.info("")
        logger.info("Backup written: %s", backup_path)

        logger.info("Deleting...")
        deleted: dict[str, int] = {}
        for label, table, where, _count in plan:
            cur.execute(f"DELETE FROM {table} WHERE {where}")
            deleted[label] = cur.rowcount
            logger.info("  %-46s %6d rows deleted", label, deleted[label])
        for t in drops:
            cur.execute(f"DROP TABLE IF EXISTS {t}")
            logger.info("  DROP TABLE %-37s done", t)

        con.commit()
    finally:
        con.close()

    # VACUUM must run outside any transaction — use a fresh autocommit connection.
    vacuum_con = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        vacuum_con.execute("VACUUM")
    finally:
        vacuum_con.close()

    size_after = db_path.stat().st_size
    logger.info("")
    logger.info("VACUUM done. File size: %s -> %s (freed %s)",
                f"{size_before:,}", f"{size_after:,}", f"{max(0, size_before - size_after):,}")

    verify_con = sqlite3.connect(str(db_path))
    try:
        verify_cur = verify_con.cursor()
        after = live_counts(verify_cur)
        mismatch = [k for k in before if before.get(k) != after.get(k)]
        integrity = verify_cur.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        verify_con.close()

    for label, count in after.items():
        logger.info("  %-46s %6d rows (was %d)", label, count, before.get(label, -1))
    if mismatch:
        logger.error("LIVE COUNT MISMATCH for: %s — restore the backup!", ", ".join(mismatch))
        return 1
    if integrity != "ok":
        logger.error("PRAGMA integrity_check = %s — restore the backup!", integrity)
        return 1

    logger.info("All live counts unchanged. integrity_check ok. Paper data removed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
