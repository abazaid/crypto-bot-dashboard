"""
Advisor — main entry point.

Usage:
    # Full run (download data + train ML + hyperopt + report)
    python -m advisor.run

    # Skip data download (use cached)
    python -m advisor.run --no-fetch

    # Only ML predictions (use cached data + existing model)
    python -m advisor.run --predict-only

    # Only Hyperopt (use cached data, skip ML)
    python -m advisor.run --hyperopt-only

    # Limit symbols for a quick test
    python -m advisor.run --symbols 20 --trials 50
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# ── Setup logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("advisor")
logging.getLogger("lightgbm").setLevel(logging.ERROR)
logging.getLogger("optuna").setLevel(logging.ERROR)


def _section(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


def run(args: argparse.Namespace) -> None:
    from advisor.config import TOP_N_SYMBOLS, HYPEROPT_TRIALS
    from advisor.data.fetcher import get_top_symbols, load_all_symbols
    from advisor.features.indicators import add_indicators
    from advisor.ml_filter.trainer import run_training, load_model
    from advisor.ml_filter.predictor import predict_all
    from advisor.hyperopt.engine import optimize_all, load_results
    from advisor.report.generator import generate

    n_symbols = args.symbols or TOP_N_SYMBOLS
    n_trials  = args.trials  or HYPEROPT_TRIALS

    # ── 1. Fetch symbols ──────────────────────────────────────────────────
    _section(f"STEP 1 / 5 — Discovering top {n_symbols} Binance symbols")
    symbols = get_top_symbols(n=n_symbols)
    print(f"  Found {len(symbols)} symbols")
    print(f"  Sample: {', '.join(symbols[:10])} ...")

    # ── 2. Download OHLCV ─────────────────────────────────────────────────
    if not args.no_fetch and not args.predict_only and not args.hyperopt_only:
        _section("STEP 2 / 5 — Downloading OHLCV history (180 days @ 1h)")
        raw_dfs = load_all_symbols(symbols, force_refresh=False)
    else:
        _section("STEP 2 / 5 — Loading cached OHLCV data")
        raw_dfs = load_all_symbols(symbols, force_refresh=False)

    print(f"  Loaded {len(raw_dfs)} symbols successfully")

    # ── 3. Add indicators ─────────────────────────────────────────────────
    _section("STEP 3 / 5 — Calculating indicators")
    indicator_dfs: dict = {}
    total = len(raw_dfs)
    for i, (sym, df) in enumerate(raw_dfs.items(), 1):
        print(f"  [{i:3}/{total}] {sym}...", end="\r")
        try:
            indicator_dfs[sym] = add_indicators(df)
        except Exception as e:
            logger.warning("Indicator error for %s: %s", sym, e)

    print(f"  Done — {len(indicator_dfs)} symbols with indicators{' '*20}")

    # ── 4. ML Training + Prediction ───────────────────────────────────────
    ml_predictions = []
    model_metrics  = {}

    if not args.hyperopt_only:
        _section("STEP 4 / 5 — ML Filter (LightGBM)")

        if args.predict_only:
            print("  Loading existing model...")
            model, model_metrics = load_model()
            if model is None:
                print("  ✗ No model found — run without --predict-only first")
                sys.exit(1)
        else:
            print(f"  Training on {len(indicator_dfs)} symbols × 180 days...")
            t0 = time.monotonic()
            model, model_metrics = run_training(indicator_dfs)
            elapsed = time.monotonic() - t0
            print(f"  Training complete in {elapsed:.1f}s")

        print("  Generating predictions for all symbols...")
        ml_predictions = predict_all(indicator_dfs, model=model)

        buy_count   = sum(1 for p in ml_predictions if p["signal"] == "BUY")
        watch_count = sum(1 for p in ml_predictions if p["signal"] == "WATCH")
        skip_count  = sum(1 for p in ml_predictions if p["signal"] == "SKIP")
        print(f"  Results: 🟢 BUY={buy_count}  🟡 WATCH={watch_count}  🔴 SKIP={skip_count}")
    else:
        _section("STEP 4 / 5 — ML Filter SKIPPED (--hyperopt-only)")

    # ── 5. Hyperopt ───────────────────────────────────────────────────────
    hyperopt_results = []

    if not args.predict_only:
        _section(f"STEP 5 / 5 — Hyperopt ({n_trials} trials per symbol)")
        print(f"  Optimising {len(indicator_dfs)} symbols — this takes a while...")
        print(f"  Tip: reduce with --symbols 30 --trials 100 for a quick test\n")
        t0 = time.monotonic()
        hyperopt_results = optimize_all(indicator_dfs, n_trials=n_trials)
        elapsed = time.monotonic() - t0
        print(f"\n  Hyperopt complete in {elapsed/60:.1f} minutes")
    else:
        _section("STEP 5 / 5 — Loading cached Hyperopt results")
        hyperopt_results = load_results()
        if not hyperopt_results:
            print("  No cached results — run without --predict-only to generate them")

    # ── Report ────────────────────────────────────────────────────────────
    _section("GENERATING REPORT")
    report = generate(
        ml_predictions=ml_predictions,
        hyperopt_results=hyperopt_results,
        model_metrics=model_metrics,
    )
    print(report)
    print(f"\n  Report saved to: advisor/report/output/")
    print(f"  JSON saved to:   advisor/report/output/latest.json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crypto Advisor — ML + Hyperopt signal generator"
    )
    parser.add_argument(
        "--symbols", type=int, default=None,
        help="Number of top symbols to analyse (default: from config)",
    )
    parser.add_argument(
        "--trials", type=int, default=None,
        help="Optuna trials per symbol (default: from config)",
    )
    parser.add_argument(
        "--no-fetch", action="store_true",
        help="Skip downloading data, use cache only",
    )
    parser.add_argument(
        "--predict-only", action="store_true",
        help="Only run ML predictions using existing model and cached Hyperopt results",
    )
    parser.add_argument(
        "--hyperopt-only", action="store_true",
        help="Skip ML, only run Hyperopt",
    )
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
