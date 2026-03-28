import os


class Settings:
    database_url = os.getenv("DATABASE_URL", "sqlite:///./paper_trading_v2.db")
    cycle_seconds = int(os.getenv("CYCLE_SECONDS", "3"))
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


settings = Settings()
