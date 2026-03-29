import os


class Settings:
    database_url = os.getenv("DATABASE_URL", "sqlite:///./paper_trading_v2.db")
    cycle_seconds = int(os.getenv("CYCLE_SECONDS", "3"))
    fast_loop_seconds = int(os.getenv("FAST_LOOP_SECONDS", os.getenv("CYCLE_SECONDS", "30")))
    medium_refresh_seconds = int(os.getenv("MEDIUM_REFRESH_SECONDS", "300"))
    slow_recalc_seconds = int(os.getenv("SLOW_RECALC_SECONDS", "14400"))
    paper_start_balance = float(os.getenv("PAPER_START_BALANCE", "10000"))
    enforce_btc_filter = os.getenv("ENFORCE_BTC_FILTER", "true").lower() == "true"
    app_timezone = os.getenv("APP_TIMEZONE", "Asia/Riyadh")
    dca_near_support_pct = float(os.getenv("DCA_NEAR_SUPPORT_PCT", "2.0"))
    dca_support_score_threshold = float(os.getenv("DCA_SUPPORT_SCORE_THRESHOLD", "70"))
    dca_rsi_oversold = float(os.getenv("DCA_RSI_OVERSOLD", "35"))
    dca_reversal_min_conditions = int(os.getenv("DCA_REVERSAL_MIN_CONDITIONS", "2"))
    dca_max_symbol_allocation_x = float(os.getenv("DCA_MAX_SYMBOL_ALLOCATION_X", "7.0"))
    dca_scale_1 = float(os.getenv("DCA_SCALE_1", "1.5"))
    dca_scale_2 = float(os.getenv("DCA_SCALE_2", "2.0"))
    dca_scale_3 = float(os.getenv("DCA_SCALE_3", "3.0"))
    dca_scale_4 = float(os.getenv("DCA_SCALE_4", "3.5"))
    dca_scale_5 = float(os.getenv("DCA_SCALE_5", "4.5"))
    binance_api_key = os.getenv("BINANCE_API_KEY", "").strip()
    binance_api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    live_entry_limit_buffer_pct = float(os.getenv("LIVE_ENTRY_LIMIT_BUFFER_PCT", "0.03"))
    live_entry_limit_fallback_market = os.getenv("LIVE_ENTRY_LIMIT_FALLBACK_MARKET", "true").lower() == "true"


settings = Settings()
