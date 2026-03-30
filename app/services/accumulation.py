from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.paper_v2 import AccumulationPlan, AccumulationTrade, ActivityLog
from app.services.binance_live import (
    get_order_fee_usdt,
    get_usdt_free,
    place_limit_buy_quote,
    place_market_buy_quote,
    place_market_sell_qty,
)
from app.services.binance_public import get_prices


def _log(db: Session, mode: str, event: str, symbol: str, message: str) -> None:
    prefix = "LIVE_" if mode == "live" else ""
    db.add(ActivityLog(event_type=f"{prefix}{event}", symbol=symbol or "-", message=message))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _recalc_used_capital(plan: AccumulationPlan) -> None:
    plan.used_capital_usdt = max(0.0, float(plan.avg_entry_price or 0.0) * float(plan.coin_qty or 0.0))


def _record_trade(
    db: Session,
    plan: AccumulationPlan,
    side: str,
    price: float,
    qty: float,
    quote_usdt: float,
    fee_usdt: float,
    reason: str,
    pnl_usdt: float | None = None,
) -> None:
    db.add(
        AccumulationTrade(
            plan_id=plan.id,
            side=side,
            price=float(price),
            qty=float(qty),
            quote_usdt=float(quote_usdt),
            fee_usdt=float(fee_usdt),
            pnl_usdt=(float(pnl_usdt) if pnl_usdt is not None else None),
            reason=reason,
            created_at=datetime.utcnow(),
        )
    )


def _paper_buy(db: Session, plan: AccumulationPlan, price: float, usdt: float, reason: str) -> bool:
    usdt = float(usdt)
    if price <= 0 or usdt < float(plan.min_order_usdt or 5.0):
        return False
    if float(plan.reserved_cash_usdt or 0.0) + 1e-9 < usdt:
        return False
    qty = usdt / float(price)
    if qty <= 0:
        return False
    prev_qty = float(plan.coin_qty or 0.0)
    prev_cost = float(plan.avg_entry_price or 0.0) * prev_qty
    new_qty = prev_qty + qty
    new_cost = prev_cost + usdt
    plan.coin_qty = new_qty
    plan.avg_entry_price = (new_cost / new_qty) if new_qty > 0 else 0.0
    plan.reserved_cash_usdt = float(plan.reserved_cash_usdt or 0.0) - usdt
    plan.total_bought_qty = float(plan.total_bought_qty or 0.0) + qty
    if float(plan.initial_coin_qty or 0.0) <= 0:
        plan.initial_coin_qty = qty
    plan.buy_count = int(plan.buy_count or 0) + 1
    plan.last_action_at = datetime.utcnow()
    _recalc_used_capital(plan)
    _record_trade(db, plan, "BUY", price, qty, usdt, 0.0, reason)
    _log(db, plan.mode, "ACC_BUY", plan.symbol, f"Plan={plan.name} | reason={reason} | price={price:.6f} | usdt={usdt:.2f} | qty={qty:.8f}")
    return True


def _paper_sell(db: Session, plan: AccumulationPlan, price: float, qty: float, reason: str) -> bool:
    qty = min(float(qty), float(plan.coin_qty or 0.0))
    if price <= 0 or qty <= 0:
        return False
    gross = qty * float(price)
    avg = float(plan.avg_entry_price or 0.0)
    cost = qty * avg
    pnl = gross - cost
    remaining = float(plan.coin_qty or 0.0) - qty
    plan.coin_qty = max(0.0, remaining)
    plan.reserved_cash_usdt = float(plan.reserved_cash_usdt or 0.0) + gross
    plan.realized_pnl_usdt = float(plan.realized_pnl_usdt or 0.0) + pnl
    plan.total_sold_qty = float(plan.total_sold_qty or 0.0) + qty
    plan.sell_count = int(plan.sell_count or 0) + 1
    plan.last_action_at = datetime.utcnow()
    if plan.coin_qty <= 1e-12:
        plan.coin_qty = 0.0
        plan.avg_entry_price = 0.0
    _recalc_used_capital(plan)
    _record_trade(db, plan, "SELL", price, qty, gross, 0.0, reason, pnl_usdt=pnl)
    _log(db, plan.mode, "ACC_SELL", plan.symbol, f"Plan={plan.name} | reason={reason} | price={price:.6f} | qty={qty:.8f} | pnl={pnl:+.2f}")
    return True


def _live_buy(db: Session, plan: AccumulationPlan, usdt: float, reason: str) -> bool:
    usdt = float(usdt)
    if usdt < float(plan.min_order_usdt or 5.0):
        return False
    if float(plan.reserved_cash_usdt or 0.0) + 1e-9 < usdt:
        return False
    if get_usdt_free() + 1e-9 < usdt:
        _log(db, plan.mode, "ACC_SKIP", plan.symbol, f"Plan={plan.name} | reason=insufficient_exchange_cash | need={usdt:.2f}")
        return False
    try:
        order = place_limit_buy_quote(
            plan.symbol,
            usdt,
            price_buffer_pct=max(0.0, float(settings.live_entry_limit_buffer_pct)),
        )
        if float(order.get("quote_qty", 0.0)) <= 0.0:
            if settings.live_entry_limit_fallback_market:
                order = place_market_buy_quote(plan.symbol, usdt)
            else:
                _log(db, plan.mode, "ACC_SKIP", plan.symbol, f"Plan={plan.name} | reason=limit_no_fill")
                return False
    except Exception:
        if settings.live_entry_limit_fallback_market:
            order = place_market_buy_quote(plan.symbol, usdt)
        else:
            _log(db, plan.mode, "ACC_SKIP", plan.symbol, f"Plan={plan.name} | reason=limit_failed_no_fallback")
            return False

    qty = float(order.get("executed_qty", 0.0))
    spent = float(order.get("quote_qty", 0.0))
    price = float(order.get("avg_price", 0.0))
    if qty <= 0 or spent <= 0 or price <= 0:
        return False
    order_id = int(float(order.get("order_id", 0.0) or 0.0))
    fee_usdt = 0.0
    if order_id > 0:
        try:
            fee_usdt = float(get_order_fee_usdt(plan.symbol, order_id))
        except Exception:
            fee_usdt = 0.0
    effective_spent = spent + fee_usdt
    prev_qty = float(plan.coin_qty or 0.0)
    prev_cost = float(plan.avg_entry_price or 0.0) * prev_qty
    new_qty = prev_qty + qty
    new_cost = prev_cost + effective_spent
    plan.coin_qty = new_qty
    plan.avg_entry_price = (new_cost / new_qty) if new_qty > 0 else 0.0
    plan.reserved_cash_usdt = float(plan.reserved_cash_usdt or 0.0) - effective_spent
    plan.realized_fees_usdt = float(plan.realized_fees_usdt or 0.0) + fee_usdt
    plan.total_bought_qty = float(plan.total_bought_qty or 0.0) + qty
    if float(plan.initial_coin_qty or 0.0) <= 0:
        plan.initial_coin_qty = qty
    plan.buy_count = int(plan.buy_count or 0) + 1
    plan.last_action_at = datetime.utcnow()
    _recalc_used_capital(plan)
    _record_trade(db, plan, "BUY", price, qty, spent, fee_usdt, reason)
    _log(
        db,
        plan.mode,
        "ACC_BUY",
        plan.symbol,
        (
            f"Plan={plan.name} | reason={reason} | price={price:.6f} | spent={spent:.2f} "
            f"| fee={fee_usdt:.4f} | qty={qty:.8f}"
        ),
    )
    return True


def _live_sell(db: Session, plan: AccumulationPlan, qty: float, reason: str) -> bool:
    qty = min(float(qty), float(plan.coin_qty or 0.0))
    if qty <= 0:
        return False
    try:
        order = place_market_sell_qty(plan.symbol, qty)
    except Exception as e:
        _log(db, plan.mode, "ACC_SELL_FAIL", plan.symbol, f"Plan={plan.name} | reason={reason} | error={e}")
        return False

    exec_qty = float(order.get("executed_qty", 0.0))
    quote = float(order.get("quote_qty", 0.0))
    price = float(order.get("avg_price", 0.0))
    if exec_qty <= 0 or quote <= 0:
        return False
    order_id = int(float(order.get("order_id", 0.0) or 0.0))
    fee_usdt = 0.0
    if order_id > 0:
        try:
            fee_usdt = float(get_order_fee_usdt(plan.symbol, order_id))
        except Exception:
            fee_usdt = 0.0
    net_quote = quote - fee_usdt
    avg = float(plan.avg_entry_price or 0.0)
    cost = exec_qty * avg
    pnl = net_quote - cost
    plan.coin_qty = max(0.0, float(plan.coin_qty or 0.0) - exec_qty)
    plan.reserved_cash_usdt = float(plan.reserved_cash_usdt or 0.0) + net_quote
    plan.realized_pnl_usdt = float(plan.realized_pnl_usdt or 0.0) + pnl
    plan.realized_fees_usdt = float(plan.realized_fees_usdt or 0.0) + fee_usdt
    plan.total_sold_qty = float(plan.total_sold_qty or 0.0) + exec_qty
    plan.sell_count = int(plan.sell_count or 0) + 1
    plan.last_action_at = datetime.utcnow()
    if plan.coin_qty <= 1e-12:
        plan.coin_qty = 0.0
        plan.avg_entry_price = 0.0
    _recalc_used_capital(plan)
    _record_trade(db, plan, "SELL", (price if price > 0 else net_quote / max(exec_qty, 1e-12)), exec_qty, quote, fee_usdt, reason, pnl_usdt=pnl)
    _log(
        db,
        plan.mode,
        "ACC_SELL",
        plan.symbol,
        (
            f"Plan={plan.name} | reason={reason} | qty={exec_qty:.8f} | quote={quote:.2f} "
            f"| fee={fee_usdt:.4f} | pnl={pnl:+.2f}"
        ),
    )
    return True


def create_plan(
    db: Session,
    *,
    mode: str,
    name: str,
    symbol: str,
    total_capital_usdt: float,
    initial_entry_usdt: float,
    dca_drop_pct: float,
    dca_allocation_pct: float,
    partial_tp_pct: float,
    partial_sell_pct: float,
    min_order_usdt: float = 5.0,
) -> AccumulationPlan:
    total_capital_usdt = max(1.0, float(total_capital_usdt))
    initial_entry_usdt = max(1.0, float(initial_entry_usdt))
    plan = AccumulationPlan(
        name=(name.strip() or "Accumulation Plan"),
        mode=("live" if mode == "live" else "paper"),
        symbol=str(symbol or "").upper().strip(),
        status="active",
        total_capital_usdt=total_capital_usdt,
        initial_entry_usdt=min(initial_entry_usdt, total_capital_usdt),
        dca_drop_pct=max(0.2, float(dca_drop_pct)),
        dca_allocation_pct=max(1.0, float(dca_allocation_pct)),
        partial_tp_pct=max(0.1, float(partial_tp_pct)),
        partial_sell_pct=_clamp(float(partial_sell_pct), 1.0, 95.0),
        min_order_usdt=max(1.0, float(min_order_usdt)),
        reserved_cash_usdt=total_capital_usdt,
    )
    db.add(plan)
    db.flush()
    _log(db, plan.mode, "ACC_PLAN_CREATE", plan.symbol, f"Plan={plan.name} | capital={plan.total_capital_usdt:.2f} | entry={plan.initial_entry_usdt:.2f}")
    return plan


def _run_plan_cycle(db: Session, plan: AccumulationPlan, price: float) -> bool:
    changed = False
    plan.last_price = float(price)
    if plan.status != "active":
        return False

    # Initial entry when no inventory.
    if float(plan.coin_qty or 0.0) <= 0:
        init_amt = min(float(plan.initial_entry_usdt), float(plan.reserved_cash_usdt))
        if init_amt >= float(plan.min_order_usdt or 5.0):
            ok = _live_buy(db, plan, init_amt, "initial_entry") if plan.mode == "live" else _paper_buy(db, plan, price, init_amt, "initial_entry")
            changed = changed or ok
        return changed

    avg = float(plan.avg_entry_price or 0.0)
    if avg <= 0:
        return changed

    # Sell partial on rebound.
    sell_trigger = avg * (1.0 + (float(plan.partial_tp_pct) / 100.0))
    if price >= sell_trigger:
        sell_qty = float(plan.coin_qty) * (float(plan.partial_sell_pct) / 100.0)
        if sell_qty > 0:
            ok = _live_sell(db, plan, sell_qty, "partial_take_profit") if plan.mode == "live" else _paper_sell(db, plan, price, sell_qty, "partial_take_profit")
            changed = changed or ok
            if ok:
                return True

    # Buy dip under average (DCA).
    buy_trigger = avg * (1.0 - (float(plan.dca_drop_pct) / 100.0))
    if price <= buy_trigger:
        dca_amt = float(plan.initial_entry_usdt) * (float(plan.dca_allocation_pct) / 100.0)
        dca_amt = min(dca_amt, float(plan.reserved_cash_usdt))
        if dca_amt >= float(plan.min_order_usdt or 5.0):
            ok = _live_buy(db, plan, dca_amt, "dca_buy") if plan.mode == "live" else _paper_buy(db, plan, price, dca_amt, "dca_buy")
            changed = changed or ok
    return changed


def run_accumulation_cycle(db: Session, mode: str) -> None:
    plans = (
        db.query(AccumulationPlan)
        .filter(AccumulationPlan.mode == ("live" if mode == "live" else "paper"))
        .all()
    )
    if not plans:
        return
    symbols = sorted({p.symbol for p in plans if p.symbol})
    prices = get_prices(symbols) if symbols else {}
    changed = False
    for plan in plans:
        px = float(prices.get(plan.symbol, 0.0))
        if px <= 0:
            continue
        changed = _run_plan_cycle(db, plan, px) or changed
    if changed:
        db.commit()


def toggle_plan_status(db: Session, plan: AccumulationPlan) -> str:
    plan.status = "paused" if str(plan.status) == "active" else "active"
    plan.last_action_at = datetime.utcnow()
    _log(db, plan.mode, "ACC_PLAN_STATUS", plan.symbol, f"Plan={plan.name} | status={plan.status}")
    return str(plan.status)


def manual_partial_sell(db: Session, plan: AccumulationPlan, sell_pct: float) -> bool:
    if float(plan.coin_qty or 0.0) <= 0:
        return False
    pct = _clamp(float(sell_pct), 0.1, 95.0)
    qty = float(plan.coin_qty) * (pct / 100.0)
    if qty <= 0:
        return False
    if plan.mode == "live":
        return _live_sell(db, plan, qty, f"manual_partial_sell_{pct:.2f}%")
    price = float(plan.last_price or 0.0)
    if price <= 0:
        px = get_prices([plan.symbol])
        price = float(px.get(plan.symbol, 0.0))
    if price <= 0:
        return False
    return _paper_sell(db, plan, price, qty, f"manual_partial_sell_{pct:.2f}%")
