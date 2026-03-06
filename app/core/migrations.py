from sqlalchemy import text
from sqlalchemy.engine import Engine


def _column_exists(engine: Engine, table: str, column: str) -> bool:
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


def apply_sqlite_migrations(engine: Engine) -> None:
    # Minimal forward-only migration for existing local SQLite database.
    additions = [
        ("trades", "highest_price", "REAL"),
        ("trades", "trailing_active", "INTEGER NOT NULL DEFAULT 0"),
        ("trades", "trailing_stop_price", "REAL"),
    ]
    with engine.begin() as conn:
        for table, column, column_type in additions:
            exists = any(r[1] == column for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall())
            if not exists:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"))
