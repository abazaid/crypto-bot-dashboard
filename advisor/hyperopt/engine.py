"""
Hyperopt engine — uses Optuna to find the best strategy parameters
for each symbol by running thousands of backtests.
"""
from __future__ import annotations

import json
import logging
import warnings
from pathlib import Path

import optuna
import pandas as pd

from advisor.config import HYPEROPT_TRIALS, MIN_TRADES, HYPEROPT_RESULTS
from advisor.hyperopt.backtest import run_backtest

warnings.filterwarnings("ignore")
optuna.logging.set_verbosity(optuna.logging.WARNING)
logger = logging.getLogger(__name__)


# ── Parameter search space ────────────────────────────────────────────────────

def _objective(trial: optuna.Trial, df: pd.DataFrame) -> float:
    """
    Optuna objective: suggest params → run backtest → return score to maximise.
    Score = Sharpe ratio (maximise). Penalises if too few trades.
    """
    params = {
        "entry_rsi":    trial.suggest_float("entry_rsi",    25.0, 50.0),
        "entry_bb_pct": trial.suggest_float("entry_bb_pct",  0.05, 0.40),
        "dca_drop_1":   trial.suggest_float("dca_drop_1",    3.0, 10.0),
        "dca_drop_2":   trial.suggest_float("dca_drop_2",    8.0, 25.0),
        "dca_alloc_1":  trial.suggest_float("dca_alloc_1",  50.0, 200.0),
        "dca_alloc_2":  trial.suggest_float("dca_alloc_2",  80.0, 300.0),
        "tp_pct":       trial.suggest_float("tp_pct",        2.0, 10.0),
        "sl_pct":       trial.suggest_float("sl_pct",        8.0, 30.0),
    }

    # Constraint: dca_drop_2 must be larger than dca_drop_1
    if params["dca_drop_2"] <= params["dca_drop_1"]:
        return -99.0

    metrics = run_backtest(df, params)

    if metrics["total_trades"] < MIN_TRADES:
        return -99.0  # Not enough trades to be meaningful

    # Objective: maximise sharpe, weighted by win rate
    sharpe   = metrics["sharpe_ratio"]
    win_rate = metrics["win_rate"] / 100.0
    trades   = metrics["total_trades"]

    # Penalise strategies with very few trades (not statistically significant)
    trade_bonus = min(1.0, trades / 50.0)

    return sharpe * win_rate * trade_bonus


# ── Per-symbol optimisation ───────────────────────────────────────────────────

def optimize_symbol(
    symbol: str,
    df: pd.DataFrame,
    n_trials: int = HYPEROPT_TRIALS,
) -> dict:
    """Run Optuna on a single symbol. Returns best params + backtest metrics."""
    study = optuna.create_study(direction="maximize")
    study.optimize(
        lambda trial: _objective(trial, df),
        n_trials=n_trials,
        show_progress_bar=False,
        n_jobs=1,
    )

    best = study.best_trial
    best_params = best.params

    # Ensure dca_drop_2 > dca_drop_1
    if best_params.get("dca_drop_2", 0) <= best_params.get("dca_drop_1", 0):
        best_params["dca_drop_2"] = best_params["dca_drop_1"] + 5.0

    metrics = run_backtest(df, best_params)

    return {
        "symbol":      symbol,
        "best_params": {k: round(v, 2) for k, v in best_params.items()},
        "metrics":     metrics,
        "score":       round(best.value, 4) if best.value else 0.0,
    }


# ── All symbols ───────────────────────────────────────────────────────────────

def optimize_all(
    symbol_dfs: dict[str, pd.DataFrame],
    n_trials: int = HYPEROPT_TRIALS,
) -> list[dict]:
    """
    Run Hyperopt for every symbol.
    Returns list of results sorted by score (best first).
    Saves individual JSON files + combined results.json.
    """
    Path(HYPEROPT_RESULTS).mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    total = len(symbol_dfs)

    for idx, (symbol, df) in enumerate(symbol_dfs.items(), 1):
        print(f"  [{idx:3}/{total}] Hyperopt {symbol} ({n_trials} trials)...", end="", flush=True)
        try:
            result = optimize_symbol(symbol, df, n_trials)
            results.append(result)
            score = result["score"]
            trades = result["metrics"]["total_trades"]
            win_rate = result["metrics"]["win_rate"]
            print(f" score={score:.3f} | trades={trades} | win={win_rate:.1f}%")

            # Save individual file
            sym_path = Path(HYPEROPT_RESULTS) / f"{symbol}.json"
            with open(sym_path, "w") as f:
                json.dump(result, f, indent=2)

        except Exception as e:
            print(f" ✗ {e}")
            logger.exception("Hyperopt failed for %s", symbol)

    # Sort by score descending
    results.sort(key=lambda r: r.get("score", -99), reverse=True)

    # Save combined results
    combined_path = Path(HYPEROPT_RESULTS) / "results.json"
    with open(combined_path, "w") as f:
        json.dump(results, f, indent=2)

    logger.info("Hyperopt complete. Results saved to %s", combined_path)
    return results


def load_results() -> list[dict]:
    """Load previously saved Hyperopt results."""
    path = Path(HYPEROPT_RESULTS) / "results.json"
    if not path.exists():
        return []
    with open(path) as f:
        return json.load(f)
