"""
Live Smart Campaign service — real Binance order execution.

Every decision is logged with full context: why a buy was placed,
why a symbol was skipped, why DCA was triggered or skipped.

Safety rules:
  - Check USDT balance before every BUY (open + DCA)
  - Use actual filled qty/price from Binance order response
  - Separate models from paper trading (no risk of mixing)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.models.live_smart_campaign import (
    LiveSmartCampaign,
    LiveSmartCampaignLog,
    LiveSmartPosition,
)
from app.services.binance_live import (
    get_usdt_free,
    is_configured,
    place_market_buy_quote,
    place_market_sell_qty,
)
from app.services.binance_public import get_prices

logger = logging.getLogger(__name__)

# Debounce: only log SKIP_BALANCE once per 5 minutes per campaign
_last_balance_warn: dict[int, datetime] = {}


# ── Logging helper ────────────────────────────────────────────────────────────

def _log(
    db: Session,
    event: str,
    message: str,
    campaign_id: Optional[int] = None,
    position_id: Optional[int] = None,
    symbol: Optional[str] = None,
    price: Optional[float] = None,
    amount_usdt: Optional[float] = None,
    pnl_usdt: Optional[float] = None,
    pnl_pct: Optional[float] = None,
    balance_before: Optional[float] = None,
    balance_after: Optional[float] = None,
) -> None:
    entry = LiveSmartCampaignLog(
        campaign_id=campaign_id,
        position_id=position_id,
        symbol=symbol,
        event=event,
        message=message,
        price=price,
        amount_usdt=amount_usdt,
        pnl_usdt=pnl_usdt,
        pnl_pct=pnl_pct,
        balance_before=balance_before,
        balance_after=balance_after,
    )
    db.add(entry)
    db.flush()
    logger.info("[LiveSmart] %s | %s", event, message)


# ── Advisor data ───────────────────────────────────────────────────────────────

def get_advisor_recommendations() -> list[dict]:
    try:
        from advisor.config import REPORT_DIR
        latest = Path(REPORT_DIR) / "latest.json"
        if not latest.exists():
            return []
        with open(latest) as f:
            data = json.load(f)
        return data.get("recommendations", [])
    except Exception as e:
        logger.warning("LiveSmart: could not load advisor recs: %s", e)
        return []


# ── Open position ─────────────────────────────────────────────────────────────

def _open_live_position(
    db: Session,
    campaign: LiveSmartCampaign,
    rec: dict,
    balance_before: float,
) -> Optional[LiveSmartPosition]:
    symbol = rec["symbol"]
    params = rec.get("params", {})
    entry  = campaign.entry_amount_usdt

    # Safety: re-check balance right before placing order
    usdt_free = get_usdt_free()
    if usdt_free < entry:
        _log(
            db, "SKIP_BALANCE",
            f"SKIP {symbol} — balance too low: ${usdt_free:.2f} free < ${entry:.2f} needed",
            campaign_id=campaign.id, symbol=symbol,
            balance_before=usdt_free,
        )
        db.commit()
        return None

    # Place real market buy
    try:
        order = place_market_buy_quote(symbol, entry)
    except Exception as e:
        _log(
            db, "ERROR",
            f"BUY order failed for {symbol}: {e}",
            campaign_id=campaign.id, symbol=symbol,
            balance_before=usdt_free,
        )
        db.commit()
        return None

    filled_qty   = order["executed_qty"]
    avg_price    = order["avg_price"]
    quote_spent  = order["quote_qty"]

    if filled_qty <= 0 or avg_price <= 0:
        _log(
            db, "ERROR",
            f"BUY order for {symbol} returned zero qty/price: {order}",
            campaign_id=campaign.id, symbol=symbol,
        )
        db.commit()
        return None

    balance_after = get_usdt_free()

    pos = LiveSmartPosition(
        campaign_id         = campaign.id,
        symbol              = symbol,
        entry_price         = avg_price,
        entry_amount_usdt   = quote_spent,
        qty                 = filled_qty,
        binance_order_id    = str(order.get("order_id", "")),
        tp_pct              = params.get("tp_pct",      3.0),
        sl_pct              = params.get("sl_pct",     15.0),
        dca_drop_1          = params.get("dca_drop_1",  5.0),
        dca_drop_2          = params.get("dca_drop_2", 10.0),
        dca_alloc_1         = params.get("dca_alloc_1", 150.0),
        dca_alloc_2         = params.get("dca_alloc_2", 250.0),
        avg_price           = avg_price,
        total_invested_usdt = quote_spent,
        total_qty           = filled_qty,
        current_price       = avg_price,
        pnl_pct             = 0.0,
        pnl_usdt            = 0.0,
    )
    db.add(pos)
    db.flush()

    signal  = rec.get("signal", "?")
    ml_prob = rec.get("ml_prob", 0.0)
    _log(
        db, "OPEN",
        f"✅ BUY {symbol} ${quote_spent:.2f} @ ${avg_price:.8g} | "
        f"Signal: {signal} ({ml_prob*100:.0f}%) | "
        f"TP: +{pos.tp_pct}% SL: -{pos.sl_pct}% | "
        f"Balance: ${balance_before:.2f} → ${balance_after:.2f}",
        campaign_id=campaign.id,
        position_id=pos.id,
        symbol=symbol,
        price=avg_price,
        amount_usdt=quote_spent,
        balance_before=balance_before,
        balance_after=balance_after,
    )
    db.commit()
    logger.info(
        "LiveSmart %d: opened %s @ %.8g ($%.2f) TP+%.1f%% SL-%.1f%%",
        campaign.id, symbol, avg_price, quote_spent, pos.tp_pct, pos.sl_pct,
    )
    return pos


# ── Close position ────────────────────────────────────────────────────────────

def _close_live_position(
    db: Session,
    pos: LiveSmartPosition,
    price: float,
    reason: str,
) -> None:
    balance_before = get_usdt_free()

    # Place real market sell
    try:
        order = place_market_sell_qty(pos.symbol, pos.total_qty)
        sell_qty   = order["executed_qty"]
        sell_price = order["avg_price"] if order["avg_price"] > 0 else price
        proceeds   = order["quote_qty"]
    except Exception as e:
        _log(
            db, "ERROR",
            f"SELL order failed for {pos.symbol}: {e}",
            campaign_id=pos.campaign_id, position_id=pos.id, symbol=pos.symbol,
        )
        db.commit()
        return

    pnl_usdt = proceeds - pos.total_invested_usdt
    pnl_pct  = pnl_usdt / pos.total_invested_usdt * 100 if pos.total_invested_usdt else 0.0
    balance_after = get_usdt_free()

    pos.status         = f"sold_{reason}"
    pos.closed_at      = datetime.utcnow()
    pos.close_reason   = reason
    pos.close_price    = sell_price
    pos.close_pnl_usdt = round(pnl_usdt, 4)
    pos.current_price  = sell_price
    pos.pnl_pct        = round(pnl_pct, 3)
    pos.pnl_usdt       = round(pnl_usdt, 4)

    icon = {"tp": "✅", "sl": "❌", "manual": "⊘"}.get(reason, "•")
    _log(
        db, f"CLOSE_{reason.upper()}",
        f"{icon} SELL {pos.symbol} ({reason.upper()}) @ ${sell_price:.8g} | "
        f"{pnl_pct:+.2f}% ({pnl_usdt:+.4f} USDT) | "
        f"Invested: ${pos.total_invested_usdt:.2f} | Proceeds: ${proceeds:.2f} | "
        f"Balance: ${balance_before:.2f} → ${balance_after:.2f}",
        campaign_id=pos.campaign_id, position_id=pos.id, symbol=pos.symbol,
        price=sell_price, pnl_usdt=round(pnl_usdt, 4), pnl_pct=round(pnl_pct, 3),
        balance_before=balance_before, balance_after=balance_after,
    )
    db.commit()


# ── Tick position (TP / SL / DCA) ────────────────────────────────────────────

def _tick_live_position(db: Session, pos: LiveSmartPosition, price: float) -> bool:
    """Check TP/SL/DCA for a live position. Returns True if closed."""
    pos.current_price = price

    # ── DCA 1 ─────────────────────────────────────────────────────────────
    if not pos.dca1_triggered and not pos.dca1_skipped:
        drop = (price - pos.entry_price) / pos.entry_price * 100
        if drop <= -pos.dca_drop_1:
            dca_amount = pos.entry_amount_usdt * (pos.dca_alloc_1 / 100)
            usdt_free  = get_usdt_free()
            if usdt_free < dca_amount:
                pos.dca1_skipped = True
                _log(
                    db, "DCA_SKIPPED",
                    f"⚠️ SKIP DCA1 {pos.symbol} — balance ${usdt_free:.2f} < ${dca_amount:.2f} needed "
                    f"(drop was {drop:.2f}%)",
                    campaign_id=pos.campaign_id, position_id=pos.id, symbol=pos.symbol,
                    price=price, amount_usdt=dca_amount, balance_before=usdt_free,
                )
            else:
                try:
                    order = place_market_buy_quote(pos.symbol, dca_amount)
                    dca_qty   = order["executed_qty"]
                    dca_price = order["avg_price"]
                    dca_spent = order["quote_qty"]
                    balance_after = get_usdt_free()

                    pos.dca1_triggered       = True
                    pos.dca1_price           = dca_price
                    pos.dca1_qty             = dca_qty
                    pos.dca1_order_id        = str(order.get("order_id", ""))
                    pos.total_invested_usdt += dca_spent
                    pos.total_qty           += dca_qty
                    pos.avg_price            = pos.total_invested_usdt / pos.total_qty

                    _log(
                        db, "DCA1",
                        f"🔄 DCA1 {pos.symbol} ${dca_spent:.2f} @ ${dca_price:.8g} | "
                        f"Drop: {drop:.2f}% from entry | New avg: ${pos.avg_price:.8g} | "
                        f"Balance: ${usdt_free:.2f} → ${balance_after:.2f}",
                        campaign_id=pos.campaign_id, position_id=pos.id, symbol=pos.symbol,
                        price=dca_price, amount_usdt=dca_spent,
                        balance_before=usdt_free, balance_after=balance_after,
                    )
                    logger.info("LiveSmart: DCA1 %s @ %.8g", pos.symbol, dca_price)
                except Exception as e:
                    pos.dca1_skipped = True
                    _log(
                        db, "ERROR",
                        f"DCA1 order failed for {pos.symbol}: {e}",
                        campaign_id=pos.campaign_id, position_id=pos.id, symbol=pos.symbol,
                    )

    # ── DCA 2 ─────────────────────────────────────────────────────────────
    if pos.dca1_triggered and not pos.dca2_triggered and not pos.dca2_skipped:
        drop = (price - pos.entry_price) / pos.entry_price * 100
        if drop <= -pos.dca_drop_2:
            dca_amount = pos.entry_amount_usdt * (pos.dca_alloc_2 / 100)
            usdt_free  = get_usdt_free()
            if usdt_free < dca_amount:
                pos.dca2_skipped = True
                _log(
                    db, "DCA_SKIPPED",
                    f"⚠️ SKIP DCA2 {pos.symbol} — balance ${usdt_free:.2f} < ${dca_amount:.2f} needed "
                    f"(drop was {drop:.2f}%)",
                    campaign_id=pos.campaign_id, position_id=pos.id, symbol=pos.symbol,
                    price=price, amount_usdt=dca_amount, balance_before=usdt_free,
                )
            else:
                try:
                    order = place_market_buy_quote(pos.symbol, dca_amount)
                    dca_qty   = order["executed_qty"]
                    dca_price = order["avg_price"]
                    dca_spent = order["quote_qty"]
                    balance_after = get_usdt_free()

                    pos.dca2_triggered       = True
                    pos.dca2_price           = dca_price
                    pos.dca2_qty             = dca_qty
                    pos.dca2_order_id        = str(order.get("order_id", ""))
                    pos.total_invested_usdt += dca_spent
                    pos.total_qty           += dca_qty
                    pos.avg_price            = pos.total_invested_usdt / pos.total_qty

                    _log(
                        db, "DCA2",
                        f"🔄 DCA2 {pos.symbol} ${dca_spent:.2f} @ ${dca_price:.8g} | "
                        f"Drop: {drop:.2f}% from entry | New avg: ${pos.avg_price:.8g} | "
                        f"Balance: ${usdt_free:.2f} → ${balance_after:.2f}",
                        campaign_id=pos.campaign_id, position_id=pos.id, symbol=pos.symbol,
                        price=dca_price, amount_usdt=dca_spent,
                        balance_before=usdt_free, balance_after=balance_after,
                    )
                    logger.info("LiveSmart: DCA2 %s @ %.8g", pos.symbol, dca_price)
                except Exception as e:
                    pos.dca2_skipped = True
                    _log(
                        db, "ERROR",
                        f"DCA2 order failed for {pos.symbol}: {e}",
                        campaign_id=pos.campaign_id, position_id=pos.id, symbol=pos.symbol,
                    )

    # ── Refresh PnL ────────────────────────────────────────────────────────
    pnl_pct  = (price - pos.avg_price) / pos.avg_price * 100
    pnl_usdt = (price - pos.avg_price) * pos.total_qty
    pos.pnl_pct  = round(pnl_pct,  3)
    pos.pnl_usdt = round(pnl_usdt, 4)

    # ── Take Profit ────────────────────────────────────────────────────────
    if pnl_pct >= pos.tp_pct:
        _close_live_position(db, pos, price, "tp")
        return True

    # ── Stop Loss ──────────────────────────────────────────────────────────
    if pnl_pct <= -pos.sl_pct:
        _close_live_position(db, pos, price, "sl")
        return True

    db.commit()
    return False


# ── Main cycle ────────────────────────────────────────────────────────────────

def run_live_smart_cycle(db: Session) -> None:
    """Tick all active live campaigns. Called by scheduler every 10s."""
    if not is_configured():
        return

    campaigns = db.query(LiveSmartCampaign).filter(
        LiveSmartCampaign.status.in_(["running", "stopped"])
    ).all()
    if not campaigns:
        return

    all_symbols: set[str] = set()
    recs = get_advisor_recommendations()
    for r in recs:
        all_symbols.add(r["symbol"])
    for c in campaigns:
        for p in db.query(LiveSmartPosition).filter(
            LiveSmartPosition.campaign_id == c.id,
            LiveSmartPosition.status == "active",
        ).all():
            all_symbols.add(p.symbol)

    if not all_symbols:
        return

    prices = get_prices(list(all_symbols))

    for campaign in campaigns:
        try:
            _process_live_campaign(db, campaign, prices, recs)
        except Exception as e:
            logger.error("LiveSmart %d cycle error: %s", campaign.id, e)


def _process_live_campaign(
    db: Session,
    campaign: LiveSmartCampaign,
    prices: dict,
    recs: list[dict],
) -> None:
    active = db.query(LiveSmartPosition).filter(
        LiveSmartPosition.campaign_id == campaign.id,
        LiveSmartPosition.status == "active",
    ).all()

    for pos in active:
        price = prices.get(pos.symbol)
        if price:
            _tick_live_position(db, pos, price)

    if campaign.status != "running":
        return

    db.expire_all()
    active_symbols = {
        p.symbol for p in db.query(LiveSmartPosition).filter(
            LiveSmartPosition.status == "active",
        ).all()
    }

    slots = campaign.max_symbols - len(
        [p for p in db.query(LiveSmartPosition).filter(
            LiveSmartPosition.campaign_id == campaign.id,
            LiveSmartPosition.status == "active",
        ).all()]
    )
    if slots <= 0:
        return

    usdt_free = get_usdt_free()
    candidates = [r for r in recs if r["symbol"] not in active_symbols]

    opened = 0
    for rec in candidates:
        if opened >= slots:
            break
        symbol = rec["symbol"]
        price  = prices.get(symbol)
        if not price:
            continue

        # Only open BUY signals — skip others silently (no log spam every 10s)
        signal = rec.get("signal", "SKIP")
        if signal not in ("BUY",):
            continue

        usdt_free = get_usdt_free()
        if usdt_free < campaign.entry_amount_usdt:
            # Debounce: only log once per 5 min per campaign
            now = datetime.utcnow()
            last = _last_balance_warn.get(campaign.id)
            if not last or (now - last) > timedelta(minutes=5):
                _last_balance_warn[campaign.id] = now
                _log(
                    db, "SKIP_BALANCE",
                    f"⚠️ SKIP — insufficient balance: ${usdt_free:.2f} free < "
                    f"${campaign.entry_amount_usdt:.2f} needed",
                    campaign_id=campaign.id, symbol=symbol, price=price,
                    balance_before=usdt_free,
                )
                db.commit()
            break  # No point checking more symbols — no balance

        pos = _open_live_position(db, campaign, rec, balance_before=usdt_free)
        if pos:
            opened += 1


# ── Manual sell ───────────────────────────────────────────────────────────────

def manual_sell_live(db: Session, position_id: int) -> dict:
    pos = db.query(LiveSmartPosition).filter(LiveSmartPosition.id == position_id).first()
    if not pos or pos.status != "active":
        return {"ok": False, "error": "Position not found or not active"}
    prices = get_prices([pos.symbol])
    price = prices.get(pos.symbol)
    if not price:
        return {"ok": False, "error": "Could not fetch current price"}
    _close_live_position(db, pos, price, "manual")
    pnl = pos.close_pnl_usdt or 0.0
    return {"ok": True, "pnl_usdt": round(pnl, 4), "price": price}


# ── Campaign CRUD ─────────────────────────────────────────────────────────────

def create_live_campaign(
    db: Session, max_symbols: int, entry_amount: float
) -> dict:
    """Create campaign only if balance is sufficient."""
    if not is_configured():
        return {"ok": False, "error": "Binance API keys not configured"}

    usdt_free = get_usdt_free()
    needed    = entry_amount * max_symbols

    # Warn if balance < entry for even 1 position
    if usdt_free < entry_amount:
        return {
            "ok": False,
            "error": f"Insufficient balance: ${usdt_free:.2f} free, need at least ${entry_amount:.2f} to open 1 position",
        }

    c = LiveSmartCampaign(max_symbols=max_symbols, entry_amount_usdt=entry_amount)
    db.add(c)
    db.flush()

    _log(
        db, "CAMPAIGN_CREATED",
        f"Campaign #{c.id} created | max={max_symbols} symbols | entry=${entry_amount} | "
        f"Balance: ${usdt_free:.2f} (worst-case reserve: ${needed:.2f})",
        campaign_id=c.id,
        balance_before=usdt_free,
    )
    db.commit()
    db.refresh(c)
    logger.info(
        "LiveSmart campaign created: id=%d max=%d entry=$%.2f balance=$%.2f",
        c.id, max_symbols, entry_amount, usdt_free,
    )
    return {"ok": True, "campaign": c}


def stop_live_campaign(db: Session, campaign_id: int) -> bool:
    c = db.query(LiveSmartCampaign).filter(LiveSmartCampaign.id == campaign_id).first()
    if not c:
        return False
    c.status = "stopped"
    _log(db, "CAMPAIGN_STOPPED", f"Campaign #{campaign_id} stopped", campaign_id=campaign_id)
    db.commit()
    return True


def resume_live_campaign(db: Session, campaign_id: int) -> bool:
    c = db.query(LiveSmartCampaign).filter(LiveSmartCampaign.id == campaign_id).first()
    if not c:
        return False
    c.status = "running"
    _log(db, "CAMPAIGN_RESUMED", f"Campaign #{campaign_id} resumed", campaign_id=campaign_id)
    db.commit()
    return True


# ── Summary helpers ───────────────────────────────────────────────────────────

def live_campaign_summary(db: Session, campaign: LiveSmartCampaign) -> dict:
    positions = db.query(LiveSmartPosition).filter(
        LiveSmartPosition.campaign_id == campaign.id
    ).order_by(LiveSmartPosition.created_at.desc()).all()

    active = [p for p in positions if p.status == "active"]
    closed = [p for p in positions if p.status != "active"]
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
        "positions":      [_live_pos_dict(p) for p in positions],
    }


def _live_pos_dict(p: LiveSmartPosition) -> dict:
    return {
        "id":              p.id,
        "symbol":          p.symbol,
        "status":          p.status,
        "entry_price":     p.entry_price,
        "avg_price":       p.avg_price,
        "current_price":   p.current_price,
        "pnl_pct":         p.pnl_pct,
        "pnl_usdt":        p.pnl_usdt,
        "close_pnl_usdt":  p.close_pnl_usdt,
        "close_price":     p.close_price,
        "total_invested":  p.total_invested_usdt,
        "total_qty":       p.total_qty,
        "tp_pct":          p.tp_pct,
        "sl_pct":          p.sl_pct,
        "dca_drop_1":      p.dca_drop_1,
        "dca_drop_2":      p.dca_drop_2,
        "dca1_triggered":  p.dca1_triggered,
        "dca1_skipped":    p.dca1_skipped,
        "dca2_triggered":  p.dca2_triggered,
        "dca2_skipped":    p.dca2_skipped,
        "created_at":      p.created_at.strftime("%Y-%m-%d %H:%M") if p.created_at else "",
        "closed_at":       p.closed_at.strftime("%Y-%m-%d %H:%M") if p.closed_at else None,
        "close_reason":    p.close_reason,
        "binance_order_id": p.binance_order_id,
    }


def get_recent_logs(db: Session, campaign_id: Optional[int] = None, limit: int = 100) -> list[dict]:
    q = db.query(LiveSmartCampaignLog)
    if campaign_id:
        q = q.filter(LiveSmartCampaignLog.campaign_id == campaign_id)
    logs = q.order_by(LiveSmartCampaignLog.created_at.desc()).limit(limit).all()
    return [
        {
            "id":             l.id,
            "event":          l.event,
            "symbol":         l.symbol,
            "message":        l.message,
            "price":          l.price,
            "amount_usdt":    l.amount_usdt,
            "pnl_usdt":       l.pnl_usdt,
            "pnl_pct":        l.pnl_pct,
            "balance_before": l.balance_before,
            "balance_after":  l.balance_after,
            "created_at":     l.created_at.strftime("%Y-%m-%d %H:%M:%S") if l.created_at else "",
        }
        for l in logs
    ]
