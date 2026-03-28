import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc

from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.models.paper_v2 import ActivityLog, Campaign, DcaRule, Position, PositionDcaState
from app.services.binance_public import get_prices, search_symbols
from app.services.paper_trading import create_campaign_positions, ensure_defaults, run_cycle, wallet_snapshot

app = FastAPI(title="Crypto Bots - Rebuild")
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
templates = Jinja2Templates(directory="app/web/templates")

scheduler = BackgroundScheduler(timezone="UTC")
cycle_lock = threading.Lock()


def _context(active: str, **kwargs) -> dict:
    base = {"active_page": active, "now": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
    base.update(kwargs)
    return base


def _campaign_stats(db, campaign: Campaign) -> dict:
    open_positions = db.query(Position).filter(Position.campaign_id == campaign.id, Position.status == "open").all()
    closed_positions = db.query(Position).filter(Position.campaign_id == campaign.id, Position.status == "closed").all()

    symbols = sorted({p.symbol for p in open_positions})
    prices = get_prices(symbols) if symbols else {}
    unrealized = sum((float(prices.get(p.symbol, p.average_price)) * p.total_qty) - p.total_invested_usdt for p in open_positions)
    realized = sum(float(p.realized_pnl_usdt or 0.0) for p in closed_positions)

    return {
        "open_count": len(open_positions),
        "closed_count": len(closed_positions),
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
    }


def _safe_float(value: str | None, default: float | None = None) -> float | None:
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(str(value).replace("%", "").strip())
    except Exception:
        return default


def _scheduled_cycle() -> None:
    if not cycle_lock.acquire(blocking=False):
        return
    db = SessionLocal()
    try:
        run_cycle(db)
    finally:
        db.close()
        cycle_lock.release()


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        ensure_defaults(db, settings.paper_start_balance)
    finally:
        db.close()

    scheduler.add_job(_scheduled_cycle, "interval", seconds=max(settings.cycle_seconds, 3), id="paper_cycle", replace_existing=True)
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
        logs = db.query(ActivityLog).order_by(desc(ActivityLog.id)).limit(50).all()
        return templates.TemplateResponse(
            "paper_dashboard.html",
            _context("paper", request=request, wallet=wallet, campaigns=items, logs=logs),
        )
    finally:
        db.close()


@app.get("/live", response_class=HTMLResponse)
async def live_dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("live_dashboard.html", _context("live", request=request))


@app.get("/paper/campaigns/{campaign_id}", response_class=HTMLResponse)
async def paper_campaign_details(request: Request, campaign_id: int) -> HTMLResponse:
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.mode == "paper").first()
        if not campaign:
            return RedirectResponse("/paper", status_code=303)
        rules = db.query(DcaRule).filter(DcaRule.campaign_id == campaign.id).order_by(DcaRule.drop_pct.asc()).all()
        positions = db.query(Position).filter(Position.campaign_id == campaign.id).order_by(desc(Position.id)).all()
        open_symbols = [p.symbol for p in positions if p.status == "open"]
        prices = get_prices(open_symbols) if open_symbols else {}
        stats = _campaign_stats(db, campaign)
        return templates.TemplateResponse(
            "paper_campaign.html",
            _context(
                "paper",
                request=request,
                campaign=campaign,
                rules=rules,
                positions=positions,
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
    symbols: str = Form(...),
    tp_pct: str = Form(""),
    sl_pct: str = Form(""),
    dca_drop_1: str = Form(""),
    dca_alloc_1: str = Form(""),
    dca_drop_2: str = Form(""),
    dca_alloc_2: str = Form(""),
    dca_drop_3: str = Form(""),
    dca_alloc_3: str = Form(""),
) -> RedirectResponse:
    db = SessionLocal()
    try:
        entry_amount = _safe_float(entry_amount_usdt, 0.0) or 0.0
        if entry_amount <= 0:
            return RedirectResponse("/paper", status_code=303)

        picked = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        campaign = Campaign(
            name=name.strip() or "Paper Campaign",
            mode="paper",
            status="active",
            entry_amount_usdt=entry_amount,
            tp_pct=_safe_float(tp_pct, None),
            sl_pct=_safe_float(sl_pct, None),
        )
        db.add(campaign)
        db.flush()

        dca_raw = [
            ("DCA-1", _safe_float(dca_drop_1, None), _safe_float(dca_alloc_1, None)),
            ("DCA-2", _safe_float(dca_drop_2, None), _safe_float(dca_alloc_2, None)),
            ("DCA-3", _safe_float(dca_drop_3, None), _safe_float(dca_alloc_3, None)),
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

        opened, errors = create_campaign_positions(db, campaign, picked)
        if errors:
            campaign.status = "paused"
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


@app.get("/api/binance/symbols")
async def api_symbol_search(q: str = "") -> JSONResponse:
    data = search_symbols(q, limit=40)
    return JSONResponse({"items": data})


@app.get("/api/paper/positions/{position_id}/dca")
async def api_position_dca(position_id: int) -> JSONResponse:
    db = SessionLocal()
    try:
        states = (
            db.query(PositionDcaState)
            .join(DcaRule, DcaRule.id == PositionDcaState.dca_rule_id)
            .filter(PositionDcaState.position_id == position_id)
            .order_by(DcaRule.drop_pct.asc())
            .all()
        )
        items = []
        for st in states:
            items.append(
                {
                    "rule": st.rule.name,
                    "drop_pct": st.rule.drop_pct,
                    "allocation_pct": st.rule.allocation_pct,
                    "executed": st.executed,
                    "executed_price": st.executed_price,
                }
            )
        return JSONResponse({"items": items})
    finally:
        db.close()
