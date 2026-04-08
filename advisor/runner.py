"""
Background runner for the advisor — runs in a daemon thread
so it doesn't block the FastAPI app.

State is stored in a module-level dict so any route can read it.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from advisor.config import REPORT_DIR

logger = logging.getLogger(__name__)

# ── Shared state (read by FastAPI routes) ─────────────────────────────────────
_lock = threading.Lock()
_state: dict = {
    "status":       "idle",        # idle | running | done | error
    "started_at":   None,
    "finished_at":  None,
    "step":         "",
    "progress":     0,             # 0-100
    "error":        None,
    "result":       None,          # latest.json content when done
}


def get_state() -> dict:
    with _lock:
        return dict(_state)


def _set(key: str, value) -> None:
    with _lock:
        _state[key] = value


def _update(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)


# ── Load existing results on startup ──────────────────────────────────────────
def _try_load_existing() -> None:
    latest = Path(REPORT_DIR) / "latest.json"
    if latest.exists():
        try:
            with open(latest) as f:
                data = json.load(f)
            _update(status="done", result=data,
                    finished_at=data.get("generated_at", ""))
            logger.info("Advisor: loaded existing results from %s", latest)
        except Exception:
            pass


_try_load_existing()


# ── The actual run logic ───────────────────────────────────────────────────────
def _run_advisor(n_symbols: int, n_trials: int) -> None:
    try:
        _update(status="running", started_at=datetime.now(timezone.utc).isoformat(),
                finished_at=None, error=None, result=None, progress=0)

        # Step 1: Discover symbols
        _update(step="Discovering top symbols from Binance...", progress=5)
        from advisor.data.fetcher import get_top_symbols, load_all_symbols
        symbols = get_top_symbols(n=n_symbols)
        logger.info("Advisor: found %d symbols", len(symbols))

        # Step 2: Download OHLCV
        _update(step=f"Downloading OHLCV history for {len(symbols)} symbols...", progress=10)
        raw_dfs = load_all_symbols(symbols, force_refresh=False)
        logger.info("Advisor: loaded %d symbols", len(raw_dfs))

        # Step 3: Indicators
        _update(step="Calculating technical indicators...", progress=25)
        from advisor.features.indicators import add_indicators
        indicator_dfs: dict = {}
        for sym, df in raw_dfs.items():
            try:
                indicator_dfs[sym] = add_indicators(df)
            except Exception as e:
                logger.warning("Indicator error %s: %s", sym, e)
        logger.info("Advisor: indicators done for %d symbols", len(indicator_dfs))

        # Step 4: ML training
        _update(step="Training LightGBM model...", progress=40)
        from advisor.ml_filter.trainer import run_training
        model, model_metrics = run_training(indicator_dfs)

        _update(step="Generating ML predictions...", progress=55)
        from advisor.ml_filter.predictor import predict_all
        ml_predictions = predict_all(indicator_dfs, model=model)
        buy_count = sum(1 for p in ml_predictions if p["signal"] == "BUY")
        logger.info("Advisor: ML done — %d BUY signals", buy_count)

        # Step 5: Hyperopt
        _update(step=f"Running Hyperopt ({n_trials} trials × {len(indicator_dfs)} symbols)...", progress=60)
        from advisor.hyperopt.engine import optimize_all
        hyperopt_results = optimize_all(indicator_dfs, n_trials=n_trials)
        logger.info("Advisor: hyperopt done for %d symbols", len(hyperopt_results))

        # Step 6: Report
        _update(step="Generating report...", progress=95)
        from advisor.report.generator import generate
        generate(
            ml_predictions=ml_predictions,
            hyperopt_results=hyperopt_results,
            model_metrics=model_metrics,
        )

        # Load result
        latest = Path(REPORT_DIR) / "latest.json"
        result = {}
        if latest.exists():
            with open(latest) as f:
                result = json.load(f)

        _update(
            status="done",
            step="Complete",
            progress=100,
            finished_at=datetime.now(timezone.utc).isoformat(),
            result=result,
        )
        logger.info("Advisor: run complete")

    except Exception as e:
        logger.exception("Advisor run failed")
        _update(status="error", step="", error=str(e), progress=0)


# ── Public API ─────────────────────────────────────────────────────────────────
_thread: threading.Thread | None = None


def start(n_symbols: int = 50, n_trials: int = 100) -> bool:
    """
    Start advisor in background thread.
    Returns False if already running.
    """
    global _thread
    with _lock:
        if _state["status"] == "running":
            return False

    _thread = threading.Thread(
        target=_run_advisor,
        args=(n_symbols, n_trials),
        daemon=True,
        name="advisor-runner",
    )
    _thread.start()
    return True
