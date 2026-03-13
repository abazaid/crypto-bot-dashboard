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
        ("trades", "live_entry_fee_usdt", "REAL"),
        ("trades", "live_exit_fee_usdt", "REAL"),
        ("trades", "live_entry_order_id", "VARCHAR(40)"),
        ("trades", "live_exit_order_id", "VARCHAR(40)"),
    ]
    with engine.begin() as conn:
        for table, column, column_type in additions:
            exists = any(r[1] == column for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall())
            if not exists:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS market_observations (
                    id INTEGER PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    observed_at DATETIME NOT NULL,
                    last_price REAL NOT NULL DEFAULT 0.0,
                    volume_24h REAL NOT NULL DEFAULT 0.0,
                    spread_pct REAL NOT NULL DEFAULT 0.0,
                    score REAL NOT NULL DEFAULT 0.0,
                    trend_status VARCHAR(20) NOT NULL DEFAULT 'Neutral',
                    signal_status VARCHAR(30) NOT NULL DEFAULT 'No Data',
                    decision_reason VARCHAR(120) NOT NULL DEFAULT '-'
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_market_observations_symbol ON market_observations(symbol)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_market_observations_observed_at ON market_observations(observed_at)"))

        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS shadow_trades (
                    id INTEGER PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'open',
                    entry_time DATETIME NOT NULL,
                    exit_time DATETIME,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    quantity REAL NOT NULL,
                    notional_usdt REAL NOT NULL DEFAULT 0.0,
                    tp_price REAL NOT NULL,
                    sl_price REAL NOT NULL,
                    pnl REAL,
                    pnl_pct REAL,
                    exit_reason VARCHAR(50),
                    source_score REAL NOT NULL DEFAULT 0.0
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_shadow_trades_symbol ON shadow_trades(symbol)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_shadow_trades_status ON shadow_trades(status)"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ai_trades (
                    id INTEGER PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'open',
                    entry_time DATETIME NOT NULL,
                    exit_time DATETIME,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    quantity REAL NOT NULL,
                    notional_usdt REAL NOT NULL DEFAULT 0.0,
                    tp_price REAL NOT NULL,
                    sl_price REAL NOT NULL,
                    trailing_stop_price REAL,
                    trailing_active INTEGER NOT NULL DEFAULT 0,
                    highest_price REAL,
                    pnl REAL,
                    pnl_pct REAL,
                    exit_reason VARCHAR(50),
                    strategy_id VARCHAR(120) NOT NULL DEFAULT '-',
                    strategy_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_trades_symbol ON ai_trades(symbol)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_trades_status ON ai_trades(status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_trades_strategy_id ON ai_trades(strategy_id)"))
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(ai_trades)")).fetchall()]
        if "ai_provider" not in cols:
            conn.execute(text("ALTER TABLE ai_trades ADD COLUMN ai_provider VARCHAR(20) NOT NULL DEFAULT 'openai'"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_trades_ai_provider ON ai_trades(ai_provider)"))

        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ai_chat_messages (
                    id INTEGER PRIMARY KEY,
                    ai_provider VARCHAR(20) NOT NULL,
                    role VARCHAR(20) NOT NULL,
                    message TEXT NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_chat_messages_ai_provider ON ai_chat_messages(ai_provider)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_chat_messages_created_at ON ai_chat_messages(created_at)"))

        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ai_agent_memories (
                    id INTEGER PRIMARY KEY,
                    ai_provider VARCHAR(20) NOT NULL,
                    memory_type VARCHAR(40) NOT NULL,
                    content TEXT NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_agent_memories_ai_provider ON ai_agent_memories(ai_provider)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_agent_memories_type ON ai_agent_memories(memory_type)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_agent_memories_created_at ON ai_agent_memories(created_at)"))

        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ai_provider_usage (
                    id INTEGER PRIMARY KEY,
                    ai_provider VARCHAR(20) NOT NULL,
                    call_type VARCHAR(30) NOT NULL DEFAULT 'unknown',
                    model_name VARCHAR(80),
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_provider_usage_ai_provider ON ai_provider_usage(ai_provider)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_provider_usage_call_type ON ai_provider_usage(call_type)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ai_provider_usage_created_at ON ai_provider_usage(created_at)"))
