from datetime import datetime, timedelta, timezone
import json
import os
import random
from statistics import mean, pstdev
from typing import Dict, List

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AIAgentMemory, AITrade, LogEntry, MarketObservation, Setting, ShadowTrade, SymbolSnapshot, Trade
from app.services.binance_public import get_24h_tickers, get_book_tickers, get_exchange_info, get_klines, get_prices
from app.services.strategy import (
    atr_from_klines,
    bb_width,
    percent_change,
    ema,
    is_volatility_expanding,
    is_volume_accumulation,
    relative_strength_ok,
    trend_pullback_signal_with_checks,
)
from app.services.ai_providers import propose_strategy_with_usage
from app.services.ai_usage import record_usage
from app.services.binance_live import (
    get_base_asset,
    get_free_asset_balance,
    is_configured as binance_live_configured,
    place_market_buy_quote,
    place_market_sell_qty,
)
from app.services.telegram_alerts import send_telegram_message

EXCLUDED_SYMBOLS = {"USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "USDPUSDT", "DAIUSDT"}
SAFETY_MIN_24H_VOLUME = 10_000_000.0
SAFETY_MAX_SPREAD_PCT = 0.20
SAFETY_MIN_ATR_RATIO = 0.015
KSA_OFFSET = timedelta(hours=3)


def _ksa_now() -> datetime:
    return datetime.utcnow() + KSA_OFFSET


def _ksa_day_start_utc() -> datetime:
    now_ksa = _ksa_now()
    day_start_ksa = now_ksa.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start_ksa - KSA_OFFSET


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


def _is_live_mode(db: Session) -> bool:
    return _get_setting(db, "trading_mode", "paper").strip().lower() == "live"


def _live_link_key(trade_id: int) -> str:
    return f"live_link_trade_{trade_id}"


def _set_live_link(db: Session, trade_id: int, symbol: str, quantity: float, order_id: int | str) -> None:
    value = f"{symbol.upper()}|{quantity:.12f}|{order_id}"
    _set_setting(db, _live_link_key(trade_id), value[:200])


def _get_live_link(db: Session, trade_id: int) -> tuple[str, float, str] | None:
    row = db.query(Setting).filter(Setting.key == _live_link_key(trade_id)).first()
    raw = row.value if row else ""
    if not raw:
        return None
    parts = raw.split("|")
    if len(parts) != 3:
        return None
    symbol = parts[0].strip().upper()
    try:
        qty = float(parts[1])
    except (TypeError, ValueError):
        return None
    order_id = parts[2].strip()
    if not symbol or qty <= 0 or not order_id:
        return None
    return symbol, qty, order_id


def _clear_live_link(db: Session, trade_id: int) -> None:
    row = db.query(Setting).filter(Setting.key == _live_link_key(trade_id)).first()
    if row:
        db.delete(row)


def _mirror_open_live(db: Session, trade: Trade, allocation_usdt: float) -> bool:
    if not _is_live_mode(db):
        return True
    if not binance_live_configured():
        _notify(db, "LIVE", "Live mode is enabled but BINANCE_API_KEY/SECRET are missing", trade.symbol, telegram=True)
        return False
    try:
        order = place_market_buy_quote(trade.symbol, allocation_usdt)
        executed_qty = float(order.get("executedQty", "0") or 0.0)
        order_id = order.get("orderId", "-")
        if executed_qty <= 0:
            _notify(db, "LIVE", f"entry failed: executedQty=0 orderId={order_id}", trade.symbol, telegram=True)
            return False
        _set_live_link(db, trade.id, trade.symbol, executed_qty, order_id)
        _notify(
            db,
            "LIVE",
            f"entry mirrored orderId={order_id} executed_qty={executed_qty:.8f} quote_usdt={allocation_usdt:.2f}",
            trade.symbol,
            telegram=True,
        )
        return True
    except Exception as exc:
        _notify(db, "LIVE", f"entry failed: {exc}", trade.symbol, telegram=True)
        return False


def _mirror_close_live(db: Session, trade: Trade, reason: str, force_live: bool = False) -> None:
    if not _is_live_mode(db) and not force_live:
        return
    link = _get_live_link(db, trade.id)
    if not link:
        _notify(db, "LIVE", "close skipped: no live link found for paper trade", trade.symbol, telegram=True)
        return
    symbol, linked_qty, _ = link
    try:
        base_asset = get_base_asset(symbol)
        free_balance = get_free_asset_balance(base_asset)
        sell_qty = min(linked_qty, free_balance * 0.999)
        if sell_qty <= 0:
            _notify(
                db,
                "LIVE",
                f"close skipped: insufficient free {base_asset} balance (free={free_balance:.8f})",
                symbol,
                telegram=True,
            )
            return
        order = place_market_sell_qty(symbol, sell_qty)
        order_id = order.get("orderId", "-")
        executed_qty = float(order.get("executedQty", "0") or 0.0)
        _notify(
            db,
            "LIVE",
            f"close mirrored ({reason}) orderId={order_id} executed_qty={executed_qty:.8f}",
            symbol,
            telegram=True,
        )
        _clear_live_link(db, trade.id)
    except Exception as exc:
        _notify(db, "LIVE", f"close failed: {exc}", symbol, telegram=True)


def mirror_close_for_manual_action(db: Session, trade: Trade, reason: str = "Manual Close", force_live: bool = False) -> None:
    _mirror_close_live(db, trade, reason, force_live=force_live)


def register_live_link_for_trade(db: Session, trade: Trade, symbol: str, quantity: float, order_id: int | str) -> None:
    _set_live_link(db, trade.id, symbol, quantity, order_id)


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
        "trailing_stop_pct": "0.01",
        "risk_per_trade_pct": "1.0",
        "max_entry_usdt": "0",
        "max_symbols": str(settings.max_symbols),
        "max_open_trades": str(settings.max_open_trades),
        "min_quote_volume": str(settings.min_quote_volume),
        "max_spread_pct": str(settings.max_spread_pct),
        "time_stop_minutes": str(settings.time_stop_minutes),
        "cooldown_minutes": "30",
        "daily_loss_limit_pct": "3.0",
        "max_trades_per_day": "10",
        "btc_filter_enabled": "true",
        "daily_anchor_date": "",
        "daily_start_equity": str(settings.paper_start_balance),
        "slippage_enabled": "false",
        "slippage_bps": "8",
        "telegram_enabled": "false",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "strategy_use_score_system": "true",
        "strategy_score_threshold": "3",
        "strategy_trend_enabled": "true",
        "strategy_pullback_enabled": "true",
        "strategy_rsi_enabled": "true",
        "strategy_volume_spike_enabled": "true",
        "strategy_resistance_enabled": "true",
        "strategy_price_above_ema50_enabled": "false",
        "strategy_pullback_max_dist_pct": "1.0",
        "strategy_rsi_min": "35",
        "strategy_rsi_max": "65",
        "strategy_volume_spike_multiplier": "1.3",
        "strategy_resistance_min_dist_pct": "1.5",
        "momentum_volume_enabled": "true",
        "momentum_volatility_enabled": "true",
        "momentum_relative_strength_enabled": "true",
        "momentum_price_above_ema200_1h_enabled": "true",
        "shadow_enabled": "true",
        "shadow_notional_usdt": "100",
        "shadow_max_open": "30",
        "ai_trading_enabled": "true",
        "ai_balance_usdt": "500",
        "ai_entry_usdt": "30",
        "ai_max_open": "15",
        "ai_trials_per_cycle": "20",
        "ai_classic_enabled": "true",
        "ai_classic_balance_usdt": "500",
        "ai_classic_entry_usdt": "30",
        "ai_classic_max_open": "15",
        "ai_classic_trials_per_cycle": "20",
        "ai_classic_scan_symbols": "80",
        "ai_classic_min_quote_volume": "10000000",
        "ai_classic_max_spread_pct": "0.20",
        "ai_classic_daily_loss_limit_pct": "3.0",
        "ai_classic_max_trades_per_day": "20",
        "ai_classic_max_risk_per_trade_pct": "1.0",
        "ai_openai_enabled": "true",
        "ai_openai_balance_usdt": "500",
        "ai_openai_entry_usdt": "30",
        "ai_openai_max_open": "10",
        "ai_openai_trials_per_cycle": "20",
        "ai_openai_scan_symbols": "80",
        "ai_openai_min_quote_volume": "10000000",
        "ai_openai_max_spread_pct": "0.20",
        "ai_openai_daily_loss_limit_pct": "3.0",
        "ai_openai_max_trades_per_day": "20",
        "ai_openai_max_risk_per_trade_pct": "1.0",
        "ai_claude_enabled": "true",
        "ai_claude_balance_usdt": "500",
        "ai_claude_entry_usdt": "30",
        "ai_claude_max_open": "10",
        "ai_claude_trials_per_cycle": "20",
        "ai_claude_scan_symbols": "80",
        "ai_claude_min_quote_volume": "10000000",
        "ai_claude_max_spread_pct": "0.20",
        "ai_claude_daily_loss_limit_pct": "3.0",
        "ai_claude_max_trades_per_day": "20",
        "ai_claude_max_risk_per_trade_pct": "1.0",
        "ai_deepseek_enabled": "true",
        "ai_deepseek_balance_usdt": "500",
        "ai_deepseek_entry_usdt": "30",
        "ai_deepseek_max_open": "10",
        "ai_deepseek_trials_per_cycle": "20",
        "ai_deepseek_scan_symbols": "80",
        "ai_deepseek_min_quote_volume": "10000000",
        "ai_deepseek_max_spread_pct": "0.20",
        "ai_deepseek_daily_loss_limit_pct": "3.0",
        "ai_deepseek_max_trades_per_day": "20",
        "ai_deepseek_max_risk_per_trade_pct": "1.0",
    }
    for key, default in defaults.items():
        _get_setting(db, key, default)

    # Backward compatibility: keep old generic AI settings mirrored to classic lab.
    legacy_to_classic = {
        "ai_balance_usdt": "ai_classic_balance_usdt",
        "ai_entry_usdt": "ai_classic_entry_usdt",
        "ai_max_open": "ai_classic_max_open",
        "ai_trials_per_cycle": "ai_classic_trials_per_cycle",
        "ai_trading_enabled": "ai_classic_enabled",
    }
    for legacy_key, classic_key in legacy_to_classic.items():
        legacy_row = db.query(Setting).filter(Setting.key == legacy_key).first()
        classic_row = db.query(Setting).filter(Setting.key == classic_key).first()
        if legacy_row and classic_row and classic_row.value == defaults[classic_key]:
            classic_row.value = legacy_row.value

    # One-time split: move historical single-lab rows to classic provider.
    split_row = db.query(Setting).filter(Setting.key == "ai_provider_split_done").first()
    if not split_row or split_row.value != "true":
        db.query(AITrade).filter(AITrade.ai_provider == "openai").update({"ai_provider": "classic"}, synchronize_session=False)
        if split_row:
            split_row.value = "true"
        else:
            db.add(Setting(key="ai_provider_split_done", value="true"))
        _log(db, "AI_TRADE", "Migrated legacy AI trades to classic provider")

    # Upgrade old defaults to enhanced profile only once.
    upgrade_done = db.query(Setting).filter(Setting.key == "enhanced_defaults_upgrade_done").first()
    if not upgrade_done or upgrade_done.value != "true":
        upgrades = {
            "trailing_stop_pct": {"0.008", "0.0080000000000000"},
            "time_stop_minutes": {"180", "180.0"},
            "min_quote_volume": {"20000000", "20000000.0"},
            "max_spread_pct": {"0.15", "0.1500000000000000"},
        }
        target = {
            "trailing_stop_pct": "0.01",
            "time_stop_minutes": str(settings.time_stop_minutes),
            "min_quote_volume": str(settings.min_quote_volume),
            "max_spread_pct": str(settings.max_spread_pct),
        }
        for key, legacy_values in upgrades.items():
            row = db.query(Setting).filter(Setting.key == key).first()
            if row and row.value in legacy_values:
                row.value = target[key]
        if upgrade_done:
            upgrade_done.value = "true"
        else:
            db.add(Setting(key="enhanced_defaults_upgrade_done", value="true"))
    db.commit()


def _strategy_config(db: Session) -> dict:
    return {
        "use_score_system": _get_bool(db, "strategy_use_score_system", True),
        "score_threshold": _get_int(db, "strategy_score_threshold", 3),
        "trend_enabled": _get_bool(db, "strategy_trend_enabled", True),
        "pullback_enabled": _get_bool(db, "strategy_pullback_enabled", True),
        "rsi_enabled": _get_bool(db, "strategy_rsi_enabled", True),
        "volume_spike_enabled": _get_bool(db, "strategy_volume_spike_enabled", True),
        "resistance_enabled": _get_bool(db, "strategy_resistance_enabled", True),
        "price_above_ema50_enabled": _get_bool(db, "strategy_price_above_ema50_enabled", False),
        "pullback_max_dist_pct": _get_float(db, "strategy_pullback_max_dist_pct", 1.0),
        "rsi_min": _get_float(db, "strategy_rsi_min", 35.0),
        "rsi_max": _get_float(db, "strategy_rsi_max", 65.0),
        "volume_spike_multiplier": _get_float(db, "strategy_volume_spike_multiplier", 1.3),
        "resistance_min_dist_pct": _get_float(db, "strategy_resistance_min_dist_pct", 1.5),
    }


def _ai_provider_cfg(db: Session, provider: str) -> dict:
    p = provider.lower().strip()
    if p == "classic":
        return {
            "enabled": _get_bool(db, "ai_classic_enabled", _get_bool(db, "ai_trading_enabled", True)),
            "balance": _get_float(db, "ai_classic_balance_usdt", _get_float(db, "ai_balance_usdt", 500.0)),
            "entry_usdt": _get_float(db, "ai_classic_entry_usdt", _get_float(db, "ai_entry_usdt", 30.0)),
            "max_open": _get_int(db, "ai_classic_max_open", _get_int(db, "ai_max_open", 15)),
            "trials_per_cycle": _get_int(db, "ai_classic_trials_per_cycle", _get_int(db, "ai_trials_per_cycle", 20)),
            "daily_loss_limit_pct": _get_float(db, "ai_classic_daily_loss_limit_pct", 3.0),
            "max_trades_per_day": _get_int(db, "ai_classic_max_trades_per_day", 20),
            "max_risk_per_trade_pct": _get_float(db, "ai_classic_max_risk_per_trade_pct", 1.0),
        }
    return {
        "enabled": _get_bool(db, f"ai_{p}_enabled", True),
        "balance": _get_float(db, f"ai_{p}_balance_usdt", 500.0),
        "entry_usdt": _get_float(db, f"ai_{p}_entry_usdt", 30.0),
        "max_open": _get_int(db, f"ai_{p}_max_open", 10),
        "trials_per_cycle": _get_int(db, f"ai_{p}_trials_per_cycle", 20),
        "daily_loss_limit_pct": _get_float(db, f"ai_{p}_daily_loss_limit_pct", 3.0),
        "max_trades_per_day": _get_int(db, f"ai_{p}_max_trades_per_day", 20),
        "max_risk_per_trade_pct": _get_float(db, f"ai_{p}_max_risk_per_trade_pct", 1.0),
    }


def _set_ai_provider_balance(db: Session, provider: str, value: float) -> None:
    p = provider.lower().strip()
    safe_value = f"{max(0.0, value):.8f}"
    _set_setting(db, f"ai_{p}_balance_usdt", safe_value)
    if p == "classic":
        _set_setting(db, "ai_balance_usdt", safe_value)


def _remember_ai(db: Session, provider: str, memory_type: str, payload: dict | str) -> None:
    content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=True)
    db.add(AIAgentMemory(ai_provider=provider, memory_type=memory_type, content=str(content)[:8000]))
    total = db.query(AIAgentMemory).filter(AIAgentMemory.ai_provider == provider).count()
    keep_limit = 5000
    if total > keep_limit:
        to_delete = total - keep_limit
        old_rows = (
            db.query(AIAgentMemory)
            .filter(AIAgentMemory.ai_provider == provider)
            .order_by(AIAgentMemory.id.asc())
            .limit(to_delete)
            .all()
        )
        for row in old_rows:
            db.delete(row)


def _ai_provider_env(provider: str) -> dict:
    return {
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
        "OPENAI_MODEL": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
        "CLAUDE_MODEL": os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022"),
        "DEEPSEEK_API_KEY": os.getenv("DEEPSEEK_API_KEY", ""),
        "DEEPSEEK_MODEL": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
    }


def _provider_has_api_key(provider: str, env_cfg: dict) -> bool:
    p = provider.lower().strip()
    if p == "openai":
        return bool(env_cfg.get("OPENAI_API_KEY"))
    if p == "claude":
        return bool(env_cfg.get("ANTHROPIC_API_KEY"))
    if p == "deepseek":
        return bool(env_cfg.get("DEEPSEEK_API_KEY"))
    return True


def _provider_learning_context(db: Session, provider: str) -> dict:
    closed = (
        db.query(AITrade)
        .filter(AITrade.status == "closed", AITrade.ai_provider == provider)
        .order_by(desc(AITrade.exit_time))
        .limit(120)
        .all()
    )
    if not closed:
        return {"closed_count": 0, "win_rate": 0.0, "net_pnl": 0.0, "top_strategies": []}

    wins = [t for t in closed if float(t.pnl or 0.0) > 0]
    win_rate = (len(wins) / len(closed)) * 100
    net_pnl = sum(float(t.pnl or 0.0) for t in closed)

    by_strategy: dict[str, dict] = {}
    for t in closed:
        sid = t.strategy_id or "-"
        row = by_strategy.setdefault(sid, {"count": 0, "wins": 0, "net": 0.0})
        row["count"] += 1
        pnl_v = float(t.pnl or 0.0)
        row["net"] += pnl_v
        if pnl_v > 0:
            row["wins"] += 1

    ranked = []
    for sid, row in by_strategy.items():
        if row["count"] < 3:
            continue
        ranked.append(
            {
                "strategy_id": sid,
                "trades": row["count"],
                "win_rate": round((row["wins"] / row["count"]) * 100, 2),
                "net_pnl": round(row["net"], 4),
            }
        )
    ranked.sort(key=lambda x: x["net_pnl"], reverse=True)

    return {
        "closed_count": len(closed),
        "win_rate": round(win_rate, 2),
        "net_pnl": round(net_pnl, 4),
        "top_strategies": ranked[:5],
    }


def _ai_provider_day_start_utc() -> datetime:
    return _ksa_day_start_utc()


def _ai_provider_daily_realized_pnl(db: Session, provider: str) -> float:
    day_start = _ai_provider_day_start_utc()
    rows = (
        db.query(AITrade)
        .filter(AITrade.status == "closed", AITrade.ai_provider == provider, AITrade.exit_time >= day_start)
        .all()
    )
    return sum(float(t.pnl or 0.0) for t in rows)


def _sanitize_ai_strategy(cfg: dict) -> dict:
    out = dict(cfg or {})
    out["score_threshold"] = max(2, min(4, int(float(out.get("score_threshold", 3)))))
    out["pullback_max_dist_pct"] = max(0.5, min(2.0, float(out.get("pullback_max_dist_pct", 1.2))))
    out["rsi_min"] = max(20.0, min(45.0, float(out.get("rsi_min", 35.0))))
    out["rsi_max"] = max(55.0, min(80.0, float(out.get("rsi_max", 65.0))))
    if out["rsi_max"] <= out["rsi_min"] + 8:
        out["rsi_max"] = out["rsi_min"] + 8
    out["volume_spike_multiplier"] = max(1.0, min(2.5, float(out.get("volume_spike_multiplier", 1.3))))
    out["resistance_min_dist_pct"] = max(0.5, min(3.5, float(out.get("resistance_min_dist_pct", 1.5))))
    out["tp_pct"] = max(0.01, min(0.06, float(out.get("tp_pct", 0.03))))
    out["sl_pct"] = max(0.005, min(0.03, float(out.get("sl_pct", 0.012))))
    out["trailing_stop_pct"] = max(0.003, min(0.02, float(out.get("trailing_stop_pct", 0.01))))
    out["time_stop_minutes"] = max(30, min(360, int(float(out.get("time_stop_minutes", 120)))))
    out["use_score_system"] = True
    out["trend_enabled"] = True
    out["pullback_enabled"] = True
    out["rsi_enabled"] = True
    out["volume_spike_enabled"] = True
    out["resistance_enabled"] = True
    out["price_above_ema50_enabled"] = bool(out.get("price_above_ema50_enabled", True))
    return out


def _scan_symbols_for_ai_provider(db: Session, provider: str) -> List[dict]:
    p = provider.lower().strip()
    max_symbols = max(5, _get_int(db, f"ai_{p}_scan_symbols", 80))
    min_quote_volume = max(_get_float(db, f"ai_{p}_min_quote_volume", 10_000_000.0), SAFETY_MIN_24H_VOLUME)
    max_spread_pct = min(_get_float(db, f"ai_{p}_max_spread_pct", 0.20), SAFETY_MAX_SPREAD_PCT)

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
        candidates.append({"symbol": symbol, "volume_24h": quote_vol, "spread_pct": spread_pct, "last_price": last})

    if not candidates:
        return []

    btc_k15 = get_klines("BTCUSDT", "15m", 60)
    btc_closes_15m = [float(k[4]) for k in btc_k15]
    btc_change_15m = percent_change(btc_closes_15m, 1)

    ranked: List[dict] = []
    for item in candidates:
        symbol = item["symbol"]
        try:
            k5 = get_klines(symbol, "5m", 260)
            k15 = get_klines(symbol, "15m", 260)
            quote_volumes_5m = [float(k[7]) for k in k5]
            closes_5m = [float(k[4]) for k in k5]
            closes_15m = [float(k[4]) for k in k15]
            if len(quote_volumes_5m) < 50 or len(closes_15m) < 10:
                continue
            atr14 = atr_from_klines(k15, 14)
            last_price = max(float(item["last_price"]), 1e-9)
            atr_ratio = atr14 / last_price
            if atr_ratio < SAFETY_MIN_ATR_RATIO:
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
            final_score = dynamic_volume_score + (relative_strength * 0.8) + (short_term_momentum * 0.4) + (volatility_expansion * 0.6)
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
                    "atr_ratio": atr_ratio,
                    "k5": k5,
                    "k15": k15,
                }
            )
        except Exception:
            continue

    ranked.sort(key=lambda x: x["score"], reverse=True)
    selected = ranked[:max_symbols]
    _notify(
        db,
        "AI_SCAN",
        (
            f"{provider} scan selected={len(selected)} "
            f"max_symbols={max_symbols} min_vol={min_quote_volume:.0f} max_spread={max_spread_pct:.2f}%"
        ),
    )
    top = selected[:5]
    _remember_ai(
        db,
        provider,
        "scan_summary",
        {
            "selected": len(selected),
            "max_symbols": max_symbols,
            "min_quote_volume": min_quote_volume,
            "max_spread_pct": max_spread_pct,
            "top_symbols": [
                {
                    "symbol": t["symbol"],
                    "score": round(float(t.get("score", 0.0)), 4),
                    "vol24h": round(float(t.get("volume_24h", 0.0)), 2),
                    "spread": round(float(t.get("spread_pct", 0.0)), 4),
                }
                for t in top
            ],
        },
    )
    return selected


def _ai_strategy_id(cfg: dict) -> str:
    return (
        f"sc{cfg.get('score_threshold', 3)}_pb{cfg.get('pullback_max_dist_pct', 1.0)}_"
        f"rsi{cfg.get('rsi_min', 35)}-{cfg.get('rsi_max', 65)}_"
        f"vol{cfg.get('volume_spike_multiplier', 1.3)}_"
        f"res{cfg.get('resistance_min_dist_pct', 1.5)}_"
        f"ema50{1 if cfg.get('price_above_ema50_enabled') else 0}"
    )


def _record_market_observations(db: Session, snapshots: List[dict], reasons: Dict[str, str]) -> None:
    now = datetime.utcnow()
    for s in snapshots:
        db.add(
            MarketObservation(
                symbol=s["symbol"],
                observed_at=now,
                last_price=float(s.get("last_price", 0.0)),
                volume_24h=float(s.get("volume_24h", 0.0)),
                spread_pct=float(s.get("spread_pct", 0.0)),
                score=float(s.get("score", 0.0)),
                trend_status=str(s.get("trend_status", "Neutral")),
                signal_status=str(s.get("signal_status", "No Data")),
                decision_reason=reasons.get(s["symbol"], "-"),
            )
        )
    # Keep storage bounded.
    total = db.query(MarketObservation).count()
    limit = 25000
    if total > limit:
        to_delete = total - limit
        oldest = db.query(MarketObservation).order_by(MarketObservation.id.asc()).limit(to_delete).all()
        for row in oldest:
            db.delete(row)


def _manage_shadow_positions(db: Session) -> None:
    if not _get_bool(db, "shadow_enabled", True):
        return
    open_shadow = db.query(ShadowTrade).filter(ShadowTrade.status == "open").all()
    if not open_shadow:
        return
    try:
        prices = get_prices([t.symbol for t in open_shadow])
    except Exception as exc:
        _notify(db, "ERROR", f"Shadow price update failed: {exc}")
        return

    fee_rate = _get_float(db, "fee_rate", settings.fee_rate)
    time_stop_minutes = _get_int(db, "time_stop_minutes", settings.time_stop_minutes)
    for t in open_shadow:
        price = prices.get(t.symbol)
        if price is None:
            continue
        age_minutes = (datetime.utcnow() - t.entry_time).total_seconds() / 60.0
        reason = None
        if price >= t.tp_price:
            reason = "TP"
        elif price <= t.sl_price:
            reason = "SL"
        elif age_minutes >= time_stop_minutes:
            reason = "Time Stop"
        if not reason:
            continue

        proceeds = t.quantity * price
        entry_value = t.entry_price * t.quantity
        entry_fee = entry_value * fee_rate
        exit_fee = proceeds * fee_rate
        pnl_value = (price - t.entry_price) * t.quantity - entry_fee - exit_fee
        pnl_pct = ((price - t.entry_price) / t.entry_price) * 100 if t.entry_price > 0 else 0.0
        t.status = "closed"
        t.exit_time = datetime.utcnow()
        t.exit_price = price
        t.pnl = pnl_value
        t.pnl_pct = pnl_pct
        t.exit_reason = reason
        _notify(db, "SHADOW", f"closed reason={reason} pnl_pct={pnl_pct:+.2f}% pnl_usdt={pnl_value:+.4f}", t.symbol)


def _open_shadow_trades(db: Session, watchlist: List[dict]) -> None:
    if not _get_bool(db, "shadow_enabled", True):
        return
    max_open = _get_int(db, "shadow_max_open", 30)
    open_count = db.query(ShadowTrade).filter(ShadowTrade.status == "open").count()
    if open_count >= max_open:
        return

    notional = _get_float(db, "shadow_notional_usdt", 100.0)
    tp_pct = _get_float(db, "take_profit_pct", settings.take_profit_pct)
    sl_pct = _get_float(db, "stop_loss_pct", settings.stop_loss_pct)
    for item in watchlist:
        if open_count >= max_open:
            break
        if not item.get("strategy_ready"):
            continue
        symbol = item["symbol"]
        if db.query(ShadowTrade).filter(ShadowTrade.status == "open", ShadowTrade.symbol == symbol).first():
            continue
        price = float(item.get("last_price", 0.0))
        if price <= 0:
            continue
        qty = notional / price
        shadow = ShadowTrade(
            symbol=symbol,
            status="open",
            entry_time=datetime.utcnow(),
            entry_price=price,
            quantity=qty,
            notional_usdt=notional,
            tp_price=price * (1 + tp_pct),
            sl_price=price * (1 - sl_pct),
            source_score=float(item.get("scanner_score", 0.0)),
        )
        db.add(shadow)
        open_count += 1
        _notify(db, "SHADOW", f"opened notional={notional:.2f} entry={price:.6f} qty={qty:.6f}", symbol)


def _manage_ai_positions(db: Session, provider: str) -> None:
    cfg_p = _ai_provider_cfg(db, provider)
    if not cfg_p["enabled"]:
        return
    open_ai = db.query(AITrade).filter(AITrade.status == "open", AITrade.ai_provider == provider).all()
    if not open_ai:
        return
    try:
        prices = get_prices([t.symbol for t in open_ai])
    except Exception as exc:
        _notify(db, "ERROR", f"AI price update failed: {exc}")
        return

    fee_rate = _get_float(db, "fee_rate", settings.fee_rate)
    cash = cfg_p["balance"]
    for t in open_ai:
        price = prices.get(t.symbol)
        if price is None:
            continue
        if not t.highest_price or price > t.highest_price:
            t.highest_price = price
        if t.trailing_active and t.trailing_stop_price:
            t.trailing_stop_price = max(t.trailing_stop_price, (t.highest_price or price) * (1 - 0.008))

        strategy_cfg = {}
        try:
            strategy_cfg = json.loads(t.strategy_json or "{}")
        except Exception:
            strategy_cfg = {}
        time_stop_minutes = int(strategy_cfg.get("time_stop_minutes", _get_int(db, "time_stop_minutes", settings.time_stop_minutes)))
        reason = None
        age_minutes = (datetime.utcnow() - t.entry_time).total_seconds() / 60.0
        if t.trailing_active and t.trailing_stop_price and price <= t.trailing_stop_price:
            reason = "Trailing Stop"
        elif price <= t.sl_price:
            reason = "SL"
        elif price >= t.tp_price:
            t.trailing_active = 1
            t.trailing_stop_price = price * (1 - float(strategy_cfg.get("trailing_stop_pct", 0.008)))
        if age_minutes >= time_stop_minutes and not reason:
            reason = "Time Stop"
        if not reason:
            continue

        proceeds = t.quantity * price
        entry_value = t.entry_price * t.quantity
        entry_fee = entry_value * fee_rate
        exit_fee = proceeds * fee_rate
        pnl_value = (price - t.entry_price) * t.quantity - entry_fee - exit_fee
        pnl_pct = ((price - t.entry_price) / t.entry_price) * 100 if t.entry_price > 0 else 0.0
        cash += proceeds - exit_fee
        t.status = "closed"
        t.exit_time = datetime.utcnow()
        t.exit_price = price
        t.pnl = pnl_value
        t.pnl_pct = pnl_pct
        t.exit_reason = reason
        _notify(
            db,
            "AI_TRADE",
            f"{provider} closed reason={reason} pnl_pct={pnl_pct:+.2f}% pnl_usdt={pnl_value:+.4f}",
            t.symbol,
        )
        _remember_ai(
            db,
            provider,
            "exit",
            {
                "symbol": t.symbol,
                "reason": reason,
                "entry_price": round(float(t.entry_price), 8),
                "exit_price": round(float(price), 8),
                "pnl_pct": round(float(pnl_pct), 4),
                "pnl_usdt": round(float(pnl_value), 6),
            },
        )

    _set_ai_provider_balance(db, provider, cash)


def _open_ai_trades(db: Session, provider: str) -> None:
    cfg_p = _ai_provider_cfg(db, provider)
    if not cfg_p["enabled"]:
        return
    env_cfg = _ai_provider_env(provider)
    if provider in {"openai", "claude", "deepseek"} and not _provider_has_api_key(provider, env_cfg):
        _notify(db, "AI_TRADE", f"{provider} skipped: missing API key")
        _remember_ai(db, provider, "guard", {"status": "skipped", "reason": "missing_api_key"})
        return
    day_pnl = _ai_provider_daily_realized_pnl(db, provider)
    daily_loss_limit_pct = float(cfg_p.get("daily_loss_limit_pct", 3.0))
    if day_pnl <= -(cfg_p["balance"] * (daily_loss_limit_pct / 100.0)):
        _notify(db, "AI_RISK", f"{provider} paused for day: daily loss limit reached ({day_pnl:+.4f} USDT)")
        _remember_ai(
            db,
            provider,
            "guard",
            {
                "status": "blocked",
                "reason": "daily_loss_limit",
                "day_pnl": round(day_pnl, 4),
                "limit_pct": daily_loss_limit_pct,
            },
        )
        return

    ai_balance = cfg_p["balance"]
    if ai_balance < 20:
        return
    global_max_open = _get_int(db, "max_open_trades", settings.max_open_trades)
    max_open = min(int(cfg_p["max_open"]), global_max_open)
    open_count = db.query(AITrade).filter(AITrade.status == "open", AITrade.ai_provider == provider).count()
    global_open_count = db.query(AITrade).filter(AITrade.status == "open").count()
    if open_count >= max_open or global_open_count >= global_max_open:
        return
    entry_notional = float(cfg_p["entry_usdt"])
    trials = int(cfg_p["trials_per_cycle"])
    max_trades_per_day = int(cfg_p.get("max_trades_per_day", 20))
    day_start = _ai_provider_day_start_utc()
    opened_today = (
        db.query(AITrade)
        .filter(AITrade.ai_provider == provider, AITrade.entry_time >= day_start)
        .count()
    )
    if opened_today >= max_trades_per_day:
        _notify(db, "AI_RISK", f"{provider} paused for day: max trades reached ({opened_today}/{max_trades_per_day})")
        _remember_ai(
            db,
            provider,
            "guard",
            {
                "status": "blocked",
                "reason": "max_trades_per_day",
                "opened_today": opened_today,
                "max_trades_per_day": max_trades_per_day,
            },
        )
        return
    ranked_symbols = _scan_symbols_for_ai_provider(db, provider)

    pool = ranked_symbols[: min(len(ranked_symbols), 80)]
    if not pool:
        _remember_ai(db, provider, "plan", {"status": "idle", "reason": "empty_scan_pool"})
        return
    learning = _provider_learning_context(db, provider)
    _remember_ai(
        db,
        provider,
        "plan",
        {
            "status": "active",
            "balance": round(ai_balance, 4),
            "open_count": open_count,
            "max_open": max_open,
            "global_open_count": global_open_count,
            "global_max_open": global_max_open,
            "trials": trials,
            "pool_size": len(pool),
            "learning": learning,
            "next_action": "evaluate_candidates_and_open_if_strategy_ready",
        },
    )
    random.shuffle(pool)
    attempts = 0
    for item in pool:
        if attempts >= trials or open_count >= max_open or global_open_count >= global_max_open:
            break
        attempts += 1
        symbol = item["symbol"]
        if db.query(AITrade).filter(AITrade.status == "open", AITrade.symbol == symbol, AITrade.ai_provider == provider).first():
            continue
        price = float(item.get("last_price", 0.0))
        if price <= 0:
            continue
        ctx = {
            "symbol": symbol,
            "score": round(float(item.get("score", 0.0)), 4),
            "vol24h": round(float(item.get("volume_24h", 0.0)), 2),
            "spread_pct": round(float(item.get("spread_pct", 0.0)), 4),
            "relative_strength": round(float(item.get("relative_strength", 0.0)), 4),
            "vol_exp": round(float(item.get("recent_volume_expansion", 0.0)), 4),
            "learning": learning,
        }
        cfg, usage = propose_strategy_with_usage(
            provider,
            ctx,
            env_cfg,
        )
        record_usage(db, provider, "strategy", usage)
        cfg = _sanitize_ai_strategy(cfg)
        if provider in {"openai", "claude", "deepseek"}:
            expected_source = f"llm_{provider}"
            if cfg.get("strategy_source") != expected_source:
                _notify(
                    db,
                    "AI_TRADE",
                    f"{provider} skipped candidate {symbol}: source={cfg.get('strategy_source', 'unknown')} expected={expected_source}",
                )
                _remember_ai(
                    db,
                    provider,
                    "entry_reject",
                    {
                        "symbol": symbol,
                        "reason": "non_provider_source",
                        "source": cfg.get("strategy_source", "unknown"),
                        "expected": expected_source,
                    },
                )
                continue
        ready, _, checks = trend_pullback_signal_with_checks(item.get("k5") or [], item.get("k15") or [], config=cfg)
        if not ready:
            _remember_ai(
                db,
                provider,
                "entry_reject",
                {
                    "symbol": symbol,
                    "reason": "strategy_not_ready",
                    "score": f"{checks.get('score_count', 0)}/{checks.get('score_threshold', 3)}",
                },
            )
            continue

        tp_pct = _get_float(db, "take_profit_pct", settings.take_profit_pct)
        sl_pct = _get_float(db, "stop_loss_pct", settings.stop_loss_pct)
        max_risk_pct = max(0.1, float(cfg_p.get("max_risk_per_trade_pct", 1.0)))
        risk_notional_cap = ai_balance * (max_risk_pct / 100.0) / max(sl_pct, 0.001)
        notional = min(entry_notional, ai_balance * 0.25, risk_notional_cap)
        if notional < 10:
            continue
        qty = notional / price
        trade = AITrade(
            symbol=symbol,
            ai_provider=provider,
            status="open",
            entry_time=datetime.utcnow(),
            entry_price=price,
            quantity=qty,
            notional_usdt=notional,
            tp_price=price * (1 + tp_pct),
            sl_price=price * (1 - sl_pct),
            trailing_stop_price=None,
            trailing_active=0,
            highest_price=price,
            strategy_id=_ai_strategy_id(cfg),
            strategy_json=json.dumps(
                {
                    **cfg,
                    "provider_source": cfg.get("strategy_source", "unknown"),
                    "score_count": checks.get("score_count", 0),
                    "score_threshold": checks.get("score_threshold", 3),
                }
            ),
        )
        db.add(trade)
        fee_rate = _get_float(db, "fee_rate", settings.fee_rate)
        ai_balance -= notional + (notional * fee_rate)
        open_count += 1
        global_open_count += 1
        _notify(
            db,
            "AI_TRADE",
            (
                f"{provider} opened strategy={trade.strategy_id} symbol={symbol} "
                f"entry={price:.6f} notional={notional:.2f} risk_cap={max_risk_pct:.2f}% "
                f"source={cfg.get('strategy_source', 'unknown')} "
                f"score={checks.get('score_count', 0)}/{checks.get('score_threshold', 3)}"
            ),
            symbol,
        )
        _remember_ai(
            db,
            provider,
            "entry_accept",
            {
                "symbol": symbol,
                "entry_price": round(price, 8),
                "notional": round(notional, 4),
                "strategy_id": trade.strategy_id,
                "source": cfg.get("strategy_source", "unknown"),
                "score": f"{checks.get('score_count', 0)}/{checks.get('score_threshold', 3)}",
                "next_action": "monitor_open_trade_for_tp_sl_trailing_timestop",
            },
        )

    _set_ai_provider_balance(db, provider, ai_balance)


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
    today = _ksa_now().strftime("%Y-%m-%d")
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
    klines = get_klines("BTCUSDT", "4h", 220)
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


def _scan_symbols(db: Session) -> tuple[List[dict], dict]:
    min_quote_volume = max(_get_float(db, "min_quote_volume", settings.min_quote_volume), SAFETY_MIN_24H_VOLUME)
    max_spread_pct = min(_get_float(db, "max_spread_pct", settings.max_spread_pct), SAFETY_MAX_SPREAD_PCT)
    t24 = get_24h_tickers()
    books = get_book_tickers()
    book_map: Dict[str, dict] = {b["symbol"]: b for b in books}

    candidates = []
    total_usdt_pairs = 0
    for t in t24:
        symbol = t["symbol"]
        if not symbol.endswith("USDT"):
            continue
        total_usdt_pairs += 1
        if symbol in EXCLUDED_SYMBOLS:
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
    stats = {
        "total_usdt_pairs": total_usdt_pairs,
        "safety_passed": len(candidates),
        "min_quote_volume": min_quote_volume,
        "max_spread_pct": max_spread_pct,
    }
    return candidates, stats


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
            atr14 = atr_from_klines(k15, 14)
            last_price = max(float(item["last_price"]), 1e-9)
            atr_ratio = atr14 / last_price
            if atr_ratio < SAFETY_MIN_ATR_RATIO:
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
                    "atr_ratio": atr_ratio,
                    "k5": k5,
                    "k15": k15,
                }
            )
        except Exception:
            continue

    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:max_symbols]


def _build_priority_watchlist(db: Session, scanned: List[dict], strategy_cfg: dict) -> List[dict]:
    watchlist = []
    btc_klines_15m = get_klines("BTCUSDT", "15m", 120)
    btc_closes = [float(k[4]) for k in btc_klines_15m]
    momentum_volume_enabled = _get_bool(db, "momentum_volume_enabled", True)
    momentum_volatility_enabled = _get_bool(db, "momentum_volatility_enabled", True)
    momentum_rs_enabled = _get_bool(db, "momentum_relative_strength_enabled", True)
    momentum_price_1h_enabled = _get_bool(db, "momentum_price_above_ema200_1h_enabled", True)

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
            k1h = get_klines(symbol, "1h", 230)
            closes_1h = [float(k[4]) for k in k1h]
            ema200_1h = ema(closes_1h[-210:], 200) if len(closes_1h) >= 210 else 0.0
            cond_price_above_ema200_1h = item["last_price"] > ema200_1h if ema200_1h > 0 else False
            momentum_checks = {
                "volume": (not momentum_volume_enabled) or cond_volume,
                "volatility": (not momentum_volatility_enabled) or cond_volatility,
                "relative_strength": (not momentum_rs_enabled) or cond_rel,
                "price_above_ema200_1h": (not momentum_price_1h_enabled) or cond_price_above_ema200_1h,
            }
            momentum = all(momentum_checks.values())

            signal, signal_status, checks = trend_pullback_signal_with_checks(k5, k15, config=strategy_cfg)
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
                        f"rs={item.get('relative_strength', 0):+.3f} "
                        f"atr={item.get('atr_ratio', 0)*100:.2f}% "
                        f"price_above_ema200_1h={cond_price_above_ema200_1h}"
                    ),
                    symbol,
                )
                _notify(
                    db,
                    "ENTRY_DECISION",
                    (
                        f"strategy_ready={signal} reason={checks.get('reason_code')} trend={checks.get('trend_ok')} pullback={checks.get('pullback_ok')} "
                        f"price_above_ema50_15m={checks.get('price_above_ema50_15m_ok')} "
                        f"score={checks.get('score_count', 0)}/{checks.get('score_threshold', 3)} "
                        f"rsi_ok={checks.get('rsi_ok')} rsi={checks.get('rsi_value', 0):.2f} "
                        f"volume_spike={checks.get('volume_spike_ok')} vol_now={checks.get('volume_now', 0):.2f} "
                        f"vol_avg20={checks.get('volume_avg20', 0):.2f} resistance_ok={checks.get('resistance_ok')} "
                        f"failed={','.join(checks.get('failed_checks', [])) if checks.get('failed_checks') else 'none'}"
                    ),
                    symbol,
                )
            else:
                scanner_reasons = []
                if momentum_volume_enabled and not cond_volume:
                    scanner_reasons.append("volume_accumulation_failed")
                if momentum_volatility_enabled and not cond_volatility:
                    scanner_reasons.append("volatility_expansion_failed")
                if momentum_rs_enabled and not cond_rel:
                    scanner_reasons.append("relative_strength_failed")
                if momentum_price_1h_enabled and not cond_price_above_ema200_1h:
                    scanner_reasons.append("price_below_ema200_1h")
                if signal_status == "Blocked":
                    scanner_reasons.append("trend_blocked")
                reason_text = ",".join(scanner_reasons) if scanner_reasons else "scanner_filter_failed"
                _notify(
                    db,
                    "ENTRY_DECISION",
                    (
                        f"rejected reason={reason_text} "
                        f"signal_status={signal_status} "
                        f"trend={checks.get('trend_ok')} pullback={checks.get('pullback_ok')} "
                        f"price_above_ema50_15m={checks.get('price_above_ema50_15m_ok')} "
                        f"score={checks.get('score_count', 0)}/{checks.get('score_threshold', 3)} "
                        f"rsi_ok={checks.get('rsi_ok')} volume_spike={checks.get('volume_spike_ok')} "
                        f"resistance_ok={checks.get('resistance_ok')}"
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
    today_start = _ksa_day_start_utc()
    max_trades_per_day = _get_int(db, "max_trades_per_day", 10)
    trades_today = db.query(Trade).filter(Trade.entry_time >= today_start).count()
    if trades_today >= max_trades_per_day:
        _notify(
            db,
            "ENTRY_DECISION",
            f"rejected reason=max_trades_per_day ({trades_today}/{max_trades_per_day})",
            symbol,
        )
        return

    cash = _cash_balance(db)
    if cash <= 25:
        _notify(db, "ENTRY_DECISION", "rejected reason=insufficient_cash", symbol)
        return

    fee_rate = _get_float(db, "fee_rate", settings.fee_rate)
    tp_pct = _get_float(db, "take_profit_pct", settings.take_profit_pct)
    sl_pct = _get_float(db, "stop_loss_pct", settings.stop_loss_pct)
    risk_pct = _get_float(db, "risk_per_trade_pct", 1.0) / 100.0
    max_entry_usdt = _get_float(db, "max_entry_usdt", 0.0)
    market_price = float(item["last_price"])
    entry_price = _apply_slippage(db, market_price, "buy")
    sl_price = entry_price * (1 - sl_pct)
    risk_per_unit = max(entry_price - sl_price, entry_price * 0.001)
    risk_capital = _compute_equity(db) * risk_pct
    qty = risk_capital / risk_per_unit
    max_affordable_qty = (cash * 0.95) / entry_price
    if max_entry_usdt > 0:
        max_affordable_qty = min(max_affordable_qty, max_entry_usdt / entry_price)
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
    if max_entry_usdt > 0 and max_entry_usdt < min_notional:
        _notify(
            db,
            "ENTRY_DECISION",
            f"rejected reason=max_entry_usdt_too_low ({max_entry_usdt:.4f}<{min_notional:.4f})",
            symbol,
        )
        return
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
    db.flush()
    if not _mirror_open_live(db, trade, allocation):
        _notify(
            db,
            "LIVE",
            "entry mirror failed; paper trade kept open (paper-only execution for this entry)",
            symbol,
            telegram=True,
        )
    _update_cash_balance(db, new_cash)
    checks = item.get("entry_checks", {})
    _notify(
        db,
        "ENTRY_DECISION",
        (
            f"accepted reason={checks.get('reason_code', 'score_based_accept')} "
            f"score={checks.get('score_count', 0)}/{checks.get('score_threshold', 3)} "
            f"scanner_score={item.get('scanner_score', 0):.4f} "
            f"entry_usdt={allocation:.4f} "
            f"qty={qty:.6f} entry_price={entry_price:.6f} risk_pct={risk_pct*100:.2f}"
        ),
        symbol,
    )
    _notify(
        db,
        "TRADE",
        (
            f"Paper trade opened entry={entry_price:.6f} qty={qty:.6f} "
            f"entry_usdt={allocation:.2f} tp={trade.tp_price:.6f} sl={trade.sl_price:.6f}"
        ),
        symbol,
        telegram=True,
    )


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
    time_stop_minutes = _get_int(db, "time_stop_minutes", settings.time_stop_minutes)
    trailing_stop_pct = _get_float(db, "trailing_stop_pct", 0.01)
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
            pnl_pct = ((price - t.entry_price) / t.entry_price) * 100 if t.entry_price > 0 else 0.0
            cash += proceeds - exit_fee
            t.status = "closed"
            t.exit_price = price
            t.exit_time = datetime.utcnow()
            t.pnl = pnl_value
            t.exit_reason = reason
            _mirror_close_live(db, t, reason)
            _notify(
                db,
                "TRADE",
                (
                    f"Paper trade closed ({reason}) exit={price:.6f} "
                    f"pnl_pct={pnl_pct:+.2f}% pnl_usdt={pnl_value:+.4f}"
                ),
                t.symbol,
                telegram=True,
            )
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
        pnl_pct = ((price - t.entry_price) / t.entry_price) * 100 if t.entry_price > 0 else 0.0
        cash += proceeds - exit_fee
        t.status = "closed"
        t.exit_price = price
        t.exit_time = datetime.utcnow()
        t.pnl = pnl_value
        t.exit_reason = reason
        _mirror_close_live(db, t, reason)
        _notify(
            db,
            "TRADE",
            (
                f"Paper trade closed ({reason}) exit={price:.6f} "
                f"pnl_pct={pnl_pct:+.2f}% pnl_usdt={pnl_value:+.4f}"
            ),
            t.symbol,
            telegram=True,
        )

    _update_cash_balance(db, cash)


def run_position_watch_cycle(db: Session) -> None:
    init_defaults(db)
    _reconcile_cash_if_needed(db)
    _manage_open_positions(db)
    _manage_shadow_positions(db)
    for provider in ("classic", "openai", "claude", "deepseek"):
        _manage_ai_positions(db, provider)
    db.commit()


def run_cycle(db: Session) -> None:
    init_defaults(db)
    _reconcile_cash_if_needed(db)
    if _is_live_mode(db) and not binance_live_configured():
        _notify(db, "LIVE", "Live mode is selected but API keys are missing. Entries will be rejected.", telegram=True)

    run_position_watch_cycle(db)
    blocked_by_daily_loss = _daily_loss_triggered(db)
    if blocked_by_daily_loss:
        _notify(db, "ENTRY_DECISION", "rejected reason=daily_loss_limit_reached", "-")

    paused = _mode_is_paused(db)
    if paused:
        _notify(db, "PAUSE", "Bot paused. Skipping new entries.", telegram=True)
        _notify(db, "ENTRY_DECISION", "rejected reason=bot_paused", "-")

    _notify(db, "SCAN", "Market scan started")
    try:
        btc_filter_enabled = _get_bool(db, "btc_filter_enabled", True)
        market_blocked = _btc_market_blocked() if btc_filter_enabled else False
        _notify(db, "REGIME", f"BTC filter={'on' if btc_filter_enabled else 'off'} timeframe=4h blocked={market_blocked}")
        safety_symbols, scan_stats = _scan_symbols(db)
        ranked_symbols = _dynamic_rank_symbols(db, safety_symbols)
        strategy_cfg = _strategy_config(db)
        filters_map = _symbol_filters_map([r["symbol"] for r in ranked_symbols])
        _notify(
            db,
            "SCAN",
            (
                f"scan_stats total_usdt={scan_stats.get('total_usdt_pairs', 0)} "
                f"safety_passed={scan_stats.get('safety_passed', 0)} "
                f"ranked={len(ranked_symbols)} "
                f"max_symbols={_get_int(db, 'max_symbols', settings.max_symbols)} "
                f"min_vol={scan_stats.get('min_quote_volume', 0):.0f} "
                f"max_spread={scan_stats.get('max_spread_pct', 0):.2f}%"
            ),
        )
        for ranked in ranked_symbols[:5]:
            _notify(
                db,
                "SCANNER",
                (
                    f"rank score={ranked.get('score', 0):.4f} "
                    f"vol24h={ranked['volume_24h']:.0f} "
                    f"ratio1h24h={ranked.get('vol_ratio_1h_24h', 0):.5f} "
                    f"volExp={ranked.get('recent_volume_expansion', 0):.3f} "
                    f"rs={ranked.get('relative_strength', 0):+.3f} "
                    f"atr={ranked.get('atr_ratio', 0)*100:.2f}%"
                ),
                ranked["symbol"],
            )
        watchlist = _build_priority_watchlist(db, ranked_symbols, strategy_cfg)
        _notify(db, "SCAN", f"watchlist_size={len(watchlist)}")
        for provider in ("classic", "openai", "claude", "deepseek"):
            _open_ai_trades(db, provider)
    except Exception as exc:
        _notify(db, "ERROR", f"Scanner failed: {exc}", telegram=True)
        db.commit()
        return

    # Persist learning data snapshots for internal advisor/training datasets.
    latest_decisions = (
        db.query(LogEntry)
        .filter(LogEntry.event_type == "ENTRY_DECISION")
        .order_by(desc(LogEntry.id))
        .limit(800)
        .all()
    )
    decision_map: Dict[str, str] = {}
    for log in latest_decisions:
        sym = (log.symbol or "").strip()
        if not sym or sym == "-" or sym in decision_map:
            continue
        reason_match = "unknown"
        msg = log.message or ""
        if "reason=" in msg:
            reason_match = msg.split("reason=", 1)[1].split()[0]
        decision_map[sym] = reason_match
    observations = [
        {
            "symbol": r["symbol"],
            "last_price": float(r.get("last_price", 0.0)),
            "volume_24h": float(r.get("volume_24h", 0.0)),
            "spread_pct": float(r.get("spread_pct", 0.0)),
            "score": float(r.get("score", 0.0)),
            "trend_status": "Bullish",
            "signal_status": "Ranked",
        }
        for r in ranked_symbols
    ]
    _record_market_observations(db, observations, decision_map)
    _notify(db, "DATA", f"stored_observations={len(observations)}")

    if market_blocked:
        _notify(db, "RISK", "BTC regime filter active on 4h (EMA50<EMA200). No new trades.")
        for item in watchlist:
            _notify(db, "ENTRY_DECISION", "rejected reason=btc_regime_blocked_4h", item["symbol"])
    elif not paused and not blocked_by_daily_loss:
        _open_shadow_trades(db, watchlist)
        for item in watchlist:
            if item.get("strategy_ready"):
                _try_open_trade(db, item, filters_map)
            else:
                checks = item.get("entry_checks", {})
                _notify(
                    db,
                    "ENTRY_DECISION",
                    (
                        f"rejected reason={checks.get('reason_code', 'strategy_not_ready')} trend={checks.get('trend_ok')} pullback={checks.get('pullback_ok')} "
                        f"price_above_ema50_15m={checks.get('price_above_ema50_15m_ok')} "
                        f"score={checks.get('score_count', 0)}/{checks.get('score_threshold', 3)} "
                        f"rsi_ok={checks.get('rsi_ok')} volume_spike={checks.get('volume_spike_ok')} resistance_ok={checks.get('resistance_ok')}"
                    ),
                    item["symbol"],
                )
    else:
        _open_shadow_trades(db, watchlist)
        _notify(db, "ENTRY_DECISION", "rejected reason=entries_blocked_but_shadow_running", "-")

    _notify(db, "CYCLE", "Trading cycle completed")
    db.commit()


def portfolio_snapshot(db: Session) -> dict:
    cash = _cash_balance(db)
    open_trades = db.query(Trade).filter(Trade.status == "open").all()
    day_start = _ksa_day_start_utc()
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
