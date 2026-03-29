from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.paper_v2 import ActivityLog, AppSetting, Campaign, DcaRule, Position, PositionDcaState
from app.services.binance_public import get_24h_tickers, get_klines, get_prices


def get_setting(db: Session, key: str, default: str) -> str:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if not row:
        return default
    return row.value


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


def add_log(db: Session, event_type: str, symbol: str, message: str) -> None:
    db.add(ActivityLog(event_type=event_type, symbol=symbol or "-", message=message))


def ensure_defaults(db: Session, start_balance: float) -> None:
    if db.query(AppSetting).filter(AppSetting.key == "paper_cash").first() is None:
        set_setting(db, "paper_cash", f"{start_balance:.8f}")
        add_log(db, "SYSTEM", "-", f"Initialized paper wallet: {start_balance:.2f} USDT")
        db.commit()


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _pivot_supports(prev_high: float, prev_low: float, prev_close: float) -> tuple[float, float, float]:
    p = (prev_high + prev_low + prev_close) / 3.0
    s1 = (2 * p) - prev_high
    s2 = p - (prev_high - prev_low)
    s3 = prev_low - 2 * (prev_high - p)
    return s1, s2, s3


def _trend_profile() -> str:
    try:
        kl = get_klines("BTCUSDT", "4h", 260)
    except Exception:
        return "neutral"
    closes = [float(k[4]) for k in kl]
    if len(closes) < 220:
        return "neutral"
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    if ema50 is None or ema200 is None:
        return "neutral"
    if closes[-1] < ema200 and ema50 < ema200:
        return "bearish"
    if closes[-1] > ema200 and ema50 > ema200:
        return "bullish"
    return "neutral"


def btc_market_state() -> str:
    try:
        kl = get_klines("BTCUSDT", "4h", 260)
    except Exception:
        return "neutral"
    closes = [float(k[4]) for k in kl]
    if len(closes) < 220:
        return "neutral"
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200)
    if ema50 is None or ema200 is None:
        return "neutral"

    current = closes[-1]
    down_12h = closes[-1] < closes[-4]
    below_ema200 = current < ema200
    ema_bear = ema50 < ema200
    deep_under_ema200 = current < (ema200 * 0.975)

    if below_ema200 and ema_bear and deep_under_ema200 and down_12h:
        return "strong_bearish"
    if below_ema200 and ema_bear:
        return "bearish"
    if current > ema200 and ema50 > ema200:
        return "bullish"
    return "neutral"


def _is_tradeable_usdt_symbol(symbol: str) -> bool:
    s = (symbol or "").upper().strip()
    if not s.endswith("USDT"):
        return False
    blocked_tokens = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT", "USDUSDT")
    if any(s.endswith(x) for x in blocked_tokens):
        return False
    base = s[:-4]
    stable_bases = {"USDT", "USDC", "BUSD", "TUSD", "FDUSD", "USDE", "USDD", "USDP", "DAI", "USD1"}
    if base in stable_bases:
        return False
    return True


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _historical_bounce_zones(kl4h: list[list]) -> list[dict]:
    lows = [float(k[3]) for k in kl4h]
    closes = [float(k[4]) for k in kl4h]
    pivots: list[float] = []
    for i in range(3, len(lows) - 4):
        local_min = lows[i] <= min(lows[i - 3 : i + 4])
        bounced = max(closes[i + 1 : i + 4]) >= lows[i] * 1.015
        if local_min and bounced:
            pivots.append(lows[i])
    if not pivots:
        return []
    pivots.sort()
    zones: list[dict] = []
    for p in pivots:
        if not zones:
            zones.append({"center": p, "touches": 1})
            continue
        last = zones[-1]
        if abs(p - last["center"]) / max(last["center"], 1e-9) <= 0.012:
            touches = last["touches"] + 1
            last["center"] = ((last["center"] * last["touches"]) + p) / touches
            last["touches"] = touches
        else:
            zones.append({"center": p, "touches": 1})
    zones.sort(key=lambda z: z["touches"], reverse=True)
    return zones[:6]


def _volume_nodes(kl1h: list[list], bins: int = 30) -> list[float]:
    closes = [float(k[4]) for k in kl1h]
    vols = [float(k[5]) for k in kl1h]
    if not closes:
        return []
    p_min = min(closes)
    p_max = max(closes)
    if p_max <= p_min:
        return []
    step = (p_max - p_min) / bins
    if step <= 0:
        return []
    buckets = [0.0 for _ in range(bins)]
    for p, v in zip(closes, vols):
        idx = int((p - p_min) / step)
        idx = min(max(idx, 0), bins - 1)
        buckets[idx] += v
    top_idx = sorted(range(len(buckets)), key=lambda i: buckets[i], reverse=True)[:3]
    return [p_min + (i + 0.5) * step for i in top_idx]


def _merge_support_candidates(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []
    candidates.sort(key=lambda x: x["price"])
    merged: list[dict] = []
    for c in candidates:
        if not merged:
            merged.append(
                {
                    "price": c["price"],
                    "score": c["score"],
                    "sources": {c["source"]},
                    "touches": c.get("touches", 0),
                }
            )
            continue
        last = merged[-1]
        if abs(c["price"] - last["price"]) / max(last["price"], 1e-9) <= 0.012:
            new_score = last["score"] + c["score"]
            new_sources = set(last["sources"])
            new_sources.add(c["source"])
            merged_touches = max(last["touches"], c.get("touches", 0))
            merged[-1] = {
                "price": (last["price"] + c["price"]) / 2.0,
                "score": new_score,
                "sources": new_sources,
                "touches": merged_touches,
            }
        else:
            merged.append(
                {
                    "price": c["price"],
                    "score": c["score"],
                    "sources": {c["source"]},
                    "touches": c.get("touches", 0),
                }
            )
    out = []
    for m in merged:
        confluence_bonus = 10 if len(m["sources"]) >= 2 else 0
        score = min(100.0, m["score"] + confluence_bonus)
        out.append(
            {
                "price": m["price"],
                "score": score,
                "sources": sorted(m["sources"]),
                "touches": m["touches"],
            }
        )
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def _support_engine(symbol: str) -> dict | None:
    try:
        kl1h = get_klines(symbol, "1h", 320)
        kl4h = get_klines(symbol, "4h", 360)
        kld = get_klines(symbol, "1d", 3)
    except Exception:
        return None
    if len(kl1h) < 220 or len(kl4h) < 200 or len(kld) < 2:
        return None
    price = float(kl1h[-1][4])
    if price <= 0:
        return None

    closes_1h = [float(k[4]) for k in kl1h]
    closes_4h = [float(k[4]) for k in kl4h]
    prev_d = kld[-2]
    prev_4h = kl4h[-2]
    s1d, s2d, s3d = _pivot_supports(float(prev_d[2]), float(prev_d[3]), float(prev_d[4]))
    s14, s24, s34 = _pivot_supports(float(prev_4h[2]), float(prev_4h[3]), float(prev_4h[4]))

    ema50 = _ema(closes_1h, 50)
    ema100 = _ema(closes_1h, 100)
    ema200 = _ema(closes_1h, 200)
    if ema50 is None or ema100 is None or ema200 is None:
        return None

    candidates: list[dict] = []
    for p, source in [
        (s1d, "pivot_1d"),
        (s2d, "pivot_1d"),
        (s3d, "pivot_1d"),
        (s14, "pivot_4h"),
        (s24, "pivot_4h"),
        (s34, "pivot_4h"),
    ]:
        if p > 0:
            candidates.append({"price": float(p), "score": 20.0, "source": source})
    candidates.append({"price": float(ema50), "score": 12.0, "source": "ema50"})
    candidates.append({"price": float(ema100), "score": 16.0, "source": "ema100"})
    candidates.append({"price": float(ema200), "score": 20.0, "source": "ema200"})

    for z in _historical_bounce_zones(kl4h):
        zone_score = min(30.0, 10.0 * float(z["touches"]))
        candidates.append({"price": float(z["center"]), "score": zone_score, "source": "bounce_zone", "touches": z["touches"]})

    for node in _volume_nodes(kl1h):
        candidates.append({"price": float(node), "score": 18.0, "source": "volume_node"})

    merged = _merge_support_candidates(candidates)
    supports_below = [s for s in merged if s["price"] < price]
    if not supports_below:
        return None

    strongest = supports_below[0]
    near_strong = abs(price - strongest["price"]) / price * 100.0 <= settings.dca_near_support_pct
    return {
        "price": price,
        "rsi": _rsi(closes_1h, 14) or 50.0,
        "ema200": float(ema200),
        "supports": supports_below,
        "strongest": strongest,
        "near_strong": near_strong,
        "drawdown_pct": ((max([float(k[2]) for k in kl1h[-200:]]) - price) / max([float(k[2]) for k in kl1h[-200:]])) * 100.0,
        "volume_spike": (float(kl1h[-1][5]) > (sum([float(k[5]) for k in kl1h[-21:-1]]) / 20.0) * 1.2),
    }


def _dca_scale_allocations_pct(max_levels: int = 5) -> list[float]:
    raw_scale = [
        settings.dca_scale_1,
        settings.dca_scale_2,
        settings.dca_scale_3,
        settings.dca_scale_4,
        settings.dca_scale_5,
    ][:max_levels]
    safe_scale = [max(0.0, float(s)) for s in raw_scale]
    # If later scales are missing/zero, auto-expand progressively
    # so DCA-4/5 are not dead by default.
    if safe_scale and safe_scale[0] <= 0:
        safe_scale[0] = 1.0
    for i in range(1, len(safe_scale)):
        if safe_scale[i] <= 0 and safe_scale[i - 1] > 0:
            safe_scale[i] = round(safe_scale[i - 1] * 1.2, 4)
    budget = max(0.0, settings.dca_max_symbol_allocation_x - 1.0)
    total_requested = sum(safe_scale)
    if budget <= 0.0 or total_requested <= 0.0:
        return [0.0 for _ in safe_scale]
    if total_requested <= budget:
        final_scale = safe_scale
    else:
        ratio = budget / total_requested
        final_scale = [s * ratio for s in safe_scale]
    return [round(x * 100.0, 2) for x in final_scale]


def _sl_drop_cap(sl_pct: float | None) -> float | None:
    if sl_pct is None:
        return None
    try:
        sl = float(sl_pct)
    except Exception:
        return None
    if sl <= 0:
        return None
    # Keep DCA comfortably below the stop-loss boundary.
    return max(0.8, sl * 0.85)


def _max_dca_levels_for_sl(sl_pct: float | None, hard_cap: int = 5) -> int:
    if sl_pct is None:
        return max(1, hard_cap)
    try:
        sl = float(sl_pct)
    except Exception:
        return max(1, hard_cap)
    if sl <= 0:
        return 1
    if sl <= 3:
        return min(1, hard_cap)
    if sl <= 5:
        return min(2, hard_cap)
    if sl <= 8:
        return min(3, hard_cap)
    if sl <= 12:
        return min(4, hard_cap)
    return max(1, hard_cap)


def _cap_drop_levels_to_sl(levels: list[float], sl_pct: float | None) -> list[float]:
    cap = _sl_drop_cap(sl_pct)
    if cap is None:
        return [round(max(0.8, float(x)), 2) for x in levels]
    out = []
    for x in levels:
        v = round(max(0.8, min(float(x), cap)), 2)
        if v < cap:
            out.append(v)
    out = sorted(out)
    cleaned: list[float] = []
    for v in out:
        if not cleaned:
            cleaned.append(v)
            continue
        # Keep meaningful spacing between DCA levels.
        if (v - cleaned[-1]) >= 0.5:
            cleaned.append(v)
    return cleaned


def suggest_top_symbols(limit: int = 5) -> dict:
    market_state = btc_market_state()
    raw = get_24h_tickers()
    candidates = []
    for row in raw:
        symbol = str(row.get("symbol", "")).upper()
        if not _is_tradeable_usdt_symbol(symbol):
            continue
        quote_volume = _safe_float(row.get("quoteVolume"), 0.0)
        if quote_volume < 10_000_000:
            continue
        candidates.append(
            {
                "symbol": symbol,
                "quote_volume": quote_volume,
                "price_change_pct_24h": _safe_float(row.get("priceChangePercent"), 0.0),
            }
        )
    candidates.sort(key=lambda x: x["quote_volume"], reverse=True)
    universe = candidates[:18]

    results = []
    for c in universe:
        symbol = c["symbol"]
        ctx = _support_engine(symbol)
        if not ctx:
            continue

        strongest_score = float(ctx["strongest"]["score"])
        support_distance_pct = abs(ctx["price"] - ctx["strongest"]["price"]) / ctx["price"] * 100.0
        near_support = ctx["near_strong"]
        rsi = float(ctx["rsi"])
        rsi_ok = 25.0 <= rsi <= 40.0
        volume_spike = bool(ctx["volume_spike"])
        drawdown_from_recent_high = float(ctx["drawdown_pct"])
        dip_valid = (-10.0 <= c["price_change_pct_24h"] <= -3.0) or (5.0 <= drawdown_from_recent_high <= 20.0)
        trend_good = ctx["price"] > float(ctx["ema200"])

        score = 0
        if near_support:
            score += 30
        score += min(30, int(strongest_score / 3.5))
        if rsi_ok:
            score += 20
        if volume_spike:
            score += 20
        if trend_good:
            score += 15
        if dip_valid:
            score += 15

        # BTC regime adjustment
        if market_state == "strong_bearish":
            score -= 25
        elif market_state == "bearish":
            score -= 10

        if score < 35:
            continue

        results.append(
            {
                "symbol": symbol,
                "score": score,
                "quote_volume": c["quote_volume"],
                "price_change_pct_24h": c["price_change_pct_24h"],
                "rsi": rsi,
                "support_score": strongest_score,
                "support_distance_pct": support_distance_pct,
                "drawdown_pct": drawdown_from_recent_high,
            }
        )

    results.sort(key=lambda x: (x["score"], x["quote_volume"]), reverse=True)
    picked = results[: max(1, limit)]
    return {
        "market_state": market_state,
        "items": picked,
        "symbols_csv": ",".join([x["symbol"] for x in picked]),
    }


def _symbol_ai_support_drops(symbol: str) -> list[float]:
    try:
        kl4h = get_klines(symbol, "4h", 260)
        kld = get_klines(symbol, "1d", 3)
    except Exception:
        return []
    if len(kl4h) < 220 or len(kld) < 2:
        return []

    closes = [float(k[4]) for k in kl4h]
    current = closes[-1]
    if current <= 0:
        return []

    prev_day = kld[-2]
    s1, s2, s3 = _pivot_supports(float(prev_day[2]), float(prev_day[3]), float(prev_day[4]))
    ema50 = _ema(closes, 50)
    ema100 = _ema(closes, 100)
    ema200 = _ema(closes, 200)
    raw_levels = [s1, s2, s3, ema50, ema100, ema200]
    levels = [float(x) for x in raw_levels if x is not None and x > 0 and x < current]
    drops = [((current - lv) / current) * 100.0 for lv in levels]
    filtered = [d for d in drops if 0.7 <= d <= 25.0]
    uniq = sorted(set(round(x, 2) for x in filtered))
    return uniq[:8]


def build_ai_dca_rules(symbols: list[str], sl_pct: float | None = None) -> tuple[list[tuple[str, float, float]], str, str]:
    picked = [s.strip().upper() for s in symbols if s and s.strip()]
    sample = picked[:12]
    target_levels = _max_dca_levels_for_sl(sl_pct, 5)
    allocations = _dca_scale_allocations_pct(max_levels=target_levels)
    fallback_drops = _cap_drop_levels_to_sl([2.0, 4.5, 8.0, 12.0, 16.0], sl_pct)
    if len(fallback_drops) < target_levels:
        fallback_drops = fallback_drops[:]
        cap = _sl_drop_cap(sl_pct)
        probe = fallback_drops[-1] + 0.6 if fallback_drops else 1.0
        while len(fallback_drops) < target_levels:
            if cap is not None and probe >= cap:
                break
            fallback_drops.append(round(probe, 2))
            probe += 0.6
    target_levels = min(target_levels, len(fallback_drops), len(allocations))
    if target_levels <= 0:
        target_levels = 1
        fallback_drops = [1.0]
        allocations = _dca_scale_allocations_pct(max_levels=1)
    if not sample:
        rules = [(f"AI-DCA-{i+1}", fallback_drops[i], allocations[i]) for i in range(target_levels)]
        return rules, "neutral", "Fallback AI DCA."

    symbol_drops: list[list[float]] = []
    for symbol in sample:
        ctx = _support_engine(symbol)
        if not ctx:
            continue
        price = float(ctx["price"])
        strong = [s for s in ctx["supports"] if float(s["score"]) >= settings.dca_support_score_threshold and s["price"] < price]
        strong.sort(key=lambda x: x["price"], reverse=True)
        if len(strong) < 3:
            continue
        drops = [((price - float(s["price"])) / price) * 100.0 for s in strong[:5]]
        symbol_drops.append(drops)

    profile = _trend_profile()

    if not symbol_drops:
        rules = [(f"AI-DCA-{i+1}", fallback_drops[i], allocations[i]) for i in range(target_levels)]
        return rules, profile, f"AI DCA fallback profile={profile}."

    all_flat = sorted(x for row in symbol_drops for x in row)
    n = len(all_flat)
    qs = [0.20, 0.40, 0.60, 0.80, 0.95]
    floors = [0.8, 1.2, 1.8, 2.6, 3.5]
    levels = []
    for i, q in enumerate(qs):
        d = all_flat[max(0, int(n * q) - 1)]
        levels.append(max(floors[i], d))
    levels = _cap_drop_levels_to_sl(sorted(levels), sl_pct)
    if len(levels) < target_levels:
        for f in fallback_drops:
            if len(levels) >= target_levels:
                break
            if not levels or abs(f - levels[-1]) >= 0.5:
                levels.append(round(f, 2))
    levels = levels[:target_levels]
    rules = [(f"AI-DCA-{i+1}", round(levels[i], 2), allocations[i]) for i in range(len(levels))]
    note = (
        f"AI DCA from supports (Pivot+EMA) over {len(symbol_drops)} symbols. "
        f"Trend profile={profile}. Levels={len(levels)} Drops={'/'.join([f'{x:.2f}' for x in levels])}%."
    )
    return rules, profile, note


def build_symbol_ai_dca_rules(
    symbol: str, profile: str, fallback_rules: list[tuple[str, float, float]], sl_pct: float | None = None
) -> list[tuple[str, float, float, float | None]]:
    ctx = _support_engine(symbol)
    target_levels = _max_dca_levels_for_sl(sl_pct, len(fallback_rules) if fallback_rules else 5)
    fallback_drops = _cap_drop_levels_to_sl([2.0, 4.5, 8.0, 12.0, 16.0], sl_pct)
    if len(fallback_drops) < target_levels:
        fallback_drops = fallback_drops[:]
        cap = _sl_drop_cap(sl_pct)
        probe = fallback_drops[-1] + 0.6 if fallback_drops else 1.0
        while len(fallback_drops) < target_levels:
            if cap is not None and probe >= cap:
                break
            fallback_drops.append(round(probe, 2))
            probe += 0.6
    target_levels = min(target_levels, len(fallback_drops))
    if target_levels <= 0:
        return []
    fallback_allocs = _dca_scale_allocations_pct(max_levels=target_levels)
    base_fallback = [
        (
            fallback_rules[idx][0] if idx < len(fallback_rules) else f"AI-DCA-{idx+1}",
            float(fallback_rules[idx][1]) if idx < len(fallback_rules) else fallback_drops[idx],
            float(fallback_rules[idx][2]) if idx < len(fallback_rules) else fallback_allocs[idx],
        )
        for idx in range(target_levels)
    ]
    if not ctx:
        return [(n, d, a, None) for n, d, a in base_fallback]

    price = float(ctx["price"])
    # Use symbol-specific supports even when score is below threshold.
    # Execution gate still checks score threshold later, but levels remain per-symbol.
    supports = [s for s in ctx["supports"] if s["price"] < price]
    supports.sort(key=lambda x: x["price"], reverse=True)
    if not supports:
        return [(n, d, a, None) for n, d, a in base_fallback]

    out = []
    raw_symbol_drops: list[float] = []
    raw_symbol_scores: list[float | None] = []
    for idx in range(target_levels):
        if idx < len(supports):
            support = supports[idx]
            raw_symbol_drops.append(max(0.8, ((price - float(support["price"])) / price) * 100.0))
            raw_symbol_scores.append(float(support["score"]))
        else:
            _, drop_pct, _ = base_fallback[idx]
            raw_symbol_drops.append(float(drop_pct))
            raw_symbol_scores.append(None)
    symbol_drops = _cap_drop_levels_to_sl(raw_symbol_drops, sl_pct)
    if len(symbol_drops) < target_levels:
        for _, drop_pct, _ in base_fallback:
            if len(symbol_drops) >= target_levels:
                break
            d = round(float(drop_pct), 2)
            if not symbol_drops or abs(d - symbol_drops[-1]) >= 0.5:
                symbol_drops.append(d)
    symbol_drops = symbol_drops[:target_levels]

    for idx in range(target_levels):
        name_rule = base_fallback[idx][0]
        alloc_pct = fallback_allocs[idx]
        out.append((name_rule, float(symbol_drops[idx]), round(alloc_pct, 2), raw_symbol_scores[idx]))
    return out


def _is_hammer(candle: list[float]) -> bool:
    o, h, l, c = candle
    body = abs(c - o)
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    if body <= 0:
        return False
    return lower_wick >= (body * 2.0) and upper_wick <= body


def _is_bullish_engulfing(prev_candle: list[float], cur_candle: list[float]) -> bool:
    po, _, _, pc = prev_candle
    co, _, _, cc = cur_candle
    prev_red = pc < po
    cur_green = cc > co
    return prev_red and cur_green and cc >= po and co <= pc


def _strong_breakdown(symbol: str, support_price: float) -> tuple[bool, str]:
    try:
        kl = get_klines(symbol, "15m", 80)
    except Exception:
        return False, "no_market_data"
    if len(kl) < 25:
        return False, "insufficient_data"
    closes = [float(k[4]) for k in kl]
    vols = [float(k[5]) for k in kl]
    recent_avg_vol = sum(vols[-21:-1]) / 20.0
    high_break_vol = vols[-1] > (recent_avg_vol * 1.8) if recent_avg_vol > 0 else False
    two_closes_below = closes[-1] < support_price * 0.995 and closes[-2] < support_price * 0.995
    deep_close = closes[-1] < support_price * 0.99
    broken = deep_close and (two_closes_below or high_break_vol)
    return broken, f"deep_close={deep_close} two_closes={two_closes_below} high_vol={high_break_vol}"


def _ai_dca_confirm(symbol: str, support_price: float) -> tuple[bool, bool, str]:
    try:
        kl = get_klines(symbol, "15m", 140)
    except Exception:
        return False, False, "no_market_data"
    if len(kl) < 50:
        return False, False, "insufficient_data"

    ohlc = [[float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in kl]
    closes = [x[3] for x in ohlc]
    lows = [x[2] for x in ohlc]
    volumes = [float(k[5]) for k in kl]
    current = closes[-1]
    rsi = _rsi(closes, 14)
    ema50 = _ema(closes, 50)
    if rsi is None or ema50 is None:
        return False, False, "indicators_unavailable"

    near_oversold = rsi <= settings.dca_rsi_oversold
    rsi_turning = len(closes) >= 4 and closes[-1] >= closes[-2] >= closes[-3]
    recent_support = min(lows[-20:])
    strong_breakdown = current < (recent_support * 0.985) and current < (ema50 * 0.97)
    reversal = _is_hammer(ohlc[-1]) or _is_bullish_engulfing(ohlc[-2], ohlc[-1])

    sell_vol_now = sum(volumes[-3:]) / 3.0
    sell_vol_before = sum(volumes[-6:-3]) / 3.0
    volume_weakening = sell_vol_now <= sell_vol_before

    breakdown_hit, breakdown_reason = _strong_breakdown(symbol, support_price)
    checks = [
        near_oversold or rsi_turning,
        reversal,
        volume_weakening,
        not strong_breakdown,
    ]
    ok_count = sum(1 for x in checks if x)
    allowed = (ok_count >= max(1, settings.dca_reversal_min_conditions)) and (not breakdown_hit)
    reason = (
        f"rsi={rsi:.1f} oversold={near_oversold} rsi_turn={rsi_turning} "
        f"breakdown={strong_breakdown} reversal={reversal} volWeak={volume_weakening} "
        f"ok_count={ok_count} breakdown_hit={breakdown_hit} ({breakdown_reason})"
    )
    return allowed, breakdown_hit, reason


def wallet_snapshot(db: Session) -> dict:
    cash = float(get_setting(db, "paper_cash", "0"))
    open_positions = db.query(Position).filter(Position.status == "open").all()
    symbols = sorted({p.symbol for p in open_positions})
    prices = get_prices(symbols) if symbols else {}
    invested_open = sum(float(p.total_invested_usdt) for p in open_positions)
    market_value = sum(float(prices.get(p.symbol, p.average_price)) * float(p.total_qty) for p in open_positions)
    unrealized = market_value - invested_open
    closed = db.query(Position).filter(Position.status == "closed").all()
    realized = sum(float(p.realized_pnl_usdt or 0.0) for p in closed)
    equity = cash + market_value
    return {
        "cash": cash,
        "invested_open": invested_open,
        "market_value": market_value,
        "unrealized_pnl": unrealized,
        "realized_pnl": realized,
        "equity": equity,
    }


def _open_position_with_rules(
    db: Session,
    campaign: Campaign,
    symbol: str,
    price: float,
    rules: list[DcaRule],
    rules_by_name: dict[str, DcaRule],
    fallback_ai_rules: list[tuple[str, float, float]],
    ai_profile: str,
    event_type: str,
    event_label: str,
) -> float:
    qty = campaign.entry_amount_usdt / price
    pos = Position(
        campaign_id=campaign.id,
        symbol=symbol,
        initial_price=price,
        initial_qty=qty,
        total_invested_usdt=campaign.entry_amount_usdt,
        total_qty=qty,
        average_price=price,
    )
    db.add(pos)
    db.flush()
    if campaign.ai_dca_enabled:
        symbol_rules = build_symbol_ai_dca_rules(symbol, ai_profile, fallback_ai_rules, campaign.sl_pct)
        for name_rule, drop_pct, alloc_pct, support_score in symbol_rules:
            rule_ref = rules_by_name.get(name_rule) or (rules[0] if rules else None)
            if not rule_ref:
                continue
            db.add(
                PositionDcaState(
                    position_id=pos.id,
                    dca_rule_id=rule_ref.id,
                    executed=False,
                    custom_drop_pct=drop_pct,
                    custom_allocation_pct=alloc_pct,
                    custom_support_score=support_score,
                )
            )
    else:
        for rule in rules:
            db.add(PositionDcaState(position_id=pos.id, dca_rule_id=rule.id, executed=False))
    add_log(
        db,
        event_type,
        symbol,
        (
            f"Campaign={campaign.name} | {event_label} at {price:.6f} "
            f"| Qty={qty:.8f} | USDT={campaign.entry_amount_usdt:.2f}"
        ),
    )
    return campaign.entry_amount_usdt


def create_campaign_positions(db: Session, campaign: Campaign, symbols: list[str]) -> tuple[int, list[str]]:
    picked = sorted(set([s.strip().upper() for s in symbols if s and s.strip()]))
    if not picked:
        return 0, ["No symbols selected."]

    prices = get_prices(picked)
    valid = [s for s in picked if s in prices and prices[s] > 0]
    if not valid:
        return 0, ["No valid symbols with price feed."]

    wallet = wallet_snapshot(db)
    needed = campaign.entry_amount_usdt * len(valid)
    if wallet["cash"] < needed:
        return 0, [f"Insufficient paper cash. Need {needed:.2f} USDT, have {wallet['cash']:.2f} USDT."]

    rules = (
        db.query(DcaRule)
        .filter(DcaRule.campaign_id == campaign.id)
        .order_by(DcaRule.drop_pct.asc(), DcaRule.id.asc())
        .all()
    )
    rules_by_name = {r.name: r for r in rules}
    fallback_ai_rules = [(r.name, float(r.drop_pct), float(r.allocation_pct)) for r in rules]
    ai_profile = campaign.ai_dca_profile or "neutral"

    opened = 0
    for symbol in valid:
        price = float(prices[symbol])
        _open_position_with_rules(
            db=db,
            campaign=campaign,
            symbol=symbol,
            price=price,
            rules=rules,
            rules_by_name=rules_by_name,
            fallback_ai_rules=fallback_ai_rules,
            ai_profile=ai_profile,
            event_type="OPEN",
            event_label="Initial buy",
        )
        opened += 1

    cash = wallet["cash"] - needed
    set_setting(db, "paper_cash", f"{cash:.8f}")
    db.commit()
    return opened, []


def run_cycle(db: Session) -> None:
    campaigns = db.query(Campaign).filter(Campaign.mode == "paper").all()
    if not campaigns:
        return

    open_positions = (
        db.query(Position)
        .join(Campaign, Campaign.id == Position.campaign_id)
        .filter(Position.status == "open", Campaign.mode == "paper")
        .all()
    )
    symbols = sorted({p.symbol for p in open_positions})
    prices = get_prices(symbols) if symbols else {}

    cash = float(get_setting(db, "paper_cash", "0"))
    changed = False
    now = datetime.utcnow()
    ai_filter_cache: dict[tuple[str, float], tuple[bool, bool, str]] = {}
    btc_state = btc_market_state()

    for pos in open_positions:
        price = float(prices.get(pos.symbol, 0.0))
        if price <= 0:
            continue
        campaign = pos.campaign
        tp_hit = campaign.tp_pct is not None and price >= (pos.average_price * (1 + (campaign.tp_pct / 100.0)))
        sl_hit = campaign.sl_pct is not None and price <= (pos.average_price * (1 - (campaign.sl_pct / 100.0)))
        if tp_hit or sl_hit:
            proceeds = pos.total_qty * price
            pnl = proceeds - pos.total_invested_usdt
            pos.status = "closed"
            pos.closed_at = now
            pos.close_price = price
            pos.realized_pnl_usdt = pnl
            pos.close_reason = "TP" if tp_hit else "SL"
            cash += proceeds
            changed = True
            add_log(
                db,
                "CLOSE",
                pos.symbol,
                (
                    f"Campaign={campaign.name} | Reason={pos.close_reason} | Close={price:.6f} "
                    f"| Invested={pos.total_invested_usdt:.2f} | Proceeds={proceeds:.2f} | PnL={pnl:+.2f}"
                ),
            )
            continue

        states = (
            db.query(PositionDcaState)
            .join(DcaRule, DcaRule.id == PositionDcaState.dca_rule_id)
            .filter(PositionDcaState.position_id == pos.id)
            .order_by(DcaRule.drop_pct.asc(), DcaRule.id.asc())
            .all()
        )
        if campaign.status == "active" and not pos.dca_paused:
            for state in states:
                if state.executed:
                    continue
                rule = state.rule
                drop_pct = float(state.custom_drop_pct if state.custom_drop_pct is not None else rule.drop_pct)
                alloc_pct = float(
                    state.custom_allocation_pct if state.custom_allocation_pct is not None else rule.allocation_pct
                )
                support_score_raw = state.custom_support_score
                support_score = float(support_score_raw or 0.0)
                if campaign.ai_dca_enabled:
                    if bool(campaign.strict_support_score_required) and support_score_raw is None:
                        add_log(
                            db,
                            "AI_DCA_SKIP",
                            pos.symbol,
                            f"Campaign={campaign.name} | Rule={rule.name} | skipped: missing support score (strict mode).",
                        )
                        continue
                    if support_score and support_score < settings.dca_support_score_threshold:
                        continue
                trigger_price = pos.initial_price * (1 - (drop_pct / 100.0))
                if price > trigger_price:
                    continue

                usdt = campaign.entry_amount_usdt * (alloc_pct / 100.0)
                if usdt <= 0 or cash < usdt:
                    continue

                if campaign.trend_filter_enabled:
                    if btc_state == "strong_bearish":
                        add_log(
                            db,
                            "TREND_FILTER_SKIP",
                            pos.symbol,
                            (
                                f"Campaign={campaign.name} | Rule={rule.name} | "
                                "BTC trend is strong bearish, DCA buy blocked."
                            ),
                        )
                        continue
                    if btc_state == "bearish":
                        usdt = usdt * 0.5

                if campaign.ai_dca_enabled:
                    key = (pos.symbol, round(trigger_price, 6))
                    if key not in ai_filter_cache:
                        ai_filter_cache[key] = _ai_dca_confirm(pos.symbol, trigger_price)
                    allowed, breakdown_hit, debug_reason = ai_filter_cache[key]
                    if breakdown_hit:
                        pos.dca_paused = True
                        pos.dca_pause_reason = "strong_breakdown_detected"
                        changed = True
                        add_log(
                            db,
                            "DCA_PAUSED",
                            pos.symbol,
                            f"Campaign={campaign.name} | Rule={rule.name} | {debug_reason}",
                        )
                        continue
                    if not allowed:
                        add_log(
                            db,
                            "AI_DCA_SKIP",
                            pos.symbol,
                            f"Campaign={campaign.name} | Rule={rule.name} | {debug_reason}",
                        )
                        continue

                qty = usdt / price
                pos.total_invested_usdt += usdt
                pos.total_qty += qty
                pos.average_price = pos.total_invested_usdt / pos.total_qty
                state.executed = True
                state.executed_at = now
                state.executed_price = price
                state.executed_qty = qty
                state.executed_usdt = usdt
                cash -= usdt
                changed = True
                add_log(
                    db,
                    "AI_DCA" if campaign.ai_dca_enabled else "DCA",
                    pos.symbol,
                    (
                        f"Campaign={campaign.name} | Rule={rule.name} | Drop={drop_pct:.2f}% "
                        f"| Buy at {price:.6f} | Qty={qty:.8f} | USDT={usdt:.2f} "
                        f"| TrendFilter={'on' if campaign.trend_filter_enabled else 'off'} ({btc_state}) "
                        f"| Avg={pos.average_price:.6f}"
                    ),
                )

    # Loop mode: keep target open count with best-score symbols at all times.
    for campaign in campaigns:
        if campaign.mode != "paper" or campaign.status != "active" or not campaign.loop_enabled:
            continue
        target_count = max(1, int(campaign.loop_target_count or 5))
        open_rows = db.query(Position.symbol).filter(Position.campaign_id == campaign.id, Position.status == "open").all()
        open_symbols = {str(s).upper() for (s,) in open_rows if s}
        missing = target_count - len(open_symbols)
        if missing <= 0:
            continue

        cash = float(get_setting(db, "paper_cash", "0"))
        min_required = campaign.entry_amount_usdt
        if cash < min_required:
            add_log(
                db,
                "LOOP_SKIP",
                "-",
                (
                    f"Campaign={campaign.name} | reason=insufficient_cash "
                    f"| cash={cash:.2f} | required_per_symbol={min_required:.2f}"
                ),
            )
            changed = True
            continue

        rules = (
            db.query(DcaRule)
            .filter(DcaRule.campaign_id == campaign.id)
            .order_by(DcaRule.drop_pct.asc(), DcaRule.id.asc())
            .all()
        )
        rules_by_name = {r.name: r for r in rules}
        fallback_ai_rules = [(r.name, float(r.drop_pct), float(r.allocation_pct)) for r in rules]
        ai_profile = campaign.ai_dca_profile or "neutral"

        scan = suggest_top_symbols(max(15, target_count * 4))
        ranked_symbols = [str(item.get("symbol", "")).upper() for item in (scan.get("items") or []) if item.get("symbol")]
        picks = []
        for symbol in ranked_symbols:
            if symbol in open_symbols:
                continue
            picks.append(symbol)
            if len(picks) >= missing:
                break
        if not picks:
            add_log(
                db,
                "LOOP_SKIP",
                "-",
                (
                    f"Campaign={campaign.name} | reason=no_candidates "
                    f"| target={target_count} | open={len(open_symbols)} | missing={missing}"
                ),
            )
            changed = True
            continue

        price_map = get_prices(picks)
        for symbol in picks:
            price = float(price_map.get(symbol, 0.0))
            if price <= 0 or cash < campaign.entry_amount_usdt:
                continue
            spent = _open_position_with_rules(
                db=db,
                campaign=campaign,
                symbol=symbol,
                price=price,
                rules=rules,
                rules_by_name=rules_by_name,
                fallback_ai_rules=fallback_ai_rules,
                ai_profile=ai_profile,
                event_type="LOOP_OPEN",
                event_label="Loop refill buy",
            )
            cash -= spent
            changed = True

    # Auto re-entry: reopen symbols that are currently closed (no open position),
    # while campaign remains active and this option is enabled.
    for campaign in campaigns:
        if campaign.mode != "paper" or campaign.status != "active" or not campaign.auto_reentry_enabled:
            continue

        tracked_rows = db.query(Position.symbol).filter(Position.campaign_id == campaign.id).distinct().all()
        tracked_symbols = {str(s).upper() for (s,) in tracked_rows if s}
        if not tracked_symbols:
            continue

        open_rows = db.query(Position.symbol).filter(Position.campaign_id == campaign.id, Position.status == "open").all()
        open_symbols = {str(s).upper() for (s,) in open_rows if s}
        to_reopen = sorted(tracked_symbols - open_symbols)
        if not to_reopen:
            continue

        symbol_prices = get_prices(to_reopen)
        rules = (
            db.query(DcaRule)
            .filter(DcaRule.campaign_id == campaign.id)
            .order_by(DcaRule.drop_pct.asc(), DcaRule.id.asc())
            .all()
        )
        rules_by_name = {r.name: r for r in rules}
        fallback_ai_rules = [(r.name, float(r.drop_pct), float(r.allocation_pct)) for r in rules]
        ai_profile = campaign.ai_dca_profile or "neutral"

        for symbol in to_reopen:
            price = float(symbol_prices.get(symbol, 0.0))
            if price <= 0:
                continue
            if cash < campaign.entry_amount_usdt:
                continue

            spent = _open_position_with_rules(
                db=db,
                campaign=campaign,
                symbol=symbol,
                price=price,
                rules=rules,
                rules_by_name=rules_by_name,
                fallback_ai_rules=fallback_ai_rules,
                ai_profile=ai_profile,
                event_type="REENTRY",
                event_label="Reopened",
            )
            cash -= spent
            changed = True

    if changed:
        set_setting(db, "paper_cash", f"{cash:.8f}")
        db.commit()


def recalculate_campaign_dca(db: Session, campaign: Campaign) -> tuple[int, int]:
    if campaign.mode != "paper":
        return 0, 0

    rules = (
        db.query(DcaRule)
        .filter(DcaRule.campaign_id == campaign.id)
        .order_by(DcaRule.drop_pct.asc(), DcaRule.id.asc())
        .all()
    )
    if not rules:
        return 0, 0

    rules_by_name = {r.name: r for r in rules}
    fallback_ai_rules = [(r.name, float(r.drop_pct), float(r.allocation_pct)) for r in rules]
    ai_profile = campaign.ai_dca_profile or "neutral"

    open_positions = db.query(Position).filter(Position.campaign_id == campaign.id, Position.status == "open").all()
    touched_positions = 0
    updated_states = 0

    for pos in open_positions:
        states = (
            db.query(PositionDcaState)
            .join(DcaRule, DcaRule.id == PositionDcaState.dca_rule_id)
            .filter(PositionDcaState.position_id == pos.id)
            .order_by(DcaRule.id.asc())
            .all()
        )
        if not states:
            continue

        if campaign.ai_dca_enabled:
            symbol_rules = build_symbol_ai_dca_rules(pos.symbol, ai_profile, fallback_ai_rules, campaign.sl_pct)
        else:
            symbol_rules = [(r.name, float(r.drop_pct), float(r.allocation_pct), None) for r in rules]

        symbol_rules_by_name = {name: (drop, alloc, score) for name, drop, alloc, score in symbol_rules}

        changed_any = False
        for st in states:
            if st.executed:
                continue
            rule_name = st.rule.name
            rule_data = symbol_rules_by_name.get(rule_name)
            if not rule_data:
                # Fallback by index-like rule names if names were reshuffled
                if rule_name.startswith("AI-DCA-"):
                    idx = int(rule_name.split("AI-DCA-")[1]) - 1
                    if 0 <= idx < len(symbol_rules):
                        _, d, a, sc = symbol_rules[idx]
                        rule_data = (d, a, sc)
            if not rule_data:
                # Disable stale levels not present in the current smart DCA plan.
                st.custom_allocation_pct = 0.0
                st.custom_support_score = None
                changed_any = True
                updated_states += 1
                continue
            drop_pct, alloc_pct, support_score = rule_data
            st.custom_drop_pct = float(drop_pct)
            st.custom_allocation_pct = float(alloc_pct)
            st.custom_support_score = float(support_score) if support_score is not None else None
            changed_any = True
            updated_states += 1

        if changed_any:
            pos.dca_paused = False
            pos.dca_pause_reason = None
            touched_positions += 1

    return touched_positions, updated_states
