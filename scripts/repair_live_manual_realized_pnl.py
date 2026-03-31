#!/usr/bin/env python
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable


def _sqlite_path_from_database_url(database_url: str) -> str | None:
    # Supports: sqlite:///./file.db or sqlite:////abs/path.db
    if not database_url or not database_url.startswith("sqlite:///"):
        return None
    raw = database_url.replace("sqlite:///", "", 1)
    if raw.startswith("/"):
        return raw
    return str(Path.cwd() / raw)


def _find_existing_db(candidates: Iterable[str]) -> str | None:
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _column_exists(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(r[1]).lower() == column.lower() for r in rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repair wrong realized PnL for old LIVE manual close rows (MANUAL_SELL / MANUAL_ALL_COINS)."
    )
    parser.add_argument("--db", default="", help="Path to sqlite db file. If omitted, auto-detect from DATABASE_URL.")
    parser.add_argument("--apply", action="store_true", help="Apply updates. Without this flag, runs in dry-run mode.")
    parser.add_argument("--eps", type=float, default=0.01, help="Minimum absolute diff to consider row mismatched.")
    args = parser.parse_args()

    db_path = args.db.strip()
    if not db_path:
        env_db_url = os.getenv("DATABASE_URL", "").strip()
        from_env = _sqlite_path_from_database_url(env_db_url)
        db_path = _find_existing_db(
            [
                from_env or "",
                str(Path.cwd() / "paper_trading_v2.db"),
                str(Path.cwd() / "paper_trading.db"),
                "/data/paper_trading_v2.db",
                "/data/paper_trading.db",
            ]
        ) or ""

    if not db_path or not os.path.exists(db_path):
        print("No sqlite database found. Pass --db explicitly.")
        return 1

    print(f"Using DB: {db_path}")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    has_close_fee = _column_exists(cur, "positions", "close_fee_usdt")
    if not has_close_fee:
        print("Warning: positions.close_fee_usdt is missing; fee will be treated as 0.0 for repair.")

    fee_sql = "COALESCE(p.close_fee_usdt, 0.0) AS close_fee_usdt" if has_close_fee else "0.0 AS close_fee_usdt"
    rows = cur.execute(
        f"""
        SELECT
            p.id,
            c.name AS campaign_name,
            p.symbol,
            p.close_reason,
            p.total_invested_usdt,
            p.total_qty,
            p.close_price,
            {fee_sql},
            COALESCE(p.realized_pnl_usdt, 0.0) AS realized_pnl_usdt
        FROM positions p
        JOIN campaigns c ON c.id = p.campaign_id
        WHERE
            c.mode = 'live'
            AND p.status = 'closed'
            AND p.close_reason IN ('MANUAL_SELL', 'MANUAL_ALL_COINS')
        ORDER BY p.id ASC
        """
    ).fetchall()

    to_update: list[tuple[float, int]] = []
    skipped_unrecoverable = 0
    mismatched_preview = []

    for r in rows:
        invested = float(r["total_invested_usdt"] or 0.0)
        qty = float(r["total_qty"] or 0.0)
        close_price = float(r["close_price"] or 0.0)
        close_fee = float(r["close_fee_usdt"] or 0.0)
        stored = float(r["realized_pnl_usdt"] or 0.0)

        # Repair is only reliable when invested/qty/close_price are still present.
        if invested <= 0.0 or qty <= 0.0 or close_price <= 0.0:
            skipped_unrecoverable += 1
            continue

        expected = (close_price * qty) - invested - close_fee
        if abs(expected - stored) >= float(args.eps):
            to_update.append((expected, int(r["id"])))
            if len(mismatched_preview) < 12:
                mismatched_preview.append(
                    (
                        int(r["id"]),
                        str(r["campaign_name"]),
                        str(r["symbol"]),
                        str(r["close_reason"]),
                        round(stored, 6),
                        round(expected, 6),
                    )
                )

    print(f"Closed live manual rows scanned: {len(rows)}")
    print(f"Rows fixable + mismatched: {len(to_update)}")
    print(f"Rows skipped (not enough data to recover): {skipped_unrecoverable}")

    if mismatched_preview:
        print("\nPreview (id, campaign, symbol, reason, stored, expected):")
        for p in mismatched_preview:
            print(" ", p)

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to write updates.")
        con.close()
        return 0

    # Backup before writing.
    backup = f"{db_path}.bak.{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(db_path, backup)
    print(f"Backup created: {backup}")

    cur.executemany("UPDATE positions SET realized_pnl_usdt = ? WHERE id = ?", to_update)
    con.commit()
    print(f"Updated rows: {len(to_update)}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
