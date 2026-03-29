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
    ohlc_1h = [[float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in kl1h]
    lows_1h = [x[2] for x in ohlc_1h]
    volumes_1h = [float(k[5]) for k in kl1h]
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
    rsi_value = _rsi(closes_1h, 14) or 50.0
    rsi_turning = len(closes_1h) >= 4 and closes_1h[-1] >= closes_1h[-2] >= closes_1h[-3]
    reversal_candle = _is_hammer(ohlc_1h[-1]) or _is_bullish_engulfing(ohlc_1h[-2], ohlc_1h[-1])
    recent_support = min(lows_1h[-20:])
    recent_avg_vol = sum(volumes_1h[-21:-1]) / 20.0
    high_break_vol = volumes_1h[-1] > (recent_avg_vol * 1.8) if recent_avg_vol > 0 else False
    breakdown_suspected = (
        price < (strongest["price"] * 0.99)
        and (price < (recent_support * 0.995) or high_break_vol)
    )
    vol_now = sum(volumes_1h[-3:]) / 3.0
    vol_before = sum(volumes_1h[-6:-3]) / 3.0
    volume_weakening = vol_now <= vol_before
    reversal_ok_count = sum(
        [
            bool(rsi_value <= settings.dca_rsi_oversold or rsi_turning),
            bool(reversal_candle),
            bool(volume_weakening),
            bool(price >= strongest["price"]),
        ]
    )
    reversal_confirmed = reversal_ok_count >= max(1, settings.dca_reversal_min_conditions)
    return {
        "price": price,
        "rsi": rsi_value,
        "ema200": float(ema200),
        "supports": supports_below,
        "strongest": strongest,
        "near_strong": near_strong,
        "drawdown_pct": ((max([float(k[2]) for k in kl1h[-200:]]) - price) / max([float(k[2]) for k in kl1h[-200:]])) * 100.0,
        "volume_spike": (float(kl1h[-1][5]) > (sum([float(k[5]) for k in kl1h[-21:-1]]) / 20.0) * 1.2),
        "rsi_turning": rsi_turning,
        "reversal_candle": reversal_candle,
        "volume_weakening": volume_weakening,
        "reversal_ok_count": reversal_ok_count,
        "reversal_confirmed": reversal_confirmed,
        "breakdown_suspected": breakdown_suspected,
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


def _weighted_zone_allocations_pct(drops: list[float], scores: list[float], budget_pct: float) -> list[float]:
    if not drops or budget_pct <= 0:
        return [0.0 for _ in drops]
    max_drop = max(max(drops), 0.0001)
    weights: list[float] = []
    for idx, drop in enumerate(drops):
        depth_factor = max(0.0, float(drop) / max_drop)
        score_factor = max(0.0, min(100.0, float(scores[idx] if idx < len(scores) else 0.0))) / 100.0
        # Weighted Smart DCA:
        # - deeper zones get higher weight (60%)
        # - stronger support score gets higher weight (40%)
        w = (depth_factor * 0.6) + (score_factor * 0.4) + 0.05
        weights.append(w)
    total_w = sum(weights)
    if total_w <= 0:
        unit = budget_pct / len(drops)
        return [round(unit, 2) for _ in drops]
    return [round((w / total_w) * budget_pct, 2) for w in weights]


def _strategy_mode_weights(levels: int, strategy_mode: str) -> list[float]:
    n = max(1, int(levels))
    mode = str(strategy_mode or "balanced").strip().lower()
    if mode not in {"conservative", "balanced", "aggressive"}:
        mode = "balanced"
    if mode == "conservative":
        base = [float(i) for i in range(1, n + 1)]  # deeper levels heavier
    elif mode == "aggressive":
        base = [float(n - i + 1) for i in range(1, n + 1)]  # early levels heavier
    else:
        # Balanced: slight depth preference but near-uniform.
        base = [1.0 + (0.25 * ((i - 1) / max(n - 1, 1))) for i in range(1, n + 1)]
    total = sum(base)
    if total <= 0:
        return [1.0 / n for _ in range(n)]
    return [x / total for x in base]


def _smart_allocations_pct(
    drops: list[float],
    scores: list[float],
    budget_pct: float,
    strategy_mode: str,
) -> list[float]:
    if not drops or budget_pct <= 0:
        return [0.0 for _ in drops]
    n = len(drops)
    max_drop = max(max(drops), 0.0001)
    strategy_weights = _strategy_mode_weights(n, strategy_mode)
    raw_weights: list[float] = []
    for i in range(n):
        depth_norm = max(0.0, float(drops[i]) / max_drop)
        score_norm = max(0.0, min(100.0, float(scores[i] if i < len(scores) else 0.0))) / 100.0
        strategy_norm = float(strategy_weights[i])
        # Dynamic (real-data based) allocation:
        # - strategy intent
        # - depth of support
        # - support quality score
        w = (strategy_norm * 0.55) + (depth_norm * 0.25) + (score_norm * 0.20)
        raw_weights.append(max(0.0001, w))
    total_w = sum(raw_weights)
    if total_w <= 0:
        return [round(budget_pct / n, 2) for _ in range(n)]
    return [round((w / total_w) * budget_pct, 2) for w in raw_weights]


def _adaptive_drop_targets(expected_drawdown_pct: float, strategy_mode: str, count: int = 4) -> list[float]:
    mode = str(strategy_mode or "balanced").strip().lower()
    if mode not in {"conservative", "balanced", "aggressive"}:
        mode = "balanced"
    e = max(4.0, float(expected_drawdown_pct or 0.0))
    if mode == "aggressive":
        end_depth = max(6.0, min(20.0, e * 0.80))
        mult = [0.28, 0.48, 0.72, 1.00, 1.15]
    elif mode == "conservative":
        end_depth = max(10.0, min(35.0, e * 1.20))
        mult = [0.15, 0.35, 0.62, 1.00, 1.30]
    else:
        end_depth = max(8.0, min(28.0, e * 1.00))
        mult = [0.20, 0.42, 0.68, 1.00, 1.20]
    out: list[float] = []
    for m in mult:
        v = round(max(0.7, end_depth * m), 2)
        if not out or abs(v - out[-1]) >= 0.4:
            out.append(v)
    return out[: max(1, int(count))]


def _target_drawdown_by_regime(market_state: str, strategy_mode: str, base_drawdown: float) -> float:
    state = str(market_state or "neutral").strip().lower()
    mode = str(strategy_mode or "balanced").strip().lower()
    d = max(4.0, float(base_drawdown or 0.0))

    floors = {
        "strong_bearish": {"conservative": 35.0, "balanced": 25.0, "aggressive": 15.0},
        "bearish": {"conservative": 25.0, "balanced": 18.0, "aggressive": 12.0},
        "neutral": {"conservative": 18.0, "balanced": 12.0, "aggressive": 8.0},
        "bullish": {"conservative": 14.0, "balanced": 10.0, "aggressive": 7.0},
    }
    if mode not in {"conservative", "balanced", "aggressive"}:
        mode = "balanced"
    if state not in floors:
        state = "neutral"
    return max(d, floors[state][mode])


def _market_state_simple(symbol: str = "BTCUSDT", interval: str = "4h") -> str:
    try:
        kl = get_klines(symbol, interval, 260)
    except Exception:
        return "sideways"
    closes = [float(k[4]) for k in kl]
    if len(closes) < 220:
        return "sideways"
    ema200 = _ema(closes, 200)
    if ema200 is None or ema200 <= 0:
        return "sideways"
    price = closes[-1]
    dist = abs(price - ema200) / ema200
    if price > ema200:
        return "bullish"
    if dist < 0.05:
        return "sideways"
    return "bearish"


def _auto_strategy_mode(symbol: str) -> tuple[str, str]:
    state = _market_state_simple(symbol, "4h")
    if state == "bullish":
        return "aggressive", state
    if state == "sideways":
        return "balanced", state
    return "conservative", state


def _depth_multiplier(drop_pct: float) -> float:
    # Canonical table from design document.
    points = [
        (5.0, 1.0),
        (10.0, 1.2),
        (17.0, 1.4),
        (25.0, 1.7),
        (35.0, 2.0),
        (45.0, 2.3),
    ]
    d = float(drop_pct)
    if d <= points[0][0]:
        return points[0][1]
    if d >= points[-1][0]:
        return points[-1][1]
    for i in range(1, len(points)):
        x0, y0 = points[i - 1]
        x1, y1 = points[i]
        if x0 <= d <= x1:
            ratio = (d - x0) / (x1 - x0)
            return y0 + ((y1 - y0) * ratio)
    return 1.0


def _zone_support_score(
    symbol: str,
    zone_price: float,
    kl1h: list[list],
    kl4h: list[list],
    kld: list[list],
    quote_volume_24h: float,
) -> tuple[float | None, dict]:
    if zone_price <= 0 or len(kl1h) < 80 or len(kl4h) < 60:
        return None, {}

    closes_1h = [float(k[4]) for k in kl1h]
    lows_4h = [float(k[3]) for k in kl4h]
    closes_4h = [float(k[4]) for k in kl4h]
    vols_1h = [float(k[5]) for k in kl1h]

    # 1) Volume profile score around zone (30%)
    p_min = min(closes_1h)
    p_max = max(closes_1h)
    bins = 40
    step = (p_max - p_min) / bins if p_max > p_min else 0.0
    vp_score = 30.0
    if step > 0:
        buckets = [0.0 for _ in range(bins)]
        for p, v in zip(closes_1h, vols_1h):
            idx = int((p - p_min) / step)
            idx = min(max(idx, 0), bins - 1)
            buckets[idx] += v
        zone_idx = int((zone_price - p_min) / step)
        zone_idx = min(max(zone_idx, 0), bins - 1)
        zone_vol = buckets[zone_idx]
        if zone_idx > 0:
            zone_vol += 0.5 * buckets[zone_idx - 1]
        if zone_idx < bins - 1:
            zone_vol += 0.5 * buckets[zone_idx + 1]
        max_vol = max(buckets) if buckets else 1.0
        vp_score = max(0.0, min(100.0, (zone_vol / max(max_vol, 1e-9)) * 100.0))

    # 2) Liquidity score (25%) using 24h quote volume
    qv = max(0.0, float(quote_volume_24h or 0.0))
    if qv >= 5_000_000_000:
        liq_score = 100.0
    elif qv >= 1_000_000_000:
        liq_score = 85.0
    elif qv >= 300_000_000:
        liq_score = 70.0
    elif qv >= 100_000_000:
        liq_score = 55.0
    elif qv >= 30_000_000:
        liq_score = 40.0
    else:
        liq_score = 25.0

    # 3) Reaction strength score (25%) from 4h touches + bounce
    touches = 0
    bounces = 0
    for i in range(3, len(lows_4h) - 3):
        near_zone = abs(lows_4h[i] - zone_price) / zone_price <= 0.018
        if not near_zone:
            continue
        touches += 1
        future_peak = max(closes_4h[i + 1 : i + 4])
        if future_peak >= lows_4h[i] * 1.015:
            bounces += 1
    reaction_score = min(100.0, (touches * 14.0) + (bounces * 18.0))

    # 4) Timeframe confluence score (20%) vs pivots + EMA (4h/1d)
    tf_hits = 0
    prev_d = kld[-2] if len(kld) >= 2 else None
    prev_4h = kl4h[-2] if len(kl4h) >= 2 else None
    closes_4h_all = [float(k[4]) for k in kl4h]
    ema50 = _ema(closes_4h_all, 50)
    ema100 = _ema(closes_4h_all, 100)
    ema200 = _ema(closes_4h_all, 200)
    levels: list[float] = []
    if prev_d is not None:
        levels.extend(list(_pivot_supports(float(prev_d[2]), float(prev_d[3]), float(prev_d[4]))))
    if prev_4h is not None:
        levels.extend(list(_pivot_supports(float(prev_4h[2]), float(prev_4h[3]), float(prev_4h[4]))))
    for x in [ema50, ema100, ema200]:
        if x is not None:
            levels.append(float(x))
    for lv in levels:
        if lv > 0 and abs(lv - zone_price) / zone_price <= 0.02:
            tf_hits += 1
    tf_score = min(100.0, tf_hits * 22.0)

    final_score = (
        (vp_score * 0.30)
        + (liq_score * 0.25)
        + (reaction_score * 0.25)
        + (tf_score * 0.20)
    )
    details = {
        "volume_profile": round(vp_score, 2),
        "liquidity": round(liq_score, 2),
        "reaction": round(reaction_score, 2),
        "timeframe_confluence": round(tf_score, 2),
        "touches": touches,
        "bounces": bounces,
    }
    return round(max(0.0, min(100.0, final_score)), 2), details


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


def suggest_top_symbols(limit: int = 5, use_v2: bool = False, max_candidates: int | None = None) -> dict:
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
    if max_candidates is None:
        # Dynamic cap to avoid heavy scans on every request/cycle.
        # V2 still scans wider than V1, but bounded by requested limit.
        universe_size = min(40, max(18, limit * 2)) if use_v2 else 18
    else:
        universe_size = max(8, int(max_candidates))
    universe = candidates[:universe_size]

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

        if use_v2:
            # Hard filters (must pass):
            # 1) near strong support
            # 2) support score >= threshold
            # 3) no breakdown structure
            # 4) reversal confirmed
            if not near_support:
                continue
            if strongest_score < settings.dca_support_score_threshold:
                continue
            if bool(ctx.get("breakdown_suspected", False)):
                continue
            if not bool(ctx.get("reversal_confirmed", False)):
                continue
            if settings.enforce_btc_filter and market_state == "strong_bearish":
                continue

            # Ranking score (after hard filters only).
            score = 40
            score += min(30, int(strongest_score / 2.8))
            if rsi_ok:
                score += 10
            if volume_spike:
                score += 10
            if trend_good:
                score += 8
            if dip_valid:
                score += 8
            score += min(8, int(max(0.0, (c["quote_volume"] / 10_000_000) - 1)))
            if market_state == "bearish":
                score -= 8
        else:
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
                "reversal_ok_count": int(ctx.get("reversal_ok_count", 0)),
                "breakdown_suspected": bool(ctx.get("breakdown_suspected", False)),
            }
        )

    results.sort(key=lambda x: (x["score"], x["quote_volume"]), reverse=True)
    picked = results[: max(1, limit)]
    return {
        "market_state": market_state,
        "engine_version": "v2" if use_v2 else "v1",
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


def build_smart_dca_plan(
    symbol: str,
    entry_amount_usdt: float,
    tp_pct: float | None = None,
    sl_pct: float | None = None,
    max_levels: int = 5,
    strategy_mode: str = "balanced",
) -> dict:
    sym = str(symbol or "").strip().upper()
    if not sym or entry_amount_usdt <= 0:
        return {"ok": False, "error": "Invalid symbol or entry amount."}
    ctx = _support_engine(sym)
    if not ctx:
        return {"ok": False, "error": f"No enough market context for {sym} now."}

    # Treat empty/zero/negative stop-loss as "not set".
    if sl_pct is not None:
        try:
            if float(sl_pct) <= 0:
                sl_pct = None
        except Exception:
            sl_pct = None

    current_price = float(ctx["price"])
    max_levels = max(1, min(int(max_levels), 6))
    mode_input = str(strategy_mode or "balanced").strip().lower()
    if mode_input not in {"conservative", "balanced", "aggressive", "auto"}:
        mode_input = "balanced"
    auto_market_state = None
    if mode_input == "auto":
        mode, auto_market_state = _auto_strategy_mode(sym)
    else:
        mode = mode_input

    # Design-doc canonical drop levels.
    canonical_drops = [5.0, 10.0, 17.0, 25.0, 35.0, 45.0][:max_levels]
    sl_cap = _sl_drop_cap(sl_pct)
    if sl_cap is not None:
        canonical_drops = [d for d in canonical_drops if d <= sl_cap]
    if not canonical_drops:
        canonical_drops = [min(5.0, max(0.8, float(sl_cap or 5.0)))]

    # Preload data once for dynamic zone scoring.
    try:
        kl1h_plan = get_klines(sym, "1h", 320)
        kl4h_plan = get_klines(sym, "4h", 320)
        kld_plan = get_klines(sym, "1d", 3)
    except Exception:
        kl1h_plan, kl4h_plan, kld_plan = [], [], []
    quote_volume_24h = 0.0
    try:
        tick = next((x for x in get_24h_tickers() if str(x.get("symbol", "")).upper() == sym), None)
        if tick:
            quote_volume_24h = float(tick.get("quoteVolume") or 0.0)
    except Exception:
        quote_volume_24h = 0.0

    # Build zones from canonical drops; prefer nearest known support as trigger if available.
    supports = [s for s in (ctx.get("supports") or []) if float(s.get("price", 0.0)) < current_price]
    supports.sort(key=lambda x: float(x.get("price", 0.0)), reverse=True)
    zones: list[dict] = []
    for d in canonical_drops:
        target_price = current_price * (1.0 - (d / 100.0))
        trigger = target_price
        source = "drop_level"
        if supports:
            nearest = min(supports, key=lambda s: abs(float(s["price"]) - target_price))
            if abs(float(nearest["price"]) - target_price) / max(target_price, 1e-9) <= 0.03:
                trigger = float(nearest["price"])
                source = "support_aligned"
        zones.append(
            {
                "trigger_price": round(float(trigger), 8),
                "drop_pct": round(float(d), 2),
                "score": None,
                "sources": [source],
            }
        )

    # Dynamic zone scoring.
    for z in zones:
        dyn_score, details = _zone_support_score(
            symbol=sym,
            zone_price=float(z["trigger_price"]),
            kl1h=kl1h_plan,
            kl4h=kl4h_plan,
            kld=kld_plan,
            quote_volume_24h=quote_volume_24h,
        )
        z["score"] = dyn_score if dyn_score is not None else 35.0
        z["score_details"] = details

    drops = [float(z["drop_pct"]) for z in zones]
    scores = [float(z["score"]) for z in zones]

    # Allocation Engine (design-doc aligned):
    # allocation = base_amount * (score / 100) * depth_multiplier * profile_mult
    profile_mult = 1.0
    if mode == "aggressive":
        profile_mult = 0.9
    elif mode == "conservative":
        profile_mult = 1.2

    boost = 1.0
    if float(ctx.get("rsi", 50.0)) < 35.0:
        boost *= 1.1
    if bool(ctx.get("volume_spike", False)):
        boost *= 1.1

    raw_amounts: list[float] = []
    for d, s in zip(drops, scores):
        amount = float(entry_amount_usdt) * (max(1.0, s) / 100.0) * _depth_multiplier(d) * profile_mult * boost
        raw_amounts.append(max(0.0, amount))

    # Risk Manager: max_total = entry * 6  => DCA budget = entry * 5
    dca_budget = float(entry_amount_usdt) * 5.0
    total_raw = sum(raw_amounts)
    scale = 1.0
    if total_raw > dca_budget > 0:
        scale = dca_budget / total_raw

    # Emergency Mode: sharp deep drawdown => reduce allocation.
    market_state = _market_state_simple("BTCUSDT", "4h")
    expected_drawdown_raw = max(float(ctx.get("drawdown_pct", 0.0)), max(drops))
    if expected_drawdown_raw > 40.0:
        scale *= 0.85

    dca_amounts = [a * scale for a in raw_amounts]
    allocs = [((a / float(entry_amount_usdt)) * 100.0) for a in dca_amounts]

    # Build final rules shallow -> deep.
    rows = []
    for idx, z in enumerate(zones):
        alloc_pct = float(allocs[idx]) if idx < len(allocs) else 0.0
        if alloc_pct <= 0:
            continue
        rows.append(
            {
                "name": f"SMART-DCA-{len(rows) + 1}",
                "drop_pct": round(float(z["drop_pct"]), 2),
                "allocation_pct": round(alloc_pct, 2),
                "support_score": round(float(z["score"] if z.get("score") is not None else 35.0), 2),
                "support_score_details": z.get("score_details", {}),
                "trigger_price": round(float(z["trigger_price"]), 8),
                "sources": z["sources"],
            }
        )
    if not rows:
        return {"ok": False, "error": f"Could not build SMART DCA rows for {sym}."}

    initial_qty = entry_amount_usdt / current_price
    total_invested = float(entry_amount_usdt)
    total_qty = float(initial_qty)
    for r in rows:
        dca_usdt = entry_amount_usdt * (float(r["allocation_pct"]) / 100.0)
        total_invested += dca_usdt
        total_qty += dca_usdt / max(float(r["trigger_price"]), 1e-12)
    estimated_avg = total_invested / max(total_qty, 1e-12)
    effective_tp = float(tp_pct or 0.0)
    target_price = estimated_avg * (1.0 + (effective_tp / 100.0)) if effective_tp > 0 else None

    deepest_drop_pct = max([float(r["drop_pct"]) for r in rows], default=0.0)
    theoretical_total = max(float(total_invested), float(entry_amount_usdt))
    expected_drawdown_pct = max(float(expected_drawdown_raw), deepest_drop_pct)
    if expected_drawdown_pct <= 12.0:
        risk_level = "low"
    elif expected_drawdown_pct <= 25.0:
        risk_level = "medium"
    else:
        risk_level = "high"
    suggested_total_with_buffer = max(theoretical_total, total_invested) * 1.10
    dca_total_only = max(0.0, total_invested - float(entry_amount_usdt))

    # Estimated typical usage range (not guaranteed):
    # depends on market regime and strategy profile.
    state = str(market_state).lower()
    if state == "bullish":
        low_f, high_f = 0.20, 0.55
    elif state == "sideways":
        low_f, high_f = 0.35, 0.70
    else:  # bearish
        low_f, high_f = 0.55, 0.90
    if mode == "aggressive":
        low_f -= 0.08
        high_f -= 0.08
    elif mode == "conservative":
        low_f += 0.08
        high_f += 0.06
    low_f = max(0.10, min(0.95, low_f))
    high_f = max(low_f, min(0.98, high_f))
    typical_low = float(entry_amount_usdt) + (dca_total_only * low_f)
    typical_high = float(entry_amount_usdt) + (dca_total_only * high_f)

    return {
        "ok": True,
        "symbol": sym,
        "entry_price": round(current_price, 8),
        "entry_amount_usdt": round(float(entry_amount_usdt), 4),
        "tp_pct": effective_tp,
        "sl_pct": sl_pct,
        "rules": rows,
        "estimate": {
            "dca_total_usdt": round(total_invested - entry_amount_usdt, 4),
            "total_if_all_filled_usdt": round(total_invested, 4),
            "estimated_avg_price_if_all_filled": round(estimated_avg, 8),
            "estimated_target_price_if_all_filled": round(target_price, 8) if target_price else None,
        },
        "capital_planning": {
            "base_entry_amount_usdt": round(float(entry_amount_usdt), 4),
            "first_zone_weight_pct": round(float(rows[0]["allocation_pct"]) if rows else 0.0, 2),
            "theoretical_total_required_usdt": round(theoretical_total, 4),
            "max_reserved_capital_usdt": round(total_invested, 4),
            "planned_total_if_all_levels_hit_usdt": round(total_invested, 4),
            "suggested_total_with_buffer_usdt": round(suggested_total_with_buffer, 4),
            "estimated_typical_usage_low_usdt": round(typical_low, 4),
            "estimated_typical_usage_high_usdt": round(typical_high, 4),
            "expected_drawdown_pct": round(expected_drawdown_pct, 2),
            "planned_drawdown_coverage_pct": round(deepest_drop_pct, 2),
            "deepest_dca_drop_pct": round(deepest_drop_pct, 2),
            "total_multiplier_sum": round(total_invested / max(float(entry_amount_usdt), 1e-9), 6),
            "risk_level": risk_level,
        },
        "market_state": market_state,
        "strategy_mode": mode,
        "strategy_mode_input": mode_input,
        "strategy_auto_resolved": bool(mode_input == "auto"),
        "strategy_auto_market_state": auto_market_state,
        "note": (
            f"Smart DCA ({mode}{' from auto' if mode_input == 'auto' else ''}) with canonical drop levels + dynamic zone score + risk-capped allocation."
        ),
    }


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
        alloc_pct = float(base_fallback[idx][2])
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
    # Loop safety: never open duplicate symbol while an open position exists
    # in the same campaign. Re-entry is allowed only after prior one is closed.
    if bool(campaign.loop_enabled):
        exists_open = (
            db.query(Position.id)
            .filter(
                Position.campaign_id == campaign.id,
                Position.symbol == symbol,
                Position.status == "open",
            )
            .first()
            is not None
        )
        if exists_open:
            add_log(
                db,
                "LOOP_SKIP",
                symbol,
                f"Campaign={campaign.name} | reason=duplicate_open_symbol",
            )
            return 0.0

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
        if bool(campaign.smart_dca_enabled):
            score_by_name: dict[str, float | None] = {}
            try:
                suggested = campaign.ai_dca_suggested_rules_json or "[]"
                import json as _json

                parsed = _json.loads(suggested)
                if isinstance(parsed, list):
                    for row in parsed:
                        n = str(row.get("name", "")).strip()
                        if not n:
                            continue
                        sc = row.get("support_score")
                        score_by_name[n] = float(sc) if sc is not None else None
            except Exception:
                score_by_name = {}
            symbol_rules = [
                (r.name, float(r.drop_pct), float(r.allocation_pct), score_by_name.get(r.name))
                for r in rules
            ]
        else:
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

        try:
            scan = suggest_top_symbols(
                max(15, target_count * 2),
                use_v2=bool(campaign.loop_v2_enabled),
                max_candidates=max(18, target_count * 2),
            )
        except Exception:
            scan = suggest_top_symbols(
                max(15, target_count * 2),
                use_v2=False,
                max_candidates=max(18, target_count * 2),
            )
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
            if symbol in open_symbols:
                continue
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
            if spent <= 0:
                continue
            cash -= spent
            open_symbols.add(symbol)
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
            if bool(campaign.smart_dca_enabled):
                score_by_name: dict[str, float | None] = {}
                try:
                    import json as _json

                    parsed = _json.loads(campaign.ai_dca_suggested_rules_json or "[]")
                    if isinstance(parsed, list):
                        for row in parsed:
                            n = str(row.get("name", "")).strip()
                            if not n:
                                continue
                            sc = row.get("support_score")
                            score_by_name[n] = float(sc) if sc is not None else None
                except Exception:
                    score_by_name = {}
                symbol_rules = [(r.name, float(r.drop_pct), float(r.allocation_pct), score_by_name.get(r.name)) for r in rules]
            else:
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
