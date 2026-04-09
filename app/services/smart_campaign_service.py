"""
Smart Campaign service — auto-manages positions based on Advisor recommendations.

Logic:
  - Every cycle: check prices → handle DCA / TP / SL → open new slots if campaign is running
  - TP/SL are taken from advisor params at entry time
  - When advisor refreshes, new positions use updated params (old positions keep original params)
  - Stopping a campaign: no new buys, existing positions remain active until manual sell or TP/SL
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.smart_campaign import SmartCampaign, SmartPosition
from app.services.binance_public import get_prices

logger = logging.getLogger(__name__)


# ── Advisor data ───────────────────────────────────────────────────────────────

def get_advisor_recommendations() -> list[dict]:
    """Load latest combined recommendations from advisor latest.json."""
    try:
        from advisor.config import REPORT_DIR
        latest = Path(REPORT_DIR) / "latest.json"
        if not latest.exists():
            return []
        with open(latest) as f:
            data = json.load(f)
        return data.get("recommendations", [])
    except Exception as e:
        logger.warning("SmartCampaign: could not load advisor recs: %s", e)
        return []


# ── Capital calculator ────────────────────────────────────────────────────────

def calculate_required_capital(
    entry_amount: float,
    n_symbols: int,
    recs: list[dict],
) -> dict:
    """
    Worst-case capital needed: entry + DCA1 + DCA2 for each slot.
    Uses average DCA allocations from top N recommendations.
    """
    top = recs[:n_symbols] if recs else []
    if top:
        avg_alloc1 = sum(r.get("params", {}).get("dca_alloc_1", 150) for r in top) / len(top)
        avg_alloc2 = sum(r.get("params", {}).get("dca_alloc_2", 250) for r in top) / len(top)
    else:
        avg_alloc1, avg_alloc2 = 150.0, 250.0

    per_entry   = entry_amount
    per_dca1    = entry_amount * avg_alloc1 / 100
    per_dca2    = entry_amount * avg_alloc2 / 100
    per_symbol  = per_entry + per_dca1 + per_dca2
    total       = per_symbol * n_symbols

    return {
        "per_symbol_entry":  round(per_entry,  2),
        "per_symbol_dca1":   round(per_dca1,   2),
        "per_symbol_dca2":   round(per_dca2,   2),
        "per_symbol_total":  round(per_symbol, 2),
        "total_capital":     round(total,      2),
        "n_symbols":         n_symbols,
        "avg_alloc1_pct":    round(avg_alloc1, 1),
        "avg_alloc2_pct":    round(avg_alloc2, 1),
    }


# ── Position management ───────────────────────────────────────────────────────

def _open_position(
    db: Session,
    campaign: SmartCampaign,
    rec: dict,
    price: float,
) -> SmartPosition:
    params = rec.get("params", {})
    entry  = campaign.entry_amount_usdt
    qty    = entry / price

    pos = SmartPosition(
        campaign_id        = campaign.id,
        symbol             = rec["symbol"],
        entry_price        = price,
        entry_amount_usdt  = entry,
        qty                = qty,
        tp_pct             = params.get("tp_pct",      3.0),
        sl_pct             = params.get("sl_pct",     15.0),
        dca_drop_1         = params.get("dca_drop_1",  5.0),
        dca_drop_2         = params.get("dca_drop_2", 10.0),
        dca_alloc_1        = params.get("dca_alloc_1", 150.0),
        dca_alloc_2        = params.get("dca_alloc_2", 250.0),
        avg_price          = price,
        total_invested_usdt = entry,
        total_qty          = qty,
        current_price      = price,
        pnl_pct            = 0.0,
        pnl_usdt           = 0.0,
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    logger.info(
        "SmartCampaign %d: opened %s @ %.6f ($%.2f, TP+%.1f%% SL-%.1f%%)",
        campaign.id, rec["symbol"], price, entry, pos.tp_pct, pos.sl_pct,
    )
    return pos


def _close_position(
    db: Session,
    pos: SmartPosition,
    price: float,
    reason: str,
) -> None:
    pnl_pct  = (price - pos.avg_price) / pos.avg_price * 100
    pnl_usdt = (price - pos.avg_price) * pos.total_qty

    pos.status         = f"sold_{reason}"
    pos.closed_at      = datetime.utcnow()
    pos.close_reason   = reason
    pos.close_pnl_usdt = round(pnl_usdt, 4)
    pos.current_price  = price
    pos.pnl_pct        = round(pnl_pct, 3)
    pos.pnl_usdt       = round(pnl_usdt, 4)
    db.commit()
    logger.info(
        "SmartCampaign: closed %s via %s @ %.6f (PnL: %+.2f%% / $%+.4f)",
        pos.symbol, reason, price, pnl_pct, pnl_usdt,
    )


def _tick_position(db: Session, pos: SmartPosition, price: float) -> bool:
    """Update a position with latest price. Returns True if position was closed."""
    pos.current_price = price

    # ── DCA 1 ────────────────────────────────────────────────────────────
    if not pos.dca1_triggered:
        drop_from_entry = (price - pos.entry_price) / pos.entry_price * 100
        if drop_from_entry <= -pos.dca_drop_1:
            dca_amount = pos.entry_amount_usdt * (pos.dca_alloc_1 / 100)
            dca_qty    = dca_amount / price
            pos.dca1_triggered       = True
            pos.dca1_price           = price
            pos.dca1_qty             = dca_qty
            pos.total_invested_usdt += dca_amount
            pos.total_qty           += dca_qty
            pos.avg_price            = pos.total_invested_usdt / pos.total_qty
            logger.info("SmartCampaign: DCA1 triggered %s @ %.6f", pos.symbol, price)

    # ── DCA 2 ────────────────────────────────────────────────────────────
    if pos.dca1_triggered and not pos.dca2_triggered:
        drop_from_entry = (price - pos.entry_price) / pos.entry_price * 100
        if drop_from_entry <= -pos.dca_drop_2:
            dca_amount = pos.entry_amount_usdt * (pos.dca_alloc_2 / 100)
            dca_qty    = dca_amount / price
            pos.dca2_triggered       = True
            pos.dca2_price           = price
            pos.dca2_qty             = dca_qty
            pos.total_invested_usdt += dca_amount
            pos.total_qty           += dca_qty
            pos.avg_price            = pos.total_invested_usdt / pos.total_qty
            logger.info("SmartCampaign: DCA2 triggered %s @ %.6f", pos.symbol, price)

    # Refresh PnL after potential DCA
    pnl_pct  = (price - pos.avg_price) / pos.avg_price * 100
    pnl_usdt = (price - pos.avg_price) * pos.total_qty
    pos.pnl_pct  = round(pnl_pct,  3)
    pos.pnl_usdt = round(pnl_usdt, 4)

    # ── Take Profit ───────────────────────────────────────────────────────
    if pnl_pct >= pos.tp_pct:
        _close_position(db, pos, price, "tp")
        return True

    # ── Stop Loss ─────────────────────────────────────────────────────────
    if pnl_pct <= -pos.sl_pct:
        _close_position(db, pos, price, "sl")
        return True

    db.commit()
    return False


# ── Main cycle (called by scheduler) ─────────────────────────────────────────

def run_smart_cycle(db: Session) -> None:
    """Tick all active smart campaigns."""
    campaigns = db.query(SmartCampaign).filter(
        SmartCampaign.status.in_(["running", "stopped"])
    ).all()

    if not campaigns:
        return

    # Collect symbols needed
    all_symbols: set[str] = set()
    recs = get_advisor_recommendations()
    for r in recs:
        all_symbols.add(r["symbol"])
    for c in campaigns:
        for p in db.query(SmartPosition).filter(
            SmartPosition.campaign_id == c.id,
            SmartPosition.status == "active",
        ).all():
            all_symbols.add(p.symbol)

    if not all_symbols:
        return

    prices = get_prices(list(all_symbols))

    for campaign in campaigns:
        try:
            _process_campaign(db, campaign, prices, recs)
        except Exception as e:
            logger.error("SmartCampaign %d cycle error: %s", campaign.id, e)


def _process_campaign(
    db: Session,
    campaign: SmartCampaign,
    prices: dict,
    recs: list[dict],
) -> None:
    active = db.query(SmartPosition).filter(
        SmartPosition.campaign_id == campaign.id,
        SmartPosition.status == "active",
    ).all()

    # Tick each active position
    for pos in active:
        price = prices.get(pos.symbol)
        if price:
            _tick_position(db, pos, price)

    # Open new positions only when campaign is running
    if campaign.status != "running":
        return

    # Refresh active count after potential closes
    db.expire_all()
    active_symbols = {
        p.symbol for p in db.query(SmartPosition).filter(
            SmartPosition.campaign_id == campaign.id,
            SmartPosition.status == "active",
        ).all()
    }
    slots = campaign.max_symbols - len(active_symbols)
    if slots <= 0:
        return

    # Symbols active in ANY campaign (prevent duplicates across campaigns)
    all_active_symbols = {
        p.symbol for p in db.query(SmartPosition).filter(
            SmartPosition.status == "active",
        ).all()
    }

    # Pick BUY-signal candidates not already active in any campaign
    candidates = [r for r in recs if r["symbol"] not in all_active_symbols]
    opened = 0
    for rec in candidates:
        if opened >= slots:
            break
        price = prices.get(rec["symbol"])
        if not price:
            continue
        _open_position(db, campaign, rec, price)
        opened += 1


# ── Manual sell ───────────────────────────────────────────────────────────────

def manual_sell(db: Session, position_id: int) -> dict:
    pos = db.query(SmartPosition).filter(SmartPosition.id == position_id).first()
    if not pos or pos.status != "active":
        return {"ok": False, "error": "Position not found or not active"}
    prices = get_prices([pos.symbol])
    price = prices.get(pos.symbol)
    if not price:
        return {"ok": False, "error": "Could not fetch current price"}
    _close_position(db, pos, price, "manual")
    pnl = (price - pos.avg_price) / pos.avg_price * 100
    return {"ok": True, "pnl_pct": round(pnl, 2), "price": price}


# ── Campaign CRUD ─────────────────────────────────────────────────────────────

def create_campaign(db: Session, max_symbols: int, entry_amount: float) -> SmartCampaign:
    c = SmartCampaign(max_symbols=max_symbols, entry_amount_usdt=entry_amount)
    db.add(c)
    db.commit()
    db.refresh(c)
    logger.info("SmartCampaign created: id=%d max=%d entry=$%.2f", c.id, max_symbols, entry_amount)
    return c


def stop_campaign(db: Session, campaign_id: int) -> bool:
    c = db.query(SmartCampaign).filter(SmartCampaign.id == campaign_id).first()
    if not c:
        return False
    c.status = "stopped"
    db.commit()
    logger.info("SmartCampaign %d stopped", campaign_id)
    return True


def resume_campaign(db: Session, campaign_id: int) -> bool:
    c = db.query(SmartCampaign).filter(SmartCampaign.id == campaign_id).first()
    if not c:
        return False
    c.status = "running"
    db.commit()
    logger.info("SmartCampaign %d resumed", campaign_id)
    return True


def campaign_summary(db: Session, campaign: SmartCampaign) -> dict:
    positions = db.query(SmartPosition).filter(
        SmartPosition.campaign_id == campaign.id
    ).order_by(SmartPosition.created_at.desc()).all()

    active   = [p for p in positions if p.status == "active"]
    closed   = [p for p in positions if p.status != "active"]
    total_pnl = sum(p.close_pnl_usdt or 0 for p in closed)
    open_pnl  = sum(p.pnl_usdt or 0 for p in active)

    return {
        "id":             campaign.id,
        "status":         campaign.status,
        "max_symbols":    campaign.max_symbols,
        "entry_amount":   campaign.entry_amount_usdt,
        "active_count":   len(active),
        "closed_count":   len(closed),
        "total_pnl_usdt": round(total_pnl, 4),
        "open_pnl_usdt":  round(open_pnl,  4),
        "created_at":     campaign.created_at.strftime("%Y-%m-%d %H:%M") if campaign.created_at else "",
        "positions": [_pos_dict(p) for p in positions],
    }


def _pos_dict(p: SmartPosition) -> dict:
    return {
        "id":               p.id,
        "symbol":           p.symbol,
        "status":           p.status,
        "entry_price":      p.entry_price,
        "avg_price":        p.avg_price,
        "current_price":    p.current_price,
        "pnl_pct":          p.pnl_pct,
        "pnl_usdt":         p.pnl_usdt,
        "close_pnl_usdt":   p.close_pnl_usdt,
        "total_invested":   p.total_invested_usdt,
        "total_qty":        p.total_qty,
        "tp_pct":           p.tp_pct,
        "sl_pct":           p.sl_pct,
        "dca_drop_1":       p.dca_drop_1,
        "dca_drop_2":       p.dca_drop_2,
        "dca1_triggered":   p.dca1_triggered,
        "dca2_triggered":   p.dca2_triggered,
        "created_at":       p.created_at.strftime("%Y-%m-%d %H:%M") if p.created_at else "",
        "closed_at":        p.closed_at.strftime("%Y-%m-%d %H:%M") if p.closed_at else None,
        "close_reason":     p.close_reason,
    }
