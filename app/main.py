import time
import threading
import re
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.core.migrations import apply_sqlite_migrations
from app.models import AIAgentMemory, AIChatMessage, AIProviderUsage, AITrade, LogEntry, MarketObservation, Setting, ShadowTrade, SymbolSnapshot, Trade
from app.services.paper_engine import init_defaults, portfolio_snapshot, run_cycle, statistics_snapshot
from app.services.ai_providers import chat_with_provider_with_usage
from app.services.ai_usage import record_usage
from app.services.telegram_alerts import telegram_test

app = FastAPI(title="Crypto Bot Dashboard")
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
templates = Jinja2Templates(directory="app/web/templates")
scheduler = BackgroundScheduler(timezone="UTC")
cycle_lock = threading.Lock()
try:
    APP_TZ = ZoneInfo(settings.app_timezone)
    UTC_TZ = ZoneInfo("UTC")
except Exception:
    APP_TZ = timezone(timedelta(hours=3), name="AST")
    UTC_TZ = timezone.utc


def _base_context(active_page: str) -> dict:
    now_local = datetime.utcnow().replace(tzinfo=UTC_TZ).astimezone(APP_TZ)
    return {
        "active_page": active_page,
        "last_update": now_local.strftime("%Y-%m-%d %H:%M %Z"),
    }


def _as_local(dt: datetime | None) -> datetime:
    if not dt:
        return datetime.utcnow().replace(tzinfo=UTC_TZ).astimezone(APP_TZ)
    return dt.replace(tzinfo=UTC_TZ).astimezone(APP_TZ)


def _format_age(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    mins = total_seconds // 60
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h {mins % 60}m"


def _extract_log_field(message: str, key: str) -> str | None:
    if not message:
        return None
    m = re.search(rf"{re.escape(key)}=([^\s]+)", message)
    return m.group(1) if m else None


def _as_check(value: str | None) -> str:
    if value is None:
        return "-"
    low = value.strip().lower()
    if low in {"true", "1", "yes", "on"}:
        return "OK"
    if low in {"false", "0", "no", "off", "none"}:
        return "X"
    return "-"


def _entry_recommendation(signal: str, reason: str, score: str) -> str:
    signal_l = (signal or "").lower()
    reason_l = (reason or "").lower()
    if signal_l == "buy ready":
        return "Candidate looks ready. Wait for next cycle confirmation and execution limits."
    if "btc_regime_blocked" in reason_l:
        return "Blocked by BTC regime filter. No new entries until regime turns positive."
    if "bot_paused" in reason_l or "daily_loss" in reason_l:
        return "Bot-level risk protection is active. Review pause and daily loss status."
    if "score<3" in reason_l:
        return f"Not enough score ({score}). Wait for one or more conditions to turn positive."
    if signal_l == "watch":
        return "Close to entry. Keep under watch for improving score/volume."
    if signal_l == "momentum candidate":
        return "Momentum detected; keep monitoring until entry checks confirm."
    if signal_l == "blocked":
        return "Core conditions blocked for now."
    return "No action yet. Continue monitoring."


def _provider_api_connected(provider: str) -> bool:
    p = provider.lower().strip()
    if p == "openai":
        return bool(os.getenv("OPENAI_API_KEY", "").strip())
    if p == "claude":
        return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    if p == "deepseek":
        return bool(os.getenv("DEEPSEEK_API_KEY", "").strip())
    return True


def _provider_env_cfg() -> dict:
    return {
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
        "OPENAI_MODEL": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
        "CLAUDE_MODEL": os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022"),
        "DEEPSEEK_API_KEY": os.getenv("DEEPSEEK_API_KEY", ""),
        "DEEPSEEK_MODEL": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
    }


def _local_day_start_utc_naive() -> datetime:
    now_local = datetime.utcnow().replace(tzinfo=UTC_TZ).astimezone(APP_TZ)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(UTC_TZ).replace(tzinfo=None)


def _extract_action_json(text: str) -> dict | None:
    if not text:
        return None
    marker = "ACTION_JSON:"
    idx = text.find(marker)
    if idx < 0:
        return None
    raw = text[idx + len(marker) :].strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except Exception:
        return None


def _set_setting_value(db, key: str, value: str) -> None:
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def _apply_ai_chat_actions(db, provider: str, action_payload: dict | None) -> list[str]:
    if not action_payload or not isinstance(action_payload, dict):
        return []
    set_block = action_payload.get("set")
    if not isinstance(set_block, dict):
        return []

    p = provider.lower().strip()
    allowed = {
        "max_trades_per_day": (f"ai_{p}_max_trades_per_day", int, 1, 2000),
        "daily_loss_limit_pct": (f"ai_{p}_daily_loss_limit_pct", float, 0.1, 50.0),
        "max_open": (f"ai_{p}_max_open", int, 1, 200),
        "entry_usdt": (f"ai_{p}_entry_usdt", float, 5.0, 100000.0),
        "trials_per_cycle": (f"ai_{p}_trials_per_cycle", int, 1, 500),
        "scan_symbols": (f"ai_{p}_scan_symbols", int, 5, 1000),
        "min_quote_volume": (f"ai_{p}_min_quote_volume", float, 1000000.0, 10000000000.0),
        "max_spread_pct": (f"ai_{p}_max_spread_pct", float, 0.01, 5.0),
        "max_risk_per_trade_pct": (f"ai_{p}_max_risk_per_trade_pct", float, 0.1, 10.0),
        "enabled": (f"ai_{p}_enabled", bool, 0, 1),
    }

    applied: list[str] = []
    for k, v in set_block.items():
        if k not in allowed:
            continue
        setting_key, typ, min_v, max_v = allowed[k]
        try:
            if typ is bool:
                b = str(v).lower() in {"1", "true", "yes", "on"}
                _set_setting_value(db, setting_key, "true" if b else "false")
                applied.append(f"{k}={'true' if b else 'false'}")
            elif typ is int:
                iv = int(float(v))
                iv = max(min_v, min(max_v, iv))
                _set_setting_value(db, setting_key, str(iv))
                applied.append(f"{k}={iv}")
            else:
                fv = float(v)
                fv = max(min_v, min(max_v, fv))
                _set_setting_value(db, setting_key, str(fv))
                applied.append(f"{k}={fv}")
        except Exception:
            continue
    if applied:
        db.add(LogEntry(event_type="AI_CONFIG", symbol=provider.upper(), message=f"Applied via chat: {', '.join(applied)}"))
    return applied


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    apply_sqlite_migrations(engine)
    db = SessionLocal()
    try:
        init_defaults(db)
        run_cycle(db)
    finally:
        db.close()
    scheduler.add_job(_scheduled_cycle, "interval", seconds=settings.cycle_seconds, id="paper_cycle", replace_existing=True)
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def _scheduled_cycle() -> None:
    if not cycle_lock.acquire(blocking=False):
        db = SessionLocal()
        try:
            db.add(LogEntry(event_type="CYCLE", symbol="-", message="Skipped overlapping cycle"))
            db.commit()
        finally:
            db.close()
        return
    db = SessionLocal()
    try:
        run_cycle(db)
    finally:
        db.close()
        cycle_lock.release()


@app.get("/", response_class=HTMLResponse)
async def overview(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        snap = portfolio_snapshot(db)
        stats = statistics_snapshot(db)
        ctx = _base_context("overview")
        ctx.update(
            {
                "request": request,
                "metrics": {
                    "balance": f"{snap['balance']:.2f} USDT",
                    "daily_pnl": f"{snap['daily_pnl']:+.2f} USDT",
                    "weekly_pnl": f"{snap['weekly_pnl']:+.2f} USDT",
                    "daily_pnl_value": snap["daily_pnl"],
                    "weekly_pnl_value": snap["weekly_pnl"],
                    "win_rate": f"{snap['win_rate']:.1f}%",
                    "open_positions": snap["open_positions"],
                    "drawdown": f"{stats['max_drawdown_pct']:.2f}%",
                },
                "equity_points": _equity_points(db, snap["balance"]),
            }
        )
        return templates.TemplateResponse("overview.html", ctx)
    finally:
        db.close()


def _equity_points(db, current_balance: float) -> list[float]:
    trades = db.query(Trade).filter(Trade.status == "closed").order_by(Trade.exit_time.asc()).limit(5).all()
    points = [settings.paper_start_balance]
    running = settings.paper_start_balance
    for t in trades:
        running += t.pnl or 0.0
        points.append(round(running, 2))
    points.append(round(current_balance, 2))
    return points[-6:]


@app.get("/symbols", response_class=HTMLResponse)
async def symbols(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        rows = db.query(SymbolSnapshot).order_by(SymbolSnapshot.volume_24h.desc()).all()
        entry_logs = db.query(LogEntry).filter(LogEntry.event_type == "ENTRY_DECISION").order_by(desc(LogEntry.id)).limit(800).all()
        latest_by_symbol: dict[str, str] = {}
        for log in entry_logs:
            sym = (log.symbol or "").strip()
            if not sym or sym == "-" or sym in latest_by_symbol:
                continue
            latest_by_symbol[sym] = log.message or ""
        symbols_data = [
            {
                "symbol": r.symbol,
                "volume_24h": f"{r.volume_24h:,.0f}",
                "spread": f"{r.spread_pct:.3f}%",
                "trend": r.trend_status,
                "signal": r.signal_status,
                "score": _extract_log_field(latest_by_symbol.get(r.symbol, ""), "score") or "-",
                "trend_ok": _as_check(_extract_log_field(latest_by_symbol.get(r.symbol, ""), "trend")),
                "pullback_ok": _as_check(_extract_log_field(latest_by_symbol.get(r.symbol, ""), "pullback")),
                "rsi_ok": _as_check(_extract_log_field(latest_by_symbol.get(r.symbol, ""), "rsi_ok")),
                "volume_ok": _as_check(_extract_log_field(latest_by_symbol.get(r.symbol, ""), "volume_spike")),
                "resistance_ok": _as_check(_extract_log_field(latest_by_symbol.get(r.symbol, ""), "resistance_ok")),
                "reason": _extract_log_field(latest_by_symbol.get(r.symbol, ""), "reason") or "-",
            }
            for r in rows
        ]
        ctx = _base_context("symbols")
        ctx.update({"request": request, "symbols": symbols_data})
        return templates.TemplateResponse("symbols.html", ctx)
    finally:
        db.close()


@app.get("/trades", response_class=HTMLResponse)
async def trades(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        open_rows = db.query(Trade).filter(Trade.status == "open").order_by(Trade.entry_time.desc()).all()
        closed_rows = db.query(Trade).filter(Trade.status == "closed").order_by(Trade.exit_time.desc()).limit(30).all()

        from app.services.binance_public import get_prices

        prices = get_prices([t.symbol for t in open_rows]) if open_rows else {}
        open_data = []
        now = datetime.utcnow()
        for t in open_rows:
            cur = prices.get(t.symbol, t.entry_price)
            pnl_pct = ((cur - t.entry_price) / t.entry_price) * 100
            open_data.append(
                {
                    "id": t.id,
                    "symbol": t.symbol,
                    "entry": f"{t.entry_price:.6f}",
                    "entry_usdt": f"{(t.entry_price * t.quantity):.2f} USDT",
                    "current": f"{cur:.6f}",
                    "pnl_pct": pnl_pct,
                    "tp": f"{t.tp_price:.6f}",
                    "sl": f"{t.sl_price:.6f}",
                    "age": _format_age(now - t.entry_time),
                }
            )

        closed_data = [
            {
                "symbol": t.symbol,
                "entry_time": _as_local(t.entry_time).strftime("%Y-%m-%d %H:%M"),
                "exit_time": _as_local(t.exit_time).strftime("%Y-%m-%d %H:%M") if t.exit_time else "-",
                "entry_usdt": f"{(t.entry_price * t.quantity):.2f} USDT",
                "pnl_pct": ((t.exit_price - t.entry_price) / t.entry_price * 100) if t.exit_price else 0,
                "exit_reason": t.exit_reason or "-",
            }
            for t in closed_rows
        ]

        ctx = _base_context("trades")
        ctx.update({"request": request, "open_trades": open_data, "closed_trades": closed_data})
        return templates.TemplateResponse("trades.html", ctx)
    finally:
        db.close()


@app.get("/trades/close/{trade_id}")
async def close_trade_manually(trade_id: int) -> RedirectResponse:
    db = SessionLocal()
    try:
        trade = db.query(Trade).filter(Trade.id == trade_id, Trade.status == "open").first()
        if not trade:
            return RedirectResponse("/trades", status_code=303)

        from app.services.binance_public import get_prices

        prices = get_prices([trade.symbol])
        exit_price = float(prices.get(trade.symbol, trade.entry_price))

        fee_row = db.query(Setting).filter(Setting.key == "fee_rate").first()
        fee_rate = float(fee_row.value) if fee_row and fee_row.value else settings.fee_rate

        cash_row = db.query(Setting).filter(Setting.key == "paper_cash_balance").first()
        current_cash = float(cash_row.value) if cash_row and cash_row.value else settings.paper_start_balance

        proceeds = trade.quantity * exit_price
        exit_fee = proceeds * fee_rate
        entry_fee = (trade.entry_price * trade.quantity) * fee_rate
        pnl_value = (exit_price - trade.entry_price) * trade.quantity - entry_fee - exit_fee

        trade.status = "closed"
        trade.exit_price = exit_price
        trade.exit_time = datetime.utcnow()
        trade.pnl = pnl_value
        trade.exit_reason = "Manual Close"

        new_cash = current_cash + proceeds - exit_fee
        if cash_row:
            cash_row.value = f"{new_cash:.8f}"
        else:
            db.add(Setting(key="paper_cash_balance", value=f"{new_cash:.8f}"))

        db.add(
            LogEntry(
                event_type="TRADE",
                symbol=trade.symbol,
                message=f"Paper trade closed manually exit={exit_price:.6f} pnl_usdt={pnl_value:+.4f}",
            )
        )
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/trades", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        settings_map = {s.key: s.value for s in db.query(Setting).all()}
        ctx = _base_context("settings")
        ctx.update(
            {
                "request": request,
                "settings": {
                    "max_symbols_scanned": int(float(settings_map.get("max_symbols", settings.max_symbols))),
                    "max_open_trades": int(float(settings_map.get("max_open_trades", settings.max_open_trades))),
                    "minimum_volume": f"{float(settings_map.get('min_quote_volume', settings.min_quote_volume)):,.0f}",
                    "maximum_spread": f"{float(settings_map.get('max_spread_pct', settings.max_spread_pct)):.2f}%",
                    "take_profit": f"{float(settings_map.get('take_profit_pct', settings.take_profit_pct)) * 100:.2f}%",
                    "stop_loss": f"{float(settings_map.get('stop_loss_pct', settings.stop_loss_pct)) * 100:.2f}%",
                    "cooldown_minutes": int(float(settings_map.get("cooldown_minutes", settings.cooldown_minutes))),
                    "daily_loss_limit_pct": f"{float(settings_map.get('daily_loss_limit_pct', settings.daily_loss_limit_pct)):.2f}%",
                    "btc_filter_enabled": settings_map.get("btc_filter_enabled", "true").lower() == "true",
                    "time_stop_minutes": int(float(settings_map.get("time_stop_minutes", settings.time_stop_minutes))),
                    "max_trades_per_day": int(float(settings_map.get("max_trades_per_day", 10))),
                    "risk_per_trade_pct": f"{float(settings_map.get('risk_per_trade_pct', 1.0)):.2f}%",
                    "max_entry_usdt": f"{float(settings_map.get('max_entry_usdt', 0.0)):,.2f}",
                    "trailing_stop_pct": f"{float(settings_map.get('trailing_stop_pct', 0.01)) * 100:.2f}%",
                    "slippage_enabled": settings_map.get("slippage_enabled", "false").lower() == "true",
                    "slippage_bps": f"{float(settings_map.get('slippage_bps', 8.0)):.2f}",
                    "trading_mode": settings_map.get("trading_mode", "paper").capitalize(),
                    "bot_paused": settings_map.get("bot_paused", "false").lower() == "true",
                    "telegram_enabled": settings_map.get("telegram_enabled", "false").lower() == "true",
                    "telegram_chat_id": settings_map.get("telegram_chat_id", ""),
                    "telegram_token_set": bool(settings_map.get("telegram_bot_token", "").strip()),
                },
            }
        )
        return templates.TemplateResponse("settings.html", ctx)
    finally:
        db.close()


@app.get("/strategy", response_class=HTMLResponse)
async def strategy_page(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        m = {s.key: s.value for s in db.query(Setting).all()}
        ctx = _base_context("strategy")
        ctx.update(
            {
                "request": request,
                "strategy": {
                    "use_score_system": m.get("strategy_use_score_system", "true").lower() == "true",
                    "score_threshold": int(float(m.get("strategy_score_threshold", "3"))),
                    "trend_enabled": m.get("strategy_trend_enabled", "true").lower() == "true",
                    "pullback_enabled": m.get("strategy_pullback_enabled", "true").lower() == "true",
                    "rsi_enabled": m.get("strategy_rsi_enabled", "true").lower() == "true",
                    "volume_spike_enabled": m.get("strategy_volume_spike_enabled", "true").lower() == "true",
                    "resistance_enabled": m.get("strategy_resistance_enabled", "true").lower() == "true",
                    "price_above_ema50_enabled": m.get("strategy_price_above_ema50_enabled", "false").lower() == "true",
                    "pullback_max_dist_pct": f"{float(m.get('strategy_pullback_max_dist_pct', '1.0')):.2f}",
                    "rsi_min": f"{float(m.get('strategy_rsi_min', '35')):.2f}",
                    "rsi_max": f"{float(m.get('strategy_rsi_max', '65')):.2f}",
                    "volume_spike_multiplier": f"{float(m.get('strategy_volume_spike_multiplier', '1.3')):.2f}",
                    "resistance_min_dist_pct": f"{float(m.get('strategy_resistance_min_dist_pct', '1.5')):.2f}",
                    "momentum_volume_enabled": m.get("momentum_volume_enabled", "true").lower() == "true",
                    "momentum_volatility_enabled": m.get("momentum_volatility_enabled", "true").lower() == "true",
                    "momentum_relative_strength_enabled": m.get("momentum_relative_strength_enabled", "true").lower() == "true",
                    "momentum_price_above_ema200_1h_enabled": m.get("momentum_price_above_ema200_1h_enabled", "true").lower() == "true",
                },
            }
        )
        return templates.TemplateResponse("strategy.html", ctx)
    finally:
        db.close()


@app.post("/strategy/save")
async def strategy_save(request: Request) -> RedirectResponse:
    def _to_int(value: str, default: int, min_value: int) -> int:
        try:
            return max(min_value, int(float(value)))
        except (TypeError, ValueError):
            return default

    def _to_float(value: str, default: float, min_value: float) -> float:
        try:
            cleaned = str(value).replace(",", "").replace("%", "")
            return max(min_value, float(cleaned))
        except (TypeError, ValueError):
            return default

    form = await request.form()
    to_bool = lambda key: str(form.get(key, "off")).lower() in {"on", "true", "1", "yes"}
    mapping = {
        "strategy_use_score_system": "true" if to_bool("use_score_system") else "false",
        "strategy_score_threshold": str(_to_int(str(form.get("score_threshold", "3")), 3, 1)),
        "strategy_trend_enabled": "true" if to_bool("trend_enabled") else "false",
        "strategy_pullback_enabled": "true" if to_bool("pullback_enabled") else "false",
        "strategy_rsi_enabled": "true" if to_bool("rsi_enabled") else "false",
        "strategy_volume_spike_enabled": "true" if to_bool("volume_spike_enabled") else "false",
        "strategy_resistance_enabled": "true" if to_bool("resistance_enabled") else "false",
        "strategy_price_above_ema50_enabled": "true" if to_bool("price_above_ema50_enabled") else "false",
        "strategy_pullback_max_dist_pct": str(_to_float(str(form.get("pullback_max_dist_pct", "1.0")), 1.0, 0.1)),
        "strategy_rsi_min": str(_to_float(str(form.get("rsi_min", "35")), 35.0, 0.0)),
        "strategy_rsi_max": str(_to_float(str(form.get("rsi_max", "65")), 65.0, 0.0)),
        "strategy_volume_spike_multiplier": str(_to_float(str(form.get("volume_spike_multiplier", "1.3")), 1.3, 0.1)),
        "strategy_resistance_min_dist_pct": str(_to_float(str(form.get("resistance_min_dist_pct", "1.5")), 1.5, 0.1)),
        "momentum_volume_enabled": "true" if to_bool("momentum_volume_enabled") else "false",
        "momentum_volatility_enabled": "true" if to_bool("momentum_volatility_enabled") else "false",
        "momentum_relative_strength_enabled": "true" if to_bool("momentum_relative_strength_enabled") else "false",
        "momentum_price_above_ema200_1h_enabled": "true" if to_bool("momentum_price_above_ema200_1h_enabled") else "false",
    }

    db = SessionLocal()
    try:
        for key, value in mapping.items():
            row = db.query(Setting).filter(Setting.key == key).first()
            if row:
                row.value = value
            else:
                db.add(Setting(key=key, value=value))
        db.add(LogEntry(event_type="STRATEGY", symbol="-", message="Strategy rules updated from dashboard"))
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/strategy", status_code=303)


@app.post("/settings/save")
async def save_settings(request: Request) -> RedirectResponse:
    def _to_int(value: str, default: int, min_value: int) -> int:
        try:
            return max(min_value, int(float(value)))
        except (TypeError, ValueError):
            return default

    def _to_float(value: str, default: float, min_value: float) -> float:
        try:
            cleaned = str(value).replace(",", "").replace("%", "")
            return max(min_value, float(cleaned))
        except (TypeError, ValueError):
            return default

    form = await request.form()
    db = SessionLocal()
    try:
        max_symbols = _to_int(str(form.get("max_symbols_scanned", settings.max_symbols)), settings.max_symbols, 1)
        max_open = _to_int(str(form.get("max_open_trades", settings.max_open_trades)), settings.max_open_trades, 1)
        min_volume = _to_float(str(form.get("minimum_volume", settings.min_quote_volume)), settings.min_quote_volume, 1.0)
        max_spread = _to_float(str(form.get("maximum_spread", settings.max_spread_pct)), settings.max_spread_pct, 0.01)
        tp_pct = _to_float(str(form.get("take_profit", settings.take_profit_pct * 100)), settings.take_profit_pct * 100, 0.1) / 100
        sl_pct = _to_float(str(form.get("stop_loss", settings.stop_loss_pct * 100)), settings.stop_loss_pct * 100, 0.1) / 100
        cooldown_minutes = _to_int(str(form.get("cooldown_minutes", settings.cooldown_minutes)), settings.cooldown_minutes, 1)
        daily_loss_limit_pct = _to_float(str(form.get("daily_loss_limit_pct", settings.daily_loss_limit_pct)), settings.daily_loss_limit_pct, 0.1)
        time_stop_minutes = _to_int(str(form.get("time_stop_minutes", settings.time_stop_minutes)), settings.time_stop_minutes, 1)
        max_trades_per_day = _to_int(str(form.get("max_trades_per_day", "10")), 10, 1)
        risk_per_trade_pct = _to_float(str(form.get("risk_per_trade_pct", "1.0")), 1.0, 0.1)
        max_entry_usdt = _to_float(str(form.get("max_entry_usdt", "0")), 0.0, 0.0)
        trailing_stop_pct = _to_float(str(form.get("trailing_stop_pct", "1.0")), 1.0, 0.1) / 100
        slippage_enabled = str(form.get("slippage_enabled", "off")).lower() in {"on", "true", "1", "yes"}
        slippage_bps = _to_float(str(form.get("slippage_bps", "8")), 8.0, 0.0)
        btc_filter_enabled = str(form.get("btc_filter_enabled", "off")).lower() in {"on", "true", "1", "yes"}
        telegram_enabled = str(form.get("telegram_enabled", "off")).lower() in {"on", "true", "1", "yes"}
        telegram_chat_id = str(form.get("telegram_chat_id", "")).strip()
        telegram_bot_token = str(form.get("telegram_bot_token", "")).strip()
        mapping = {
            "max_symbols": str(max_symbols),
            "max_open_trades": str(max_open),
            "min_quote_volume": str(min_volume),
            "max_spread_pct": str(max_spread),
            "take_profit_pct": str(tp_pct),
            "stop_loss_pct": str(sl_pct),
            "cooldown_minutes": str(cooldown_minutes),
            "daily_loss_limit_pct": str(daily_loss_limit_pct),
            "time_stop_minutes": str(time_stop_minutes),
            "max_trades_per_day": str(max_trades_per_day),
            "risk_per_trade_pct": str(risk_per_trade_pct),
            "max_entry_usdt": str(max_entry_usdt),
            "trailing_stop_pct": str(trailing_stop_pct),
            "slippage_enabled": "true" if slippage_enabled else "false",
            "slippage_bps": str(slippage_bps),
            "btc_filter_enabled": "true" if btc_filter_enabled else "false",
            "trading_mode": "paper" if str(form.get("trading_mode", "paper")).lower() != "live" else "live",
            "telegram_enabled": "true" if telegram_enabled else "false",
            "telegram_chat_id": telegram_chat_id,
        }
        def _apply_changes() -> None:
            for key, value in mapping.items():
                row = db.query(Setting).filter(Setting.key == key).first()
                if row:
                    row.value = value
                else:
                    db.add(Setting(key=key, value=value))
            if telegram_bot_token:
                row = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
                if row:
                    row.value = telegram_bot_token
                else:
                    db.add(Setting(key="telegram_bot_token", value=telegram_bot_token))
            db.add(LogEntry(event_type="SETTINGS", symbol="-", message="Settings updated from dashboard"))

        for _ in range(4):
            try:
                _apply_changes()
                db.commit()
                break
            except OperationalError:
                db.rollback()
                time.sleep(0.25)
        else:
            db.add(LogEntry(event_type="ERROR", symbol="-", message="Settings save failed due to database lock"))
            db.commit()
    finally:
        db.close()
    return RedirectResponse("/settings", status_code=303)


@app.get("/settings/pause")
async def pause_bot() -> RedirectResponse:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "bot_paused").first()
        if row:
            row.value = "true"
            db.commit()
    finally:
        db.close()
    return RedirectResponse("/settings", status_code=303)


@app.get("/settings/resume")
async def resume_bot() -> RedirectResponse:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "bot_paused").first()
        if row:
            row.value = "false"
            db.commit()
    finally:
        db.close()
    return RedirectResponse("/settings", status_code=303)


@app.get("/settings/run-now")
async def run_now() -> RedirectResponse:
    _scheduled_cycle()
    return RedirectResponse("/logs", status_code=303)


@app.get("/settings/reset-paper")
async def reset_paper_account() -> RedirectResponse:
    db = SessionLocal()
    try:
        db.query(Trade).delete()
        db.query(SymbolSnapshot).delete()
        updates = {
            "paper_cash_balance": str(settings.paper_start_balance),
            "daily_start_equity": str(settings.paper_start_balance),
            "daily_anchor_date": "",
            "bot_paused": "false",
            "trading_mode": "paper",
        }
        for key, value in updates.items():
            row = db.query(Setting).filter(Setting.key == key).first()
            if row:
                row.value = value
            else:
                db.add(Setting(key=key, value=value))
        db.add(LogEntry(event_type="RESET", symbol="-", message=f"Paper account reset to {settings.paper_start_balance:.2f} USDT"))
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/overview", status_code=303)


@app.get("/settings/test-telegram")
async def test_telegram() -> RedirectResponse:
    db = SessionLocal()
    try:
        settings_map = {s.key: s.value for s in db.query(Setting).all()}
        enabled = settings_map.get("telegram_enabled", "false").lower() == "true"
        token = settings_map.get("telegram_bot_token", "")
        chat_id = settings_map.get("telegram_chat_id", "")
        ok, reason = telegram_test(token=token, chat_id=chat_id, text="Test message from Crypto Bot dashboard") if enabled else (False, "Telegram is disabled")
        db.add(
            LogEntry(
                event_type="TELEGRAM",
                symbol="-",
                message="Telegram test sent successfully" if ok else f"Telegram test failed: {reason}",
            )
        )
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/settings", status_code=303)


@app.get("/logs", response_class=HTMLResponse)
async def logs(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        rows = db.query(LogEntry).order_by(desc(LogEntry.timestamp)).limit(80).all()
        logs_data = [
            {
                "time": _as_local(r.timestamp).strftime("%H:%M:%S"),
                "event": r.event_type,
                "symbol": r.symbol or "-",
                "message": r.message,
            }
            for r in rows
        ]
        ctx = _base_context("logs")
        ctx.update({"request": request, "logs": logs_data})
        return templates.TemplateResponse("logs.html", ctx)
    finally:
        db.close()


@app.get("/statistics", response_class=HTMLResponse)
async def statistics_page(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        stats = statistics_snapshot(db)
        ctx = _base_context("statistics")
        ctx.update({"request": request, "stats": stats})
        return templates.TemplateResponse("statistics.html", ctx)
    finally:
        db.close()


@app.get("/advisor", response_class=HTMLResponse)
async def advisor_page(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        snap = portfolio_snapshot(db)
        stats = statistics_snapshot(db)
        symbol_rows = db.query(SymbolSnapshot).order_by(SymbolSnapshot.volume_24h.desc()).limit(30).all()
        total_observations = db.query(MarketObservation).count()
        shadow_open = db.query(ShadowTrade).filter(ShadowTrade.status == "open").count()
        shadow_closed = db.query(ShadowTrade).filter(ShadowTrade.status == "closed").count()
        shadow_pnl = db.query(ShadowTrade).filter(ShadowTrade.status == "closed", ShadowTrade.pnl.isnot(None)).all()
        shadow_net = sum(float(t.pnl or 0.0) for t in shadow_pnl)
        entry_logs = db.query(LogEntry).filter(LogEntry.event_type == "ENTRY_DECISION").order_by(desc(LogEntry.id)).limit(400).all()
        latest_by_symbol: dict[str, str] = {}
        reason_counter: Counter[str] = Counter()
        for log in entry_logs:
            message = log.message or ""
            reason = _extract_log_field(message, "reason") or "unknown"
            reason_counter[reason] += 1
            sym = (log.symbol or "").strip()
            if sym and sym != "-" and sym not in latest_by_symbol:
                latest_by_symbol[sym] = message

        global_notes: list[str] = []
        if stats["total_trades"] < 15:
            global_notes.append("Sample size is still small (<15 closed trades). Keep collecting paper data before major parameter changes.")
        if stats["profit_factor"] < 1.0 and stats["total_trades"] > 0:
            global_notes.append("Profit factor is below 1.0. Strategy quality is not stable yet.")
        if stats["max_drawdown_pct"] > 5:
            global_notes.append("Drawdown is elevated. Consider reducing risk per trade or max open trades.")
        if snap["open_positions"] == 0:
            global_notes.append("No open positions now. Check latest ENTRY_DECISION reasons for what is blocking entries.")
        if reason_counter:
            top_reason, count = reason_counter.most_common(1)[0]
            global_notes.append(f"Most frequent block reason recently: {top_reason} ({count} times).")
        if not global_notes:
            global_notes.append("System behavior is stable. Keep monitoring and compare weekly performance before tuning.")

        suggestions = []
        for r in symbol_rows:
            msg = latest_by_symbol.get(r.symbol, "")
            score = _extract_log_field(msg, "score") or "-"
            reason = _extract_log_field(msg, "reason") or "-"
            suggestions.append(
                {
                    "symbol": r.symbol,
                    "signal": r.signal_status,
                    "trend": r.trend_status,
                    "score": score,
                    "reason": reason,
                    "recommendation": _entry_recommendation(r.signal_status, reason, score),
                }
            )

        ctx = _base_context("advisor")
        ctx.update(
            {
                "request": request,
                "global_notes": global_notes,
                "suggestions": suggestions,
                "summary": {
                    "balance": f"{snap['balance']:.2f} USDT",
                    "win_rate": f"{stats['win_rate']:.2f}%",
                    "profit_factor": f"{stats['profit_factor']:.3f}",
                    "total_trades": stats["total_trades"],
                    "observations": total_observations,
                    "shadow_open": shadow_open,
                    "shadow_closed": shadow_closed,
                    "shadow_net": f"{shadow_net:+.2f} USDT",
                },
            }
        )
        return templates.TemplateResponse("advisor.html", ctx)
    finally:
        db.close()


def _ai_provider_dashboard(db: SessionLocal, provider: str) -> dict:
    p = provider.lower().strip()
    settings_map = {s.key: s.value for s in db.query(Setting).all()}
    enabled = settings_map.get(f"ai_{p}_enabled", "true").lower() == "true"
    balance = float(settings_map.get(f"ai_{p}_balance_usdt", "500"))
    open_rows = (
        db.query(AITrade)
        .filter(AITrade.status == "open", AITrade.ai_provider == p)
        .order_by(AITrade.entry_time.desc())
        .all()
    )
    closed_rows = (
        db.query(AITrade)
        .filter(AITrade.status == "closed", AITrade.ai_provider == p)
        .order_by(AITrade.exit_time.desc())
        .limit(120)
        .all()
    )

    total_closed = len(closed_rows)
    wins = [t for t in closed_rows if (t.pnl or 0.0) > 0]
    net_pnl = sum(float(t.pnl or 0.0) for t in closed_rows)
    win_rate = (len(wins) / total_closed * 100) if total_closed else 0.0
    from app.services.binance_public import get_prices
    prices = get_prices([t.symbol for t in open_rows]) if open_rows else {}

    open_data = [
        {
            "symbol": t.symbol,
            "entry_time": _as_local(t.entry_time).strftime("%Y-%m-%d %H:%M"),
            "entry_price": f"{t.entry_price:.6f}",
            "current_price": f"{float(prices.get(t.symbol, t.entry_price)):.6f}",
            "notional": f"{t.notional_usdt:.2f} USDT",
            "pnl_pct": ((float(prices.get(t.symbol, t.entry_price)) - t.entry_price) / t.entry_price * 100) if t.entry_price else 0.0,
            "pnl_usdt": ((float(prices.get(t.symbol, t.entry_price)) - t.entry_price) * t.quantity) if t.quantity else 0.0,
            "tp_price": f"{float(t.tp_price or 0.0):.6f}",
            "sl_price": f"{float(t.sl_price or 0.0):.6f}",
            "trailing_active": bool(t.trailing_active),
            "trailing_stop_price": f"{float(t.trailing_stop_price or 0.0):.6f}" if t.trailing_stop_price else "-",
            "strategy_id": t.strategy_id,
        }
        for t in open_rows
    ]
    closed_data = [
        {
            "symbol": t.symbol,
            "entry_time": _as_local(t.entry_time).strftime("%Y-%m-%d %H:%M"),
            "exit_time": _as_local(t.exit_time).strftime("%Y-%m-%d %H:%M") if t.exit_time else "-",
            "entry_price": f"{t.entry_price:.6f}",
            "exit_price": f"{float(t.exit_price or 0.0):.6f}",
            "pnl_pct": float(t.pnl_pct or 0.0),
            "pnl_usdt": f"{float(t.pnl or 0.0):+.4f}",
            "exit_reason": t.exit_reason or "-",
            "strategy_id": t.strategy_id,
        }
        for t in closed_rows
    ]

    by_strategy: dict[str, dict] = {}
    for t in closed_rows:
        sid = t.strategy_id or "-"
        row = by_strategy.setdefault(sid, {"count": 0, "wins": 0, "net": 0.0})
        row["count"] += 1
        pnl_v = float(t.pnl or 0.0)
        row["net"] += pnl_v
        if pnl_v > 0:
            row["wins"] += 1
    recommendations = []
    for sid, row in by_strategy.items():
        if row["count"] < 3:
            continue
        wr = (row["wins"] / row["count"]) * 100
        verdict = "Consider testing in main strategy" if row["net"] > 0 and wr >= 55 else "Keep in AI lab only"
        recommendations.append(
            {
                "strategy_id": sid,
                "trades": row["count"],
                "win_rate": f"{wr:.1f}%",
                "net_pnl": f"{row['net']:+.4f}",
                "verdict": verdict,
            }
        )
    recommendations.sort(key=lambda x: float(x["net_pnl"]), reverse=True)
    cfg = {
        "max_trades_per_day": settings_map.get(f"ai_{p}_max_trades_per_day", "20"),
        "daily_loss_limit_pct": settings_map.get(f"ai_{p}_daily_loss_limit_pct", "3.0"),
        "max_open": settings_map.get(f"ai_{p}_max_open", "10"),
        "entry_usdt": settings_map.get(f"ai_{p}_entry_usdt", "30"),
        "trials_per_cycle": settings_map.get(f"ai_{p}_trials_per_cycle", "20"),
        "scan_symbols": settings_map.get(f"ai_{p}_scan_symbols", "80"),
        "min_quote_volume": settings_map.get(f"ai_{p}_min_quote_volume", "10000000"),
        "max_spread_pct": settings_map.get(f"ai_{p}_max_spread_pct", "0.20"),
        "max_risk_per_trade_pct": settings_map.get(f"ai_{p}_max_risk_per_trade_pct", "1.0"),
    }

    usage_rows = db.query(AIProviderUsage).filter(AIProviderUsage.ai_provider == p).all()
    daily_start = _local_day_start_utc_naive()
    usage_daily = [u for u in usage_rows if u.created_at >= daily_start]
    daily_tokens = sum(int(u.total_tokens or 0) for u in usage_daily)
    daily_cost = sum(float(u.estimated_cost_usd or 0.0) for u in usage_daily)
    total_tokens = sum(int(u.total_tokens or 0) for u in usage_rows)
    total_cost = sum(float(u.estimated_cost_usd or 0.0) for u in usage_rows)

    brain = {
        "focus": "Monitoring momentum candidates and risk-adjusted entries.",
        "entry_logic": "Trend + score-based confirmation from model-generated config.",
        "exit_logic": "SL/TP/Trailing/Time Stop driven by active strategy config.",
        "active_strategies": list({t.strategy_id for t in open_rows if t.strategy_id})[:4],
        "recent_activity": [],
    }
    recent_logs = db.query(LogEntry).order_by(desc(LogEntry.id)).limit(300).all()
    provider_logs = []
    for lg in recent_logs:
        msg = (lg.message or "").lower()
        if msg.startswith(f"{p} ") or f"{p} scan" in msg or f"{p} opened" in msg or f"{p} closed" in msg:
            provider_logs.append(lg)
        if len(provider_logs) >= 8:
            break
    if provider_logs:
        brain["recent_activity"] = [
            f"{_as_local(l.timestamp).strftime('%H:%M:%S')} | {l.event_type} | {l.message}" for l in provider_logs
        ]

    memory_rows = (
        db.query(AIAgentMemory)
        .filter(AIAgentMemory.ai_provider == p)
        .order_by(desc(AIAgentMemory.id))
        .limit(20)
        .all()
    )
    memory_rows.reverse()
    memory_feed = []
    for m in memory_rows:
        memory_feed.append(
            {
                "time": _as_local(m.created_at).strftime("%H:%M:%S"),
                "type": m.memory_type,
                "content": m.content,
            }
        )
    brain["memory_feed"] = memory_feed

    latest_plan = next((m for m in reversed(memory_rows) if m.memory_type == "plan"), None)
    if latest_plan:
        try:
            plan_json = json.loads(latest_plan.content)
            nxt = plan_json.get("next_action")
            if nxt:
                brain["focus"] = f"{brain['focus']} Next: {nxt}"
        except Exception:
            pass

    sample_trade = open_rows[0] if open_rows else (closed_rows[0] if closed_rows else None)
    if sample_trade and sample_trade.strategy_json:
        try:
            cfg = json.loads(sample_trade.strategy_json)
            brain["entry_logic"] = (
                f"score>={cfg.get('score_threshold', 3)}, RSI {cfg.get('rsi_min', 35)}-{cfg.get('rsi_max', 65)}, "
                f"volSpike x{cfg.get('volume_spike_multiplier', 1.3)}, resistance>={cfg.get('resistance_min_dist_pct', 1.5)}%"
            )
            brain["exit_logic"] = (
                f"TP {float(cfg.get('tp_pct', 0.02))*100:.2f}% | SL {float(cfg.get('sl_pct', 0.012))*100:.2f}% | "
                f"Trailing {float(cfg.get('trailing_stop_pct', 0.008))*100:.2f}% | TimeStop {cfg.get('time_stop_minutes', 120)}m"
            )
        except Exception:
            pass

    chat_rows = (
        db.query(AIChatMessage)
        .filter(AIChatMessage.ai_provider == p)
        .order_by(AIChatMessage.id.desc())
        .limit(80)
        .all()
    )
    chat_rows.reverse()
    chat_history = [
        {
            "role": r.role,
            "message": r.message,
            "time": _as_local(r.created_at).strftime("%H:%M"),
        }
        for r in chat_rows
    ]

    api_connected = _provider_api_connected(p)
    return {
        "provider": p,
        "summary": {
            "enabled": enabled,
            "api_connected": api_connected,
            "balance": f"{balance:.2f} USDT",
            "open_count": len(open_rows),
            "closed_count": total_closed,
            "win_rate": f"{win_rate:.2f}%",
            "net_pnl": f"{net_pnl:+.4f} USDT",
            "daily_tokens": f"{daily_tokens:,}",
            "daily_cost": f"${daily_cost:.4f}",
            "total_tokens": f"{total_tokens:,}",
            "total_cost": f"${total_cost:.4f}",
        },
        "open_trades": open_data,
        "closed_trades": closed_data,
        "recommendations": recommendations[:12],
        "brain": brain,
        "chat_history": chat_history,
        "config": cfg,
    }


@app.get("/ai-trading", response_class=HTMLResponse)
async def ai_trading_classic(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        data = _ai_provider_dashboard(db, "classic")
        ctx = _base_context("ai_trading")
        ctx.update(
            {
                "request": request,
                "summary": data["summary"],
                "open_trades": data["open_trades"],
                "closed_trades": data["closed_trades"],
                "recommendations": data["recommendations"],
            }
        )
        return templates.TemplateResponse("ai_trading.html", ctx)
    finally:
        db.close()


@app.get("/ai-hub", response_class=HTMLResponse)
async def ai_trading_hub(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        providers = []
        for p in ("openai", "claude", "deepseek"):
            data = _ai_provider_dashboard(db, p)
            providers.append(
                {
                    "id": p,
                    "title": p.capitalize(),
                    "enabled": data["summary"]["enabled"],
                    "api_connected": data["summary"]["api_connected"],
                    "balance": data["summary"]["balance"],
                    "open_count": data["summary"]["open_count"],
                    "closed_count": data["summary"]["closed_count"],
                    "win_rate": data["summary"]["win_rate"],
                    "net_pnl": data["summary"]["net_pnl"],
                }
            )
        ctx = _base_context("ai_trading")
        ctx.update({"request": request, "providers": providers})
        return templates.TemplateResponse("ai_trading_hub.html", ctx)
    finally:
        db.close()


@app.get("/ai-trading/{provider}", response_class=HTMLResponse)
async def ai_trading_provider(request: Request, provider: str) -> HTMLResponse:
    p = provider.lower().strip()
    if p not in {"openai", "claude", "deepseek"}:
        return RedirectResponse("/ai-hub", status_code=303)
    db = SessionLocal()
    try:
        data = _ai_provider_dashboard(db, p)
        ctx = _base_context("ai_trading")
        ctx.update(
            {
                "request": request,
                "provider": p,
                "summary": data["summary"],
                "open_trades": data["open_trades"],
                "closed_trades": data["closed_trades"],
                "recommendations": data["recommendations"],
                "brain": data["brain"],
                "chat_history": data["chat_history"],
            }
        )
        return templates.TemplateResponse("ai_trading_provider.html", ctx)
    finally:
        db.close()


@app.post("/ai-trading/{provider}/chat")
async def ai_trading_provider_chat(request: Request, provider: str) -> RedirectResponse:
    p = provider.lower().strip()
    if p not in {"openai", "claude", "deepseek"}:
        return RedirectResponse("/ai-hub", status_code=303)

    form = await request.form()
    user_msg = str(form.get("message", "")).strip()
    if not user_msg:
        return RedirectResponse(f"/ai-trading/{p}", status_code=303)

    db = SessionLocal()
    try:
        user_msg = user_msg[:2000]
        db.add(AIChatMessage(ai_provider=p, role="user", message=user_msg))
        db.flush()

        data = _ai_provider_dashboard(db, p)
        system_prompt = (
            f"You are the trading agent for provider={p}. "
            "Be concise, practical, and transparent. "
            "Always explain what you are currently doing, your planned next action, "
            "entry logic, exit logic, and risk controls. Do not claim guaranteed profit.\n"
            "If the user asks to change configuration, append one JSON block at the END in this exact format:\n"
            "ACTION_JSON:{\"set\":{\"max_trades_per_day\":50,\"entry_usdt\":30}}\n"
            "Allowed keys in set: max_trades_per_day,daily_loss_limit_pct,max_open,entry_usdt,trials_per_cycle,scan_symbols,min_quote_volume,max_spread_pct,max_risk_per_trade_pct,enabled.\n"
            f"Current summary: {json.dumps(data['summary'], ensure_ascii=True)}\n"
            f"Current brain: {json.dumps(data['brain'], ensure_ascii=True)}\n"
            f"Current config: {json.dumps(data['config'], ensure_ascii=True)}"
        )

        history_rows = (
            db.query(AIChatMessage)
            .filter(AIChatMessage.ai_provider == p)
            .order_by(AIChatMessage.id.desc())
            .limit(20)
            .all()
        )
        history_rows.reverse()
        messages = [{"role": ("assistant" if h.role == "assistant" else "user"), "content": h.message} for h in history_rows]

        reply, usage = chat_with_provider_with_usage(p, system_prompt, messages, _provider_env_cfg())
        record_usage(db, p, "chat", usage)
        applied = _apply_ai_chat_actions(db, p, _extract_action_json(reply))
        if applied:
            reply = f"{reply}\n\nApplied settings: {', '.join(applied)}"
        db.add(AIChatMessage(ai_provider=p, role="assistant", message=reply[:4000]))
        db.commit()
    finally:
        db.close()

    return RedirectResponse(f"/ai-trading/{p}", status_code=303)

