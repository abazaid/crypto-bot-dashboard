"""
Advisor configuration — edit these settings before running.
Completely independent from the main app/.
"""

# ── Binance symbols ──────────────────────────────────────────────────────────
TOP_N_SYMBOLS      = 150        # Fetch top N USDT pairs by 24h volume
MIN_QUOTE_VOLUME   = 5_000_000  # Minimum 24h volume in USDT (liquidity filter)
EXCLUDED_SYMBOLS   = {          # Stablecoins / wrapped tokens to skip
    "USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT",
    "USDPUSDT", "DAIUSDT", "EURUSDT", "PAXGUSDT",
    "WBTCUSDT", "BTCDOMUSDT",
}

# ── Historical data ──────────────────────────────────────────────────────────
OHLCV_INTERVAL     = "1h"       # Candle interval
OHLCV_DAYS         = 180        # Days of history to download
CACHE_DIR          = "/data/advisor/data/cache"

# ── Hyperopt ─────────────────────────────────────────────────────────────────
HYPEROPT_TRIALS    = 300        # Optuna trials per symbol (more = better, slower)
HYPEROPT_JOBS      = 1          # Parallel Optuna jobs (1 = safe on Windows)
MIN_TRADES         = 10         # Minimum trades for a backtest to be valid
HYPEROPT_RESULTS   = "/data/advisor/hyperopt/results"

# ── ML Filter ────────────────────────────────────────────────────────────────
ML_TARGET_HORIZON  = 24         # Candles ahead to predict (24h at 1h interval)
ML_TARGET_GAIN_PCT = 2.0        # Min % gain to label as positive (buy signal)
ML_TEST_SPLIT      = 0.2        # 20% of data kept for testing
ML_MODELS_DIR      = "/data/advisor/ml_filter/models"

# ── Report ───────────────────────────────────────────────────────────────────
REPORT_TOP_N       = 20         # Show top N symbols in final report
REPORT_DIR         = "/data/advisor/report/output"
