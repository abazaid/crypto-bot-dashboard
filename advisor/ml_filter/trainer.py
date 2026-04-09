"""
LightGBM trainer — learns from historical OHLCV + indicators
to predict whether a symbol will rise >= TARGET_GAIN_PCT within the next
ML_TARGET_HORIZON candles.

Label:  1 = price rises >= 2% in next 24h  (buy opportunity)
        0 = price doesn't reach target       (skip)

Output: trained model saved to ML_MODELS_DIR/model.pkl
        + feature importance + test metrics
"""
from __future__ import annotations

import json
import logging
import warnings
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import (
    accuracy_score, roc_auc_score,
    precision_score, recall_score, f1_score,
)

from advisor.config import (
    ML_TARGET_HORIZON, ML_TARGET_GAIN_PCT,
    ML_TEST_SPLIT, ML_MODELS_DIR,
)
from advisor.features.indicators import get_feature_cols

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)


# ── Dataset preparation ───────────────────────────────────────────────────────

def _make_labels(df: pd.DataFrame) -> pd.Series:
    """
    Create binary labels:
      1  if max(close[i+1 .. i+horizon]) >= close[i] * (1 + gain_pct/100)
      0  otherwise
    """
    closes = df["close"].values
    n = len(closes)
    labels = np.zeros(n, dtype=int)
    target_mult = 1 + ML_TARGET_GAIN_PCT / 100.0

    for i in range(n - ML_TARGET_HORIZON):
        future_max = closes[i + 1: i + 1 + ML_TARGET_HORIZON].max()
        if future_max >= closes[i] * target_mult:
            labels[i] = 1

    # Last ML_TARGET_HORIZON rows cannot have labels → mark NaN
    labels[n - ML_TARGET_HORIZON:] = -1
    return pd.Series(labels, index=df.index)


def prepare_dataset(
    symbol_dfs: dict[str, pd.DataFrame],
    feature_version: str = "v1",
) -> tuple[pd.DataFrame, pd.Series]:
    """
    Combine all symbols into one feature matrix X and label series y.
    Adds a 'symbol' column for tracking but excludes it from features.
    """
    feature_cols = get_feature_cols(feature_version)
    frames = []
    for symbol, df in symbol_dfs.items():
        missing = [c for c in feature_cols if c not in df.columns]
        if missing:
            logger.warning("Symbol %s missing columns: %s — skipped", symbol, missing)
            continue

        sub = df[feature_cols].copy()
        sub["_symbol"] = symbol
        sub["_label"]  = _make_labels(df)
        frames.append(sub)

    if not frames:
        raise ValueError("No valid symbol data for training")

    combined = pd.concat(frames)
    combined = combined[combined["_label"] != -1]
    combined = combined.dropna(subset=feature_cols)

    X = combined[feature_cols]
    y = combined["_label"]

    pos_rate = y.mean() * 100
    logger.info(
        "Dataset: %d rows | %d symbols | %.1f%% positive labels",
        len(X), len(symbol_dfs), pos_rate,
    )
    return X, y


# ── Training ──────────────────────────────────────────────────────────────────

def train_model(X: pd.DataFrame, y: pd.Series) -> tuple[object, dict]:
    """
    Train a LightGBM classifier with time-series cross-validation.
    Returns (model, metrics_dict).
    """
    import lightgbm as lgb

    # Time-series split: last 20% for final test
    split_idx = int(len(X) * (1 - ML_TEST_SPLIT))
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

    pos_weight = (y_train == 0).sum() / max((y_train == 1).sum(), 1)

    model = lgb.LGBMClassifier(
        n_estimators=500,
        learning_rate=0.05,
        max_depth=6,
        num_leaves=31,
        min_child_samples=20,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=pos_weight,
        random_state=42,
        verbose=-1,
        n_jobs=-1,
    )

    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(-1)],
    )

    # ── Evaluate ──────────────────────────────────────────────────────────
    y_pred      = model.predict(X_test)
    y_prob      = model.predict_proba(X_test)[:, 1]

    metrics = {
        "accuracy":   round(float(accuracy_score(y_test, y_pred)),  4),
        "auc":        round(float(roc_auc_score(y_test, y_prob)),    4),
        "precision":  round(float(precision_score(y_test, y_pred, zero_division=0)), 4),
        "recall":     round(float(recall_score(y_test, y_pred, zero_division=0)),    4),
        "f1":         round(float(f1_score(y_test, y_pred, zero_division=0)),        4),
        "train_rows": int(len(X_train)),
        "test_rows":  int(len(X_test)),
        "pos_rate":   round(float(y.mean()) * 100, 2),
        "best_iteration": int(getattr(model, "best_iteration_", model.n_estimators)),
    }

    # Feature importance
    importance = dict(zip(
        X.columns.tolist(),
        [round(float(v), 4) for v in model.feature_importances_],
    ))
    importance = dict(sorted(importance.items(), key=lambda x: x[1], reverse=True))
    metrics["feature_importance"] = importance

    logger.info(
        "Model trained | AUC=%.3f | Acc=%.3f | F1=%.3f",
        metrics["auc"], metrics["accuracy"], metrics["f1"],
    )
    return model, metrics


# ── Save / load ───────────────────────────────────────────────────────────────

def save_model(model: object, metrics: dict) -> None:
    """Save model to disk."""
    Path(ML_MODELS_DIR).mkdir(parents=True, exist_ok=True)
    model_path   = Path(ML_MODELS_DIR) / "model.pkl"
    metrics_path = Path(ML_MODELS_DIR) / "metrics.json"

    joblib.dump(model, model_path)
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)

    logger.info("Model saved to %s", model_path)


def load_model() -> tuple[object | None, dict]:
    """Load model from disk. Returns (model, metrics) or (None, {})."""
    model_path   = Path(ML_MODELS_DIR) / "model.pkl"
    metrics_path = Path(ML_MODELS_DIR) / "metrics.json"

    if not model_path.exists():
        return None, {}

    model = joblib.load(model_path)
    metrics = {}
    if metrics_path.exists():
        with open(metrics_path) as f:
            metrics = json.load(f)

    return model, metrics


def run_training(
    symbol_dfs: dict[str, pd.DataFrame],
    feature_version: str = "v1",
) -> tuple[object, dict]:
    """Full training pipeline: prepare → train → save → return."""
    print(f"  Preparing dataset (feature set: {feature_version.upper()})...")
    X, y = prepare_dataset(symbol_dfs, feature_version=feature_version)
    print(f"  Dataset: {len(X):,} rows | {y.mean()*100:.1f}% positive")

    print("  Training LightGBM model...")
    model, metrics = train_model(X, y)
    metrics["feature_version"] = feature_version
    print(f"  AUC={metrics['auc']:.3f} | Accuracy={metrics['accuracy']:.3f} | F1={metrics['f1']:.3f}")

    save_model(model, metrics)
    return model, metrics
