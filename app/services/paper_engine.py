from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev
from typing import Dict, List

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import LogEntry, Setting, SymbolSnapshot, Trade
from app.services.binance_public import get_24h_tickers, get_book_tickers, get_exchange_info, get_klines, get_prices
from app.services.strategy import (
    bb_width,
    percent_change,
    ema,
    is_volatility_expanding,
    is_volume_accumulation,
    relative_strength_ok,
    trend_pullback_signal_with_checks,
)
from app.services.telegram_alerts import send_telegram_message

EXCLUDED_SYMBOLS = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDPUSDT", "USD1USDT", "DAIUSDT"}
SAFETY_MIN_24H_VOLUME = 20_000_000.0
SAFETY_MAX_SPREAD_PCT = 0.15


def _log(db: Session, event_type: str, message: str, symbol: str | None = None) -> None:
    db.add(LogEntry(event_type=event_type, symbol=symbol, message=message))


def _notify(db: Session, event_type: str, message: str, symbol: str | None = None, telegram: bool = False) -> None:
    _log(db, event_type, message, symbol)
    if telegram:
        enabled = _get_setting(db, "telegram_enabled", "false").lower() in {"1", "true", "yes", "on"}
        token = _get_setting(db, "telegram_bot_token", "")
        chat_id = _get_setting(db, "telegram_chat_id", "")
        if not enabled or not token or not chat_id:
            return
        prefix = f"[{event_type}]"
        if symbol:
            send_telegram_message(f"{prefix} {symbol} - {message}", token=token, chat_id=chat_id)
        else:
            send_telegram_message(f"{prefix} {message}", token=token, chat_id=chat_id)


def _get_setting(db: Session, key: str, default: str) -> str:
    row = db.query(Setting).filter(Setting.key == key).first()
    if not row:
        row = Setting(key=key, value=default)
        db.add(row)
        db.flush()
    return row.value


def _set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(Setting).filter(Setting.key == key).first()
    if not row:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def _get_float(db: Session, key: str, default: float) -> float:
    try:
        return float(_get_setting(db, key, str(default)))
    except (TypeError, ValueError):
        return default


def _get_int(db: Session, key: str, default: int) -> int:
    try:
        return int(float(_get_setting(db, key, str(default))))
    except (TypeError, ValueError):
        return default


def _get_bool(db: Session, key: str, default: bool) -> bool:
    val = _get_setting(db, key, "true" if default else "false").lower()
    return val in {"1", "true", "yes", "on"}


def _cash_balance(db: Session) -> float:
    return _get_float(db, "paper_cash_balance", settings.paper_start_balance)


def _update_cash_balance(db: Session, value: float) -> None:
    _set_setting(db, "paper_cash_balance", f"{value:.8f}")


def init_defaults(db: Session) -> None:
    defaults = {
        "trading_mode": "paper",
        "bot_paused": "false",
        "paper_cash_balance": str(settings.paper_start_balance),
        "fee_rate": str(settings.fee_rate),
        "take_profit_pct": str(settings.take_profit_pct),
        "stop_loss_pct": str(settings.stop_loss_pct),
        "trailing_stop_pct": "0.008",
        "risk_per_trade_pct": "1.0",
        "max_symbols": str(settings.max_symbols),
        "max_open_trades": str(settings.max_open_trades),
        "min_quote_volume": str(settings.min_quote_volume),
        "max_spread_pct": str(settings.max_spread_pct),
        "time_stop_minutes": "180",
        "cooldown_minutes": "30",
        "daily_loss_limit_pct": "3.0",
        "btc_filter_enabled": "true",
        "daily_anchor_date": "",
        "daily_start_equity": str(settings.paper_start_balance),
        "slippage_enabled": "false",
        "slippage_bps": "8",
        "telegram_enabled": "false",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
    }
    for key, default in defaults.items():
        _get_setting(db, key, default)
    db.commit()


def _mode_is_paused(db: Session) -> bool:
    return _get_bool(db, "bot_paused", False)


def _compute_equity(db: Session) -> float:
    cash = _cash_balance(db)
    open_trades = db.query(Trade).filter(Trade.status == "open").all()
    if not open_trades:
        return cash
    try:
        prices = get_prices([t.symbol for t in open_trades])
    except Exception:
        prices = {}
    positions_value = sum(t.quantity * prices.get(t.symbol, t.entry_price) for t in open_trades)
    return cash + positions_value


def _expected_cash_from_trades(db: Session) -> float:
    fee_rate = _get_float(db, "fee_rate", settings.fee_rate)
    expected = settings.paper_start_balance
    trades = db.query(Trade).order_by(Trade.entry_time.asc(), Trade.id.asc()).all()
    for t in trades:
        allocation = t.entry_price * t.quantity
        entry_fee = allocation * fee_rate
        expected -= allocation + entry_fee
        if t.status == "closed" and t.exit_price is not None:
            proceeds = t.exit_price * t.quantity
            exit_fee = proceeds * fee_rate
            expected += proceeds - exit_fee
    return expected


def _reconcile_cash_if_needed(db: Session) -> None:
    expected = _expected_cash_from_trades(db)
    current = _cash_balance(db)
    if abs(expected - current) >= 0.5:
        _update_cash_balance(db, expected)
        _notify(db, "ACCOUNT", f"Cash reconciled from {current:.4f} to {expected:.4f}")


def _daily_loss_triggered(db: Session) -> bool:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    anchor = _get_setting(db, "daily_anchor_date", "")
    if anchor != today:
        _set_setting(db, "daily_anchor_date", today)
        _set_setting(db, "daily_start_equity", f"{_compute_equity(db):.8f}")
        return False

    start_equity = _get_float(db, "daily_start_equity", settings.paper_start_balance)
    if start_equity <= 0:
        return False
    current = _compute_equity(db)
    loss_pct = ((start_equity - current) / start_equity) * 100
    max_loss = _get_float(db, "daily_loss_limit_pct", 3.0)
    if loss_pct >= max_loss:
        _set_setting(db, "bot_paused", "true")
        _notify(db, "RISK", f"Daily loss reached {loss_pct:.2f}% >= {max_loss:.2f}%. Bot paused.", telegram=True)
        return True
    return False


def _btc_market_blocked() -> bool:
    klines = get_klines("BTCUSDT", "1h", 220)
    closes = [float(k[4]) for k in klines]
    return ema(closes[-160:], 50) < ema(closes[-210:], 200)


def _round_step_down(value: float, step: float) -> float:
    if step <= 0:
        return value
    units = int(value / step)
    return units * step


def _symbol_filters_map(symbols: List[str]) -> Dict[str, dict]:
    try:
        info = get_exchange_info()
    except Exception:
        return {}
    wanted = set(symbols)
    out: Dict[str, dict] = {}
    for s in info.get("symbols", []):
        sym = s.get("symbol")
        if sym not in wanted:
            continue
        min_qty = 0.0
        step_size = 0.0
        min_notional = 10.0
        for f in s.get("filters", []):
            ft = f.get("filterType")
            if ft == "LOT_SIZE":
                min_qty = float(f.get("minQty", 0.0))
                step_size = float(f.get("stepSize", 0.0))
            elif ft == "MIN_NOTIONAL":
                min_notional = float(f.get("minNotional", 10.0))
            elif ft == "NOTIONAL":
                min_notional = float(f.get("minNotional", min_notional))
        out[sym] = {"min_qty": min_qty, "step_size": step_size, "min_notional": min_notional}
    return out


def _apply_slippage(db: Session, price: float, side: str) -> float:
    enabled = _get_bool(db, "slippage_enabled", False)
    if not enabled:
        return price
    bps = _get_float(db, "slippage_bps", 8.0)
    pct = max(0.0, bps) / 10_000.0
    if side == "buy":
        return price * (1 + pct)
    return price * (1 - pct)


def _scan_symbols(db: Session) -> List[dict]:
    min_quote_volume = max(_get_float(db, "min_quote_volume", settings.min_quote_volume), SAFETY_MIN_24H_VOLUME)
    max_spread_pct = min(_get_float(db, "max_spread_pct", settings.max_spread_pct), SAFETY_MAX_SPREAD_PCT)
    t24 = get_24h_tickers()
    books = get_book_tickers()
    book_map: Dict[str, dict] = {b["symbol"]: b for b in books}

    candidates = []
    for t in t24:
        symbol = t["symbol"]
        if not symbol.endswith("USDT") or symbol in EXCLUDED_SYMBOLS:
            continue
        quote_vol = float(t.get("quoteVolume", 0.0))
        if quote_vol < min_quote_volume:
            continue
        book = book_map.get(symbol)
        if not book:
            continue
        bid = float(book.get("bidPrice", 0.0))
        ask = float(book.get("askPrice", 0.0))
        last = float(t.get("lastPrice", 0.0))
        if bid <= 0 or ask <= 0 or last <= 0:
            continue
        spread_pct = ((ask - bid) / last) * 100
        if spread_pct > max_spread_pct:
            continue
        candidates.append(
            {
                "symbol": symbol,
                "volume_24h": quote_vol,
                "spread_pct": spread_pct,
                "last_price": last,
            }
        )
    return candidates


def _dynamic_rank_symbols(db: Session, safety_symbols: List[dict]) -> List[dict]:
    max_symbols = _get_int(db, "max_symbols", settings.max_symbols)
    if not safety_symbols:
        return []

    btc_k15 = get_klines("BTCUSDT", "15m", 60)
    btc_k5 = get_klines("BTCUSDT", "5m", 80)
    btc_closes_15m = [float(k[4]) for k in btc_k15]
    btc_change_15m = percent_change(btc_closes_15m, 1)

    ranked: List[dict] = []
    for item in safety_symbols:
        symbol = item["symbol"]
        try:
            # Keep enough history for Layer 2 entry checks while deriving Layer 1 rank features.
            k5 = get_klines(symbol, "5m", 260)
            k15 = get_klines(symbol, "15m", 260)
            quote_volumes_5m = [float(k[7]) for k in k5]
            closes_5m = [float(k[4]) for k in k5]
            closes_15m = [float(k[4]) for k in k15]
            if len(quote_volumes_5m) < 50 or len(closes_15m) < 10:
                continue

            volume_1h = sum(quote_volumes_5m[-12:])
            volume_24h = max(item["volume_24h"], 1.0)
            vol_ratio_1h_24h = volume_1h / volume_24h

            vol_last_5 = sum(quote_volumes_5m[-5:])
            avg_vol_last_50 = max(mean(quote_volumes_5m[-50:]), 1.0)
            recent_volume_expansion = vol_last_5 / avg_vol_last_50

            dynamic_volume_score = vol_ratio_1h_24h + recent_volume_expansion

            coin_change_15m = percent_change(closes_15m, 1)
            relative_strength = coin_change_15m - btc_change_15m

            short_term_momentum = percent_change(closes_5m, 3)

            current_bb = bb_width(closes_5m[-30:], 20)
            base_bb = min(
                bb_width(closes_5m[i - 20 : i], 20)
                for i in range(25, len(closes_5m))
                if len(closes_5m[i - 20 : i]) >= 20
            )
            volatility_expansion = (current_bb / base_bb) if base_bb > 0 else 0.0

            final_score = (
                dynamic_volume_score
                + (relative_strength * 0.8)
                + (short_term_momentum * 0.4)
                + (volatility_expansion * 0.6)
            )
            ranked.append(
                {
                    **item,
                    "score": final_score,
                    "dynamic_volume_score": dynamic_volume_score,
                    "vol_ratio_1h_24h": vol_ratio_1h_24h,
                    "recent_volume_expansion": recent_volume_expansion,
                    "relative_strength": relative_strength,
                    "short_term_momentum": short_term_momentum,
                    "volatility_expansion": volatility_expansion,
                    "k5": k5,
                    "k15": k15,
                }
            )
        except Exception:
            continue

    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:max_symbols]


def _build_priority_watchlist(db: Session, scanned: List[dict]) -> List[dict]:
    watchlist = []
    btc_klines_15m = get_klines("BTCUSDT", "15m", 120)
    btc_closes = [float(k[4]) for k in btc_klines_15m]

    db.query(SymbolSnapshot).delete()
    db.flush()

    for item in scanned:
        symbol = item["symbol"]
        try:
            k5 = item.get("k5") or get_klines(symbol, "5m", 250)
            k15 = item.get("k15") or get_klines(symbol, "15m", 250)
            closes_15m = [float(k[4]) for k in k15]
            volumes_15m = [float(k[5]) for k in k15]

            cond_volume = is_volume_accumulation(volumes_15m)
            cond_volatility = is_volatility_expanding(closes_15m)
            cond_rel = relative_strength_ok(closes_15m, btc_closes)
            momentum = cond_volume and cond_volatility and cond_rel

            signal, signal_status, checks = trend_pullback_signal_with_checks(k5, k15)
            trend_status = "Bullish" if signal_status in {"Buy Ready", "Watch"} else "Bearish"
            if momentum and signal_status != "Blocked":
                signal_status = "Momentum Candidate"
                watchlist.append(
                    {
                        "symbol": symbol,
                        "last_price": item["last_price"],
                        "k5": k5,
                        "k15": k15,
                        "strategy_ready": signal,
                        "entry_checks": checks,
                        "scanner_score": item.get("score", 0.0),
                    }
                )
                _notify(
                    db,
                    "SCANNER",
                    (
                        f"selected score={item.get('score', 0):.4f} "
                        f"vol24h={item['volume_24h']:.0f} "
                        f"ratio1h24h={item.get('vol_ratio_1h_24h', 0):.5f} "
                        f"volExp={item.get('recent_volume_expansion', 0):.3f} "
                        f"rs={item.get('relative_strength', 0):+.3f}"
                    ),
                    symbol,
                )
                _notify(
                    db,
                    "ENTRY_DECISION",
                    (
                        f"strategy_ready={signal} trend={checks.get('trend_ok')} pullback={checks.get('pullback_ok')} "
                        f"rsi_ok={checks.get('rsi_ok')} rsi={checks.get('rsi_value', 0):.2f} "
                        f"volume_spike={checks.get('volume_spike_ok')} vol_now={checks.get('volume_now', 0):.2f} "
                        f"vol_avg20={checks.get('volume_avg20', 0):.2f} resistance_ok={checks.get('resistance_ok')}"
                    ),
                    symbol,
                )

            db.add(
                SymbolSnapshot(
                    symbol=symbol,
                    volume_24h=item["volume_24h"],
                    spread_pct=item["spread_pct"],
                    last_price=item["last_price"],
                    trend_status=trend_status,
                    signal_status=signal_status,
                    updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
            )
        except Exception:
            db.add(
                SymbolSnapshot(
                    symbol=symbol,
                    volume_24h=item["volume_24h"],
                    spread_pct=item["spread_pct"],
                    last_price=item["last_price"],
                    trend_status="Neutral",
                    signal_status="No Data",
                    updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
            )
    return watchlist


def _in_cooldown(db: Session, symbol: str, cooldown_minutes: int) -> bool:
    last_trade = db.query(Trade).filter(Trade.symbol == symbol).order_by(desc(Trade.id)).first()
    if not last_trade:
        return False
    last_time = last_trade.exit_time or last_trade.entry_time
    age = (datetime.utcnow() - last_time).total_seconds() / 60.0
    return age < cooldown_minutes


def _try_open_trade(db: Session, item: dict, filters_map: Dict[str, dict]) -> None:
    symbol = item["symbol"]
    max_open = _get_int(db, "max_open_trades", settings.max_open_trades)
    open_count = db.query(Trade).filter(Trade.status == "open").count()
    if open_count >= max_open:
        _notify(db, "ENTRY_DECISION", f"rejected reason=max_open_trades ({open_count}/{max_open})", symbol)
        return
    if db.query(Trade).filter(Trade.status == "open", Trade.symbol == symbol).first():
        _notify(db, "ENTRY_DECISION", "rejected reason=existing_open_trade", symbol)
        return
    cooldown = _get_int(db, "cooldown_minutes", 30)
    if _in_cooldown(db, symbol, cooldown):
        _notify(db, "ENTRY_DECISION", f"rejected reason=cooldown({cooldown}m)", symbol)
        return

    cash = _cash_balance(db)
    if cash <= 25:
        _notify(db, "ENTRY_DECISION", "rejected reason=insufficient_cash", symbol)
        return

    fee_rate = _get_float(db, "fee_rate", settings.fee_rate)
    tp_pct = _get_float(db, "take_profit_pct", settings.take_profit_pct)
    sl_pct = _get_float(db, "stop_loss_pct", settings.stop_loss_pct)
    risk_pct = _get_float(db, "risk_per_trade_pct", 1.0) / 100.0
    market_price = float(item["last_price"])
    entry_price = _apply_slippage(db, market_price, "buy")
    sl_price = entry_price * (1 - sl_pct)
    risk_per_unit = max(entry_price - sl_price, entry_price * 0.001)
    risk_capital = _compute_equity(db) * risk_pct
    qty = risk_capital / risk_per_unit
    max_affordable_qty = (cash * 0.95) / entry_price
    qty = min(qty, max_affordable_qty)
    symbol_filters = filters_map.get(symbol, {"min_qty": 0.0, "step_size": 0.0, "min_notional": 10.0})
    qty = _round_step_down(qty, symbol_filters.get("step_size", 0.0))
    if qty <= 0:
        _notify(db, "ENTRY_DECISION", "rejected reason=qty_after_step_rounding<=0", symbol)
        return
    if qty < symbol_filters.get("min_qty", 0.0):
        _notify(db, "ENTRY_DECISION", f"rejected reason=min_qty ({qty:.8f}<{symbol_filters.get('min_qty', 0):.8f})", symbol)
        return

    allocation = qty * entry_price
    min_notional = max(10.0, symbol_filters.get("min_notional", 10.0))
    if allocation < min_notional:
        _notify(db, "ENTRY_DECISION", f"rejected reason=min_notional ({allocation:.4f}<{min_notional:.4f})", symbol)
        return
    entry_fee = allocation * fee_rate
    new_cash = cash - allocation - entry_fee
    if new_cash < 0:
        _notify(db, "ENTRY_DECISION", "rejected reason=negative_cash_after_fees", symbol)
        return

    trade = Trade(
        symbol=symbol,
        entry_price=entry_price,
        quantity=qty,
        status="open",
        tp_price=entry_price * (1 + tp_pct),
        sl_price=sl_price,
        entry_time=datetime.utcnow(),
        highest_price=entry_price,
        trailing_active=0,
    )
    db.add(trade)
    _update_cash_balance(db, new_cash)
    _notify(
        db,
        "ENTRY_DECISION",
        (
            f"accepted score={item.get('scanner_score', 0):.4f} qty={qty:.6f} "
            f"entry_price={entry_price:.6f} risk_pct={risk_pct*100:.2f}"
        ),
        symbol,
    )
    _notify(db, "TRADE", f"Paper trade opened at {entry_price:.6f} qty={qty:.6f}", symbol, telegram=True)


def _manage_open_positions(db: Session) -> None:
    open_trades = db.query(Trade).filter(Trade.status == "open").all()
    if not open_trades:
        return
    try:
        prices = get_prices([t.symbol for t in open_trades])
    except Exception as exc:
        _notify(db, "ERROR", f"Price update failed: {exc}", telegram=True)
        return

    fee_rate = _get_float(db, "fee_rate", settings.fee_rate)
    time_stop_minutes = _get_int(db, "time_stop_minutes", 180)
    trailing_stop_pct = _get_float(db, "trailing_stop_pct", 0.008)
    cash = _cash_balance(db)

    for t in open_trades:
        price = prices.get(t.symbol)
        if price is None:
            continue
        if t.symbol in EXCLUDED_SYMBOLS:
            reason = "Symbol Excluded"
            proceeds = t.quantity * price
            exit_fee = proceeds * fee_rate
            pnl_value = (price - t.entry_price) * t.quantity - exit_fee
            cash += proceeds - exit_fee
            t.status = "closed"
            t.exit_price = price
            t.exit_time = datetime.utcnow()
            t.pnl = pnl_value
            t.exit_reason = reason
            _notify(db, "TRADE", f"Paper trade closed ({reason}) pnl={pnl_value:.4f}", t.symbol, telegram=True)
            continue

        if not t.highest_price or price > t.highest_price:
            t.highest_price = price

        if not t.trailing_active and price >= t.tp_price:
            t.trailing_active = 1
            t.trailing_stop_price = price * (1 - trailing_stop_pct)
        elif t.trailing_active:
            new_trail = (t.highest_price or price) * (1 - trailing_stop_pct)
            t.trailing_stop_price = max(t.trailing_stop_price or 0.0, new_trail)

        age_minutes = (datetime.utcnow() - t.entry_time).total_seconds() / 60.0
        reason = None
        if t.trailing_active and t.trailing_stop_price and price <= t.trailing_stop_price:
            reason = "Trailing Stop"
        elif price <= t.sl_price:
            reason = "Stop Loss"
        elif age_minutes >= time_stop_minutes:
            reason = "Time Stop"
        if not reason:
            continue

        proceeds = t.quantity * price
        exit_fee = proceeds * fee_rate
        entry_fee = (t.entry_price * t.quantity) * fee_rate
        pnl_value = (price - t.entry_price) * t.quantity - entry_fee - exit_fee
        cash += proceeds - exit_fee
        t.status = "closed"
        t.exit_price = price
        t.exit_time = datetime.utcnow()
        t.pnl = pnl_value
        t.exit_reason = reason
        _notify(db, "TRADE", f"Paper trade closed ({reason}) pnl={pnl_value:.4f}", t.symbol, telegram=True)

    _update_cash_balance(db, cash)


def run_cycle(db: Session) -> None:
    init_defaults(db)
    _reconcile_cash_if_needed(db)
    if _get_setting(db, "trading_mode", "paper").lower() != "paper":
        _notify(db, "MODE", "Live mode disabled in this build; forced to paper mode.", telegram=True)
        _set_setting(db, "trading_mode", "paper")

    _manage_open_positions(db)
    if _daily_loss_triggered(db):
        db.commit()
        return

    if _mode_is_paused(db):
        _notify(db, "PAUSE", "Bot paused. Skipping new entries.", telegram=True)
        db.commit()
        return

    _notify(db, "SCAN", "Market scan started")
    try:
        btc_filter_enabled = _get_bool(db, "btc_filter_enabled", True)
        market_blocked = _btc_market_blocked() if btc_filter_enabled else False
        _notify(db, "REGIME", f"BTC filter={'on' if btc_filter_enabled else 'off'} blocked={market_blocked}")
        safety_symbols = _scan_symbols(db)
        ranked_symbols = _dynamic_rank_symbols(db, safety_symbols)
        filters_map = _symbol_filters_map([r["symbol"] for r in ranked_symbols])
        for ranked in ranked_symbols[:5]:
            _notify(
                db,
                "SCANNER",
                (
                    f"rank score={ranked.get('score', 0):.4f} "
                    f"vol24h={ranked['volume_24h']:.0f} "
                    f"ratio1h24h={ranked.get('vol_ratio_1h_24h', 0):.5f} "
                    f"volExp={ranked.get('recent_volume_expansion', 0):.3f} "
                    f"rs={ranked.get('relative_strength', 0):+.3f}"
                ),
                ranked["symbol"],
            )
        watchlist = _build_priority_watchlist(db, ranked_symbols)
    except Exception as exc:
        _notify(db, "ERROR", f"Scanner failed: {exc}", telegram=True)
        db.commit()
        return

    if market_blocked:
        _notify(db, "RISK", "BTC regime filter active (EMA50<EMA200). No new trades.")
        db.commit()
        return

    for item in watchlist:
        if item.get("strategy_ready"):
            _try_open_trade(db, item, filters_map)
        else:
            checks = item.get("entry_checks", {})
            _notify(
                db,
                "ENTRY_DECISION",
                (
                    f"rejected reason=strategy_not_ready trend={checks.get('trend_ok')} pullback={checks.get('pullback_ok')} "
                    f"rsi_ok={checks.get('rsi_ok')} volume_spike={checks.get('volume_spike_ok')} resistance_ok={checks.get('resistance_ok')}"
                ),
                item["symbol"],
            )

    _notify(db, "CYCLE", "Trading cycle completed")
    db.commit()


def portfolio_snapshot(db: Session) -> dict:
    cash = _cash_balance(db)
    open_trades = db.query(Trade).filter(Trade.status == "open").all()
    day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=7)
    closed_today = db.query(Trade).filter(Trade.status == "closed", Trade.exit_time >= day_start).all()
    closed_week = db.query(Trade).filter(Trade.status == "closed", Trade.exit_time >= week_start).all()

    unrealized = 0.0
    prices: Dict[str, float] = {}
    if open_trades:
        try:
            prices = get_prices([t.symbol for t in open_trades])
        except Exception:
            prices = {}
        for t in open_trades:
            p = prices.get(t.symbol, t.entry_price)
            unrealized += (p - t.entry_price) * t.quantity

    realized_today = sum(t.pnl or 0.0 for t in closed_today)
    realized_week = sum(t.pnl or 0.0 for t in closed_week)
    balance = cash + sum(t.quantity * prices.get(t.symbol, t.entry_price) for t in open_trades) if open_trades else cash
    wins = db.query(Trade).filter(Trade.status == "closed", Trade.pnl > 0).count()
    total_closed = db.query(Trade).filter(Trade.status == "closed").count()
    win_rate = (wins / total_closed * 100) if total_closed else 0.0
    return {
        "cash": cash,
        "balance": balance,
        "unrealized": unrealized,
        "daily_pnl": realized_today + unrealized,
        "weekly_pnl": realized_week,
        "win_rate": win_rate,
        "open_positions": len(open_trades),
    }


def statistics_snapshot(db: Session) -> dict:
    trades = db.query(Trade).filter(Trade.status == "closed", Trade.pnl.isnot(None)).all()
    if not trades:
        return {
            "win_rate": 0.0,
            "avg_profit": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "total_trades": 0,
        }

    pnls = [float(t.pnl or 0.0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    avg_profit = mean(wins) if wins else 0.0
    avg_loss = mean(losses) if losses else 0.0
    win_rate = (len(wins) / len(pnls)) * 100
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    pnl_std = pstdev(pnls) if len(pnls) > 1 else 0.0
    sharpe = (mean(pnls) / pnl_std) if pnl_std > 0 else 0.0

    equity = settings.paper_start_balance
    peak = equity
    max_dd = 0.0
    ordered = sorted(trades, key=lambda t: t.exit_time or datetime.utcnow())
    for t in ordered:
        equity += float(t.pnl or 0.0)
        peak = max(peak, equity)
        if peak > 0:
            dd = ((peak - equity) / peak) * 100
            max_dd = max(max_dd, dd)

    return {
        "win_rate": win_rate,
        "avg_profit": avg_profit,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": max_dd,
        "total_trades": len(pnls),
    }
