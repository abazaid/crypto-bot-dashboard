"""
Channel track-record audit: replay past channel posts against real Binance candles.

For every parsed signal we simulate OUR execution rules (enter inside the zone within the
window, sell the channel's fraction at each target, stop -> breakeven after target 1 and
-> previous target after later ones, hard stop at the channel level) and report what would
have happened. Conservative tie-break: when a candle touches both the stop and a target,
the stop is assumed to have hit first.

Pure simulation + public market data. No orders, no ledger.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings
from app.services.binance_public import get_klines_range
from app.services.telegram_signals import ParsedSignal, parse_signal, validate_signal

logger = logging.getLogger(__name__)

FEE_RT_PCT = 2.0 * float(settings.trading_fee_pct)


@dataclass
class AuditRow:
    msg_id: int
    posted_at: str
    symbol: str
    status: str  # entered/stop/done/open/missed/invalid/not_binance/unparsed
    note: str = ""
    entry: Optional[float] = None
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    stop: Optional[float] = None
    targets_hit: int = 0
    targets_total: int = 0
    channel_marks: int = 0  # ✅ count the channel itself added by editing the post
    exit_price: Optional[float] = None
    pnl_pct: Optional[float] = None  # under our rules, net of round-trip fees, on the full position
    max_gain_pct: Optional[float] = None
    duration_h: Optional[float] = None
    raw_text: str = ""
    hits: list[str] = field(default_factory=list)


def _interval_for_age(age_hours: float) -> str:
    if age_hours <= 7 * 24:
        return "15m"
    if age_hours <= 40 * 24:
        return "1h"
    return "4h"


def simulate(sig: ParsedSignal, klines: list[list], posted_ms: int, window_hours: float) -> dict:
    """Replay one signal over candles [open_time, open, high, low, close, ...]. Returns a result dict."""
    entry: Optional[float] = None
    entry_ms: Optional[int] = None
    stop = float(sig.stop_price)
    remaining = 1.0
    realized = 0.0  # sum(fraction * (exit/entry - 1))
    hits: list[str] = []
    max_high = 0.0
    last_close = 0.0
    result = "open"
    exit_price: Optional[float] = None
    exit_ms: Optional[int] = None
    targets = list(sig.targets)
    next_target = 0
    window_ms = int(window_hours * 3600 * 1000)

    for k in klines:
        t = int(k[0])
        o, h, lo, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        last_close = c
        if entry is None:
            if t > posted_ms + window_ms:
                return {"status": "missed", "note": "price never entered the zone within the window"}
            if lo <= stop:
                # touched the stop before ever giving an entry: not our trade
                if o <= sig.entry_high:
                    return {"status": "invalid", "note": "opened inside the zone but the same candle hit the stop"}
                continue
            if o <= sig.entry_high:
                entry = o
            elif lo <= sig.entry_high:
                entry = sig.entry_high
            if entry is not None:
                entry_ms = t
                max_high = max(max_high, h)
                # do not evaluate targets on the entry candle (ambiguous ordering)
            continue

        max_high = max(max_high, h)
        if lo <= stop:
            exit_price = stop
            realized += remaining * (stop / entry - 1.0)
            remaining = 0.0
            exit_ms = t
            result = "stop" if next_target == 0 else ("done" if next_target >= len(targets) else "trail")
            break
        while next_target < len(targets) and h >= float(targets[next_target].price):
            tg = targets[next_target]
            frac = min(remaining, float(tg.sell_fraction)) if next_target < len(targets) - 1 else remaining
            realized += frac * (float(tg.price) / entry - 1.0)
            remaining -= frac
            hits.append(f"T{next_target + 1}")
            stop = max(stop, entry * (1.0 + FEE_RT_PCT / 100.0) if next_target == 0 else float(targets[next_target - 1].price))
            next_target += 1
            if remaining <= 1e-9:
                exit_price = float(tg.price)
                exit_ms = t
                result = "done"
                break
        if remaining <= 1e-9:
            break

    if entry is None:
        return {"status": "missed", "note": "no candle reached the entry zone yet"}
    if remaining > 1e-9:
        # still open: mark to market
        realized_total = realized + remaining * (last_close / entry - 1.0)
        exit_price = last_close
        result = "open"
        note = f"open, {len(hits)}/{len(targets)} targets so far"
    else:
        realized_total = realized
        note = {"stop": "stopped out before any target", "trail": "raised stop hit after targets", "done": "all targets reached"}.get(result, "")
    pnl_pct = realized_total * 100.0 - FEE_RT_PCT
    end_ms = exit_ms or int(klines[-1][0]) if klines else posted_ms
    return {
        "status": result,
        "note": note,
        "entry": entry,
        "stop": stop,
        "hits": hits,
        "targets_hit": len(hits),
        "exit_price": exit_price,
        "pnl_pct": pnl_pct,
        "max_gain_pct": (max_high / entry - 1.0) * 100.0 if max_high > 0 else 0.0,
        "duration_h": ((end_ms - (entry_ms or posted_ms)) / 3600000.0) if entry_ms else None,
    }


def audit_posts(posts: list[dict], window_hours: Optional[float] = None) -> dict:
    """
    posts: [{"id": int, "date": datetime(tz-aware), "text": str}], newest first or any order.
    Returns {"rows": [AuditRow-dicts], "summary": {...}}.
    """
    window = float(window_hours or settings.telegram_entry_window_hours)
    rows: list[AuditRow] = []
    now_ms = int(time.time() * 1000)
    for p in sorted(posts, key=lambda x: x["id"], reverse=True):
        text = p.get("text") or ""
        date = p.get("date")
        posted_ms = int(date.timestamp() * 1000) if isinstance(date, datetime) else now_ms
        posted_at = date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") if isinstance(date, datetime) else ""
        marks = text.count("✅")
        sig = parse_signal(text)
        if sig is None:
            continue  # chatter / results posts are not signals
        row = AuditRow(msg_id=int(p["id"]), posted_at=posted_at, symbol=sig.symbol, status="", entry_low=sig.entry_low, entry_high=sig.entry_high, stop=sig.stop_price, targets_total=len(sig.targets), channel_marks=marks, raw_text=text)
        problems = validate_signal(sig)
        if problems:
            row.status = "not_binance" if not sig.is_binance else "invalid"
            row.note = "; ".join(problems)
            rows.append(row)
            continue
        age_h = max(0.0, (now_ms - posted_ms) / 3600000.0)
        interval = _interval_for_age(age_h)
        try:
            klines = get_klines_range(sig.symbol, interval, posted_ms, limit=1000)
        except Exception as exc:
            row.status = "error"
            row.note = f"klines: {exc}"[:200]
            rows.append(row)
            continue
        if not klines:
            row.status = "error"
            row.note = "no candles returned (symbol not on Binance spot?)"
            rows.append(row)
            continue
        res = simulate(sig, klines, posted_ms, window)
        row.status = res["status"]
        row.note = res.get("note", "")
        row.entry = res.get("entry")
        row.targets_hit = int(res.get("targets_hit", 0))
        row.hits = list(res.get("hits", []))
        row.exit_price = res.get("exit_price")
        row.pnl_pct = res.get("pnl_pct")
        row.max_gain_pct = res.get("max_gain_pct")
        row.duration_h = res.get("duration_h")
        rows.append(row)

    traded = [r for r in rows if r.status in {"stop", "trail", "done", "open"}]
    closed = [r for r in traded if r.status != "open"]
    pnls = [r.pnl_pct for r in traded if r.pnl_pct is not None]
    summary = {
        "posts_scanned": len(posts),
        "signals": len(rows),
        "executable": sum(1 for r in rows if r.status not in {"invalid", "not_binance", "error"}),
        "traded": len(traded),
        "missed": sum(1 for r in rows if r.status == "missed"),
        "stopped_before_t1": sum(1 for r in traded if r.status == "stop"),
        "hit_t1": sum(1 for r in traded if r.targets_hit >= 1),
        "hit_all": sum(1 for r in traded if r.status == "done"),
        "open": sum(1 for r in traded if r.status == "open"),
        "avg_pnl_pct": (sum(pnls) / len(pnls)) if pnls else None,
        "sum_pnl_pct": sum(pnls) if pnls else 0.0,
        "win_rate_pct": (100.0 * sum(1 for r in closed if (r.pnl_pct or 0) > 0) / len(closed)) if closed else None,
        "per_30_usdt": (sum(pnls) / 100.0 * 30.0) if pnls else 0.0,
    }
    return {"rows": [r.__dict__ for r in rows], "summary": summary}
