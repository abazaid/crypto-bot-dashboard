"""
ML predictor — uses the trained LightGBM model to generate
buy probability scores for each symbol based on the latest candles.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from advisor.config import ML_MODELS_DIR
from advisor.features.indicators import get_feature_cols
from advisor.ml_filter.trainer import load_model

logger = logging.getLogger(__name__)


def predict_all(
    symbol_dfs: dict[str, pd.DataFrame],
    model=None,
    feature_version: str = "v1",
) -> list[dict]:
    """
    Generate ML predictions for the latest candle of each symbol.

    Returns list of dicts sorted by probability descending:
    {
        symbol:        str
        probability:   float    0.0 – 1.0  (chance of +2% in next 24h)
        signal:        str      "BUY" | "WATCH" | "SKIP"
        rsi:           float
        bb_pct:        float
        trend:         str
        above_ema200:  int
        top_features:  dict     {feature: importance}
    }
    """
    if model is None:
        model, _ = load_model()
        if model is None:
            raise FileNotFoundError(
                "No trained model found. Run with --train first."
            )

    # Load feature importance for context
    metrics_path = Path(ML_MODELS_DIR) / "metrics.json"
    top_features: dict = {}
    if metrics_path.exists():
        with open(metrics_path) as f:
            m = json.load(f)
        importance = m.get("feature_importance", {})
        top_features = dict(list(importance.items())[:5])

    feature_cols = get_feature_cols(feature_version)
    results: list[dict] = []

    for symbol, df in symbol_dfs.items():
        try:
            missing = [c for c in feature_cols if c not in df.columns]
            if missing:
                logger.debug("Symbol %s missing: %s", symbol, missing)
                continue

            latest = df[feature_cols].dropna().iloc[-1:]
            if latest.empty:
                continue

            prob = float(model.predict_proba(latest)[0][1])

            # Signal thresholds
            if prob >= 0.60:
                signal = "BUY"
            elif prob >= 0.45:
                signal = "WATCH"
            else:
                signal = "SKIP"

            # Latest indicator snapshot
            row = df.iloc[-1]

            results.append({
                "symbol":       symbol,
                "probability":  round(prob, 4),
                "signal":       signal,
                "rsi":          round(float(row.get("rsi", 0)),          2),
                "bb_pct":       round(float(row.get("bb_pct", 0)),       3),
                "dist_ema200":  round(float(row.get("dist_ema200", 0)),  2),
                "pct_24h":      round(float(row.get("pct_24h", 0)),      2),
                "vol_ratio":    round(float(row.get("vol_ratio", 0)),    2),
                "trend":        str(row.get("trend", "unknown")),
                "above_ema200": int(row.get("above_ema200", 0)),
                "top_features": top_features,
            })

        except Exception as e:
            logger.debug("Prediction failed for %s: %s", symbol, e)

    results.sort(key=lambda r: r["probability"], reverse=True)
    return results
