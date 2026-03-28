from datetime import datetime

from sqlalchemy.orm import Session

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
    universe = candidates[:40]

    results = []
    for c in universe:
        symbol = c["symbol"]
        try:
            kl1h = get_klines(symbol, "1h", 260)
            kld = get_klines(symbol, "1d", 3)
        except Exception:
            continue
        if len(kl1h) < 220 or len(kld) < 2:
            continue

        closes = [float(k[4]) for k in kl1h]
        highs = [float(k[2]) for k in kl1h]
        lows = [float(k[3]) for k in kl1h]
        vols = [float(k[5]) for k in kl1h]
        price = closes[-1]
        if price <= 0:
            continue

        prev_day = kld[-2]
        s1, s2, _ = _pivot_supports(float(prev_day[2]), float(prev_day[3]), float(prev_day[4]))
        ema100 = _ema(closes, 100)
        ema200 = _ema(closes, 200)
        if ema100 is None or ema200 is None:
            continue
        swing_support = min(lows[-80:])
        support_levels = [s for s in [s1, s2, ema100, ema200, swing_support] if s > 0]
        support_distance_pct = min(abs((price - s) / price) * 100.0 for s in support_levels)
        near_support = support_distance_pct <= 2.0

        rsi = _rsi(closes, 14)
        if rsi is None:
            continue
        rsi_ok = 25.0 <= rsi <= 40.0

        avg_vol_20 = sum(vols[-21:-1]) / 20.0
        volume_spike = vols[-1] > (avg_vol_20 * 1.2) if avg_vol_20 > 0 else False

        drawdown_from_recent_high = ((max(highs[-120:]) - price) / max(highs[-120:])) * 100.0
        dip_valid = (-10.0 <= c["price_change_pct_24h"] <= -3.0) or (5.0 <= drawdown_from_recent_high <= 20.0)

        trend_good = price > ema200
        trend_stabilizing = (price > ema100) and (closes[-1] >= closes[-4])

        score = 0
        if near_support:
            score += 30
        if rsi_ok:
            score += 20
        if volume_spike:
            score += 20
        if trend_good:
            score += 15
        elif trend_stabilizing:
            score += 8
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


def build_ai_dca_rules(symbols: list[str]) -> tuple[list[tuple[str, float, float]], str, str]:
    picked = [s.strip().upper() for s in symbols if s and s.strip()]
    sample = picked[:12]
    if not sample:
        return [("AI-DCA-1", 2.0, 25.0), ("AI-DCA-2", 4.5, 35.0), ("AI-DCA-3", 8.0, 40.0)], "neutral", "Fallback AI DCA."

    symbol_drops: list[list[float]] = []
    for symbol in sample:
        drops = _symbol_ai_support_drops(symbol)
        if drops:
            symbol_drops.append(drops)

    profile = _trend_profile()
    if profile == "bearish":
        allocations = [20.0, 30.0, 50.0]
    elif profile == "bullish":
        allocations = [30.0, 35.0, 35.0]
    else:
        allocations = [25.0, 35.0, 40.0]

    if not symbol_drops:
        rules = [("AI-DCA-1", 2.0, allocations[0]), ("AI-DCA-2", 4.5, allocations[1]), ("AI-DCA-3", 8.0, allocations[2])]
        return rules, profile, f"AI DCA fallback profile={profile}."

    all_flat = sorted(x for row in symbol_drops for x in row)
    n = len(all_flat)
    d1 = all_flat[max(0, int(n * 0.25) - 1)]
    d2 = all_flat[max(0, int(n * 0.50) - 1)]
    d3 = all_flat[max(0, int(n * 0.75) - 1)]
    levels = sorted([max(0.8, d1), max(1.2, d2), max(1.8, d3)])
    rules = [
        ("AI-DCA-1", round(levels[0], 2), allocations[0]),
        ("AI-DCA-2", round(levels[1], 2), allocations[1]),
        ("AI-DCA-3", round(levels[2], 2), allocations[2]),
    ]
    note = (
        f"AI DCA from supports (Pivot+EMA) over {len(symbol_drops)} symbols. "
        f"Trend profile={profile}. Drops={levels[0]:.2f}/{levels[1]:.2f}/{levels[2]:.2f}%."
    )
    return rules, profile, note


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


def _ai_dca_confirm(symbol: str) -> tuple[bool, str]:
    try:
        kl = get_klines(symbol, "15m", 140)
    except Exception:
        return False, "no_market_data"
    if len(kl) < 50:
        return False, "insufficient_data"

    ohlc = [[float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in kl]
    closes = [x[3] for x in ohlc]
    lows = [x[2] for x in ohlc]
    volumes = [float(k[5]) for k in kl]
    current = closes[-1]
    rsi = _rsi(closes, 14)
    ema50 = _ema(closes, 50)
    if rsi is None or ema50 is None:
        return False, "indicators_unavailable"

    near_oversold = rsi <= 36.0
    recent_support = min(lows[-20:])
    strong_breakdown = current < (recent_support * 0.985) and current < (ema50 * 0.97)
    reversal = _is_hammer(ohlc[-1]) or _is_bullish_engulfing(ohlc[-2], ohlc[-1])

    sell_vol_now = sum(volumes[-3:]) / 3.0
    sell_vol_before = sum(volumes[-6:-3]) / 3.0
    volume_weakening = sell_vol_now <= sell_vol_before

    allowed = near_oversold and (not strong_breakdown) and reversal and volume_weakening
    reason = (
        f"rsi={rsi:.1f} oversold={near_oversold} "
        f"breakdown={strong_breakdown} reversal={reversal} volWeak={volume_weakening}"
    )
    return allowed, reason


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

    opened = 0
    for symbol in valid:
        price = float(prices[symbol])
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
        for rule in rules:
            db.add(PositionDcaState(position_id=pos.id, dca_rule_id=rule.id, executed=False))
        add_log(
            db,
            "OPEN",
            symbol,
            (
                f"Campaign={campaign.name} | Initial buy at {price:.6f} "
                f"| Qty={qty:.8f} | USDT={campaign.entry_amount_usdt:.2f}"
            ),
        )
        opened += 1

    cash = wallet["cash"] - needed
    set_setting(db, "paper_cash", f"{cash:.8f}")
    db.commit()
    return opened, []


def run_cycle(db: Session) -> None:
    campaigns = db.query(Campaign).filter(Campaign.mode == "paper", Campaign.status == "active").all()
    if not campaigns:
        return

    open_positions = (
        db.query(Position)
        .join(Campaign, Campaign.id == Position.campaign_id)
        .filter(Position.status == "open", Campaign.status == "active", Campaign.mode == "paper")
        .all()
    )
    if not open_positions:
        return

    symbols = sorted({p.symbol for p in open_positions})
    prices = get_prices(symbols)
    if not prices:
        return

    cash = float(get_setting(db, "paper_cash", "0"))
    changed = False
    now = datetime.utcnow()
    ai_filter_cache: dict[str, tuple[bool, str]] = {}
    btc_state = btc_market_state()

    for pos in open_positions:
        price = float(prices.get(pos.symbol, 0.0))
        if price <= 0:
            continue
        campaign = pos.campaign

        states = (
            db.query(PositionDcaState)
            .join(DcaRule, DcaRule.id == PositionDcaState.dca_rule_id)
            .filter(PositionDcaState.position_id == pos.id)
            .order_by(DcaRule.drop_pct.asc(), DcaRule.id.asc())
            .all()
        )
        for state in states:
            if state.executed:
                continue
            rule = state.rule
            trigger_price = pos.initial_price * (1 - (float(rule.drop_pct) / 100.0))
            if price > trigger_price:
                continue

            usdt = campaign.entry_amount_usdt * (float(rule.allocation_pct) / 100.0)
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
                if pos.symbol not in ai_filter_cache:
                    ai_filter_cache[pos.symbol] = _ai_dca_confirm(pos.symbol)
                allowed, debug_reason = ai_filter_cache[pos.symbol]
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
                    f"Campaign={campaign.name} | Rule={rule.name} | Drop={rule.drop_pct:.2f}% "
                    f"| Buy at {price:.6f} | Qty={qty:.8f} | USDT={usdt:.2f} "
                    f"| TrendFilter={'on' if campaign.trend_filter_enabled else 'off'} ({btc_state}) "
                    f"| Avg={pos.average_price:.6f}"
                ),
            )

        tp_hit = campaign.tp_pct is not None and price >= (pos.average_price * (1 + (campaign.tp_pct / 100.0)))
        sl_hit = campaign.sl_pct is not None and price <= (pos.average_price * (1 - (campaign.sl_pct / 100.0)))
        if not tp_hit and not sl_hit:
            continue

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

    if changed:
        set_setting(db, "paper_cash", f"{cash:.8f}")
        db.commit()
