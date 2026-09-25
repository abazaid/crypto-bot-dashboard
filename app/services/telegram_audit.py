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
    # the same signal replayed with the CHANNEL's own rules (market entry at post, 4h-close stop)
    ch_status: str = ""
    ch_entry: Optional[float] = None
    ch_targets_hit: int = 0
    ch_hits: list[str] = field(default_factory=list)
    ch_pnl_pct: Optional[float] = None
    ch_note: str = ""


def _interval_for_age(age_hours: float) -> str:
    if age_hours <= 7 * 24:
        return "15m"
    if age_hours <= 40 * 24:
        return "1h"
    return "4h"


TARGET_LOCK = 0.5  # stop after target n = prev level + 50% of the leg
RUNNER_GIVEBACK = 0.30  # runner after the last target trails 30% below its peak
GIVEBACK = 0.50  # between targets
LAST_TARGET_SELL = 0.0  # nothing sold at the last target: the whole last slice runs behind the trailing stop


ENTRY_SPLIT = 0.5  # half at first touch of the zone, half at the zone bottom (before T1, within 72h)


def simulate(sig: ParsedSignal, klines: list[list], posted_ms: int, window_hours: float, target_lock: float = TARGET_LOCK, giveback: float = GIVEBACK, runner_giveback: float = RUNNER_GIVEBACK, last_target_sell: float = LAST_TARGET_SELL, entry_split: float = ENTRY_SPLIT) -> dict:
    """
    Replay one signal over candles [open_time, open, high, low, close, ...] under OUR rules.
    PnL is expressed on the FULL allocated amount: if the second leg never fills, only the first
    leg's share of capital was at work.
    """
    entry: Optional[float] = None
    leg2_price = float(sig.entry_low)
    leg2_pending = False
    leg2_deadline = 0
    invested_share = 1.0
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
                if 0.0 < entry_split < 1.0 and entry > leg2_price * 1.002:
                    leg2_pending = True
                    invested_share = entry_split
                    leg2_deadline = t + 72 * 3600 * 1000
                # do not evaluate targets on the entry candle (ambiguous ordering)
            continue

        if leg2_pending:
            if next_target == 0 and t <= leg2_deadline and lo <= leg2_price * 1.002 and lo > stop:
                # merge the second leg at the zone bottom: new average entry, full capital at work
                entry = entry * entry_split + leg2_price * (1.0 - entry_split)
                invested_share = 1.0
                leg2_pending = False
            elif next_target > 0 or t > leg2_deadline:
                leg2_pending = False

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
            is_last = next_target == len(targets) - 1
            frac = min(remaining, float(tg.sell_fraction) * (last_target_sell if is_last else 1.0))
            realized += frac * (float(tg.price) / entry - 1.0)
            remaining -= frac
            hits.append(f"T{next_target + 1}")
            prev_level = entry if next_target == 0 else float(targets[next_target - 1].price)
            lock_stop = float(tg.price) * 0.997 if target_lock >= 0.999 else prev_level + target_lock * (float(tg.price) - prev_level)
            stop = max(stop, entry * (1.0 + FEE_RT_PCT / 100.0), lock_stop)
            next_target += 1
        # give-back guard from the peak (tighter for the runner after the last target)
        if next_target > 0:
            gb = runner_giveback if next_target >= len(targets) else giveback
            if gb < 1.0:
                stop = max(stop, entry + (max_high - entry) * (1.0 - gb))
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
    pnl_pct = (realized_total * 100.0 - FEE_RT_PCT) * invested_share
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


def simulate_channel_rules(sig: ParsedSignal, fine: list[list], k4h: list[list], posted_ms: int) -> dict:
    """
    Replay the signal the way the channel itself accounts for it:
      * entry at market on the first candle after the post (no waiting for the zone)
      * a target counts when the price touches it (fine candles)
      * stop only when a 4h candle CLOSES below the stop level (exit at that close)
      * fractions sold at targets, remainder out at the stop close or marked to market
    """
    if not fine:
        return {"status": "error", "note": "no candles"}
    entry = float(fine[0][1])
    entry_ms = int(fine[0][0])
    if entry <= 0:
        return {"status": "error", "note": "bad entry candle"}
    targets = list(sig.targets)
    next_target = 0
    remaining = 1.0
    realized = 0.0
    hits: list[str] = []
    max_high = 0.0
    result = "open"
    exit_price: Optional[float] = None
    exit_ms: Optional[int] = None
    four_h = 4 * 3600 * 1000
    stop_closes = [(int(k[0]) + four_h, float(k[4])) for k in k4h if int(k[0]) + four_h > entry_ms and float(k[4]) < float(sig.stop_price)]
    next_stop = stop_closes[0] if stop_closes else None
    last_close = entry
    for k in fine:
        t = int(k[0])
        h, c = float(k[2]), float(k[4])
        last_close = c
        if next_stop and t >= next_stop[0]:
            exit_price = next_stop[1]
            realized += remaining * (exit_price / entry - 1.0)
            remaining = 0.0
            exit_ms = next_stop[0]
            result = "stop" if next_target == 0 else "stop_after_targets"
            break
        max_high = max(max_high, h)
        while next_target < len(targets) and h >= float(targets[next_target].price):
            tg = targets[next_target]
            frac = min(remaining, float(tg.sell_fraction)) if next_target < len(targets) - 1 else remaining
            realized += frac * (float(tg.price) / entry - 1.0)
            remaining -= frac
            hits.append(f"T{next_target + 1}")
            next_target += 1
            if remaining <= 1e-9:
                exit_price, exit_ms, result = float(tg.price), t, "done"
                break
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        total = realized + remaining * (last_close / entry - 1.0)
        exit_price = last_close
        note = f"open, {len(hits)}/{len(targets)} targets so far"
    else:
        total = realized
        note = {"stop": "4h close below stop before any target", "stop_after_targets": "4h close below stop after targets", "done": "all targets reached"}.get(result, "")
    return {
        "status": result,
        "note": note,
        "entry": entry,
        "hits": hits,
        "targets_hit": len(hits),
        "exit_price": exit_price,
        "pnl_pct": total * 100.0 - FEE_RT_PCT,
        "max_gain_pct": (max_high / entry - 1.0) * 100.0 if max_high > 0 else 0.0,
        "duration_h": ((exit_ms or int(fine[-1][0])) - entry_ms) / 3600000.0,
    }


def audit_posts(posts: list[dict], window_hours: Optional[float] = None) -> dict:
    """
    posts: [{"id": int, "date": datetime(tz-aware), "text": str}], newest first or any order.
    Returns {"rows": [AuditRow-dicts], "summary": {...}}.
    """
    window = float(window_hours or settings.telegram_entry_window_hours)
    rows: list[AuditRow] = []
    replay: list[tuple] = []
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
        wide_only = bool(problems) and all("too wide" in p for p in problems)
        if problems and not wide_only:
            row.status = "not_binance" if not sig.is_binance else "invalid"
            row.note = "; ".join(problems)
            rows.append(row)
            continue
        wide_note = " | WIDE STOP: live trading would skip this signal" if wide_only else ""
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
        try:
            k4h = get_klines_range(sig.symbol, "4h", posted_ms - 4 * 3600 * 1000, limit=1000)
        except Exception as exc:
            k4h = []
            logger.warning("4h klines failed for %s: %s", sig.symbol, exc)
        ch = simulate_channel_rules(sig, klines, k4h, posted_ms)
        row.ch_status = ch["status"]
        row.ch_entry = ch.get("entry")
        row.ch_targets_hit = int(ch.get("targets_hit", 0))
        row.ch_hits = list(ch.get("hits", []))
        row.ch_pnl_pct = ch.get("pnl_pct")
        row.ch_note = ch.get("note", "")
        replay.append((sig, klines, posted_ms))
        res = simulate(sig, klines, posted_ms, window)
        row.status = res["status"]
        row.note = (res.get("note", "") + wide_note).strip(" |")
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
    ch_rows = [r for r in rows if r.ch_status in {"stop", "stop_after_targets", "done", "open"}]
    ch_closed = [r for r in ch_rows if r.ch_status != "open"]
    ch_pnls = [r.ch_pnl_pct for r in ch_rows if r.ch_pnl_pct is not None]
    summary = {
        "ch_traded": len(ch_rows),
        "ch_hit_t1": sum(1 for r in ch_rows if r.ch_targets_hit >= 1),
        "ch_hit_all": sum(1 for r in ch_rows if r.ch_status == "done"),
        "ch_stopped": sum(1 for r in ch_rows if r.ch_status in {"stop", "stop_after_targets"}),
        "ch_open": sum(1 for r in ch_rows if r.ch_status == "open"),
        "ch_avg_pnl_pct": (sum(ch_pnls) / len(ch_pnls)) if ch_pnls else None,
        "ch_sum_pnl_pct": sum(ch_pnls) if ch_pnls else 0.0,
        "ch_win_rate_pct": (100.0 * sum(1 for r in ch_closed if (r.ch_pnl_pct or 0) > 0) / len(ch_closed)) if ch_closed else None,
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
    # Parameter sweep: which lock / give-back combination would have done best on THIS channel.
    sweep = []
    for lock in (0.0, 0.25, 0.5, 0.75, 1.0):
        for gb in (0.5, 1.0):
            pn = []
            wins = 0
            closed = 0
            for sig, kl, pm in replay:
                r = simulate(sig, kl, pm, window, target_lock=lock, giveback=gb, runner_giveback=min(gb, 0.3) if gb < 1.0 else 0.3)
                if r.get("pnl_pct") is None:
                    continue
                pn.append(r["pnl_pct"])
                if r["status"] != "open":
                    closed += 1
                    wins += 1 if r["pnl_pct"] > 0 else 0
            sweep.append({
                "target_lock_pct": lock * 100.0,
                "giveback_pct": gb * 100.0,
                "trades": len(pn),
                "sum_pnl_pct": sum(pn) if pn else 0.0,
                "avg_pnl_pct": (sum(pn) / len(pn)) if pn else None,
                "win_rate_pct": (100.0 * wins / closed) if closed else None,
            })
    sweep.sort(key=lambda x: x["sum_pnl_pct"], reverse=True)
    return {"rows": [r.__dict__ for r in rows], "summary": summary, "sweep": sweep}
