"""
Technical indicator calculation for the advisor module.
Pure pandas/numpy — no external TA library required.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(com=length - 1, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=length - 1, adjust=False).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = _ema(series, fast)
    ema_slow = _ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def _bbands(series: pd.Series, length: int = 20, std: float = 2.0):
    mid = series.rolling(length).mean()
    stddev = series.rolling(length).std(ddof=0)
    upper = mid + std * stddev
    lower = mid - std * stddev
    return upper, mid, lower


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=length - 1, adjust=False).mean()


def _obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff().fillna(0))
    return (direction * volume).cumsum()


def _roc(series: pd.Series, length: int = 10) -> pd.Series:
    return series.pct_change(length) * 100


def _mom(series: pd.Series, length: int = 10) -> pd.Series:
    return series.diff(length)


# ── Main ───────────────────────────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add technical indicators to an OHLCV DataFrame.
    Returns a new DataFrame with all indicators and NaN rows dropped.
    """
    df = df.copy()

    # ── Trend ──────────────────────────────────────────────────────────────
    df["ema20"]  = _ema(df["close"], 20)
    df["ema50"]  = _ema(df["close"], 50)
    df["ema200"] = _ema(df["close"], 200)

    # ── Momentum ───────────────────────────────────────────────────────────
    df["rsi"] = _rsi(df["close"], 14)

    macd_line, signal_line, hist = _macd(df["close"])
    df["macd"]        = macd_line
    df["macd_signal"] = signal_line
    df["macd_hist"]   = hist

    df["roc"] = _roc(df["close"], 10)
    df["mom"] = _mom(df["close"], 10)

    # ── Volatility ─────────────────────────────────────────────────────────
    bb_upper, bb_mid, bb_lower = _bbands(df["close"], 20, 2.0)
    df["bb_upper"] = bb_upper
    df["bb_mid"]   = bb_mid
    df["bb_lower"] = bb_lower
    df["bb_pct"]   = (df["close"] - bb_lower) / (bb_upper - bb_lower + 1e-9)
    df["bb_width"] = (bb_upper - bb_lower) / (bb_mid + 1e-9)

    df["atr"] = _atr(df["high"], df["low"], df["close"], 14)

    # ── Volume ─────────────────────────────────────────────────────────────
    vol_ma = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / (vol_ma + 1e-9)
    df["obv"] = _obv(df["close"], df["volume"])

    # ── Price change % ─────────────────────────────────────────────────────
    df["pct_1h"]  = df["close"].pct_change(1)  * 100
    df["pct_4h"]  = df["close"].pct_change(4)  * 100
    df["pct_24h"] = df["close"].pct_change(24) * 100

    # ── Derived ────────────────────────────────────────────────────────────
    df["dist_ema200"]  = (df["close"] - df["ema200"]) / (df["ema200"] + 1e-9) * 100
    df["dist_bb_lower"] = (df["close"] - df["bb_lower"]) / (df["close"] + 1e-9) * 100

    # ── Trend classification ────────────────────────────────────────────────
    df["trend"] = "sideways"
    df.loc[df["ema20"] > df["ema50"], "trend"] = "bullish"
    df.loc[df["ema20"] < df["ema50"], "trend"] = "bearish"
    df["above_ema200"] = (df["close"] > df["ema200"]).astype(int)

    # ── Clean up ────────────────────────────────────────────────────────────
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.dropna(subset=["rsi", "ema200", "bb_pct", "macd"])

    return df


FEATURE_COLS = [
    "rsi", "macd", "macd_signal", "macd_hist",
    "bb_pct", "bb_width",
    "atr", "vol_ratio",
    "pct_1h", "pct_4h", "pct_24h",
    "dist_ema200", "dist_bb_lower",
    "above_ema200", "roc", "mom",
]
"""Columns used as ML features (must exist after add_indicators)."""
