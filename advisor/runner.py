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
from datetime import datetime, timezone, timedelta

# Riyadh = UTC+3
_RIYADH = timezone(timedelta(hours=3))

def _now_riyadh() -> str:
    return datetime.now(_RIYADH).strftime("%Y-%m-%d %H:%M AST")
from pathlib import Path

from advisor.config import REPORT_DIR

logger = logging.getLogger(__name__)

# ── Shared state (read by FastAPI routes) ─────────────────────────────────────
_lock = threading.Lock()
_state: dict = {
    "status":           "idle",    # idle | running | done | error
    "started_at":       None,
    "finished_at":      None,
    "step":             "",
    "progress":         0,         # 0-100
    "error":            None,
    "result":           None,      # latest.json content when done
    # Quick refresh fields
    "refresh_status":   "idle",    # idle | running | done | error
    "last_refresh_at":  None,
    "refresh_error":    None,
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
            finished_at=_now_riyadh(),
            result=result,
        )
        logger.info("Advisor: run complete")

    except Exception as e:
        logger.exception("Advisor run failed")
        _update(status="error", step="", error=str(e), progress=0)


# ── Quick refresh (ML only, no Hyperopt, no retraining) ───────────────────────
def _run_quick_refresh() -> None:
    """
    Fast refresh — tops up OHLCV with latest candles only,
    reuses saved LightGBM model, updates ML predictions in latest.json.
    Typical runtime: 1-2 minutes vs 15-20 for full run.
    """
    try:
        _update(refresh_status="running", refresh_error=None)
        logger.info("Advisor: starting quick ML refresh")

        # Load saved model — if none exists, skip
        from advisor.ml_filter.trainer import load_model
        model, _ = load_model()
        if model is None:
            _update(refresh_status="error",
                    refresh_error="No trained model yet — run full analysis first")
            logger.warning("Advisor quick refresh: no saved model found")
            return

        # Load existing latest.json to get symbol list + hyperopt results
        latest_path = Path(REPORT_DIR) / "latest.json"
        if not latest_path.exists():
            _update(refresh_status="error",
                    refresh_error="No previous results — run full analysis first")
            return

        with open(latest_path) as f:
            existing = json.load(f)

        # Get symbol list from previous run
        prev_ml = existing.get("top_ml", [])
        prev_hyperopt = existing.get("top_hyperopt", [])
        if not prev_ml:
            _update(refresh_status="error",
                    refresh_error="No previous ML results to refresh from")
            return

        # Use all symbols from both ml + hyperopt results
        symbols = list({r["symbol"] for r in prev_ml + prev_hyperopt})
        logger.info("Advisor quick refresh: topping up %d symbols", len(symbols))

        # Topup OHLCV — only fetches new candles since last cached
        from advisor.data.fetcher import topup_all_symbols
        raw_dfs = topup_all_symbols(symbols)

        # Recalculate indicators
        from advisor.features.indicators import add_indicators
        indicator_dfs: dict = {}
        for sym, df in raw_dfs.items():
            try:
                indicator_dfs[sym] = add_indicators(df)
            except Exception as e:
                logger.debug("Indicator error %s: %s", sym, e)

        if not indicator_dfs:
            _update(refresh_status="error", refresh_error="No valid indicators computed")
            return

        # Re-run predictions with saved model
        from advisor.ml_filter.predictor import predict_all
        ml_predictions = predict_all(indicator_dfs, model=model)
        buy_count = sum(1 for p in ml_predictions if p["signal"] == "BUY")
        logger.info("Advisor quick refresh: %d BUY signals", buy_count)

        # Rebuild combined recommendations with fresh ML + old hyperopt
        ho_by_sym = {r["symbol"]: r for r in prev_hyperopt}
        combined = []
        for pred in ml_predictions:
            if pred["signal"] != "BUY":
                continue
            ho = ho_by_sym.get(pred["symbol"])
            if not ho or ho.get("score", 0) <= 0:
                continue
            combined.append({
                "symbol":         pred["symbol"],
                "ml_prob":        pred["probability"],
                "ho_score":       ho["score"],
                "win_rate":       ho["metrics"].get("win_rate", 0),
                "avg_profit":     ho["metrics"].get("avg_profit_pct", 0),
                "params":         ho["best_params"],
                "combined_score": pred["probability"] * ho["score"],
            })
        combined.sort(key=lambda x: x["combined_score"], reverse=True)

        # Save updated latest.json (keep ml_model + hyperopt, refresh ml + recommendations)
        now_str = _now_riyadh()
        updated = dict(existing)
        updated["generated_at"]    = now_str
        updated["refreshed_at"]    = now_str
        updated["top_ml"]          = ml_predictions[:20]
        updated["recommendations"] = combined[:20]

        with open(latest_path, "w") as f:
            json.dump(updated, f, indent=2)

        # Push fresh result into state
        _update(
            result=updated,
            refresh_status="done",
            last_refresh_at=now_str,
            refresh_error=None,
        )
        # If main status was done, update it to reflect fresh data
        with _lock:
            if _state["status"] == "done":
                _state["finished_at"] = now_str

        logger.info("Advisor quick refresh complete — %d symbols, %d BUY", len(indicator_dfs), buy_count)

    except Exception as e:
        logger.exception("Advisor quick refresh failed")
        _update(refresh_status="error", refresh_error=str(e))


# ── Public API ─────────────────────────────────────────────────────────────────
_thread: threading.Thread | None = None
_refresh_thread: threading.Thread | None = None


def start_refresh() -> bool:
    """
    Start a quick ML-only refresh in background thread.
    Returns False if a full run or refresh is already in progress.
    """
    global _refresh_thread
    with _lock:
        if _state["status"] == "running":
            return False
        if _state["refresh_status"] == "running":
            return False

    _refresh_thread = threading.Thread(
        target=_run_quick_refresh,
        daemon=True,
        name="advisor-refresh",
    )
    _refresh_thread.start()
    return True


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
