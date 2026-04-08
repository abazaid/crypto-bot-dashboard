"""
Technical indicator calculation for the advisor module.
Uses pandas-ta — no TA-Lib C compilation required.
"""
from __future__ import annotations

import logging
import warnings

import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add technical indicators to an OHLCV DataFrame.
    Returns a new DataFrame with all indicators and NaN rows dropped.

    Indicators added:
      Trend:    EMA20, EMA50, EMA200
      Momentum: RSI14, MACD line/signal/hist
      Volatility: BB upper/mid/lower, ATR14, BB width
      Volume:   Volume ratio (vs 20-period MA), OBV
      Price:    % change 1h/4h/24h, distance from EMA200
    """
    import pandas_ta as ta  # import here so it doesn't break if not installed

    df = df.copy()

    # ── Trend ──────────────────────────────────────────────────────────────
    df["ema20"]  = ta.ema(df["close"], length=20)
    df["ema50"]  = ta.ema(df["close"], length=50)
    df["ema200"] = ta.ema(df["close"], length=200)

    # ── Momentum ───────────────────────────────────────────────────────────
    df["rsi"] = ta.rsi(df["close"], length=14)

    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd is not None:
        df["macd"]        = macd["MACD_12_26_9"]
        df["macd_signal"] = macd["MACDs_12_26_9"]
        df["macd_hist"]   = macd["MACDh_12_26_9"]

    df["roc"]  = ta.roc(df["close"], length=10)   # Rate of change 10h
    df["mom"]  = ta.mom(df["close"], length=10)   # Momentum 10h

    # ── Volatility ─────────────────────────────────────────────────────────
    bb = ta.bbands(df["close"], length=20, std=2)
    if bb is not None:
        df["bb_upper"] = bb["BBU_20_2.0"]
        df["bb_mid"]   = bb["BBM_20_2.0"]
        df["bb_lower"] = bb["BBL_20_2.0"]
        df["bb_pct"]   = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"] + 1e-9)
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / (df["bb_mid"] + 1e-9)

    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    # ── Volume ─────────────────────────────────────────────────────────────
    vol_ma = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / (vol_ma + 1e-9)  # > 1 = above average volume

    df["obv"] = ta.obv(df["close"], df["volume"])

    # ── Price change % ─────────────────────────────────────────────────────
    df["pct_1h"]  = df["close"].pct_change(1)  * 100
    df["pct_4h"]  = df["close"].pct_change(4)  * 100
    df["pct_24h"] = df["close"].pct_change(24) * 100

    # Distance from EMA200 (positive = above, negative = below)
    df["dist_ema200"] = (df["close"] - df["ema200"]) / (df["ema200"] + 1e-9) * 100

    # Distance from lower BB (how close to support)
    df["dist_bb_lower"] = (df["close"] - df["bb_lower"]) / (df["close"] + 1e-9) * 100

    # ── Trend classification ────────────────────────────────────────────────
    df["trend"] = "sideways"
    df.loc[df["ema20"] > df["ema50"], "trend"] = "bullish"
    df.loc[df["ema20"] < df["ema50"], "trend"] = "bearish"
    df.loc[df["close"] > df["ema200"], "above_ema200"] = 1
    df.loc[df["close"] <= df["ema200"], "above_ema200"] = 0

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
