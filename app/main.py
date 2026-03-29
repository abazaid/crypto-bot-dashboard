import threading
import json
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy import desc
from sqlalchemy import or_

from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.models.paper_v2 import ActivityLog, AppSetting, Campaign, DcaRule, Position, PositionDcaState
from app.services.binance_public import get_prices, search_symbols
from app.services.paper_trading import (
    build_ai_dca_rules,
    create_campaign_positions,
    ensure_defaults,
    recalculate_campaign_dca,
    run_cycle,
    suggest_top_symbols,
    wallet_snapshot,
)
from app.services.live_trading import (
    create_live_campaign_positions,
    live_wallet_snapshot,
    recalculate_live_campaign_dca,
    run_live_cycle,
)

app = FastAPI(title="Crypto Bots - Rebuild")
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
templates = Jinja2Templates(directory="app/web/templates")

scheduler = BackgroundScheduler(timezone="UTC")
cycle_lock = threading.Lock()
live_cycle_lock = threading.Lock()


def _context(active: str, **kwargs) -> dict:
    base = {
        "active_page": active,
        "now": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "cycle_seconds": max(settings.cycle_seconds, 3),
    }
    base.update(kwargs)
    return base


def _campaign_stats(db, campaign: Campaign) -> dict:
    open_positions = db.query(Position).filter(Position.campaign_id == campaign.id, Position.status == "open").all()
    closed_positions = db.query(Position).filter(Position.campaign_id == campaign.id, Position.status == "closed").all()

    symbols = sorted({p.symbol for p in open_positions})
    prices = get_prices(symbols) if symbols else {}
    unrealized = sum((float(prices.get(p.symbol, p.average_price)) * p.total_qty) - p.total_invested_usdt for p in open_positions)
    realized = sum(float(p.realized_pnl_usdt or 0.0) for p in closed_positions)
    dca_done_count = (
        db.query(PositionDcaState)
        .join(Position, Position.id == PositionDcaState.position_id)
        .filter(Position.campaign_id == campaign.id, PositionDcaState.executed == True)
        .count()
    )

    return {
        "open_count": len(open_positions),
        "closed_count": len(closed_positions),
        "dca_done_count": dca_done_count,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
    }


def _pnl_pct(invested: float, pnl: float) -> float:
    if invested <= 0:
        return 0.0
    return (pnl / invested) * 100.0


def _safe_float(value: str | None, default: float | None = None) -> float | None:
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(str(value).replace("%", "").strip())
    except Exception:
        return default


_DATE_FILTERS = [
    ("all", "Disable Filter", None),
    ("24h", "Last 24 hours", 24),
    ("3d", "Last 3 days", 24 * 3),
    ("7d", "Last 7 days", 24 * 7),
    ("14d", "Last 14 days", 24 * 14),
    ("30d", "Last 30 days", 24 * 30),
    ("60d", "Last 60 days", 24 * 60),
]

_STRATEGY_FILTERS = [
    ("all", "Disable Filter"),
    ("loop_ai", "Loop AI"),
    ("smart_ai", "Smart trade AI"),
    ("manual", "Manual"),
]


def _strategy_key(campaign: Campaign) -> str:
    if bool(campaign.loop_enabled):
        return "loop_ai"
    if bool(campaign.ai_dca_enabled):
        return "smart_ai"
    return "manual"


def _match_date_filter(row: dict, date_key: str, now_dt: datetime) -> bool:
    for key, _, hours in _DATE_FILTERS:
        if date_key != key:
            continue
        if hours is None:
            return True
        closed_at = row.get("closed_at")
        if not isinstance(closed_at, datetime):
            return False
        return closed_at >= (now_dt - timedelta(hours=hours))
    return True


def _history_context(db, mode: str, date_filter: str, strategy_filter: str) -> dict:
    closed_positions = (
        db.query(Position)
        .join(Campaign, Campaign.id == Position.campaign_id)
        .filter(Position.status == "closed", Campaign.mode == mode)
        .order_by(desc(Position.closed_at), desc(Position.id))
        .all()
    )
    position_ids = [p.id for p in closed_positions]
    dca_total: dict[int, int] = {}
    dca_done: dict[int, int] = {}
    if position_ids:
        states = db.query(PositionDcaState).filter(PositionDcaState.position_id.in_(position_ids)).all()
        for st in states:
            pid = int(st.position_id)
            dca_total[pid] = int(dca_total.get(pid, 0)) + 1
            if bool(st.executed):
                dca_done[pid] = int(dca_done.get(pid, 0)) + 1

    base_rows = []
    now_dt = datetime.utcnow()
    for p in closed_positions:
        invested = float(p.total_invested_usdt or 0.0)
        pnl = float(p.realized_pnl_usdt or 0.0)
        base_rows.append(
            {
                "id": p.id,
                "campaign_name": p.campaign.name,
                "symbol": str(p.symbol or "").upper(),
                "opened_at": p.opened_at,
                "closed_at": p.closed_at,
                "invested": invested,
                "exit_value": float((p.close_price or 0.0) * (p.total_qty or 0.0)),
                "pnl": pnl,
                "pnl_pct": _pnl_pct(invested, pnl),
                "close_reason": p.close_reason or "-",
                "strategy_key": _strategy_key(p.campaign),
                "dca_done": int(dca_done.get(p.id, 0)),
                "dca_total": int(dca_total.get(p.id, 0)),
            }
        )

    # Filter option counts over full dataset.
    date_filters = []
    for key, label, _ in _DATE_FILTERS:
        cnt = sum(1 for r in base_rows if _match_date_filter(r, key, now_dt))
        date_filters.append({"key": key, "label": label, "count": cnt})
    strategy_filters = []
    for key, label in _STRATEGY_FILTERS:
        cnt = len(base_rows) if key == "all" else sum(1 for r in base_rows if r["strategy_key"] == key)
        strategy_filters.append({"key": key, "label": label, "count": cnt})

    # Apply selected filters.
    rows = [r for r in base_rows if _match_date_filter(r, date_filter, now_dt)]
    if strategy_filter != "all":
        rows = [r for r in rows if r["strategy_key"] == strategy_filter]

    symbol_totals: dict[str, float] = {}
    wins = 0
    net_pnl = 0.0
    for row in rows:
        pnl = float(row["pnl"])
        if pnl > 0:
            wins += 1
        net_pnl += pnl
        symbol_totals[row["symbol"]] = float(symbol_totals.get(row["symbol"], 0.0)) + pnl
    for row in rows:
        row["symbol_total_pnl"] = float(symbol_totals.get(row["symbol"], 0.0))

    total = len(rows)
    summary = {
        "total_trades": total,
        "wins": wins,
        "losses": max(0, total - wins),
        "win_rate": ((wins / total) * 100.0) if total else 0.0,
        "net_pnl": net_pnl,
    }
    return {
        "summary": summary,
        "rows": rows,
        "date_filters": date_filters,
        "strategy_filters": strategy_filters,
        "selected_date_filter": date_filter,
        "selected_strategy_filter": strategy_filter,
    }


def _dashboard_logs(db, mode: str, limit: int = 80) -> list[ActivityLog]:
    q = db.query(ActivityLog)
    if mode == "live":
        q = q.filter(or_(ActivityLog.event_type.like("LIVE_%"), ActivityLog.event_type == "SYSTEM"))
    else:
        q = q.filter(~ActivityLog.event_type.like("LIVE_%"))
    return q.order_by(desc(ActivityLog.id)).limit(limit).all()


def _sync_open_positions_dca_states(db, campaign_id: int) -> None:
    open_positions = db.query(Position).filter(Position.campaign_id == campaign_id, Position.status == "open").all()
    rules = db.query(DcaRule).filter(DcaRule.campaign_id == campaign_id).order_by(DcaRule.drop_pct.asc(), DcaRule.id.asc()).all()
    for pos in open_positions:
        old_states = (
            db.query(PositionDcaState)
            .join(DcaRule, DcaRule.id == PositionDcaState.dca_rule_id)
            .filter(PositionDcaState.position_id == pos.id)
            .all()
        )
        executed_by_rule_name = {}
        for st in old_states:
            rule_name = st.rule.name
            executed_by_rule_name[rule_name] = {
                "executed": bool(st.executed),
                "custom_drop_pct": st.custom_drop_pct,
                "custom_allocation_pct": st.custom_allocation_pct,
                "custom_support_score": st.custom_support_score,
                "executed_at": st.executed_at,
                "executed_price": st.executed_price,
                "executed_qty": st.executed_qty,
                "executed_usdt": st.executed_usdt,
            }
            db.delete(st)
        db.flush()

        for rule in rules:
            keep = executed_by_rule_name.get(rule.name)
            db.add(
                PositionDcaState(
                    position_id=pos.id,
                    dca_rule_id=rule.id,
                    executed=bool(keep and keep["executed"]),
                    custom_drop_pct=keep["custom_drop_pct"] if keep else None,
                    custom_allocation_pct=keep["custom_allocation_pct"] if keep else None,
                    custom_support_score=keep["custom_support_score"] if keep else None,
                    executed_at=keep["executed_at"] if keep and keep["executed"] else None,
                    executed_price=keep["executed_price"] if keep and keep["executed"] else None,
                    executed_qty=keep["executed_qty"] if keep and keep["executed"] else None,
                    executed_usdt=keep["executed_usdt"] if keep and keep["executed"] else None,
                )
            )


def _apply_schema_updates() -> None:
    stmts = [
        "ALTER TABLE campaigns ADD COLUMN ai_dca_enabled BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE campaigns ADD COLUMN ai_dca_profile VARCHAR(40)",
        "ALTER TABLE campaigns ADD COLUMN ai_dca_notes TEXT",
        "ALTER TABLE campaigns ADD COLUMN ai_dca_suggested_rules_json TEXT",
        "ALTER TABLE campaigns ADD COLUMN strict_support_score_required BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE campaigns ADD COLUMN trend_filter_enabled BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE campaigns ADD COLUMN auto_reentry_enabled BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE campaigns ADD COLUMN loop_enabled BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE campaigns ADD COLUMN loop_v2_enabled BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE campaigns ADD COLUMN loop_target_count INTEGER NOT NULL DEFAULT 5",
        "ALTER TABLE position_dca_states ADD COLUMN custom_drop_pct FLOAT",
        "ALTER TABLE position_dca_states ADD COLUMN custom_allocation_pct FLOAT",
        "ALTER TABLE position_dca_states ADD COLUMN custom_support_score FLOAT",
        "ALTER TABLE positions ADD COLUMN dca_paused BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE positions ADD COLUMN dca_pause_reason VARCHAR(160)",
        "ALTER TABLE positions ADD COLUMN tp_order_id INTEGER",
        "ALTER TABLE positions ADD COLUMN tp_order_price FLOAT",
        "ALTER TABLE positions ADD COLUMN tp_order_qty FLOAT",
    ]
    with engine.begin() as conn:
        for stmt in stmts:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass
        # Backfill: old loop campaigns should run in strict AI mode.
        try:
            conn.execute(
                text(
                    "UPDATE campaigns "
                    "SET strict_support_score_required = 1 "
                    "WHERE loop_enabled = 1 AND strict_support_score_required = 0"
                )
            )
        except Exception:
            pass


def _scheduled_cycle() -> None:
    if not cycle_lock.acquire(blocking=False):
        return
    db = SessionLocal()
    try:
        run_cycle(db)
    finally:
        db.close()
        cycle_lock.release()


def _scheduled_live_cycle() -> None:
    if not live_cycle_lock.acquire(blocking=False):
        return
    db = SessionLocal()
    try:
        run_live_cycle(db)
    finally:
        db.close()
        live_cycle_lock.release()


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    _apply_schema_updates()
    db = SessionLocal()
    try:
        ensure_defaults(db, settings.paper_start_balance)
    finally:
        db.close()

    scheduler.add_job(_scheduled_cycle, "interval", seconds=max(settings.cycle_seconds, 3), id="paper_cycle", replace_existing=True)
    scheduler.add_job(
        _scheduled_live_cycle, "interval", seconds=max(settings.cycle_seconds, 3), id="live_cycle", replace_existing=True
    )
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


@app.get("/", response_class=HTMLResponse)
async def mode_home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("mode_home.html", _context("home", request=request))


@app.get("/paper", response_class=HTMLResponse)
async def paper_dashboard(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        campaigns = db.query(Campaign).filter(Campaign.mode == "paper").order_by(desc(Campaign.created_at)).all()
        wallet = wallet_snapshot(db)
        items = []
        for c in campaigns:
            stats = _campaign_stats(db, c)
            items.append({"campaign": c, "stats": stats})
        realized_rows = sorted(
            [
                {"campaign": item["campaign"], "amount": float(item["stats"]["realized_pnl"])}
                for item in items
            ],
            key=lambda x: x["amount"],
            reverse=True,
        )
        unrealized_rows = sorted(
            [
                {"campaign": item["campaign"], "amount": float(item["stats"]["unrealized_pnl"])}
                for item in items
            ],
            key=lambda x: x["amount"],
            reverse=True,
        )
        logs = _dashboard_logs(db, "paper")
        return templates.TemplateResponse(
            "paper_home.html",
            _context(
                "paper_home",
                request=request,
                wallet=wallet,
                campaigns=items,
                realized_rows=realized_rows,
                unrealized_rows=unrealized_rows,
                logs=logs,
            ),
        )
    finally:
        db.close()


@app.get("/paper/campaigns")
async def paper_campaigns_alias() -> RedirectResponse:
    return RedirectResponse("/paper", status_code=303)


@app.get("/paper/create", response_class=HTMLResponse)
async def paper_create_campaign_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("paper_create.html", _context("paper_create", request=request))


@app.get("/live", response_class=HTMLResponse)
async def live_dashboard(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        campaigns = db.query(Campaign).filter(Campaign.mode == "live").order_by(desc(Campaign.created_at)).all()
        live_error = None
        try:
            wallet = live_wallet_snapshot(db)
        except Exception as e:
            wallet = {
                "cash": 0.0,
                "equity": 0.0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "invested_open": 0.0,
                "market_value": 0.0,
            }
            live_error = str(e)
        items = []
        for c in campaigns:
            stats = _campaign_stats(db, c)
            items.append({"campaign": c, "stats": stats})
        logs = _dashboard_logs(db, "live")
        return templates.TemplateResponse(
            "live_home.html",
            _context("live_home", request=request, wallet=wallet, campaigns=items, logs=logs, live_error=live_error),
        )
    finally:
        db.close()


@app.get("/live/campaigns")
async def live_campaigns_alias() -> RedirectResponse:
    return RedirectResponse("/live", status_code=303)


@app.get("/live/create", response_class=HTMLResponse)
async def live_create_campaign_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("live_create.html", _context("live_create", request=request))


@app.get("/live/history", response_class=HTMLResponse)
async def live_trading_history(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        date_filter = str(request.query_params.get("date_range", "all")).strip().lower()
        strategy_filter = str(request.query_params.get("strategy", "all")).strip().lower()
        valid_date = {x[0] for x in _DATE_FILTERS}
        valid_strategy = {x[0] for x in _STRATEGY_FILTERS}
        if date_filter not in valid_date:
            date_filter = "all"
        if strategy_filter not in valid_strategy:
            strategy_filter = "all"
        ctx = _history_context(db, "live", date_filter, strategy_filter)
        return templates.TemplateResponse(
            "live_history.html",
            _context("live_history", request=request, **ctx),
        )
    finally:
        db.close()


@app.get("/live/campaigns/{campaign_id}", response_class=HTMLResponse)
async def live_campaign_details(request: Request, campaign_id: int) -> HTMLResponse:
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.mode == "live").first()
        if not campaign:
            return RedirectResponse("/live", status_code=303)
        rules = db.query(DcaRule).filter(DcaRule.campaign_id == campaign.id).order_by(DcaRule.drop_pct.asc()).all()
        edit_rules = {r.name: r for r in rules}
        edit_slots = rules[:5]
        while len(edit_slots) < 5:
            edit_slots.append(None)
        ai_suggested_rows = []
        if campaign.ai_dca_enabled:
            try:
                suggested = json.loads(campaign.ai_dca_suggested_rules_json or "[]")
            except Exception:
                suggested = []
            if isinstance(suggested, list):
                for row in suggested:
                    try:
                        ai_suggested_rows.append(
                            {
                                "name": str(row.get("name", "AI-DCA")).strip() or "AI-DCA",
                                "drop_pct": float(row.get("drop_pct", 0.0)),
                                "allocation_pct": float(row.get("allocation_pct", 0.0)),
                                "suggested_usdt": campaign.entry_amount_usdt * (float(row.get("allocation_pct", 0.0)) / 100.0),
                            }
                        )
                    except Exception:
                        continue
        current_rows = [
            {
                "name": r.name,
                "drop_pct": float(r.drop_pct),
                "allocation_pct": float(r.allocation_pct),
                "current_usdt": campaign.entry_amount_usdt * (float(r.allocation_pct) / 100.0),
            }
            for r in rules
        ]
        positions = db.query(Position).filter(Position.campaign_id == campaign.id).order_by(desc(Position.id)).all()
        position_ids = [p.id for p in positions]
        executed_dca_counts: dict[int, int] = {}
        if position_ids:
            dca_states = db.query(PositionDcaState).filter(PositionDcaState.position_id.in_(position_ids)).all()
            for st in dca_states:
                if bool(st.executed):
                    executed_dca_counts[st.position_id] = int(executed_dca_counts.get(st.position_id, 0)) + 1
        open_symbols = [p.symbol for p in positions if p.status == "open"]
        prices = get_prices(open_symbols) if open_symbols else {}
        stats = _campaign_stats(db, campaign)
        return templates.TemplateResponse(
            "live_campaign.html",
            _context(
                "live_campaign",
                request=request,
                campaign=campaign,
                rules=rules,
                edit_rules=edit_rules,
                edit_slots=edit_slots,
                ai_suggested_rows=ai_suggested_rows,
                current_rows=current_rows,
                positions=positions,
                executed_dca_counts=executed_dca_counts,
                prices=prices,
                stats=stats,
            ),
        )
    finally:
        db.close()


@app.get("/paper/history", response_class=HTMLResponse)
async def paper_trading_history(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        date_filter = str(request.query_params.get("date_range", "all")).strip().lower()
        strategy_filter = str(request.query_params.get("strategy", "all")).strip().lower()
        valid_date = {x[0] for x in _DATE_FILTERS}
        valid_strategy = {x[0] for x in _STRATEGY_FILTERS}
        if date_filter not in valid_date:
            date_filter = "all"
        if strategy_filter not in valid_strategy:
            strategy_filter = "all"
        ctx = _history_context(db, "paper", date_filter, strategy_filter)
        return templates.TemplateResponse(
            "trading_history.html",
            _context("history", request=request, **ctx),
        )
    finally:
        db.close()


@app.get("/paper/campaigns/{campaign_id}", response_class=HTMLResponse)
async def paper_campaign_details(request: Request, campaign_id: int) -> HTMLResponse:
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.mode == "paper").first()
        if not campaign:
            return RedirectResponse("/paper", status_code=303)
        rules = db.query(DcaRule).filter(DcaRule.campaign_id == campaign.id).order_by(DcaRule.drop_pct.asc()).all()
        edit_rules = {r.name: r for r in rules}
        edit_slots = rules[:5]
        while len(edit_slots) < 5:
            edit_slots.append(None)
        ai_suggested_rows = []
        if campaign.ai_dca_enabled:
            try:
                suggested = json.loads(campaign.ai_dca_suggested_rules_json or "[]")
            except Exception:
                suggested = []
            if isinstance(suggested, list):
                for row in suggested:
                    try:
                        name_rule = str(row.get("name", "AI-DCA")).strip() or "AI-DCA"
                        drop_pct = float(row.get("drop_pct", 0.0))
                        allocation_pct = float(row.get("allocation_pct", 0.0))
                        ai_suggested_rows.append(
                            {
                                "name": name_rule,
                                "drop_pct": drop_pct,
                                "allocation_pct": allocation_pct,
                                "suggested_usdt": campaign.entry_amount_usdt * (allocation_pct / 100.0),
                            }
                        )
                    except Exception:
                        continue
        current_rows = []
        for row in rules:
            current_rows.append(
                {
                    "name": row.name,
                    "drop_pct": float(row.drop_pct),
                    "allocation_pct": float(row.allocation_pct),
                    "current_usdt": campaign.entry_amount_usdt * (float(row.allocation_pct) / 100.0),
                }
            )
        positions = db.query(Position).filter(Position.campaign_id == campaign.id).order_by(desc(Position.id)).all()
        position_ids = [p.id for p in positions]
        executed_dca_counts: dict[int, int] = {}
        if position_ids:
            dca_states = db.query(PositionDcaState).filter(PositionDcaState.position_id.in_(position_ids)).all()
            for st in dca_states:
                if bool(st.executed):
                    executed_dca_counts[st.position_id] = int(executed_dca_counts.get(st.position_id, 0)) + 1
        open_symbols = [p.symbol for p in positions if p.status == "open"]
        prices = get_prices(open_symbols) if open_symbols else {}
        stats = _campaign_stats(db, campaign)
        return templates.TemplateResponse(
            "paper_campaign.html",
            _context(
                "paper_campaign",
                request=request,
                campaign=campaign,
                rules=rules,
                edit_rules=edit_rules,
                edit_slots=edit_slots,
                ai_suggested_rows=ai_suggested_rows,
                current_rows=current_rows,
                positions=positions,
                executed_dca_counts=executed_dca_counts,
                prices=prices,
                stats=stats,
            ),
        )
    finally:
        db.close()


@app.post("/paper/campaigns")
async def create_paper_campaign(
    request: Request,
    name: str = Form(...),
    entry_amount_usdt: str = Form(...),
    symbols: str = Form(""),
    tp_pct: str = Form(""),
    sl_pct: str = Form(""),
    dca_drop_1: str = Form(""),
    dca_alloc_1: str = Form(""),
    dca_drop_2: str = Form(""),
    dca_alloc_2: str = Form(""),
    dca_drop_3: str = Form(""),
    dca_alloc_3: str = Form(""),
    dca_drop_4: str = Form(""),
    dca_alloc_4: str = Form(""),
    dca_drop_5: str = Form(""),
    dca_alloc_5: str = Form(""),
    ai_dca_enabled: str | None = Form(None),
    strict_support_score_required: str | None = Form(None),
    trend_filter_enabled: str | None = Form(None),
    auto_reentry_enabled: str | None = Form(None),
    loop_enabled: str | None = Form(None),
    loop_v2_enabled: str | None = Form(None),
    loop_target_count: str = Form("5"),
) -> RedirectResponse:
    db = SessionLocal()
    try:
        entry_amount = _safe_float(entry_amount_usdt, 0.0) or 0.0
        if entry_amount <= 0:
            return RedirectResponse("/paper", status_code=303)

        picked = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        loop_mode = str(loop_enabled or "").lower() in {"on", "true", "1", "yes"}
        loop_v2_mode = str(loop_v2_enabled or "").lower() in {"on", "true", "1", "yes"}
        loop_target = int(_safe_float(loop_target_count, 5.0) or 5.0)
        loop_target = min(max(loop_target, 1), 30)
        ai_mode = str(ai_dca_enabled or "").lower() in {"on", "true", "1", "yes"}
        strict_score_mode = str(strict_support_score_required or "").lower() in {"on", "true", "1", "yes"}
        trend_mode = str(trend_filter_enabled or "").lower() in {"on", "true", "1", "yes"}
        reentry_mode = str(auto_reentry_enabled or "").lower() in {"on", "true", "1", "yes"}
        if loop_mode:
            ai_mode = True
            strict_score_mode = True
            reentry_mode = False
            scan = suggest_top_symbols(max(loop_target, 10), use_v2=loop_v2_mode)
            picked = [str(item.get("symbol", "")).upper() for item in (scan.get("items") or []) if item.get("symbol")]
            picked = picked[:loop_target]
        if not picked:
            return RedirectResponse("/paper/create", status_code=303)
        campaign = Campaign(
            name=name.strip() or "Paper Campaign",
            mode="paper",
            status="active",
            entry_amount_usdt=entry_amount,
            tp_pct=_safe_float(tp_pct, None),
            sl_pct=_safe_float(sl_pct, None),
            ai_dca_enabled=ai_mode,
            strict_support_score_required=strict_score_mode,
            trend_filter_enabled=trend_mode,
            auto_reentry_enabled=reentry_mode,
            loop_enabled=loop_mode,
            loop_v2_enabled=loop_v2_mode if loop_mode else False,
            loop_target_count=loop_target if loop_mode else 0,
        )
        db.add(campaign)
        db.flush()

        if ai_mode:
            ai_rules, ai_profile, ai_note = build_ai_dca_rules(picked, campaign.sl_pct)
            campaign.ai_dca_profile = ai_profile
            campaign.ai_dca_notes = ai_note
            campaign.ai_dca_suggested_rules_json = json.dumps(
                [
                    {"name": name_rule, "drop_pct": float(drop_pct), "allocation_pct": float(alloc_pct)}
                    for name_rule, drop_pct, alloc_pct in ai_rules
                ]
            )
            for name_rule, drop_pct, alloc_pct in ai_rules:
                db.add(
                    DcaRule(
                        campaign_id=campaign.id,
                        name=name_rule,
                        drop_pct=drop_pct,
                        allocation_pct=alloc_pct,
                    )
                )
            db.add(
                ActivityLog(
                    event_type="AI_DCA",
                    symbol="-",
                    message=f"Campaign '{campaign.name}' | {ai_note}",
                )
            )
        else:
            dca_raw = [
                ("DCA-1", _safe_float(dca_drop_1, None), _safe_float(dca_alloc_1, None)),
                ("DCA-2", _safe_float(dca_drop_2, None), _safe_float(dca_alloc_2, None)),
                ("DCA-3", _safe_float(dca_drop_3, None), _safe_float(dca_alloc_3, None)),
                ("DCA-4", _safe_float(dca_drop_4, None), _safe_float(dca_alloc_4, None)),
                ("DCA-5", _safe_float(dca_drop_5, None), _safe_float(dca_alloc_5, None)),
            ]
            for name_rule, drop_pct, alloc_pct in dca_raw:
                if drop_pct is None or alloc_pct is None:
                    continue
                if drop_pct <= 0 or alloc_pct <= 0:
                    continue
                db.add(
                    DcaRule(
                        campaign_id=campaign.id,
                        name=name_rule,
                        drop_pct=drop_pct,
                        allocation_pct=alloc_pct,
                    )
                )
        db.commit()

        try:
            opened, errors = create_campaign_positions(db, campaign, picked)
        except Exception as exc:
            opened, errors = 0, [f"{type(exc).__name__}: {exc}"]
        if errors:
            campaign.status = "paused"
            db.add(
                ActivityLog(
                    event_type="CAMPAIGN_ERROR",
                    symbol="-",
                    message=f"Campaign '{campaign.name}' failed to open positions: {' | '.join(errors)}",
                )
            )
            db.commit()
        if opened > 0:
            db.add(
                ActivityLog(
                    event_type="CAMPAIGN",
                    symbol="-",
                    message=f"Campaign '{campaign.name}' started with {opened} symbols.",
                )
            )
            db.commit()
        return RedirectResponse(f"/paper/campaigns/{campaign.id}", status_code=303)
    finally:
        db.close()


@app.post("/paper/reset")
async def reset_paper_data(confirm_text: str = Form("")) -> RedirectResponse:
    db = SessionLocal()
    try:
        if str(confirm_text).strip().upper() != "RESET":
            db.add(
                ActivityLog(
                    event_type="RESET_BLOCKED",
                    symbol="-",
                    message="Paper reset blocked: invalid confirmation text.",
                )
            )
            db.commit()
            return RedirectResponse("/paper/create", status_code=303)

        db.query(PositionDcaState).delete()
        db.query(Position).delete()
        db.query(DcaRule).delete()
        db.query(Campaign).delete()
        db.query(ActivityLog).delete()
        db.query(AppSetting).delete()
        db.commit()

        ensure_defaults(db, settings.paper_start_balance)
        return RedirectResponse("/paper/create", status_code=303)
    finally:
        db.close()


@app.post("/paper/campaigns/{campaign_id}/toggle")
async def toggle_paper_campaign(campaign_id: int) -> RedirectResponse:
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.mode == "paper").first()
        if campaign:
            campaign.status = "paused" if campaign.status == "active" else "active"
            db.add(
                ActivityLog(
                    event_type="CAMPAIGN",
                    symbol="-",
                    message=f"Campaign '{campaign.name}' switched to {campaign.status}.",
                )
            )
            db.commit()
        return RedirectResponse(f"/paper/campaigns/{campaign_id}", status_code=303)
    finally:
        db.close()


@app.post("/paper/campaigns/{campaign_id}/recalculate-dca")
async def recalculate_campaign_dca_now(campaign_id: int) -> RedirectResponse:
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.mode == "paper").first()
        if not campaign:
            return RedirectResponse("/paper", status_code=303)

        touched_positions, updated_states = recalculate_campaign_dca(db, campaign)
        db.add(
            ActivityLog(
                event_type="DCA_RECALC",
                symbol="-",
                message=(
                    f"Campaign='{campaign.name}' | touched_positions={touched_positions} "
                    f"| updated_pending_states={updated_states}"
                ),
            )
        )
        db.commit()
        return RedirectResponse(f"/paper/campaigns/{campaign_id}", status_code=303)
    finally:
        db.close()


@app.post("/paper/positions/{position_id}/sell")
async def manual_sell_position(position_id: int) -> RedirectResponse:
    db = SessionLocal()
    try:
        pos = (
            db.query(Position)
            .join(Campaign, Campaign.id == Position.campaign_id)
            .filter(Position.id == position_id, Position.status == "open", Campaign.mode == "paper")
            .first()
        )
        if not pos:
            return RedirectResponse("/paper", status_code=303)

        campaign_id = pos.campaign_id
        prices = get_prices([pos.symbol])
        price = float(prices.get(pos.symbol, pos.average_price))
        proceeds = float(pos.total_qty) * price
        pnl = proceeds - float(pos.total_invested_usdt)

        pos.status = "closed"
        pos.closed_at = datetime.utcnow()
        pos.close_price = price
        pos.realized_pnl_usdt = pnl
        pos.close_reason = "MANUAL_SELL"

        cash_row = db.query(AppSetting).filter(AppSetting.key == "paper_cash").first()
        cash = float(cash_row.value) if cash_row and cash_row.value else 0.0
        cash += proceeds
        if cash_row:
            cash_row.value = f"{cash:.8f}"
        else:
            db.add(AppSetting(key="paper_cash", value=f"{cash:.8f}"))

        db.add(
            ActivityLog(
                event_type="MANUAL_SELL",
                symbol=pos.symbol,
                message=(
                    f"Campaign={pos.campaign.name} | Close={price:.6f} | Qty={pos.total_qty:.8f} "
                    f"| Proceeds={proceeds:.2f} | PnL={pnl:+.2f}"
                ),
            )
        )
        db.commit()
        return RedirectResponse(f"/paper/campaigns/{campaign_id}", status_code=303)
    finally:
        db.close()


@app.post("/paper/campaigns/{campaign_id}/edit")
async def edit_paper_campaign(
    campaign_id: int,
    tp_pct: str = Form(""),
    sl_pct: str = Form(""),
    trend_filter_enabled: str | None = Form(None),
    auto_reentry_enabled: str | None = Form(None),
    strict_support_score_required: str | None = Form(None),
    loop_v2_enabled: str | None = Form(None),
    loop_target_count: str = Form(""),
    dca_drop_1: str = Form(""),
    dca_alloc_1: str = Form(""),
    dca_drop_2: str = Form(""),
    dca_alloc_2: str = Form(""),
    dca_drop_3: str = Form(""),
    dca_alloc_3: str = Form(""),
    dca_drop_4: str = Form(""),
    dca_alloc_4: str = Form(""),
    dca_drop_5: str = Form(""),
    dca_alloc_5: str = Form(""),
) -> RedirectResponse:
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.mode == "paper").first()
        if not campaign:
            return RedirectResponse("/paper", status_code=303)

        campaign.tp_pct = _safe_float(tp_pct, None)
        campaign.sl_pct = _safe_float(sl_pct, None)
        campaign.trend_filter_enabled = str(trend_filter_enabled or "").lower() in {"on", "true", "1", "yes"}
        campaign.auto_reentry_enabled = str(auto_reentry_enabled or "").lower() in {"on", "true", "1", "yes"}
        campaign.strict_support_score_required = str(strict_support_score_required or "").lower() in {"on", "true", "1", "yes"}
        if campaign.loop_enabled:
            campaign.strict_support_score_required = True
            campaign.loop_v2_enabled = str(loop_v2_enabled or "").lower() in {"on", "true", "1", "yes"}
        if campaign.loop_enabled:
            desired = int(_safe_float(loop_target_count, float(campaign.loop_target_count or 5)) or 5)
            campaign.loop_target_count = min(max(desired, 1), 30)

        incoming = [
            ("DCA-1", _safe_float(dca_drop_1, None), _safe_float(dca_alloc_1, None)),
            ("DCA-2", _safe_float(dca_drop_2, None), _safe_float(dca_alloc_2, None)),
            ("DCA-3", _safe_float(dca_drop_3, None), _safe_float(dca_alloc_3, None)),
            ("DCA-4", _safe_float(dca_drop_4, None), _safe_float(dca_alloc_4, None)),
            ("DCA-5", _safe_float(dca_drop_5, None), _safe_float(dca_alloc_5, None)),
        ]
        existing = {r.name: r for r in db.query(DcaRule).filter(DcaRule.campaign_id == campaign.id).all()}
        kept_rule_names: set[str] = set()
        for name_rule, drop_pct, alloc_pct in incoming:
            if drop_pct is None or alloc_pct is None or drop_pct <= 0 or alloc_pct <= 0:
                continue
            kept_rule_names.add(name_rule)
            row = existing.get(name_rule)
            if row:
                row.drop_pct = drop_pct
                row.allocation_pct = alloc_pct
            else:
                db.add(
                    DcaRule(
                        campaign_id=campaign.id,
                        name=name_rule,
                        drop_pct=drop_pct,
                        allocation_pct=alloc_pct,
                    )
                )

        for name_rule, row in existing.items():
            if name_rule not in kept_rule_names:
                db.delete(row)

        db.flush()
        _sync_open_positions_dca_states(db, campaign.id)
        db.add(
            ActivityLog(
                event_type="CAMPAIGN_EDIT",
                symbol="-",
                message=(
                    f"Campaign '{campaign.name}' updated: TP={campaign.tp_pct}, SL={campaign.sl_pct}, "
                    f"DCA rules={sorted(kept_rule_names)}"
                ),
            )
        )
        db.commit()
        return RedirectResponse(f"/paper/campaigns/{campaign_id}", status_code=303)
    finally:
        db.close()


@app.post("/live/campaigns")
async def create_live_campaign(
    request: Request,
    name: str = Form(...),
    entry_amount_usdt: str = Form(...),
    symbols: str = Form(""),
    tp_pct: str = Form(""),
    sl_pct: str = Form(""),
    dca_drop_1: str = Form(""),
    dca_alloc_1: str = Form(""),
    dca_drop_2: str = Form(""),
    dca_alloc_2: str = Form(""),
    dca_drop_3: str = Form(""),
    dca_alloc_3: str = Form(""),
    dca_drop_4: str = Form(""),
    dca_alloc_4: str = Form(""),
    dca_drop_5: str = Form(""),
    dca_alloc_5: str = Form(""),
    ai_dca_enabled: str | None = Form(None),
    strict_support_score_required: str | None = Form(None),
    trend_filter_enabled: str | None = Form(None),
    auto_reentry_enabled: str | None = Form(None),
    loop_enabled: str | None = Form(None),
    loop_v2_enabled: str | None = Form(None),
    loop_target_count: str = Form("5"),
) -> RedirectResponse:
    db = SessionLocal()
    try:
        entry_amount = _safe_float(entry_amount_usdt, 0.0) or 0.0
        if entry_amount <= 0:
            return RedirectResponse("/live", status_code=303)
        picked = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        loop_mode = str(loop_enabled or "").lower() in {"on", "true", "1", "yes"}
        loop_v2_mode = str(loop_v2_enabled or "").lower() in {"on", "true", "1", "yes"}
        loop_target = int(_safe_float(loop_target_count, 5.0) or 5.0)
        loop_target = min(max(loop_target, 1), 30)
        ai_mode = str(ai_dca_enabled or "").lower() in {"on", "true", "1", "yes"}
        strict_score_mode = str(strict_support_score_required or "").lower() in {"on", "true", "1", "yes"}
        trend_mode = str(trend_filter_enabled or "").lower() in {"on", "true", "1", "yes"}
        reentry_mode = str(auto_reentry_enabled or "").lower() in {"on", "true", "1", "yes"}
        if loop_mode:
            ai_mode = True
            strict_score_mode = True
            reentry_mode = False
            scan = suggest_top_symbols(max(loop_target, 10), use_v2=loop_v2_mode)
            picked = [str(item.get("symbol", "")).upper() for item in (scan.get("items") or []) if item.get("symbol")]
            picked = picked[:loop_target]
        if not picked:
            return RedirectResponse("/live/create", status_code=303)
        campaign = Campaign(
            name=name.strip() or "Live Campaign",
            mode="live",
            status="active",
            entry_amount_usdt=entry_amount,
            tp_pct=_safe_float(tp_pct, None),
            sl_pct=_safe_float(sl_pct, None),
            ai_dca_enabled=ai_mode,
            strict_support_score_required=strict_score_mode,
            trend_filter_enabled=trend_mode,
            auto_reentry_enabled=reentry_mode,
            loop_enabled=loop_mode,
            loop_v2_enabled=loop_v2_mode if loop_mode else False,
            loop_target_count=loop_target if loop_mode else 0,
        )
        db.add(campaign)
        db.flush()
        if ai_mode:
            ai_rules, ai_profile, ai_note = build_ai_dca_rules(picked, campaign.sl_pct)
            campaign.ai_dca_profile = ai_profile
            campaign.ai_dca_notes = ai_note
            campaign.ai_dca_suggested_rules_json = json.dumps(
                [{"name": n, "drop_pct": float(d), "allocation_pct": float(a)} for n, d, a in ai_rules]
            )
            for n, d, a in ai_rules:
                db.add(DcaRule(campaign_id=campaign.id, name=n, drop_pct=d, allocation_pct=a))
        else:
            dca_raw = [
                ("DCA-1", _safe_float(dca_drop_1, None), _safe_float(dca_alloc_1, None)),
                ("DCA-2", _safe_float(dca_drop_2, None), _safe_float(dca_alloc_2, None)),
                ("DCA-3", _safe_float(dca_drop_3, None), _safe_float(dca_alloc_3, None)),
                ("DCA-4", _safe_float(dca_drop_4, None), _safe_float(dca_alloc_4, None)),
                ("DCA-5", _safe_float(dca_drop_5, None), _safe_float(dca_alloc_5, None)),
            ]
            for n, d, a in dca_raw:
                if d is None or a is None or d <= 0 or a <= 0:
                    continue
                db.add(DcaRule(campaign_id=campaign.id, name=n, drop_pct=d, allocation_pct=a))
        db.commit()
        try:
            opened, errors = create_live_campaign_positions(db, campaign, picked)
        except Exception as e:
            db.add(ActivityLog(event_type="LIVE_CREATE_FAIL", symbol="-", message=f"Campaign='{campaign.name}' | error={e}"))
            db.commit()
            return RedirectResponse("/live/create", status_code=303)
        if errors:
            campaign.status = "paused"
            db.commit()
        if opened > 0:
            db.add(ActivityLog(event_type="LIVE_CAMPAIGN", symbol="-", message=f"Live campaign '{campaign.name}' opened={opened}."))
            db.commit()
        return RedirectResponse(f"/live/campaigns/{campaign.id}", status_code=303)
    finally:
        db.close()


@app.post("/live/campaigns/{campaign_id}/toggle")
async def toggle_live_campaign(campaign_id: int) -> RedirectResponse:
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.mode == "live").first()
        if campaign:
            campaign.status = "paused" if campaign.status == "active" else "active"
            db.add(ActivityLog(event_type="LIVE_CAMPAIGN", symbol="-", message=f"Campaign '{campaign.name}' switched to {campaign.status}."))
            db.commit()
        return RedirectResponse(f"/live/campaigns/{campaign_id}", status_code=303)
    finally:
        db.close()


@app.post("/live/campaigns/{campaign_id}/recalculate-dca")
async def recalculate_live_campaign_dca_now(campaign_id: int) -> RedirectResponse:
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.mode == "live").first()
        if not campaign:
            return RedirectResponse("/live", status_code=303)
        touched_positions, updated_states = recalculate_live_campaign_dca(db, campaign)
        db.add(
            ActivityLog(
                event_type="LIVE_DCA_RECALC",
                symbol="-",
                message=f"Campaign='{campaign.name}' | touched_positions={touched_positions} | updated_pending_states={updated_states}",
            )
        )
        db.commit()
        return RedirectResponse(f"/live/campaigns/{campaign_id}", status_code=303)
    finally:
        db.close()


@app.post("/live/positions/{position_id}/sell")
async def manual_sell_live_position(position_id: int) -> RedirectResponse:
    from app.services.binance_live import cancel_order, place_market_sell_qty

    db = SessionLocal()
    try:
        pos = (
            db.query(Position)
            .join(Campaign, Campaign.id == Position.campaign_id)
            .filter(Position.id == position_id, Position.status == "open", Campaign.mode == "live")
            .first()
        )
        if not pos:
            return RedirectResponse("/live", status_code=303)
        campaign_id = pos.campaign_id
        if pos.tp_order_id:
            try:
                cancel_order(pos.symbol, int(pos.tp_order_id))
            except Exception:
                pass
        sell = place_market_sell_qty(pos.symbol, pos.total_qty)
        proceeds = float(sell["quote_qty"])
        close_price = float(sell["avg_price"] or 0.0)
        pnl = proceeds - float(pos.total_invested_usdt)
        pos.status = "closed"
        pos.closed_at = datetime.utcnow()
        pos.close_price = close_price
        pos.realized_pnl_usdt = pnl
        pos.close_reason = "MANUAL_SELL"
        pos.tp_order_id = None
        pos.tp_order_price = None
        pos.tp_order_qty = None
        db.add(
            ActivityLog(
                event_type="LIVE_MANUAL_SELL",
                symbol=pos.symbol,
                message=(
                    f"Campaign={pos.campaign.name} | Close={close_price:.6f} | Qty={pos.total_qty:.8f} "
                    f"| Proceeds={proceeds:.2f} | PnL={pnl:+.2f}"
                ),
            )
        )
        db.commit()
        return RedirectResponse(f"/live/campaigns/{campaign_id}", status_code=303)
    finally:
        db.close()


@app.post("/live/campaigns/{campaign_id}/edit")
async def edit_live_campaign(
    campaign_id: int,
    tp_pct: str = Form(""),
    sl_pct: str = Form(""),
    trend_filter_enabled: str | None = Form(None),
    auto_reentry_enabled: str | None = Form(None),
    strict_support_score_required: str | None = Form(None),
    loop_v2_enabled: str | None = Form(None),
    loop_target_count: str = Form(""),
    dca_drop_1: str = Form(""),
    dca_alloc_1: str = Form(""),
    dca_drop_2: str = Form(""),
    dca_alloc_2: str = Form(""),
    dca_drop_3: str = Form(""),
    dca_alloc_3: str = Form(""),
    dca_drop_4: str = Form(""),
    dca_alloc_4: str = Form(""),
    dca_drop_5: str = Form(""),
    dca_alloc_5: str = Form(""),
) -> RedirectResponse:
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.mode == "live").first()
        if not campaign:
            return RedirectResponse("/live", status_code=303)
        campaign.tp_pct = _safe_float(tp_pct, None)
        campaign.sl_pct = _safe_float(sl_pct, None)
        campaign.trend_filter_enabled = str(trend_filter_enabled or "").lower() in {"on", "true", "1", "yes"}
        campaign.auto_reentry_enabled = str(auto_reentry_enabled or "").lower() in {"on", "true", "1", "yes"}
        campaign.strict_support_score_required = str(strict_support_score_required or "").lower() in {"on", "true", "1", "yes"}
        if campaign.loop_enabled:
            campaign.strict_support_score_required = True
            campaign.loop_v2_enabled = str(loop_v2_enabled or "").lower() in {"on", "true", "1", "yes"}
            desired = int(_safe_float(loop_target_count, float(campaign.loop_target_count or 5)) or 5)
            campaign.loop_target_count = min(max(desired, 1), 30)
        incoming = [
            ("DCA-1", _safe_float(dca_drop_1, None), _safe_float(dca_alloc_1, None)),
            ("DCA-2", _safe_float(dca_drop_2, None), _safe_float(dca_alloc_2, None)),
            ("DCA-3", _safe_float(dca_drop_3, None), _safe_float(dca_alloc_3, None)),
            ("DCA-4", _safe_float(dca_drop_4, None), _safe_float(dca_alloc_4, None)),
            ("DCA-5", _safe_float(dca_drop_5, None), _safe_float(dca_alloc_5, None)),
        ]
        existing = {r.name: r for r in db.query(DcaRule).filter(DcaRule.campaign_id == campaign.id).all()}
        kept_rule_names: set[str] = set()
        for n, d, a in incoming:
            if d is None or a is None or d <= 0 or a <= 0:
                continue
            kept_rule_names.add(n)
            row = existing.get(n)
            if row:
                row.drop_pct = d
                row.allocation_pct = a
            else:
                db.add(DcaRule(campaign_id=campaign.id, name=n, drop_pct=d, allocation_pct=a))
        for n, row in existing.items():
            if n not in kept_rule_names:
                db.delete(row)
        db.flush()
        _sync_open_positions_dca_states(db, campaign.id)
        db.add(
            ActivityLog(
                event_type="LIVE_CAMPAIGN_EDIT",
                symbol="-",
                message=f"Campaign '{campaign.name}' updated: TP={campaign.tp_pct}, SL={campaign.sl_pct}, DCA={sorted(kept_rule_names)}",
            )
        )
        db.commit()
        return RedirectResponse(f"/live/campaigns/{campaign_id}", status_code=303)
    finally:
        db.close()


@app.get("/api/live/suggestions")
async def api_live_suggestions(limit: int = 5, v2: int = 0) -> JSONResponse:
    safe_limit = min(max(int(limit), 1), 30)
    return JSONResponse(suggest_top_symbols(safe_limit, use_v2=bool(v2)))


@app.get("/api/live/positions/{position_id}/dca")
async def api_live_position_dca(position_id: int) -> JSONResponse:
    db = SessionLocal()
    try:
        position = db.query(Position).join(Campaign, Campaign.id == Position.campaign_id).filter(
            Position.id == position_id, Campaign.mode == "live"
        ).first()
        if not position:
            return JSONResponse({"items": []})
        states = (
            db.query(PositionDcaState)
            .join(DcaRule, DcaRule.id == PositionDcaState.dca_rule_id)
            .filter(PositionDcaState.position_id == position_id)
            .order_by(DcaRule.drop_pct.asc())
            .all()
        )
        items = []
        for st in states:
            drop_pct = float(st.custom_drop_pct if st.custom_drop_pct is not None else st.rule.drop_pct)
            alloc_pct = float(st.custom_allocation_pct if st.custom_allocation_pct is not None else st.rule.allocation_pct)
            if alloc_pct <= 0:
                continue
            trigger_price = float(position.initial_price) * (1 - (drop_pct / 100.0))
            items.append(
                {
                    "rule": st.rule.name,
                    "drop_pct": drop_pct,
                    "allocation_pct": alloc_pct,
                    "support_score": st.custom_support_score,
                    "trigger_price": trigger_price,
                    "source": "symbol_specific" if st.custom_drop_pct is not None else "campaign_default",
                    "executed": st.executed,
                    "executed_price": st.executed_price,
                }
            )
        return JSONResponse({"items": items})
    finally:
        db.close()


@app.get("/api/binance/symbols")
async def api_symbol_search(q: str = "") -> JSONResponse:
    data = search_symbols(q, limit=40)
    return JSONResponse({"items": data})


@app.get("/api/paper/suggestions")
async def api_paper_suggestions(limit: int = 5, v2: int = 0) -> JSONResponse:
    safe_limit = min(max(int(limit), 1), 10)
    data = suggest_top_symbols(safe_limit, use_v2=bool(v2))
    return JSONResponse(data)


@app.get("/api/paper/positions/{position_id}/dca")
async def api_position_dca(position_id: int) -> JSONResponse:
    db = SessionLocal()
    try:
        position = db.query(Position).filter(Position.id == position_id).first()
        if not position:
            return JSONResponse({"items": []})
        states = (
            db.query(PositionDcaState)
            .join(DcaRule, DcaRule.id == PositionDcaState.dca_rule_id)
            .filter(PositionDcaState.position_id == position_id)
            .order_by(DcaRule.drop_pct.asc())
            .all()
        )
        items = []
        for st in states:
            drop_pct = float(st.custom_drop_pct if st.custom_drop_pct is not None else st.rule.drop_pct)
            alloc_pct = float(st.custom_allocation_pct if st.custom_allocation_pct is not None else st.rule.allocation_pct)
            if alloc_pct <= 0:
                continue
            trigger_price = float(position.initial_price) * (1 - (drop_pct / 100.0))
            items.append(
                {
                    "rule": st.rule.name,
                    "drop_pct": drop_pct,
                    "allocation_pct": alloc_pct,
                    "support_score": st.custom_support_score,
                    "trigger_price": trigger_price,
                    "source": "symbol_specific" if st.custom_drop_pct is not None else "campaign_default",
                    "executed": st.executed,
                    "executed_price": st.executed_price,
                }
            )
        return JSONResponse({"items": items})
    finally:
        db.close()
