import time
import threading
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
from app.models import LogEntry, Setting, SymbolSnapshot, Trade
from app.services.paper_engine import init_defaults, portfolio_snapshot, run_cycle, statistics_snapshot
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
        symbols_data = [
            {
                "symbol": r.symbol,
                "volume_24h": f"{r.volume_24h:,.0f}",
                "spread": f"{r.spread_pct:.3f}%",
                "trend": r.trend_status,
                "signal": r.signal_status,
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
                "entry_time": _as_local(t.entry_time).strftime("%H:%M"),
                "exit_time": _as_local(t.exit_time).strftime("%H:%M") if t.exit_time else "-",
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
