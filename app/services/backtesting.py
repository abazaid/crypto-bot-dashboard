from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.services.binance_public import get_klines
from app.services.paper_trading import _ema, _depth_multiplier


@dataclass
class BacktestTrade:
    opened_at: datetime
    closed_at: datetime
    entry_price: float
    exit_price: float
    avg_price: float
    invested: float
    qty: float
    pnl_usdt: float
    pnl_pct: float
    close_reason: str
    dca_done: int
    dca_total: int


def _parse_time(ms: int) -> datetime:
    return datetime.utcfromtimestamp(float(ms) / 1000.0)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _profile_multiplier(strategy_mode: str) -> float:
    mode = str(strategy_mode or "balanced").strip().lower()
    if mode == "aggressive":
        return 0.9
    if mode == "conservative":
        return 1.2
    return 1.0


def _score_for_zone(
    idx: int,
    trigger_price: float,
    lookback: list[list[float]],
    ema50: float | None,
    ema100: float | None,
    ema200: float | None,
) -> float:
    if not lookback:
        return 50.0
    lows = [k[2] for k in lookback]
    closes = [k[4] for k in lookback]
    vols = [k[5] for k in lookback]
    if not lows or not closes or not vols or trigger_price <= 0:
        return 50.0

    band = trigger_price * 0.01
    touch_count = sum(1 for l in lows if abs(l - trigger_price) <= band)
    reaction_strength = _clamp((touch_count / 4.0) * 25.0, 0.0, 25.0)

    p_min = min(closes)
    p_max = max(closes)
    volume_profile = 15.0
    if p_max > p_min:
        bins = 28
        step = (p_max - p_min) / bins
        buckets = [0.0 for _ in range(bins)]
        for c, v in zip(closes, vols):
            j = int((c - p_min) / step) if step > 0 else 0
            j = max(0, min(j, bins - 1))
            buckets[j] += v
        zone_i = int((trigger_price - p_min) / step) if step > 0 else 0
        zone_i = max(0, min(zone_i, bins - 1))
        zone_vol = buckets[zone_i]
        avg_bucket = (sum(buckets) / len(buckets)) if buckets else 1.0
        volume_profile = _clamp((zone_vol / max(avg_bucket, 1e-9)) * 15.0, 0.0, 30.0)

    liq_ratio = (sum(vols[-12:]) / max(sum(vols[-36:]), 1e-9)) if len(vols) >= 36 else 0.4
    liquidity = _clamp(liq_ratio * 62.5, 0.0, 25.0)

    tf_matches = 0
    for ema in (ema50, ema100, ema200):
        if ema and abs(trigger_price - ema) / max(trigger_price, 1e-9) <= 0.02:
            tf_matches += 1
    timeframe_confluence = (tf_matches / 3.0) * 20.0

    # Slight depth preference for deeper zones.
    depth_boost = min(8.0, idx * 1.6)
    score = volume_profile + liquidity + reaction_strength + timeframe_confluence + depth_boost
    return _clamp(score, 20.0, 95.0)


def _build_plan_from_entry(
    entry_price: float,
    entry_amount_usdt: float,
    strategy_mode: str,
    lookback: list[list[float]],
) -> list[dict]:
    canonical_drops = [5.0, 10.0, 17.0, 25.0, 35.0, 45.0]
    closes = [k[4] for k in lookback]
    ema50 = _ema(closes, 50) if len(closes) >= 50 else None
    ema100 = _ema(closes, 100) if len(closes) >= 100 else None
    ema200 = _ema(closes, 200) if len(closes) >= 200 else None
    profile_mult = _profile_multiplier(strategy_mode)

    raw_amounts: list[float] = []
    score_rows: list[float] = []
    for i, drop in enumerate(canonical_drops):
        trigger = entry_price * (1.0 - (drop / 100.0))
        score = _score_for_zone(i, trigger, lookback, ema50, ema100, ema200)
        score_rows.append(score)
        amount = entry_amount_usdt * (score / 100.0) * _depth_multiplier(drop) * profile_mult
        raw_amounts.append(max(0.0, amount))

    # Risk cap (entry + DCA <= entry * 6)
    dca_budget = entry_amount_usdt * 5.0
    total_raw = sum(raw_amounts)
    scale = (dca_budget / total_raw) if total_raw > dca_budget > 0 else 1.0
    scaled = [x * scale for x in raw_amounts]

    rows = []
    for i, drop in enumerate(canonical_drops):
        dca_usdt = scaled[i]
        alloc_pct = (dca_usdt / max(entry_amount_usdt, 1e-9)) * 100.0
        rows.append(
            {
                "name": f"SMART-DCA-{i+1}",
                "drop_pct": float(drop),
                "allocation_pct": float(alloc_pct),
                "dca_usdt": float(dca_usdt),
                "trigger_price": float(entry_price * (1.0 - (drop / 100.0))),
                "support_score": float(score_rows[i]),
            }
        )
    return rows


def run_smart_backtest(
    symbol: str,
    strategy_mode: str = "balanced",
    entry_amount_usdt: float = 15.0,
    tp_pct: float = 1.5,
    sl_pct: float | None = 5.0,
    interval: str = "1h",
    candles: int = 700,
) -> dict:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "Missing symbol."}
    if entry_amount_usdt <= 0:
        return {"ok": False, "error": "Entry amount must be > 0."}

    try:
        raw = get_klines(sym, interval, max(300, min(int(candles), 1000)))
    except Exception as e:
        return {"ok": False, "error": f"Failed to fetch klines: {e}"}

    if len(raw) < 280:
        return {"ok": False, "error": "Not enough historical candles to backtest."}

    bars: list[list[float]] = []
    for k in raw:
        bars.append(
            [
                float(k[0]),  # open_time
                float(k[1]),  # open
                float(k[2]),  # high
                float(k[3]),  # low
                float(k[4]),  # close
                float(k[5]),  # volume
            ]
        )

    warmup = 220
    capital_base = float(entry_amount_usdt) * 6.0
    realized_total = 0.0
    equity_curve: list[tuple[datetime, float]] = []
    trades: list[BacktestTrade] = []

    open_pos: dict | None = None
    for i in range(warmup, len(bars)):
        bt = bars[i]
        t = _parse_time(int(bt[0]))
        o, h, l, c = bt[1], bt[2], bt[3], bt[4]

        if open_pos is None:
            entry_price = c
            plan = _build_plan_from_entry(entry_price, entry_amount_usdt, strategy_mode, bars[max(0, i - 240) : i])
            qty = entry_amount_usdt / max(entry_price, 1e-12)
            open_pos = {
                "opened_at": t,
                "entry_price": entry_price,
                "avg_price": entry_price,
                "invested": entry_amount_usdt,
                "qty": qty,
                "tp_price": entry_price * (1.0 + (max(tp_pct, 0.0) / 100.0)),
                "plan": plan,
                "dca_done": 0,
            }

        # Execute DCA levels if touched.
        if open_pos:
            for row in open_pos["plan"]:
                if row.get("filled"):
                    continue
                trigger = float(row["trigger_price"])
                if l <= trigger:
                    dca_usdt = float(row["dca_usdt"])
                    if dca_usdt <= 0:
                        row["filled"] = True
                        continue
                    dca_qty = dca_usdt / max(trigger, 1e-12)
                    open_pos["invested"] += dca_usdt
                    open_pos["qty"] += dca_qty
                    open_pos["avg_price"] = open_pos["invested"] / max(open_pos["qty"], 1e-12)
                    open_pos["tp_price"] = open_pos["avg_price"] * (1.0 + (max(tp_pct, 0.0) / 100.0))
                    open_pos["dca_done"] += 1
                    row["filled"] = True

            # Exit priority: TP first then SL.
            close_reason = None
            exit_price = None
            if h >= float(open_pos["tp_price"]):
                close_reason = "TP"
                exit_price = float(open_pos["tp_price"])
            elif sl_pct is not None and float(sl_pct) > 0:
                sl_price = float(open_pos["avg_price"]) * (1.0 - (float(sl_pct) / 100.0))
                if l <= sl_price:
                    close_reason = "SL"
                    exit_price = sl_price

            if close_reason and exit_price is not None:
                invested = float(open_pos["invested"])
                qty = float(open_pos["qty"])
                pnl = (exit_price * qty) - invested
                pnl_pct = (pnl / invested) * 100.0 if invested > 0 else 0.0
                trade = BacktestTrade(
                    opened_at=open_pos["opened_at"],
                    closed_at=t,
                    entry_price=float(open_pos["entry_price"]),
                    exit_price=float(exit_price),
                    avg_price=float(open_pos["avg_price"]),
                    invested=invested,
                    qty=qty,
                    pnl_usdt=pnl,
                    pnl_pct=pnl_pct,
                    close_reason=close_reason,
                    dca_done=int(open_pos["dca_done"]),
                    dca_total=len(open_pos["plan"]),
                )
                trades.append(trade)
                realized_total += pnl
                open_pos = None

        # Mark-to-market equity.
        unrealized = 0.0
        if open_pos:
            unrealized = (c * float(open_pos["qty"])) - float(open_pos["invested"])
        eq = capital_base + realized_total + unrealized
        equity_curve.append((t, eq))

    # Force close open trade at last close for report consistency.
    if open_pos:
        last = bars[-1]
        t = _parse_time(int(last[0]))
        close_price = float(last[4])
        invested = float(open_pos["invested"])
        qty = float(open_pos["qty"])
        pnl = (close_price * qty) - invested
        pnl_pct = (pnl / invested) * 100.0 if invested > 0 else 0.0
        trades.append(
            BacktestTrade(
                opened_at=open_pos["opened_at"],
                closed_at=t,
                entry_price=float(open_pos["entry_price"]),
                exit_price=close_price,
                avg_price=float(open_pos["avg_price"]),
                invested=invested,
                qty=qty,
                pnl_usdt=pnl,
                pnl_pct=pnl_pct,
                close_reason="EOD",
                dca_done=int(open_pos["dca_done"]),
                dca_total=len(open_pos["plan"]),
            )
        )
        realized_total += pnl

    if not equity_curve:
        return {"ok": False, "error": "No equity data generated."}

    # MDD + recovery.
    peak = equity_curve[0][1]
    peak_time = equity_curve[0][0]
    max_dd_pct = 0.0
    max_dd_abs = 0.0
    trough_time = peak_time
    worst_peak = peak
    recovery_bars = 0
    max_recovery_bars = 0
    in_drawdown = False
    for idx, (_, eq) in enumerate(equity_curve):
        if eq > peak:
            peak = eq
            peak_time = equity_curve[idx][0]
            if in_drawdown:
                in_drawdown = False
                if recovery_bars > max_recovery_bars:
                    max_recovery_bars = recovery_bars
                recovery_bars = 0
        dd_abs = peak - eq
        dd_pct = (dd_abs / peak) * 100.0 if peak > 0 else 0.0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            max_dd_abs = dd_abs
            trough_time = equity_curve[idx][0]
            worst_peak = peak
            in_drawdown = True
            recovery_bars = 0
        elif in_drawdown:
            recovery_bars += 1

    if in_drawdown and recovery_bars > max_recovery_bars:
        max_recovery_bars = recovery_bars

    total_trades = len(trades)
    wins = sum(1 for tr in trades if tr.pnl_usdt > 0)
    losses = sum(1 for tr in trades if tr.pnl_usdt < 0)
    net_pnl = sum(tr.pnl_usdt for tr in trades)
    total_invested = sum(tr.invested for tr in trades)
    roi_reserved = (net_pnl / capital_base) * 100.0 if capital_base > 0 else 0.0
    roi_invested = (net_pnl / total_invested) * 100.0 if total_invested > 0 else 0.0
    avg_trade_bars = 0.0
    if trades:
        avg_trade_bars = sum(max(1.0, (tr.closed_at - tr.opened_at).total_seconds() / 3600.0) for tr in trades) / len(trades)

    trade_rows = [
        {
            "opened_at": tr.opened_at.strftime("%Y-%m-%d %H:%M"),
            "closed_at": tr.closed_at.strftime("%Y-%m-%d %H:%M"),
            "entry_price": round(tr.entry_price, 8),
            "avg_price": round(tr.avg_price, 8),
            "exit_price": round(tr.exit_price, 8),
            "invested": round(tr.invested, 4),
            "pnl_usdt": round(tr.pnl_usdt, 4),
            "pnl_pct": round(tr.pnl_pct, 3),
            "close_reason": tr.close_reason,
            "dca_used": f"{tr.dca_done}/{tr.dca_total}",
        }
        for tr in trades[-120:]
    ]

    return {
        "ok": True,
        "symbol": sym,
        "strategy_mode": str(strategy_mode).lower(),
        "interval": interval,
        "candles": len(bars),
        "summary": {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round((wins / total_trades) * 100.0, 2) if total_trades else 0.0,
            "net_pnl_usdt": round(net_pnl, 4),
            "roi_reserved_pct": round(roi_reserved, 3),
            "roi_invested_pct": round(roi_invested, 3),
            "max_drawdown_pct": round(max_dd_pct, 3),
            "max_drawdown_usdt": round(max_dd_abs, 4),
            "recovery_bars": int(max_recovery_bars),
            "recovery_note": (
                f"Peak {worst_peak:.2f} -> trough at {trough_time.strftime('%Y-%m-%d %H:%M')}, "
                f"max recovery bars={int(max_recovery_bars)}"
            ),
            "avg_trade_hours": round(avg_trade_bars, 2),
        },
        "trade_rows": trade_rows,
    }
