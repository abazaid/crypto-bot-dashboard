import os


class Settings:
    database_url = os.getenv("DATABASE_URL", "sqlite:///./paper_trading.db")
    cycle_seconds = int(os.getenv("CYCLE_SECONDS", "300"))
    position_watch_seconds = int(os.getenv("POSITION_WATCH_SECONDS", "10"))

    paper_start_balance = float(os.getenv("PAPER_START_BALANCE", "1000"))
    fee_rate = float(os.getenv("FEE_RATE", "0.001"))
    position_size_pct = float(os.getenv("POSITION_SIZE_PCT", "0.10"))
    max_open_trades = int(os.getenv("MAX_OPEN_TRADES", "3"))
    take_profit_pct = float(os.getenv("TAKE_PROFIT_PCT", "0.03"))
    stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "0.012"))
    time_stop_minutes = int(os.getenv("TIME_STOP_MINUTES", "120"))
    cooldown_minutes = int(os.getenv("COOLDOWN_MINUTES", "30"))
    daily_loss_limit_pct = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "3.0"))

    min_quote_volume = float(os.getenv("MIN_QUOTE_VOLUME", "10000000"))
    max_spread_pct = float(os.getenv("MAX_SPREAD_PCT", "0.20"))
    max_symbols = int(os.getenv("MAX_SYMBOLS", "25"))
    enforce_btc_filter = os.getenv("ENFORCE_BTC_FILTER", "true").lower() == "true"
    app_timezone = os.getenv("APP_TIMEZONE", "Asia/Riyadh")


settings = Settings()
