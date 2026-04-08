"""
Report generator — combines ML predictions + Hyperopt results
into a human-readable recommendation report.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from advisor.config import REPORT_TOP_N, REPORT_DIR


def _bar(value: float, max_val: float = 1.0, width: int = 20) -> str:
    filled = int(round(value / max_val * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _signal_color(signal: str) -> str:
    return {"BUY": "🟢", "WATCH": "🟡", "SKIP": "🔴"}.get(signal, "⚪")


def generate(
    ml_predictions: list[dict],
    hyperopt_results: list[dict],
    model_metrics: dict,
    top_n: int = REPORT_TOP_N,
) -> str:
    """
    Build full report string and save to REPORT_DIR.
    Returns the report text.
    """
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # Index hyperopt results by symbol
    ho_by_sym: dict[str, dict] = {r["symbol"]: r for r in hyperopt_results}

    # ── Header ───────────────────────────────────────────────────────────────
    lines = [
        "",
        "=" * 68,
        f"  ADVISOR REPORT — {now}",
        "=" * 68,
        "",
        f"  ML Model Quality",
        f"  ├─ AUC Score   : {model_metrics.get('auc', 0):.3f}  (>0.60 = useful)",
        f"  ├─ Accuracy    : {model_metrics.get('accuracy', 0):.3f}",
        f"  ├─ F1 Score    : {model_metrics.get('f1', 0):.3f}",
        f"  ├─ Train rows  : {model_metrics.get('train_rows', 0):,}",
        f"  └─ Positive %  : {model_metrics.get('pos_rate', 0):.1f}% (buy opportunities in history)",
        "",
    ]

    # ── Top feature importance ────────────────────────────────────────────────
    fi = model_metrics.get("feature_importance", {})
    if fi:
        lines += ["  Top predictive features:", "  ─" * 25]
        for feat, imp in list(fi.items())[:8]:
            bar = _bar(imp, max(fi.values()))
            lines.append(f"  {feat:<18} {bar}  {imp:.0f}")
        lines.append("")

    # ── ML predictions ────────────────────────────────────────────────────────
    buy_signals  = [p for p in ml_predictions if p["signal"] == "BUY"]
    watch_signals = [p for p in ml_predictions if p["signal"] == "WATCH"]

    lines += [
        "=" * 68,
        f"  ML SIGNAL RANKING  (top {top_n} of {len(ml_predictions)} symbols)",
        "  Predicts: chance of ≥2% gain in next 24 candles (1h each)",
        "=" * 68,
        "",
        f"  {'#':<4} {'Symbol':<12} {'Signal':<8} {'Prob':>6}  {'RSI':>5}  {'Trend':<10}  {'24h%':>6}  {'VolRatio':>8}",
        "  " + "─" * 62,
    ]

    for rank, pred in enumerate(ml_predictions[:top_n], 1):
        icon  = _signal_color(pred["signal"])
        prob  = pred["probability"] * 100
        lines.append(
            f"  {rank:<4} {pred['symbol']:<12} {icon} {pred['signal']:<6} "
            f"{prob:>5.1f}%  {pred['rsi']:>5.1f}  {pred['trend']:<10}  "
            f"{pred['pct_24h']:>+5.1f}%  {pred['vol_ratio']:>7.2f}x"
        )

    lines.append("")

    # ── Hyperopt results ──────────────────────────────────────────────────────
    lines += [
        "=" * 68,
        f"  HYPEROPT BEST PARAMETERS  (top {top_n} by score)",
        "  Optimised over 180 days of historical 1h candles",
        "=" * 68,
        "",
    ]

    shown_ho = hyperopt_results[:top_n]
    for res in shown_ho:
        sym     = res["symbol"]
        p       = res["best_params"]
        m       = res["metrics"]
        score   = res.get("score", 0)
        # Check if ML agrees
        ml_pred = next((x for x in ml_predictions if x["symbol"] == sym), None)
        ml_icon = _signal_color(ml_pred["signal"]) if ml_pred else "⚪"
        ml_prob = f"{ml_pred['probability']*100:.0f}%" if ml_pred else "N/A"

        lines += [
            f"  {sym:<12} score={score:.3f}  ML={ml_icon}{ml_prob}",
            f"  ├─ Entry:   RSI < {p.get('entry_rsi', 0):.0f}  │  BB% < {p.get('entry_bb_pct', 0):.2f}",
            f"  ├─ DCA 1:   -{p.get('dca_drop_1', 0):.1f}%  │  size = {p.get('dca_alloc_1', 0):.0f}% of entry",
            f"  ├─ DCA 2:   -{p.get('dca_drop_2', 0):.1f}%  │  size = {p.get('dca_alloc_2', 0):.0f}% of entry",
            f"  ├─ TP:      +{p.get('tp_pct', 0):.1f}%    SL: -{p.get('sl_pct', 0):.1f}%",
            f"  └─ Backtest: {m.get('total_trades', 0)} trades │ WinRate={m.get('win_rate', 0):.1f}% │ "
            f"AvgProfit={m.get('avg_profit_pct', 0):+.2f}% │ Sharpe={m.get('sharpe_ratio', 0):.2f}",
            "",
        ]

    # ── Combined recommendations ──────────────────────────────────────────────
    # Best symbols where BOTH ML says BUY and Hyperopt score > 0
    combined = []
    for pred in ml_predictions:
        if pred["signal"] != "BUY":
            continue
        ho = ho_by_sym.get(pred["symbol"])
        if not ho or ho.get("score", 0) <= 0:
            continue
        combined.append({
            "symbol":    pred["symbol"],
            "ml_prob":   pred["probability"],
            "ho_score":  ho["score"],
            "win_rate":  ho["metrics"].get("win_rate", 0),
            "avg_profit": ho["metrics"].get("avg_profit_pct", 0),
            "params":    ho["best_params"],
            "combined_score": pred["probability"] * ho["score"],
        })
    combined.sort(key=lambda x: x["combined_score"], reverse=True)

    lines += [
        "=" * 68,
        "  🎯  COMBINED RECOMMENDATIONS  (ML BUY + Hyperopt validated)",
        "=" * 68,
        "",
    ]

    if not combined:
        lines.append("  No symbols passed both ML + Hyperopt filters right now.")
        lines.append("  Try running again after market conditions change.")
    else:
        lines += [
            f"  {'#':<4} {'Symbol':<12} {'ML Prob':>8}  {'WinRate':>8}  {'AvgProfit':>10}  Recommended Params",
            "  " + "─" * 65,
        ]
        for rank, rec in enumerate(combined[:top_n], 1):
            p = rec["params"]
            lines.append(
                f"  {rank:<4} {rec['symbol']:<12} "
                f"{rec['ml_prob']*100:>6.1f}%   "
                f"{rec['win_rate']:>6.1f}%   "
                f"{rec['avg_profit']:>+8.2f}%   "
                f"RSI<{p.get('entry_rsi',0):.0f} | "
                f"DCA@-{p.get('dca_drop_1',0):.1f}%/-{p.get('dca_drop_2',0):.1f}% | "
                f"TP+{p.get('tp_pct',0):.1f}%"
            )

    lines += [
        "",
        "=" * 68,
        "  ⚠  DISCLAIMER: For educational use only.",
        "     Past performance does not guarantee future results.",
        "     Always test in paper mode before going live.",
        "=" * 68,
        "",
    ]

    report_text = "\n".join(lines)

    # Save to file
    Path(REPORT_DIR).mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M")
    report_path = Path(REPORT_DIR) / f"report_{ts}.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    # Also save latest.json for programmatic use
    latest = {
        "generated_at":   now,
        "ml_model":       model_metrics,
        "top_ml":         ml_predictions[:top_n],
        "top_hyperopt":   hyperopt_results[:top_n],
        "recommendations": combined[:top_n],
    }
    with open(Path(REPORT_DIR) / "latest.json", "w") as f:
        json.dump(latest, f, indent=2)

    return report_text
