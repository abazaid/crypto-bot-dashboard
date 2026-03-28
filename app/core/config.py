import os


class Settings:
    database_url = os.getenv("DATABASE_URL", "sqlite:///./paper_trading_v2.db")
    cycle_seconds = int(os.getenv("CYCLE_SECONDS", "8"))
    paper_start_balance = float(os.getenv("PAPER_START_BALANCE", "10000"))
    enforce_btc_filter = os.getenv("ENFORCE_BTC_FILTER", "true").lower() == "true"
    app_timezone = os.getenv("APP_TIMEZONE", "Asia/Riyadh")


settings = Settings()
