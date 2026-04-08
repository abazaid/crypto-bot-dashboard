"""
Fetches top Binance USDT symbols by volume and downloads OHLCV history.
Uses Binance public API — no API key required.
"""
from __future__ import annotations

import os
import time
import logging
from pathlib import Path
from datetime import datetime, timezone

import requests
import pandas as pd

from advisor.config import (
    TOP_N_SYMBOLS, MIN_QUOTE_VOLUME, EXCLUDED_SYMBOLS,
    OHLCV_INTERVAL, OHLCV_DAYS, CACHE_DIR,
)

logger = logging.getLogger(__name__)

BINANCE_BASE = "https://api.binance.com"


# ── Symbol discovery ──────────────────────────────────────────────────────────

def get_top_symbols(n: int = TOP_N_SYMBOLS) -> list[str]:
    """Return top N USDT pairs sorted by 24h quote volume."""
    url = f"{BINANCE_BASE}/api/v3/ticker/24hr"
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    tickers = resp.json()

    usdt_pairs = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if sym in EXCLUDED_SYMBOLS:
            continue
        vol = float(t.get("quoteVolume", 0))
        if vol < MIN_QUOTE_VOLUME:
            continue
        usdt_pairs.append((sym, vol))

    usdt_pairs.sort(key=lambda x: x[1], reverse=True)
    result = [s for s, _ in usdt_pairs[:n]]
    logger.info("Found %d qualifying USDT symbols (top %d selected)", len(usdt_pairs), n)
    return result


# ── OHLCV download ────────────────────────────────────────────────────────────

def _klines_to_df(raw: list) -> pd.DataFrame:
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"]
    df = pd.DataFrame(raw, columns=cols)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = df[c].astype(float)
    df = df[["open_time", "open", "high", "low", "close", "volume", "quote_volume"]]
    df = df.rename(columns={"open_time": "timestamp"})
    df = df.set_index("timestamp")
    return df


def fetch_ohlcv(
    symbol: str,
    interval: str = OHLCV_INTERVAL,
    days: int = OHLCV_DAYS,
) -> pd.DataFrame:
    """Download OHLCV from Binance for given symbol and period."""
    limit = 1000
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86_400_000

    all_rows: list = []
    current_start = start_ms

    while current_start < end_ms:
        url = f"{BINANCE_BASE}/api/v3/klines"
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_ms,
            "limit": limit,
        }
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_rows.extend(batch)
        last_close = int(batch[-1][6])  # close_time of last candle
        current_start = last_close + 1
        if len(batch) < limit:
            break
        time.sleep(0.1)  # rate-limit courtesy

    if not all_rows:
        raise ValueError(f"No klines returned for {symbol}")

    return _klines_to_df(all_rows)


# ── Cache layer ───────────────────────────────────────────────────────────────

def _cache_path(symbol: str, interval: str) -> Path:
    Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
    return Path(CACHE_DIR) / f"{symbol}_{interval}.parquet"


def load_or_fetch(
    symbol: str,
    interval: str = OHLCV_INTERVAL,
    days: int = OHLCV_DAYS,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Load from cache if fresh enough, otherwise download and cache."""
    path = _cache_path(symbol, interval)

    if not force_refresh and path.exists():
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours < 4:  # Cache valid for 4 hours
            df = pd.read_parquet(path)
            logger.debug("Cache hit: %s (%d rows, %.1fh old)", symbol, len(df), age_hours)
            return df

    logger.info("Downloading %s %s (%d days)...", symbol, interval, days)
    df = fetch_ohlcv(symbol, interval, days)
    df.to_parquet(path)
    return df


def load_all_symbols(
    symbols: list[str],
    interval: str = OHLCV_INTERVAL,
    days: int = OHLCV_DAYS,
    force_refresh: bool = False,
) -> dict[str, pd.DataFrame]:
    """Download/load OHLCV for a list of symbols. Returns {symbol: df}."""
    result: dict[str, pd.DataFrame] = {}
    total = len(symbols)
    for i, sym in enumerate(symbols, 1):
        print(f"  [{i:3}/{total}] {sym}", end="", flush=True)
        try:
            df = load_or_fetch(sym, interval, days, force_refresh)
            if len(df) < 100:
                print(f" ⚠ skipped (only {len(df)} rows)")
                continue
            result[sym] = df
            print(f" ✓ {len(df)} candles")
        except Exception as e:
            print(f" ✗ error: {e}")
    return result
