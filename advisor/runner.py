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

# ── Separate V2 state ─────────────────────────────────────────────────────────
_lock_v2 = threading.Lock()
_state_v2: dict = {
    "status":           "idle",
    "started_at":       None,
    "finished_at":      None,
    "step":             "",
    "progress":         0,
    "error":            None,
    "result":           None,
    "refresh_status":   "idle",
    "last_refresh_at":  None,
    "refresh_error":    None,
}


def get_state() -> dict:
    with _lock:
        return dict(_state)


def get_state_v2() -> dict:
    with _lock_v2:
        return dict(_state_v2)


def _set(key: str, value) -> None:
    with _lock:
        _state[key] = value


def _update(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)


def _update_v2(**kwargs) -> None:
    with _lock_v2:
        _state_v2.update(kwargs)


# ── Load existing results on startup ──────────────────────────────────────────
def _try_load_existing() -> None:
    for version, updater in [("v1", _update), ("v2", _update_v2)]:
        versioned = Path(REPORT_DIR) / f"latest_{version}.json"
        # Fall back to latest.json for v1 if no versioned file yet
        path = versioned if versioned.exists() else (
            Path(REPORT_DIR) / "latest.json" if version == "v1" else None
        )
        if not path or not path.exists():
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("feature_version", "v1") == version or version == "v1":
                updater(status="done", result=data,
                        finished_at=data.get("generated_at", ""))
                logger.info("Advisor: loaded %s results from %s", version, path)
        except Exception:
            pass


_try_load_existing()


# ── The actual run logic ───────────────────────────────────────────────────────
def _run_advisor_impl(
    n_symbols: int,
    n_trials: int,
    feature_version: str,
    upd,          # callable(**kwargs) to update the right state dict
    result_file: str,
) -> None:
    try:
        upd(status="running", started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None, error=None, result=None, progress=0)

        # Step 1: Discover symbols
        upd(step="Discovering top symbols from Binance...", progress=5)
        from advisor.data.fetcher import get_top_symbols, load_all_symbols
        symbols = get_top_symbols(n=n_symbols)
        logger.info("Advisor [%s]: found %d symbols", feature_version, len(symbols))

        # Step 2: Download OHLCV
        upd(step=f"Downloading OHLCV history for {len(symbols)} symbols...", progress=10)
        raw_dfs = load_all_symbols(symbols, force_refresh=False)
        logger.info("Advisor [%s]: loaded %d symbols", feature_version, len(raw_dfs))

        # Step 3: Indicators
        upd(step=f"Calculating indicators ({feature_version.upper()})...", progress=25)
        from advisor.features.indicators import add_indicators
        indicator_dfs: dict = {}
        for sym, df in raw_dfs.items():
            try:
                indicator_dfs[sym] = add_indicators(df, version=feature_version)
            except Exception as e:
                logger.warning("Indicator error %s: %s", sym, e)
        logger.info("Advisor [%s]: indicators done for %d symbols", feature_version, len(indicator_dfs))

        # Step 4: ML training
        upd(step=f"Training LightGBM model ({feature_version.upper()})...", progress=40)
        from advisor.ml_filter.trainer import run_training
        model, model_metrics = run_training(indicator_dfs, feature_version=feature_version)

        upd(step="Generating ML predictions...", progress=55)
        from advisor.ml_filter.predictor import predict_all
        ml_predictions = predict_all(indicator_dfs, model=model, feature_version=feature_version)
        buy_count = sum(1 for p in ml_predictions if p["signal"] == "BUY")
        logger.info("Advisor [%s]: ML done — %d BUY signals", feature_version, buy_count)

        # Step 5: Hyperopt
        upd(step=f"Running Hyperopt ({n_trials} trials × {len(indicator_dfs)} symbols)...", progress=60)
        from advisor.hyperopt.engine import optimize_all
        hyperopt_results = optimize_all(indicator_dfs, n_trials=n_trials)
        logger.info("Advisor [%s]: hyperopt done for %d symbols", feature_version, len(hyperopt_results))

        # Step 6: Report
        upd(step="Generating report...", progress=95)
        from advisor.report.generator import generate
        generate(
            ml_predictions=ml_predictions,
            hyperopt_results=hyperopt_results,
            model_metrics=model_metrics,
            feature_version=feature_version,
        )

        # Load result from version-specific file
        result_path = Path(REPORT_DIR) / result_file
        if not result_path.exists():
            result_path = Path(REPORT_DIR) / "latest.json"
        result = {}
        if result_path.exists():
            with open(result_path) as f:
                result = json.load(f)

        upd(
            status="done",
            step="Complete",
            progress=100,
            finished_at=_now_riyadh(),
            result=result,
        )
        logger.info("Advisor [%s]: run complete", feature_version)

    except Exception as e:
        logger.exception("Advisor [%s] run failed", feature_version)
        upd(status="error", step="", error=str(e), progress=0)


def _run_advisor(n_symbols: int, n_trials: int, feature_version: str = "v1") -> None:
    _run_advisor_impl(n_symbols, n_trials, feature_version, _update, "latest_v1.json")


def _run_advisor_v2(n_symbols: int, n_trials: int) -> None:
    _run_advisor_impl(n_symbols, n_trials, "v2", _update_v2, "latest_v2.json")


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
        model, saved_metrics = load_model()
        if model is None:
            _update(refresh_status="error",
                    refresh_error="No trained model yet — run full analysis first")
            logger.warning("Advisor quick refresh: no saved model found")
            return

        # Read feature version from saved model metrics
        feature_version = saved_metrics.get("feature_version", "v1")

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

        # Recalculate indicators using same version as training
        from advisor.features.indicators import add_indicators
        indicator_dfs: dict = {}
        for sym, df in raw_dfs.items():
            try:
                indicator_dfs[sym] = add_indicators(df, version=feature_version)
            except Exception as e:
                logger.debug("Indicator error %s: %s", sym, e)

        if not indicator_dfs:
            _update(refresh_status="error", refresh_error="No valid indicators computed")
            return

        # Re-run predictions with saved model (same feature version)
        from advisor.ml_filter.predictor import predict_all
        ml_predictions = predict_all(indicator_dfs, model=model, feature_version=feature_version)
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
_thread_v2: threading.Thread | None = None


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


def start(n_symbols: int = 50, n_trials: int = 100, feature_version: str = "v1") -> bool:
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
        args=(n_symbols, n_trials, feature_version),
        daemon=True,
        name="advisor-runner",
    )
    _thread.start()
    return True


def start_v2(n_symbols: int = 50, n_trials: int = 100) -> bool:
    """Start a V2 feature-set advisor run in its own background thread."""
    global _thread_v2
    with _lock_v2:
        if _state_v2["status"] == "running":
            return False

    _thread_v2 = threading.Thread(
        target=_run_advisor_v2,
        args=(n_symbols, n_trials),
        daemon=True,
        name="advisor-runner-v2",
    )
    _thread_v2.start()
    return True
