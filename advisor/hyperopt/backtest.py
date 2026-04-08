"""
Simple vectorised backtest engine for the Hyperopt module.
Simulates a DCA entry strategy and returns performance metrics.

Strategy logic (mirrors crypto-bots paper trading):
  - Entry: RSI < entry_rsi AND price near BB lower (bb_pct < entry_bb_pct)
  - DCA 1: price drops dca_drop_1% from entry → buy dca_alloc_1% more
  - DCA 2: price drops dca_drop_2% from entry → buy dca_alloc_2% more
  - Exit:  price rises tp_pct% above average_price → TP
           price falls sl_pct% below average_price → SL
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def run_backtest(df: pd.DataFrame, params: dict) -> dict:
    """
    Run a backtest on an indicator-enriched OHLCV DataFrame.

    params keys:
        entry_rsi       float  RSI threshold to enter (e.g. 38)
        entry_bb_pct    float  BB% threshold to enter (e.g. 0.25 = near lower band)
        dca_drop_1      float  First DCA drop % from entry (e.g. 5.0)
        dca_drop_2      float  Second DCA drop % from entry (e.g. 12.0)
        dca_alloc_1     float  First DCA size as % of initial (e.g. 100)
        dca_alloc_2     float  Second DCA size as % of initial (e.g. 150)
        tp_pct          float  Take profit % from avg price (e.g. 4.0)
        sl_pct          float  Stop loss % below avg price (e.g. 15.0)

    Returns dict with:
        total_trades, win_trades, lose_trades, win_rate,
        avg_profit_pct, max_drawdown_pct, sharpe_ratio,
        avg_hold_candles, total_pnl_pct
    """
    entry_rsi    = float(params.get("entry_rsi",    38.0))
    entry_bb_pct = float(params.get("entry_bb_pct",  0.25))
    dca_drop_1   = float(params.get("dca_drop_1",    5.0))
    dca_drop_2   = float(params.get("dca_drop_2",   12.0))
    dca_alloc_1  = float(params.get("dca_alloc_1", 100.0))
    dca_alloc_2  = float(params.get("dca_alloc_2", 150.0))
    tp_pct       = float(params.get("tp_pct",        4.0))
    sl_pct       = float(params.get("sl_pct",       15.0))

    closes = df["close"].values
    rsis   = df["rsi"].values if "rsi" in df.columns else np.full(len(df), 50.0)
    bb_pct = df["bb_pct"].values if "bb_pct" in df.columns else np.full(len(df), 0.5)

    trades_pnl:   list[float] = []
    hold_candles: list[int]   = []

    i = 0
    n = len(closes)

    while i < n - 1:
        # ── Entry condition ───────────────────────────────────────────────
        if rsis[i] >= entry_rsi or bb_pct[i] >= entry_bb_pct:
            i += 1
            continue

        entry_price  = closes[i]
        total_cost   = entry_price        # 1 unit invested
        total_qty    = 1.0
        avg_price    = entry_price
        entry_candle = i

        dca1_done = False
        dca2_done = False

        # ── Simulate trade candle by candle ───────────────────────────────
        j = i + 1
        closed = False
        while j < n:
            price = closes[j]

            # DCA 1
            if not dca1_done and price <= entry_price * (1 - dca_drop_1 / 100):
                extra_cost  = entry_price * (dca_alloc_1 / 100)
                extra_qty   = extra_cost / price
                total_cost += extra_cost
                total_qty  += extra_qty
                avg_price   = total_cost / total_qty
                dca1_done   = True

            # DCA 2
            if not dca2_done and price <= entry_price * (1 - dca_drop_2 / 100):
                extra_cost  = entry_price * (dca_alloc_2 / 100)
                extra_qty   = extra_cost / price
                total_cost += extra_cost
                total_qty  += extra_qty
                avg_price   = total_cost / total_qty
                dca2_done   = True

            # Take profit
            if price >= avg_price * (1 + tp_pct / 100):
                pnl = (price * total_qty - total_cost) / total_cost * 100
                trades_pnl.append(pnl)
                hold_candles.append(j - entry_candle)
                closed = True
                i = j + 1
                break

            # Stop loss
            if price <= avg_price * (1 - sl_pct / 100):
                pnl = (price * total_qty - total_cost) / total_cost * 100
                trades_pnl.append(pnl)
                hold_candles.append(j - entry_candle)
                closed = True
                i = j + 1
                break

            j += 1

        if not closed:
            # Force close at end of data
            price = closes[-1]
            pnl = (price * total_qty - total_cost) / total_cost * 100
            trades_pnl.append(pnl)
            hold_candles.append(n - entry_candle - 1)
            i = n

    # ── Metrics ───────────────────────────────────────────────────────────
    if not trades_pnl:
        return _empty_metrics()

    arr         = np.array(trades_pnl)
    wins        = int((arr > 0).sum())
    losses      = int((arr <= 0).sum())
    total       = len(arr)
    win_rate    = wins / total * 100 if total else 0.0
    avg_profit  = float(arr.mean())
    total_pnl   = float(arr.sum())
    avg_hold    = float(np.mean(hold_candles)) if hold_candles else 0.0

    # Sharpe (using trade returns as series, risk-free = 0)
    std = float(arr.std())
    sharpe = float(arr.mean() / std) if std > 1e-9 else 0.0

    # Max drawdown on cumulative PnL curve
    cumulative = np.cumsum(arr)
    peak = np.maximum.accumulate(cumulative)
    drawdown = peak - cumulative
    max_dd = float(drawdown.max()) if len(drawdown) else 0.0

    return {
        "total_trades":     total,
        "win_trades":       wins,
        "lose_trades":      losses,
        "win_rate":         round(win_rate, 2),
        "avg_profit_pct":   round(avg_profit, 3),
        "total_pnl_pct":    round(total_pnl, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio":     round(sharpe, 4),
        "avg_hold_candles": round(avg_hold, 1),
    }


def _empty_metrics() -> dict:
    return {
        "total_trades":     0,
        "win_trades":       0,
        "lose_trades":      0,
        "win_rate":         0.0,
        "avg_profit_pct":   0.0,
        "total_pnl_pct":    0.0,
        "max_drawdown_pct": 0.0,
        "sharpe_ratio":     -99.0,
        "avg_hold_candles": 0.0,
    }
