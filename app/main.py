import logging
import threading
import time
import json
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy import desc
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.models.paper_v2 import (
    AccumulationPlan,
    AccumulationTrade,
    ActivityLog,
    AppSetting,
    Campaign,
    DcaRule,
    Position,
    PositionDcaState,
)
from app.services.binance_public import get_prices, search_symbols
from app.services.binance_live import (
    cancel_order,
    cancel_open_orders,
    get_balances,
    get_open_orders,
    get_order_fee_usdt,
    list_spot_coin_positions,
    normalize_qty_for_sell,
    place_limit_sell_qty,
    place_market_sell_qty,
)
from app.services.binance_live_2 import (
    cancel_order as cancel_order_2,
    get_open_orders as get_open_orders_2,
    list_spot_coin_positions as list_spot_coin_positions_2,
    normalize_qty_for_sell as normalize_qty_for_sell_2,
    place_limit_sell_qty as place_limit_sell_qty_2,
    place_market_sell_qty as place_market_sell_qty_2,
)
from app.services.paper_trading import (
    add_log,
    build_smart_dca_plan,
    build_ai_dca_rules,
    create_campaign_positions,
    ensure_defaults,
    get_setting,
    recalculate_campaign_dca,
    run_cycle,
    set_setting,
    suggest_top_symbols,
    wallet_snapshot,
)
from app.services.live_trading import (
    _arm_or_rearm_tp_order,
    create_live_campaign_positions,
    add_live_log,
    live_wallet_snapshot,
    recalculate_live_campaign_dca,
    run_live_cycle,
)
from app.services.smart_runtime import refresh_smart_medium, refresh_smart_slow
from app.services.backtesting import run_smart_backtest
from app.services.accumulation import (
    create_plan as create_accumulation_plan,
    manual_partial_sell as accumulation_manual_partial_sell,
    run_accumulation_cycle,
    toggle_plan_status as accumulation_toggle_plan_status,
)
from app.services.forecasting import get_forecasts_for_symbols, get_or_build_forecast
from advisor import runner as advisor_runner
from app.models.smart_campaign import SmartCampaign, SmartPosition
from app.services.smart_campaign_service import (
    calculate_required_capital,
    campaign_summary,
    create_campaign,
    get_advisor_recommendations,
    manual_sell as smart_manual_sell,
    resume_campaign,
    run_smart_cycle,
    stop_campaign,
)
from app.services.live_smart_campaign_service import (
    create_live_campaign,
    get_recent_logs,
    live_campaign_summary,
    manual_sell_live,
    resume_live_campaign,
    run_live_smart_cycle,
    stop_live_campaign,
)

app = FastAPI(title="Crypto Bots - Rebuild")
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
templates = Jinja2Templates(directory="app/web/templates")

scheduler = BackgroundScheduler(timezone="UTC")
cycle_lock = threading.Lock()
live_cycle_lock = threading.Lock()
medium_lock = threading.Lock()
slow_lock = threading.Lock()
paper_acc_lock = threading.Lock()
live_acc_lock = threading.Lock()
all_coins_cache_lock = threading.Lock()
_ALL_COINS_PAGE_CACHE: dict[str, dict] = {}


def _context(active: str, **kwargs) -> dict:
    inferred_mode = "live" if str(active).startswith("live") else "paper"
    base = {
        "active_page": active,
        "mode": inferred_mode,
        "now": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "cycle_seconds": max(settings.fast_loop_seconds, 3),
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


def _safe_float_or_default(value: str | None, default: float) -> float:
    parsed = _safe_float(value, None)
    return float(default if parsed is None else parsed)


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
        .options(joinedload(Position.campaign))
        .join(Campaign, Campaign.id == Position.campaign_id)
        .filter(Position.status == "closed", Campaign.mode == mode)
        .order_by(desc(Position.closed_at), desc(Position.id))
        .limit(2000)
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
        fees = float(p.open_fee_usdt or 0.0) + float(p.close_fee_usdt or 0.0)
        exit_value = float((p.close_price or 0.0) * (p.total_qty or 0.0))
        if p.status == "closed" and p.realized_pnl_usdt is not None:
            # Keep history consistent with realized PnL accounting.
            exit_value = invested + pnl
        base_rows.append(
            {
                "id": p.id,
                "campaign_name": p.campaign.name,
                "symbol": str(p.symbol or "").upper(),
                "opened_at": p.opened_at,
                "closed_at": p.closed_at,
                "invested": invested,
                "exit_value": exit_value,
                "pnl": pnl,
                "pnl_pct": _pnl_pct(invested, pnl),
                "fees": fees,
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


def _acc_plan_view_row(plan: AccumulationPlan) -> dict:
    last_price = float(plan.last_price or 0.0)
    qty = float(plan.coin_qty or 0.0)
    avg = float(plan.avg_entry_price or 0.0)
    initial_qty = float(plan.initial_coin_qty or 0.0)
    initial_entry_usdt = float(plan.initial_entry_usdt or 0.0)
    start_capital = float(plan.total_capital_usdt or 0.0)
    market_value = last_price * qty if last_price > 0 and qty > 0 else 0.0
    unrealized = (last_price - avg) * qty if last_price > 0 and qty > 0 and avg > 0 else 0.0
    unrealized_pct = ((unrealized / (avg * qty)) * 100.0) if (avg > 0 and qty > 0) else 0.0
    next_dca_trigger = (avg * (1.0 - (float(plan.dca_drop_pct or 0.0) / 100.0))) if avg > 0 else 0.0
    next_sell_trigger = (avg * (1.0 + (float(plan.partial_tp_pct or 0.0) / 100.0))) if avg > 0 else 0.0
    qty_change = qty - initial_qty
    qty_change_pct = ((qty_change / initial_qty) * 100.0) if initial_qty > 0 else 0.0
    equity_now = float(plan.reserved_cash_usdt or 0.0) + market_value
    equity_change = equity_now - start_capital
    equity_change_pct = ((equity_change / start_capital) * 100.0) if start_capital > 0 else 0.0
    budget_remaining = float(plan.reserved_cash_usdt or 0.0)
    budget_spent = max(0.0, start_capital - budget_remaining)
    budget_spent_pct = ((budget_spent / start_capital) * 100.0) if start_capital > 0 else 0.0
    budget_remaining_pct = ((budget_remaining / start_capital) * 100.0) if start_capital > 0 else 0.0
    return {
        "plan": plan,
        "market_value": market_value,
        "unrealized_pnl": unrealized,
        "unrealized_pnl_pct": unrealized_pct,
        "coin_gain": qty_change,
        "initial_qty": initial_qty,
        "current_qty": qty,
        "qty_change": qty_change,
        "qty_change_pct": qty_change_pct,
        "initial_entry_usdt": initial_entry_usdt,
        "equity_now": equity_now,
        "equity_change": equity_change,
        "equity_change_pct": equity_change_pct,
        "budget_spent": budget_spent,
        "budget_spent_pct": budget_spent_pct,
        "budget_remaining": budget_remaining,
        "budget_remaining_pct": budget_remaining_pct,
        "next_dca_trigger": next_dca_trigger,
        "next_sell_trigger": next_sell_trigger,
    }


def _acc_attach_efficiency(row: dict, trades: list[AccumulationTrade]) -> dict:
    turnover_usdt = 0.0
    ledger_fees_usdt = 0.0
    for t in trades:
        side = str(t.side or "").upper()
        if side in {"BUY", "SELL"}:
            turnover_usdt += max(0.0, float(t.quote_usdt or 0.0))
        ledger_fees_usdt += max(0.0, float(t.fee_usdt or 0.0))

    qty_change = float(row.get("qty_change") or 0.0)
    efficiency_qty_per_100 = ((qty_change / turnover_usdt) * 100.0) if turnover_usdt > 1e-12 else 0.0
    buy_count = int(row["plan"].buy_count or 0)
    sell_count = int(row["plan"].sell_count or 0)
    cycle_count = min(buy_count, sell_count)

    row["acc_turnover_usdt"] = turnover_usdt
    row["acc_efficiency_qty_per_100"] = efficiency_qty_per_100
    row["acc_cycle_count"] = cycle_count
    row["acc_ledger_fees_usdt"] = ledger_fees_usdt
    fee_rate = max(0.0, float(getattr(settings, "paper_fee_pct", 0.1)) / 100.0)
    row["acc_estimated_fees_usdt"] = (turnover_usdt * fee_rate) if str(row["plan"].mode) == "paper" else ledger_fees_usdt
    return row


def _acc_meaningful_trades(trades: list[AccumulationTrade]) -> tuple[list[AccumulationTrade], int]:
    kept: list[AccumulationTrade] = []
    skipped = 0
    for t in trades:
        qty = float(t.qty or 0.0)
        quote = float(t.quote_usdt or 0.0)
        # Hide legacy dust/no-op rows from UI and test metrics.
        if qty <= 1e-12 or quote <= 1e-9:
            skipped += 1
            continue
        kept.append(t)
    return kept, skipped


def _acc_history_context(db, mode: str, date_filter: str, symbol_filter: str, reason_filter: str) -> dict:
    plans = db.query(AccumulationPlan).filter(AccumulationPlan.mode == mode).all()
    plan_by_id = {int(p.id): p for p in plans}
    q = db.query(AccumulationTrade).join(AccumulationPlan, AccumulationPlan.id == AccumulationTrade.plan_id).filter(
        AccumulationPlan.mode == mode
    )
    rows_all = q.order_by(desc(AccumulationTrade.created_at), desc(AccumulationTrade.id)).limit(2000).all()
    now_dt = datetime.utcnow()

    def _match_date(t: AccumulationTrade) -> bool:
        if date_filter == "all":
            return True
        hours_map = {"24h": 24, "3d": 72, "7d": 168, "14d": 336, "30d": 720, "60d": 1440}
        h = hours_map.get(date_filter)
        if h is None:
            return True
        return t.created_at >= (now_dt - timedelta(hours=h))

    symbols = sorted({str(plan_by_id.get(int(t.plan_id)).symbol).upper() for t in rows_all if plan_by_id.get(int(t.plan_id))})
    reasons = sorted({str(t.reason or "-") for t in rows_all})

    rows = [t for t in rows_all if _match_date(t)]
    if symbol_filter and symbol_filter != "all":
        rows = [t for t in rows if str(plan_by_id.get(int(t.plan_id)).symbol).upper() == symbol_filter]
    if reason_filter and reason_filter != "all":
        rows = [t for t in rows if str(t.reason or "-") == reason_filter]

    rendered_rows = []
    net = 0.0
    for t in rows:
        plan = plan_by_id.get(int(t.plan_id))
        if not plan:
            continue
        pnl = float(t.pnl_usdt or 0.0)
        net += pnl
        rendered_rows.append(
            {
                "id": t.id,
                "time": t.created_at,
                "plan_name": plan.name,
                "symbol": str(plan.symbol).upper(),
                "side": t.side,
                "price": float(t.price or 0.0),
                "qty": float(t.qty or 0.0),
                "quote": float(t.quote_usdt or 0.0),
                "fee": float(t.fee_usdt or 0.0),
                "pnl": pnl,
                "reason": str(t.reason or "-"),
            }
        )
    return {
        "rows": rendered_rows,
        "summary": {"count": len(rendered_rows), "net_pnl": net},
        "date_filter": date_filter,
        "symbol_filter": symbol_filter or "all",
        "reason_filter": reason_filter or "all",
        "symbols": symbols,
        "reasons": reasons,
    }


def _simulate_accumulation_scenario(
    *,
    symbol: str,
    total_capital_usdt: float,
    initial_entry_usdt: float,
    entry_price: float,
    low_price: float,
    high_price: float,
    dca_drop_pct: float,
    dca_allocation_pct: float,
    partial_tp_pct: float,
    partial_sell_pct: float,
    min_order_usdt: float,
    fee_pct: float,
) -> dict:
    total_capital = max(1.0, float(total_capital_usdt))
    initial_entry = max(1.0, float(initial_entry_usdt))
    entry = max(1e-12, float(entry_price))
    low = max(1e-12, float(low_price))
    high = max(1e-12, float(high_price))
    dca_drop = max(0.01, float(dca_drop_pct))
    dca_alloc = max(0.0, float(dca_allocation_pct))
    tp_pct = max(0.0, float(partial_tp_pct))
    partial_sell = max(0.0, min(95.0, float(partial_sell_pct)))
    min_order = max(1.0, float(min_order_usdt))
    fee_rate = max(0.0, float(fee_pct) / 100.0)

    reserved = total_capital
    qty = 0.0
    avg = 0.0
    realized = 0.0
    fees = 0.0
    initial_qty = 0.0
    buys = 0
    sells = 0
    dca_buys = 0
    events: list[dict] = []

    def buy(usdt: float, px: float, reason: str) -> bool:
        nonlocal reserved, qty, avg, fees, buys, initial_qty
        spend = max(0.0, float(usdt))
        if spend < min_order:
            return False
        fee = spend * fee_rate
        total_spent = spend + fee
        if reserved + 1e-12 < total_spent:
            return False
        got_qty = spend / px
        prev_cost = avg * qty
        qty_new = qty + got_qty
        avg_new = ((prev_cost + total_spent) / qty_new) if qty_new > 0 else 0.0
        qty = qty_new
        avg = avg_new
        reserved -= total_spent
        fees += fee
        buys += 1
        if initial_qty <= 0:
            initial_qty = got_qty
        events.append(
            {
                "phase": "down" if px <= entry else "entry",
                "side": "BUY",
                "price": px,
                "qty": got_qty,
                "quote": spend,
                "fee": fee,
                "avg_after": avg,
                "reserved_after": reserved,
                "reason": reason,
            }
        )
        return True

    def sell(sell_qty: float, px: float, reason: str) -> bool:
        nonlocal reserved, qty, avg, realized, fees, sells
        sq = min(max(0.0, float(sell_qty)), qty)
        if sq <= 0:
            return False
        gross = sq * px
        if gross + 1e-12 < min_order:
            return False
        fee = gross * fee_rate
        net = gross - fee
        cost = sq * avg
        pnl = net - cost
        qty = max(0.0, qty - sq)
        reserved += net
        realized += pnl
        fees += fee
        sells += 1
        if qty <= 1e-12:
            qty = 0.0
            avg = 0.0
        events.append(
            {
                "phase": "up",
                "side": "SELL",
                "price": px,
                "qty": sq,
                "quote": gross,
                "fee": fee,
                "pnl": pnl,
                "avg_after": avg,
                "reserved_after": reserved,
                "reason": reason,
            }
        )
        return True

    # Initial entry
    buy(min(initial_entry, reserved), entry, "initial_entry")

    # Down phase (repeated DCA at low until no longer valid or no budget)
    down_iter = 0
    while qty > 0 and down_iter < 300:
        down_iter += 1
        trigger = avg * (1.0 - dca_drop / 100.0)
        if low > trigger:
            break
        dca_usdt = min(initial_entry * (dca_alloc / 100.0), reserved)
        if dca_usdt < min_order:
            break
        if not buy(dca_usdt, low, "dca_at_low"):
            break
        dca_buys += 1

    reserved_after_down = reserved
    qty_after_down = qty
    avg_after_down = avg
    spent_by_low = total_capital - reserved_after_down

    # Up phase (multi-step partial sell while price is above avg and TP ladder is touched).
    # High Scenario is the endpoint of the up move, not the first sell point.
    # We simulate repeated TP touches from trigger up to high.
    if tp_pct > 0 and partial_sell > 0 and qty > 0 and avg > 0:
        trigger = avg * (1.0 + tp_pct / 100.0)
        step_mult = 1.0 + (tp_pct / 100.0)
        up_iter = 0
        level_price = trigger
        while up_iter < 400 and level_price <= high + 1e-12 and qty > 0:
            up_iter += 1
            # Accumulation rule: sell only extra qty above initial baseline.
            extra_qty = max(0.0, qty - initial_qty)
            if extra_qty <= 1e-12:
                break
            sq = extra_qty * (partial_sell / 100.0)
            if sq <= 1e-12:
                break
            if not sell(sq, level_price, "partial_take_profit"):
                break
            # move to next TP ladder level
            level_price *= step_mult

    market_value_now = qty * high
    equity_now = reserved + market_value_now
    total_pnl = equity_now - total_capital
    total_pnl_pct = (total_pnl / total_capital * 100.0) if total_capital > 0 else 0.0
    qty_change = qty - initial_qty
    qty_change_pct = (qty_change / initial_qty * 100.0) if initial_qty > 0 else 0.0

    return {
        "input": {
            "symbol": symbol.upper(),
            "total_capital_usdt": total_capital,
            "initial_entry_usdt": initial_entry,
            "entry_price": entry,
            "low_price": low,
            "high_price": high,
            "dca_drop_pct": dca_drop,
            "dca_allocation_pct": dca_alloc,
            "partial_tp_pct": tp_pct,
            "partial_sell_pct": partial_sell,
            "min_order_usdt": min_order,
            "fee_pct": fee_pct,
        },
        "down_phase": {
            "spent_by_low": spent_by_low,
            "remaining_after_down": reserved_after_down,
            "qty_after_down": qty_after_down,
            "avg_after_down": avg_after_down,
            "dca_buys": dca_buys,
        },
        "final": {
            "reserved_cash": reserved,
            "coin_qty": qty,
            "avg_entry": avg,
            "market_value": market_value_now,
            "equity_now": equity_now,
            "realized_pnl": realized,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "initial_qty": initial_qty,
            "qty_change": qty_change,
            "qty_change_pct": qty_change_pct,
            "fees_total": fees,
            "buys": buys,
            "sells": sells,
        },
        "events": events[-120:],
    }


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
        "ALTER TABLE campaigns ADD COLUMN smart_dca_enabled BOOLEAN NOT NULL DEFAULT 0",
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
        "ALTER TABLE positions ADD COLUMN open_fee_usdt FLOAT NOT NULL DEFAULT 0",
        "ALTER TABLE positions ADD COLUMN close_fee_usdt FLOAT NOT NULL DEFAULT 0",
        "ALTER TABLE smart_campaigns ADD COLUMN feature_version VARCHAR DEFAULT 'v1'",
        "ALTER TABLE live_smart_campaigns ADD COLUMN feature_version VARCHAR DEFAULT 'v1'",
    ]
    for stmt in stmts:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception:
            pass  # Column already exists — expected on restart
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
        logger.warning("Paper cycle skipped: previous cycle still running")
        return
    db = SessionLocal()
    t0 = time.monotonic()
    try:
        run_cycle(db)
    finally:
        elapsed = time.monotonic() - t0
        if elapsed > settings.fast_loop_seconds * 0.8:
            logger.warning("Paper cycle took %.2fs (interval=%ss)", elapsed, settings.fast_loop_seconds)
        db.close()
        cycle_lock.release()


def _scheduled_live_cycle() -> None:
    if not live_cycle_lock.acquire(blocking=False):
        logger.warning("Live cycle skipped: previous cycle still running")
        return
    db = SessionLocal()
    t0 = time.monotonic()
    try:
        run_live_cycle(db)
    finally:
        elapsed = time.monotonic() - t0
        if elapsed > settings.fast_loop_seconds * 0.8:
            logger.warning("Live cycle took %.2fs (interval=%ss)", elapsed, settings.fast_loop_seconds)
        db.close()
        live_cycle_lock.release()


def _scheduled_medium_refresh() -> None:
    if not medium_lock.acquire(blocking=False):
        logger.warning("Medium refresh skipped: previous refresh still running")
        return
    db = SessionLocal()
    try:
        refresh_smart_medium(db)
    finally:
        db.close()
        medium_lock.release()


def _scheduled_slow_recalc() -> None:
    if not slow_lock.acquire(blocking=False):
        logger.warning("Slow recalc skipped: previous recalc still running")
        return
    db = SessionLocal()
    try:
        refresh_smart_slow(db)
    finally:
        db.close()
        slow_lock.release()


def _scheduled_paper_acc_cycle() -> None:
    if not paper_acc_lock.acquire(blocking=False):
        logger.warning("Paper accumulation cycle skipped: previous cycle still running")
        return
    db = SessionLocal()
    try:
        run_accumulation_cycle(db, "paper")
    finally:
        db.close()
        paper_acc_lock.release()


def _scheduled_live_acc_cycle() -> None:
    if not live_acc_lock.acquire(blocking=False):
        logger.warning("Live accumulation cycle skipped: previous cycle still running")
        return
    db = SessionLocal()
    try:
        run_accumulation_cycle(db, "live")
    finally:
        db.close()
        live_acc_lock.release()


@app.on_event("startup")
async def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    # Create smart campaign tables
    from app.models.smart_campaign import SmartCampaign as _SC, SmartPosition as _SP
    _SC.__table__.create(bind=engine, checkfirst=True)
    _SP.__table__.create(bind=engine, checkfirst=True)
    # Create live smart campaign tables
    from app.models.live_smart_campaign import (
        LiveSmartCampaign as _LSC, LiveSmartPosition as _LSP, LiveSmartCampaignLog as _LSCL
    )
    _LSC.__table__.create(bind=engine, checkfirst=True)
    _LSP.__table__.create(bind=engine, checkfirst=True)
    _LSCL.__table__.create(bind=engine, checkfirst=True)
    _apply_schema_updates()
    db = SessionLocal()
    try:
        ensure_defaults(db, settings.paper_start_balance)
    finally:
        db.close()

    # Start real-time price WebSocket (Binance !miniTicker@arr)
    import asyncio
    from app.services.price_ws import run_price_stream
    asyncio.create_task(run_price_stream())

    scheduler.add_job(
        _scheduled_cycle,
        "interval",
        seconds=max(settings.fast_loop_seconds, 3),
        id="paper_cycle",
        replace_existing=True,
    )
    scheduler.add_job(
        _scheduled_live_cycle,
        "interval",
        seconds=max(settings.fast_loop_seconds, 3),
        id="live_cycle",
        replace_existing=True,
    )
    scheduler.add_job(
        _scheduled_medium_refresh,
        "interval",
        seconds=max(settings.medium_refresh_seconds, 60),
        id="smart_medium_refresh",
        replace_existing=True,
    )
    scheduler.add_job(
        _scheduled_slow_recalc,
        "interval",
        seconds=max(settings.slow_recalc_seconds, 900),
        id="smart_slow_recalc",
        replace_existing=True,
    )
    scheduler.add_job(
        _scheduled_paper_acc_cycle,
        "interval",
        seconds=max(settings.fast_loop_seconds, 3),
        id="paper_acc_cycle",
        replace_existing=True,
    )
    scheduler.add_job(
        _scheduled_live_acc_cycle,
        "interval",
        seconds=max(settings.fast_loop_seconds, 3),
        id="live_acc_cycle",
        replace_existing=True,
    )

    # ── Advisor: auto-run daily at 08:00 UTC ──────────────────────────────
    def _scheduled_advisor() -> None:
        state = advisor_runner.get_state()
        if state["status"] == "running":
            logger.info("Advisor: skipping scheduled run — already running")
            return
        logger.info("Advisor: starting scheduled daily run")
        advisor_runner.start(n_symbols=50, n_trials=300)

    scheduler.add_job(
        _scheduled_advisor,
        "cron",
        hour=8,
        minute=0,
        timezone="UTC",
        id="advisor_daily",
        replace_existing=True,
    )

    # ── Advisor: market volatility watcher (every 30 min) ─────────────────
    _btc_price_history: list[tuple[datetime, float]] = []  # (timestamp, price)

    def _advisor_market_watcher() -> None:
        """
        Checks BTC price every 30 min. Triggers an emergency advisor re-run if:
          1. BTC drops >4% in 4 hours  (market crash protection)
          2. BTC spikes >5% in 4 hours (euphoria/pump detection)
          3. 4h ATR (volatility) doubles vs the previous 24h baseline
        """
        try:
            from app.services.binance_public import get_prices
            prices = get_prices(["BTCUSDT"])
            btc_price = prices.get("BTCUSDT")
            if not btc_price:
                return

            now = datetime.utcnow()
            _btc_price_history.append((now, btc_price))

            # Keep only last 25 readings (~12.5 hours at 30-min intervals)
            cutoff = now - timedelta(hours=13)
            while _btc_price_history and _btc_price_history[0][0] < cutoff:
                _btc_price_history.pop(0)

            if len(_btc_price_history) < 8:  # Need at least 4h of data
                return

            # 4-hour window (8 readings × 30 min)
            price_4h_ago = _btc_price_history[-8][1]
            change_4h = (btc_price - price_4h_ago) / price_4h_ago * 100

            # 24h ATR approximation: std of last 24 readings vs last 8
            if len(_btc_price_history) >= 24:
                prices_24h = [p for _, p in _btc_price_history[-24:]]
                prices_4h  = [p for _, p in _btc_price_history[-8:]]
                import statistics
                vol_24h = statistics.stdev(prices_24h) / (sum(prices_24h) / len(prices_24h)) * 100
                vol_4h  = statistics.stdev(prices_4h)  / (sum(prices_4h)  / len(prices_4h))  * 100
                volatility_spike = vol_4h > vol_24h * 2.0
            else:
                volatility_spike = False

            state = advisor_runner.get_state()
            if state["status"] == "running":
                return

            reason = None
            if change_4h <= -4.0:
                reason = f"BTC crashed {change_4h:.1f}% in 4h — emergency re-analysis"
            elif change_4h >= 5.0:
                reason = f"BTC pumped +{change_4h:.1f}% in 4h — re-analyzing opportunities"
            elif volatility_spike:
                reason = f"Volatility spike detected (4h vol {vol_4h:.2f}% vs 24h {vol_24h:.2f}%) — re-analyzing"

            if reason:
                logger.warning("Advisor market watcher triggered: %s", reason)
                advisor_runner.start(n_symbols=50, n_trials=50)

        except Exception as e:
            logger.warning("Advisor market watcher error: %s", e)

    scheduler.add_job(
        _advisor_market_watcher,
        "interval",
        minutes=30,
        id="advisor_market_watcher",
        replace_existing=True,
    )

    # ── Smart Campaign cycle (every 10s) ─────────────────────────────────
    def _scheduled_smart_campaign() -> None:
        db = SessionLocal()
        try:
            run_smart_cycle(db)
        except Exception as e:
            logger.error("Smart campaign cycle error: %s", e)
        finally:
            db.close()

    scheduler.add_job(
        _scheduled_smart_campaign,
        "interval",
        seconds=10,
        id="smart_campaign_cycle",
        replace_existing=True,
    )

    # ── Live Smart Campaign cycle (every 10s) ─────────────────────────────
    def _scheduled_live_smart_campaign() -> None:
        db = SessionLocal()
        try:
            run_live_smart_cycle(db)
        except Exception as e:
            logger.error("Live smart campaign cycle error: %s", e)
        finally:
            db.close()

    scheduler.add_job(
        _scheduled_live_smart_campaign,
        "interval",
        seconds=10,
        id="live_smart_campaign_cycle",
        replace_existing=True,
    )

    # ── Advisor: hourly quick ML refresh (V1) ────────────────────────────
    def _scheduled_advisor_refresh() -> None:
        state = advisor_runner.get_state()
        if state["status"] == "running" or state["refresh_status"] == "running":
            return
        if state["status"] == "done":
            logger.info("Advisor V1: starting hourly quick ML refresh")
            advisor_runner.start_refresh()

    scheduler.add_job(
        _scheduled_advisor_refresh,
        "interval",
        hours=1,
        id="advisor_hourly_refresh",
        replace_existing=True,
    )

    # ── Advisor: hourly quick ML refresh (V2) ────────────────────────────
    def _scheduled_advisor_refresh_v2() -> None:
        state = advisor_runner.get_state_v2()
        if state["status"] == "running" or state["refresh_status"] == "running":
            return
        if state["status"] == "done":
            logger.info("Advisor V2: starting hourly quick ML refresh")
            advisor_runner.start_refresh_v2()

    scheduler.add_job(
        _scheduled_advisor_refresh_v2,
        "interval",
        hours=1,
        id="advisor_hourly_refresh_v2",
        replace_existing=True,
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
        acc_plans = (
            db.query(AccumulationPlan)
            .filter(AccumulationPlan.mode == "paper")
            .order_by(desc(AccumulationPlan.created_at))
            .all()
        )
        acc_rows = [_acc_plan_view_row(p) for p in acc_plans]
        acc_open_rows = [r for r in acc_rows if float(r["plan"].coin_qty or 0.0) > 0.0]
        logs = _dashboard_logs(db, "paper")
        return templates.TemplateResponse(
            "paper_home.html",
            _context(
                "paper_home",
                request=request,
                wallet=wallet,
                campaigns=items,
                accumulation_rows=acc_open_rows,
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


@app.post("/paper/cash/add")
async def paper_add_cash(amount_usdt: str = Form(...), note: str = Form("")) -> RedirectResponse:
    db = SessionLocal()
    try:
        amount = float(_safe_float(amount_usdt, 0.0) or 0.0)
        if amount <= 0:
            return RedirectResponse("/paper", status_code=303)
        current_cash = float(get_setting(db, "paper_cash", "0"))
        new_cash = current_cash + amount
        set_setting(db, "paper_cash", f"{new_cash:.8f}")
        msg = (
            f"Paper wallet top-up +{amount:.2f} USDT | cash {current_cash:.2f} -> {new_cash:.2f}"
            + (f" | note={note.strip()}" if str(note).strip() else "")
        )
        add_log(db, "WALLET_TOPUP", "-", msg)
        db.commit()
        return RedirectResponse("/paper", status_code=303)
    finally:
        db.close()


@app.get("/paper/create", response_class=HTMLResponse)
async def paper_create_campaign_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("paper_create.html", _context("paper_create", request=request))


@app.get("/paper/smart-create", response_class=HTMLResponse)
async def paper_smart_create_campaign_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("paper_smart_create.html", _context("paper_smart_create", request=request))


@app.get("/paper/accumulation", response_class=HTMLResponse)
async def paper_accumulation_page(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        plans = (
            db.query(AccumulationPlan)
            .filter(AccumulationPlan.mode == "paper")
            .order_by(desc(AccumulationPlan.created_at))
            .all()
        )
        rows = [_acc_plan_view_row(p) for p in plans]
        logs = (
            db.query(ActivityLog)
            .filter(ActivityLog.event_type.like("ACC_%"))
            .order_by(desc(ActivityLog.id))
            .limit(80)
            .all()
        )
        return templates.TemplateResponse(
            "accumulation_home.html",
            _context("paper_accumulation", request=request, mode="paper", rows=rows, logs=logs),
        )
    finally:
        db.close()


@app.post("/paper/accumulation/create")
async def paper_accumulation_create(
    name: str = Form(...),
    symbol: str = Form(...),
    total_capital_usdt: str = Form(...),
    initial_entry_usdt: str = Form(...),
    dca_drop_pct: str = Form("2.5"),
    dca_allocation_pct: str = Form("120"),
    partial_tp_pct: str = Form("1.5"),
    partial_sell_pct: str = Form("20"),
) -> RedirectResponse:
    db = SessionLocal()
    try:
        plan = create_accumulation_plan(
            db,
            mode="paper",
            name=name,
            symbol=symbol,
            total_capital_usdt=_safe_float_or_default(total_capital_usdt, 0.0),
            initial_entry_usdt=_safe_float_or_default(initial_entry_usdt, 0.0),
            dca_drop_pct=_safe_float_or_default(dca_drop_pct, 2.5),
            dca_allocation_pct=_safe_float_or_default(dca_allocation_pct, 120.0),
            partial_tp_pct=_safe_float_or_default(partial_tp_pct, 1.5),
            partial_sell_pct=_safe_float_or_default(partial_sell_pct, 20.0),
        )
        db.commit()
        return RedirectResponse(f"/paper/accumulation/{plan.id}", status_code=303)
    finally:
        db.close()


@app.get("/paper/accumulation/{plan_id:int}", response_class=HTMLResponse)
async def paper_accumulation_details(request: Request, plan_id: int) -> HTMLResponse:
    db = SessionLocal()
    try:
        plan = db.query(AccumulationPlan).filter(AccumulationPlan.id == plan_id, AccumulationPlan.mode == "paper").first()
        if not plan:
            return RedirectResponse("/paper/accumulation", status_code=303)
        row = _acc_plan_view_row(plan)
        trades = (
            db.query(AccumulationTrade)
            .filter(AccumulationTrade.plan_id == plan.id)
            .order_by(desc(AccumulationTrade.id))
            .limit(200)
            .all()
        )
        display_trades, skipped_trades = _acc_meaningful_trades(trades)
        row = _acc_attach_efficiency(row, display_trades)
        row["display_buy_count"] = sum(1 for t in display_trades if str(t.side or "").upper() == "BUY")
        row["display_sell_count"] = sum(1 for t in display_trades if str(t.side or "").upper() == "SELL")
        row["hidden_dust_count"] = skipped_trades
        return templates.TemplateResponse(
            "accumulation_plan.html",
            _context("paper_accumulation_plan", request=request, mode="paper", row=row, trades=display_trades),
        )
    finally:
        db.close()


@app.post("/paper/accumulation/{plan_id:int}/toggle")
async def paper_accumulation_toggle(plan_id: int) -> RedirectResponse:
    db = SessionLocal()
    try:
        plan = db.query(AccumulationPlan).filter(AccumulationPlan.id == plan_id, AccumulationPlan.mode == "paper").first()
        if not plan:
            return RedirectResponse("/paper/accumulation", status_code=303)
        accumulation_toggle_plan_status(db, plan)
        db.commit()
        return RedirectResponse(f"/paper/accumulation/{plan_id}", status_code=303)
    finally:
        db.close()


@app.post("/paper/accumulation/{plan_id:int}/manual-sell")
async def paper_accumulation_manual_sell(plan_id: int, sell_pct: str = Form("20")) -> RedirectResponse:
    db = SessionLocal()
    try:
        plan = db.query(AccumulationPlan).filter(AccumulationPlan.id == plan_id, AccumulationPlan.mode == "paper").first()
        if not plan:
            return RedirectResponse("/paper/accumulation", status_code=303)
        pct = _safe_float_or_default(sell_pct, 20.0)
        accumulation_manual_partial_sell(db, plan, pct)
        db.commit()
        return RedirectResponse(f"/paper/accumulation/{plan_id}", status_code=303)
    finally:
        db.close()


@app.get("/paper/accumulation/history", response_class=HTMLResponse)
async def paper_accumulation_history(
    request: Request,
    date_range: str = "all",
    symbol: str = "all",
    reason: str = "all",
) -> HTMLResponse:
    db = SessionLocal()
    try:
        ctx = _acc_history_context(db, "paper", str(date_range).strip().lower(), str(symbol).strip().upper(), str(reason).strip())
        return templates.TemplateResponse("accumulation_history.html", _context("paper_accumulation_history", request=request, mode="paper", **ctx))
    finally:
        db.close()


@app.get("/paper/accumulation/calculator", response_class=HTMLResponse)
async def paper_accumulation_calculator(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "accumulation_calculator.html",
        _context(
            "paper_accumulation_calculator",
            request=request,
            mode="paper",
            result=None,
            form_data={
                "symbol": "ETHUSDT",
                "total_capital_usdt": 1000,
                "initial_entry_usdt": 100,
                "entry_price": "",
                "low_price": 1700,
                "high_price": 3000,
                "dca_drop_pct": 2.5,
                "dca_allocation_pct": 120,
                "partial_tp_pct": 1.5,
                "partial_sell_pct": 20,
                "min_order_usdt": 5,
                "fee_pct": float(getattr(settings, "paper_fee_pct", 0.1)),
            },
        ),
    )


@app.post("/paper/accumulation/calculator", response_class=HTMLResponse)
async def paper_accumulation_calculator_run(
    request: Request,
    symbol: str = Form("ETHUSDT"),
    total_capital_usdt: str = Form("1000"),
    initial_entry_usdt: str = Form("100"),
    entry_price: str = Form(""),
    low_price: str = Form("1700"),
    high_price: str = Form("3000"),
    dca_drop_pct: str = Form("2.5"),
    dca_allocation_pct: str = Form("120"),
    partial_tp_pct: str = Form("1.5"),
    partial_sell_pct: str = Form("20"),
    min_order_usdt: str = Form("5"),
    fee_pct: str = Form("0.1"),
) -> HTMLResponse:
    sym = str(symbol or "").strip().upper()
    if sym and not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    ep = _safe_float(entry_price, None)
    if ep is None or ep <= 0:
        px = get_prices([sym]) if sym else {}
        ep = float(px.get(sym, 0.0)) if sym else 0.0
    result = None
    if sym and float(ep or 0.0) > 0:
        result = _simulate_accumulation_scenario(
            symbol=sym,
            total_capital_usdt=_safe_float_or_default(total_capital_usdt, 1000.0),
            initial_entry_usdt=_safe_float_or_default(initial_entry_usdt, 100.0),
            entry_price=float(ep),
            low_price=_safe_float_or_default(low_price, 1700.0),
            high_price=_safe_float_or_default(high_price, 3000.0),
            dca_drop_pct=_safe_float_or_default(dca_drop_pct, 2.5),
            dca_allocation_pct=_safe_float_or_default(dca_allocation_pct, 120.0),
            partial_tp_pct=_safe_float_or_default(partial_tp_pct, 1.5),
            partial_sell_pct=_safe_float_or_default(partial_sell_pct, 20.0),
            min_order_usdt=_safe_float_or_default(min_order_usdt, 5.0),
            fee_pct=_safe_float_or_default(fee_pct, float(getattr(settings, "paper_fee_pct", 0.1))),
        )
    return templates.TemplateResponse(
        "accumulation_calculator.html",
        _context(
            "paper_accumulation_calculator",
            request=request,
            mode="paper",
            result=result,
            form_data={
                "symbol": sym,
                "total_capital_usdt": _safe_float_or_default(total_capital_usdt, 1000.0),
                "initial_entry_usdt": _safe_float_or_default(initial_entry_usdt, 100.0),
                "entry_price": float(ep or 0.0),
                "low_price": _safe_float_or_default(low_price, 1700.0),
                "high_price": _safe_float_or_default(high_price, 3000.0),
                "dca_drop_pct": _safe_float_or_default(dca_drop_pct, 2.5),
                "dca_allocation_pct": _safe_float_or_default(dca_allocation_pct, 120.0),
                "partial_tp_pct": _safe_float_or_default(partial_tp_pct, 1.5),
                "partial_sell_pct": _safe_float_or_default(partial_sell_pct, 20.0),
                "min_order_usdt": _safe_float_or_default(min_order_usdt, 5.0),
                "fee_pct": _safe_float_or_default(fee_pct, float(getattr(settings, "paper_fee_pct", 0.1))),
            },
        ),
    )


@app.get("/paper/backtest", response_class=HTMLResponse)
async def paper_backtest_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("smart_backtest.html", _context("smart_backtest", request=request, mode="paper"))


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
        acc_plans = (
            db.query(AccumulationPlan)
            .filter(AccumulationPlan.mode == "live")
            .order_by(desc(AccumulationPlan.created_at))
            .all()
        )
        acc_rows = [_acc_plan_view_row(p) for p in acc_plans]
        acc_open_rows = [r for r in acc_rows if float(r["plan"].coin_qty or 0.0) > 0.0]
        logs = _dashboard_logs(db, "live")
        return templates.TemplateResponse(
            "live_home.html",
            _context(
                "live_home",
                request=request,
                wallet=wallet,
                campaigns=items,
                accumulation_rows=acc_open_rows,
                logs=logs,
                live_error=live_error,
            ),
        )
    finally:
        db.close()


@app.get("/live/api/summary")
async def live_summary_api() -> JSONResponse:
    """Return wallet stats and per-campaign PnL for in-page polling on /live."""
    db = SessionLocal()
    try:
        wallet_data: dict = {
            "cash": 0.0, "equity": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
        }
        try:
            wallet_data = live_wallet_snapshot(db)
        except Exception:
            pass

        campaigns = db.query(Campaign).filter(Campaign.mode == "live").order_by(desc(Campaign.created_at)).all()
        campaign_rows = []
        for c in campaigns:
            stats = _campaign_stats(db, c)
            campaign_rows.append(
                {
                    "id": c.id,
                    "open_count": stats["open_count"],
                    "closed_count": stats["closed_count"],
                    "dca_done_count": stats["dca_done_count"],
                    "realized_pnl": float(stats["realized_pnl"]),
                    "unrealized_pnl": float(stats["unrealized_pnl"]),
                }
            )

        acc_plans = (
            db.query(AccumulationPlan)
            .filter(AccumulationPlan.mode == "live")
            .order_by(desc(AccumulationPlan.created_at))
            .all()
        )
        acc_rows = []
        for p in acc_plans:
            if float(p.coin_qty or 0.0) <= 0.0:
                continue
            r = _acc_plan_view_row(p)
            acc_rows.append(
                {
                    "id": p.id,
                    "realized_pnl": float(p.realized_pnl_usdt or 0.0),
                    "unrealized_pnl": float(r["unrealized_pnl"]),
                }
            )

        return JSONResponse(
            {
                "wallet": {
                    "cash": float(wallet_data.get("cash", 0.0)),
                    "equity": float(wallet_data.get("equity", 0.0)),
                    "realized_pnl": float(wallet_data.get("realized_pnl", 0.0)),
                    "unrealized_pnl": float(wallet_data.get("unrealized_pnl", 0.0)),
                },
                "campaigns": campaign_rows,
                "accumulation": acc_rows,
            }
        )
    finally:
        db.close()


@app.get("/live/campaigns")
async def live_campaigns_alias() -> RedirectResponse:
    return RedirectResponse("/live", status_code=303)


@app.get("/live/create", response_class=HTMLResponse)
async def live_create_campaign_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("live_create.html", _context("live_create", request=request))


@app.get("/live/smart-create", response_class=HTMLResponse)
async def live_smart_create_campaign_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("live_smart_create.html", _context("live_smart_create", request=request))


@app.get("/live/accumulation", response_class=HTMLResponse)
async def live_accumulation_page(request: Request) -> HTMLResponse:
    db = SessionLocal()
    try:
        plans = (
            db.query(AccumulationPlan)
            .filter(AccumulationPlan.mode == "live")
            .order_by(desc(AccumulationPlan.created_at))
            .all()
        )
        rows = [_acc_plan_view_row(p) for p in plans]
        logs = (
            db.query(ActivityLog)
            .filter(ActivityLog.event_type.like("LIVE_ACC_%"))
            .order_by(desc(ActivityLog.id))
            .limit(80)
            .all()
        )
        return templates.TemplateResponse(
            "accumulation_home.html",
            _context("live_accumulation", request=request, mode="live", rows=rows, logs=logs),
        )
    finally:
        db.close()


@app.post("/live/accumulation/create")
async def live_accumulation_create(
    name: str = Form(...),
    symbol: str = Form(...),
    total_capital_usdt: str = Form(...),
    initial_entry_usdt: str = Form(...),
    dca_drop_pct: str = Form("2.5"),
    dca_allocation_pct: str = Form("120"),
    partial_tp_pct: str = Form("1.5"),
    partial_sell_pct: str = Form("20"),
) -> RedirectResponse:
    db = SessionLocal()
    try:
        plan = create_accumulation_plan(
            db,
            mode="live",
            name=name,
            symbol=symbol,
            total_capital_usdt=_safe_float_or_default(total_capital_usdt, 0.0),
            initial_entry_usdt=_safe_float_or_default(initial_entry_usdt, 0.0),
            dca_drop_pct=_safe_float_or_default(dca_drop_pct, 2.5),
            dca_allocation_pct=_safe_float_or_default(dca_allocation_pct, 120.0),
            partial_tp_pct=_safe_float_or_default(partial_tp_pct, 1.5),
            partial_sell_pct=_safe_float_or_default(partial_sell_pct, 20.0),
        )
        db.commit()
        return RedirectResponse(f"/live/accumulation/{plan.id}", status_code=303)
    finally:
        db.close()


@app.get("/live/accumulation/{plan_id:int}", response_class=HTMLResponse)
async def live_accumulation_details(request: Request, plan_id: int) -> HTMLResponse:
    db = SessionLocal()
    try:
        plan = db.query(AccumulationPlan).filter(AccumulationPlan.id == plan_id, AccumulationPlan.mode == "live").first()
        if not plan:
            return RedirectResponse("/live/accumulation", status_code=303)
        row = _acc_plan_view_row(plan)
        trades = (
            db.query(AccumulationTrade)
            .filter(AccumulationTrade.plan_id == plan.id)
            .order_by(desc(AccumulationTrade.id))
            .limit(200)
            .all()
        )
        display_trades, skipped_trades = _acc_meaningful_trades(trades)
        row = _acc_attach_efficiency(row, display_trades)
        row["display_buy_count"] = sum(1 for t in display_trades if str(t.side or "").upper() == "BUY")
        row["display_sell_count"] = sum(1 for t in display_trades if str(t.side or "").upper() == "SELL")
        row["hidden_dust_count"] = skipped_trades
        return templates.TemplateResponse(
            "accumulation_plan.html",
            _context("live_accumulation_plan", request=request, mode="live", row=row, trades=display_trades),
        )
    finally:
        db.close()


@app.post("/live/accumulation/{plan_id:int}/toggle")
async def live_accumulation_toggle(plan_id: int) -> RedirectResponse:
    db = SessionLocal()
    try:
        plan = db.query(AccumulationPlan).filter(AccumulationPlan.id == plan_id, AccumulationPlan.mode == "live").first()
        if not plan:
            return RedirectResponse("/live/accumulation", status_code=303)
        accumulation_toggle_plan_status(db, plan)
        db.commit()
        return RedirectResponse(f"/live/accumulation/{plan_id}", status_code=303)
    finally:
        db.close()


@app.post("/live/accumulation/{plan_id:int}/manual-sell")
async def live_accumulation_manual_sell(plan_id: int, sell_pct: str = Form("20")) -> RedirectResponse:
    db = SessionLocal()
    try:
        plan = db.query(AccumulationPlan).filter(AccumulationPlan.id == plan_id, AccumulationPlan.mode == "live").first()
        if not plan:
            return RedirectResponse("/live/accumulation", status_code=303)
        pct = _safe_float_or_default(sell_pct, 20.0)
        accumulation_manual_partial_sell(db, plan, pct)
        db.commit()
        return RedirectResponse(f"/live/accumulation/{plan_id}", status_code=303)
    finally:
        db.close()


@app.get("/live/accumulation/history", response_class=HTMLResponse)
async def live_accumulation_history(
    request: Request,
    date_range: str = "all",
    symbol: str = "all",
    reason: str = "all",
) -> HTMLResponse:
    db = SessionLocal()
    try:
        ctx = _acc_history_context(db, "live", str(date_range).strip().lower(), str(symbol).strip().upper(), str(reason).strip())
        return templates.TemplateResponse("accumulation_history.html", _context("live_accumulation_history", request=request, mode="live", **ctx))
    finally:
        db.close()


@app.get("/live/accumulation/calculator", response_class=HTMLResponse)
async def live_accumulation_calculator(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "accumulation_calculator.html",
        _context(
            "live_accumulation_calculator",
            request=request,
            mode="live",
            result=None,
            form_data={
                "symbol": "ETHUSDT",
                "total_capital_usdt": 1000,
                "initial_entry_usdt": 100,
                "entry_price": "",
                "low_price": 1700,
                "high_price": 3000,
                "dca_drop_pct": 2.5,
                "dca_allocation_pct": 120,
                "partial_tp_pct": 1.5,
                "partial_sell_pct": 20,
                "min_order_usdt": 5,
                "fee_pct": float(getattr(settings, "paper_fee_pct", 0.1)),
            },
        ),
    )


@app.post("/live/accumulation/calculator", response_class=HTMLResponse)
async def live_accumulation_calculator_run(
    request: Request,
    symbol: str = Form("ETHUSDT"),
    total_capital_usdt: str = Form("1000"),
    initial_entry_usdt: str = Form("100"),
    entry_price: str = Form(""),
    low_price: str = Form("1700"),
    high_price: str = Form("3000"),
    dca_drop_pct: str = Form("2.5"),
    dca_allocation_pct: str = Form("120"),
    partial_tp_pct: str = Form("1.5"),
    partial_sell_pct: str = Form("20"),
    min_order_usdt: str = Form("5"),
    fee_pct: str = Form("0.1"),
) -> HTMLResponse:
    sym = str(symbol or "").strip().upper()
    if sym and not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    ep = _safe_float(entry_price, None)
    if ep is None or ep <= 0:
        px = get_prices([sym]) if sym else {}
        ep = float(px.get(sym, 0.0)) if sym else 0.0
    result = None
    if sym and float(ep or 0.0) > 0:
        result = _simulate_accumulation_scenario(
            symbol=sym,
            total_capital_usdt=_safe_float_or_default(total_capital_usdt, 1000.0),
            initial_entry_usdt=_safe_float_or_default(initial_entry_usdt, 100.0),
            entry_price=float(ep),
            low_price=_safe_float_or_default(low_price, 1700.0),
            high_price=_safe_float_or_default(high_price, 3000.0),
            dca_drop_pct=_safe_float_or_default(dca_drop_pct, 2.5),
            dca_allocation_pct=_safe_float_or_default(dca_allocation_pct, 120.0),
            partial_tp_pct=_safe_float_or_default(partial_tp_pct, 1.5),
            partial_sell_pct=_safe_float_or_default(partial_sell_pct, 20.0),
            min_order_usdt=_safe_float_or_default(min_order_usdt, 5.0),
            fee_pct=_safe_float_or_default(fee_pct, float(getattr(settings, "paper_fee_pct", 0.1))),
        )
    return templates.TemplateResponse(
        "accumulation_calculator.html",
        _context(
            "live_accumulation_calculator",
            request=request,
            mode="live",
            result=result,
            form_data={
                "symbol": sym,
                "total_capital_usdt": _safe_float_or_default(total_capital_usdt, 1000.0),
                "initial_entry_usdt": _safe_float_or_default(initial_entry_usdt, 100.0),
                "entry_price": float(ep or 0.0),
                "low_price": _safe_float_or_default(low_price, 1700.0),
                "high_price": _safe_float_or_default(high_price, 3000.0),
                "dca_drop_pct": _safe_float_or_default(dca_drop_pct, 2.5),
                "dca_allocation_pct": _safe_float_or_default(dca_allocation_pct, 120.0),
                "partial_tp_pct": _safe_float_or_default(partial_tp_pct, 1.5),
                "partial_sell_pct": _safe_float_or_default(partial_sell_pct, 20.0),
                "min_order_usdt": _safe_float_or_default(min_order_usdt, 5.0),
                "fee_pct": _safe_float_or_default(fee_pct, float(getattr(settings, "paper_fee_pct", 0.1))),
            },
        ),
    )


@app.get("/live/backtest", response_class=HTMLResponse)
async def live_backtest_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("smart_backtest.html", _context("smart_backtest", request=request, mode="live"))


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


def _build_tp_map_from_orders(open_orders: list[dict]) -> dict[str, dict]:
    tp_map: dict[str, dict] = {}
    for o in open_orders:
        side = str(o.get("side", "")).upper()
        otype = str(o.get("type", "")).upper()
        status = str(o.get("status", "")).upper()
        if side != "SELL" or otype != "LIMIT" or status not in {"NEW", "PARTIALLY_FILLED"}:
            continue
        sym = str(o.get("symbol", "")).upper()
        price = float(o.get("price", 0.0) or 0.0)
        orig_qty = float(o.get("origQty", 0.0) or 0.0)
        exec_qty = float(o.get("executedQty", 0.0) or 0.0)
        rem_qty = max(0.0, orig_qty - exec_qty)
        if price <= 0 or rem_qty <= 0:
            continue
        cur = tp_map.get(sym)
        if not cur:
            tp_map[sym] = {"price": price, "qty": rem_qty, "count": 1}
        else:
            cur["count"] = int(cur.get("count", 1)) + 1
            if price < float(cur.get("price", price)):
                cur["price"] = price
                cur["qty"] = rem_qty
    return tp_map


def _empty_all_coins_data() -> dict:
    return {
        "rows": [],
        "summary": {
            "coins_count": 0,
            "invested_total": 0.0,
            "market_total": 0.0,
            "pnl_total": 0.0,
            "pnl_pct": 0.0,
        },
    }


def _get_all_coins_cached_payload(cache_key: str, max_age_seconds: int) -> dict | None:
    with all_coins_cache_lock:
        cached = _ALL_COINS_PAGE_CACHE.get(cache_key)
        if not cached:
            return None
        age = time.time() - float(cached.get("created_at", 0.0))
        if age > max(1, int(max_age_seconds)):
            return None
        return cached.get("payload")


def _get_all_coins_cached_summary(cache_key: str) -> dict:
    with all_coins_cache_lock:
        cached = _ALL_COINS_PAGE_CACHE.get(cache_key)
        if cached and isinstance(cached.get("payload"), dict):
            payload = cached["payload"]
            if isinstance(payload.get("summary"), dict):
                return payload["summary"]
    return _empty_all_coins_data()["summary"]


def _set_all_coins_cached_payload(cache_key: str, payload: dict) -> None:
    with all_coins_cache_lock:
        _ALL_COINS_PAGE_CACHE[cache_key] = {
            "created_at": time.time(),
            "payload": payload,
        }


def _clear_all_coins_cached_payload(cache_key: str) -> None:
    with all_coins_cache_lock:
        _ALL_COINS_PAGE_CACHE.pop(cache_key, None)


def _serialize_forecast(fc: dict | None) -> dict | None:
    if not fc:
        return None
    return {
        "expected_move_pct": float(fc.get("expected_move_pct", 0.0) or 0.0),
        "confidence_pct": float(fc.get("confidence_pct", 0.0) or 0.0),
        "bias": str(fc.get("bias", "neutral") or "neutral"),
        "arrow": str(fc.get("arrow", "-") or "-"),
        "tooltip": str(fc.get("tooltip", "") or ""),
    }


def _build_all_coins_page_payload(
    db,
    *,
    list_positions_fn,
    get_open_orders_fn,
    cache_ttl_seconds: int,
    include_forecasts: bool,
) -> dict:
    data = list_positions_fn(cache_ttl_seconds=cache_ttl_seconds)
    rows = data.get("rows", [])
    symbols = [str(r.get("symbol", "")).upper() for r in rows]
    f_map: dict[str, dict] = {}
    if include_forecasts and symbols:
        try:
            f_map = get_forecasts_for_symbols(
                db,
                symbols,
                build_limit=max(1, int(settings.forecast_build_per_request)),
            )
        except Exception:
            f_map = {}

    tp_map: dict[str, dict] = {}
    try:
        tp_map = _build_tp_map_from_orders(get_open_orders_fn())
    except Exception:
        tp_map = {}

    out_rows = []
    for r in rows:
        sym = str(r.get("symbol", "")).upper()
        tp = tp_map.get(sym)
        forecast = _serialize_forecast(f_map.get(sym))
        out_rows.append(
            {
                "symbol": sym,
                "qty_total": float(r.get("qty_total", 0.0) or 0.0),
                "qty_free": float(r.get("qty_free", 0.0) or 0.0),
                "avg_entry": float(r.get("avg_entry", 0.0) or 0.0),
                "price": float(r.get("price", 0.0) or 0.0),
                "invested": float(r.get("invested", 0.0) or 0.0),
                "market_value": float(r.get("market_value", 0.0) or 0.0),
                "pnl": float(r.get("pnl", 0.0) or 0.0),
                "pnl_pct": float(r.get("pnl_pct", 0.0) or 0.0),
                "status": str(r.get("status", "flat") or "flat"),
                "tp_price": float(tp.get("price", 0.0)) if tp else None,
                "tp_qty": float(tp.get("qty", 0.0)) if tp else None,
                "tp_count": int(tp.get("count", 0)) if tp else 0,
                "forecast": forecast,
            }
        )

    summary = data.get("summary", {})
    return {
        "rows": out_rows,
        "summary": {
            "coins_count": int(summary.get("coins_count", 0)),
            "invested_total": float(summary.get("invested_total", 0.0) or 0.0),
            "market_total": float(summary.get("market_total", 0.0) or 0.0),
            "pnl_total": float(summary.get("pnl_total", 0.0) or 0.0),
            "pnl_pct": float(summary.get("pnl_pct", 0.0) or 0.0),
        },
    }


@app.get("/live/all-coins", response_class=HTMLResponse)
async def live_all_coins_page(
    request: Request,
    symbol: str = "",
    refresh_forecast: int = 0,
) -> HTMLResponse:
    db = SessionLocal()
    try:
        error = None
        symbol_query = str(symbol or "").upper().strip()
        forecast_card = None
        data = _empty_all_coins_data()
        data["summary"] = _get_all_coins_cached_summary("live_all_coins_full")
        try:
            if symbol_query:
                qsym = symbol_query if symbol_query.endswith("USDT") else f"{symbol_query}USDT"
                try:
                    forecast_card = get_or_build_forecast(
                        db,
                        qsym,
                        force_refresh=bool(int(refresh_forecast)),
                        interval=settings.forecast_interval,
                        horizon_days=settings.forecast_horizon_days,
                    )
                except Exception as e:
                    forecast_card = {"symbol": qsym, "error": str(e)}
        except Exception as e:
            error = str(e)
        logs = (
            db.query(ActivityLog)
            .filter(
                or_(
                    ActivityLog.event_type.like("LIVE_ALL_%"),
                    ActivityLog.event_type.like("LIVE_ALLCOIN_%"),
                )
            )
            .order_by(desc(ActivityLog.id))
            .limit(60)
            .all()
        )
        return templates.TemplateResponse(
            "live_all_coins.html",
            _context(
                "live_all_coins",
                request=request,
                data=data,
                logs=logs,
                all_coins_error=error,
                forecast_symbol=symbol_query,
                forecast_card=forecast_card,
                forecast_build_per_request=max(1, int(settings.forecast_build_per_request)),
                data_api_url="/live/all-coins/api/data",
                actions_prefix="/live/all-coins",
            ),
        )
    finally:
        db.close()


@app.get("/live/all-coins/api/prices")
async def live_all_coins_prices_api() -> JSONResponse:
    """Return live prices, PnL, and open-sell TP data for all coin positions (no forecast). Used by the in-page polling JS."""
    try:
        data = list_spot_coin_positions(cache_ttl_seconds=10)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    tp_map: dict[str, dict] = {}
    try:
        open_orders = get_open_orders()
        for o in open_orders:
            side = str(o.get("side", "")).upper()
            otype = str(o.get("type", "")).upper()
            status = str(o.get("status", "")).upper()
            if side != "SELL" or otype != "LIMIT" or status not in {"NEW", "PARTIALLY_FILLED"}:
                continue
            sym = str(o.get("symbol", "")).upper()
            price = float(o.get("price", 0.0) or 0.0)
            orig_qty = float(o.get("origQty", 0.0) or 0.0)
            exec_qty = float(o.get("executedQty", 0.0) or 0.0)
            rem_qty = max(0.0, orig_qty - exec_qty)
            if price <= 0 or rem_qty <= 0:
                continue
            cur = tp_map.get(sym)
            if not cur:
                tp_map[sym] = {"price": price, "qty": rem_qty, "count": 1}
            else:
                cur["count"] = int(cur.get("count", 1)) + 1
                if price < float(cur.get("price", price)):
                    cur["price"] = price
                    cur["qty"] = rem_qty
    except Exception:
        tp_map = {}

    rows = []
    for r in data.get("rows", []):
        sym = str(r.get("symbol", "")).upper()
        tp = tp_map.get(sym)
        rows.append(
            {
                "symbol": sym,
                "price": float(r.get("price", 0.0)),
                "qty_total": float(r.get("qty_total", 0.0)),
                "qty_free": float(r.get("qty_free", 0.0)),
                "market_value": float(r.get("market_value", 0.0)),
                "pnl": float(r.get("pnl", 0.0)),
                "pnl_pct": float(r.get("pnl_pct", 0.0)),
                "status": str(r.get("status", "flat")),
                "tp_price": float(tp["price"]) if tp else None,
                "tp_qty": float(tp["qty"]) if tp else None,
                "tp_count": int(tp["count"]) if tp else 0,
            }
        )

    summary = data.get("summary", {})
    return JSONResponse(
        {
            "rows": rows,
            "summary": {
                "coins_count": int(summary.get("coins_count", 0)),
                "invested_total": float(summary.get("invested_total", 0.0)),
                "market_total": float(summary.get("market_total", 0.0)),
                "pnl_total": float(summary.get("pnl_total", 0.0)),
                "pnl_pct": float(summary.get("pnl_pct", 0.0)),
            },
        }
    )


@app.get("/live/all-coins/api/data")
async def live_all_coins_data_api() -> JSONResponse:
    cached = _get_all_coins_cached_payload("live_all_coins_full", max_age_seconds=30)
    if cached is not None:
        return JSONResponse(cached)

    db = SessionLocal()
    try:
        payload = _build_all_coins_page_payload(
            db,
            list_positions_fn=list_spot_coin_positions,
            get_open_orders_fn=get_open_orders,
            cache_ttl_seconds=max(60, int(settings.medium_refresh_seconds)),
            include_forecasts=True,
        )
        _set_all_coins_cached_payload("live_all_coins_full", payload)
        return JSONResponse(payload)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        db.close()


@app.post("/live/all-coins/{symbol}/tp")
async def live_all_coins_set_tp(symbol: str, tp_price: str = Form(...)) -> RedirectResponse:
    db = SessionLocal()
    try:
        sym = str(symbol or "").strip().upper()
        target = float(_safe_float(tp_price, 0.0) or 0.0)
        if not sym.endswith("USDT") or len(sym) < 7 or target <= 0:
            add_live_log(db, "LIVE_ALLCOIN_TP_FAIL", sym or "-", "invalid_symbol_or_tp")
            db.commit()
            return RedirectResponse("/live/all-coins", status_code=303)

        open_orders = get_open_orders(sym)
        canceled = 0
        for o in open_orders:
            side = str(o.get("side", "")).upper()
            otype = str(o.get("type", "")).upper()
            status = str(o.get("status", "")).upper()
            if side == "SELL" and otype == "LIMIT" and status in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
                oid = int(o.get("orderId", 0) or 0)
                if oid > 0:
                    try:
                        cancel_order(sym, oid)
                        canceled += 1
                    except Exception:
                        continue

        tradable_qty, min_qty, _ = normalize_qty_for_sell(sym, 1e18, cap_to_free_balance=True)
        if tradable_qty <= 0 or tradable_qty < min_qty:
            add_live_log(
                db,
                "LIVE_ALLCOIN_TP_SKIP",
                sym,
                (
                    f"skip_set_tp | reason=below_min_lot | tradable={tradable_qty:.8f} | "
                    f"min_qty={min_qty:.8f} | canceled_old={canceled}"
                ),
            )
            db.commit()
            return RedirectResponse("/live/all-coins", status_code=303)

        order = place_limit_sell_qty(sym, tradable_qty, target)
        add_live_log(
            db,
            "LIVE_ALLCOIN_TP_SET",
            sym,
            (
                f"Set TP from All Coins | TP={target:.8f} | Qty={tradable_qty:.8f} | "
                f"OrderId={int(order.get('order_id', 0) or 0)} | canceled_old={canceled}"
            ),
        )
        db.commit()
        return RedirectResponse("/live/all-coins", status_code=303)
    except Exception as e:
        add_live_log(db, "LIVE_ALLCOIN_TP_FAIL", str(symbol or "-").upper(), f"error={e}")
        db.commit()
        return RedirectResponse("/live/all-coins", status_code=303)
    finally:
        _clear_all_coins_cached_payload("live_all_coins_full")
        db.close()


@app.post("/live/all-coins/{symbol}/cancel-sell-orders")
async def live_all_coins_cancel_symbol_sell_orders(symbol: str) -> RedirectResponse:
    db = SessionLocal()
    try:
        sym = str(symbol or "").strip().upper()
        if not sym.endswith("USDT") or len(sym) < 7:
            add_live_log(db, "LIVE_ALLCOIN_CANCEL_SELL_FAIL", sym or "-", "invalid_symbol")
            db.commit()
            return RedirectResponse("/live/all-coins", status_code=303)

        canceled = 0
        failed = 0
        open_orders = get_open_orders(sym)
        for o in open_orders:
            side = str(o.get("side", "")).upper()
            otype = str(o.get("type", "")).upper()
            status = str(o.get("status", "")).upper()
            if side != "SELL" or otype != "LIMIT" or status not in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
                continue
            oid = int(o.get("orderId", 0) or 0)
            if oid <= 0:
                continue
            try:
                cancel_order(sym, oid)
                canceled += 1
            except Exception:
                failed += 1

        add_live_log(
            db,
            "LIVE_ALLCOIN_CANCEL_SELL",
            sym,
            f"Canceled SELL LIMIT orders for symbol={sym} | canceled={canceled} | failed={failed}",
        )
        db.commit()
        return RedirectResponse("/live/all-coins", status_code=303)
    except Exception as e:
        add_live_log(db, "LIVE_ALLCOIN_CANCEL_SELL_FAIL", str(symbol or "-").upper(), f"error={e}")
        db.commit()
        return RedirectResponse("/live/all-coins", status_code=303)
    finally:
        _clear_all_coins_cached_payload("live_all_coins_full")
        db.close()


@app.post("/live/all-coins/cancel-all-sell-orders")
async def live_all_coins_cancel_all_sell_orders() -> RedirectResponse:
    db = SessionLocal()
    try:
        canceled = 0
        failed = 0
        try:
            orders = get_open_orders()
        except Exception as e:
            add_live_log(db, "LIVE_ALLCOIN_CANCEL_SELLS_FAIL", "-", f"error={e}")
            db.commit()
            return RedirectResponse("/live/all-coins", status_code=303)

        for o in orders:
            side = str(o.get("side", "")).upper()
            otype = str(o.get("type", "")).upper()
            status = str(o.get("status", "")).upper()
            if side != "SELL" or otype != "LIMIT" or status not in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
                continue
            sym = str(o.get("symbol", "")).upper()
            oid = int(o.get("orderId", 0) or 0)
            if oid <= 0 or not sym:
                continue
            try:
                cancel_order(sym, oid)
                canceled += 1
            except Exception:
                failed += 1

        add_live_log(
            db,
            "LIVE_ALLCOIN_CANCEL_SELLS",
            "-",
            f"Canceled SELL LIMIT orders={canceled} | failed={failed}",
        )
        db.commit()
        return RedirectResponse("/live/all-coins", status_code=303)
    finally:
        _clear_all_coins_cached_payload("live_all_coins_full")
        db.close()


@app.post("/live/all-coins/{symbol}/close")
async def live_all_coins_close(symbol: str) -> RedirectResponse:
    db = SessionLocal()
    try:
        sym = str(symbol or "").strip().upper()
        if not sym.endswith("USDT") or len(sym) < 7:
            add_live_log(db, "LIVE_ALLCOIN_CLOSE_FAIL", sym or "-", "invalid_symbol")
            db.commit()
            return RedirectResponse("/live/all-coins", status_code=303)

        try:
            cancel_open_orders(sym)
        except Exception:
            pass

        free_base = 0.0
        locked_base = 0.0
        try:
            base_asset = sym.replace("USDT", "")
            bal = get_balances().get(base_asset, {})
            free_base = float(bal.get("free", 0.0))
            locked_base = float(bal.get("locked", 0.0))
        except Exception:
            pass

        tradable_qty, min_qty, _ = normalize_qty_for_sell(sym, 1e18, cap_to_free_balance=True)
        if tradable_qty <= 0 or tradable_qty < min_qty:
            reason = (
                f"skip_manual_close | reason=below_min_lot | tradable={tradable_qty:.8f} | "
                f"free={free_base:.8f} | locked={locked_base:.8f} | min_qty={min_qty:.8f}"
            )
            add_live_log(db, "LIVE_ALLCOIN_CLOSE_SKIP", sym, reason)
            db.commit()
            return RedirectResponse("/live/all-coins", status_code=303)

        sell = place_market_sell_qty(sym, 1e18)
        exec_qty = float(sell.get("executed_qty", 0.0) or 0.0)
        proceeds = float(sell.get("quote_qty", 0.0) or 0.0)
        avg_price = float(sell.get("avg_price", 0.0) or 0.0)
        order_id = int(float(sell.get("order_id", 0.0) or 0.0))
        fee_usdt = 0.0
        if order_id > 0:
            try:
                fee_usdt = float(get_order_fee_usdt(sym, order_id))
            except Exception:
                fee_usdt = 0.0

        open_positions = (
            db.query(Position)
            .join(Campaign, Campaign.id == Position.campaign_id)
            .filter(
                Campaign.mode == "live",
                Position.symbol == sym,
                Position.status == "open",
            )
            .all()
        )
        now = datetime.utcnow()
        total_bot_qty = sum(float(p.total_qty or 0.0) for p in open_positions)
        if total_bot_qty <= 0:
            db.commit()
            return RedirectResponse("/live/all-coins", status_code=303)

        # Only attribute to bot positions the quantity that belongs to them.
        alloc_exec_qty_total = min(exec_qty, total_bot_qty)
        proceeds_for_bot = proceeds * (alloc_exec_qty_total / exec_qty) if exec_qty > 0 else 0.0
        fee_for_bot = fee_usdt * (alloc_exec_qty_total / exec_qty) if exec_qty > 0 else 0.0

        for p in open_positions:
            qty_before = float(p.total_qty or 0.0)
            if qty_before <= 0:
                continue
            qty_share = qty_before / total_bot_qty
            sold_qty = min(qty_before, alloc_exec_qty_total * qty_share)
            if sold_qty <= 0:
                continue

            invested_before = float(p.total_invested_usdt or 0.0)
            avg_before = (invested_before / qty_before) if qty_before > 0 else float(p.average_price or 0.0)
            cost_closed = min(invested_before, avg_before * sold_qty)

            alloc_proceeds = proceeds_for_bot * qty_share
            alloc_fee = fee_for_bot * qty_share
            realized_piece = (alloc_proceeds - alloc_fee) - cost_closed

            p.close_fee_usdt = float(p.close_fee_usdt or 0.0) + alloc_fee
            p.tp_order_id = None
            p.tp_order_price = None
            p.tp_order_qty = None

            remaining_qty = max(0.0, qty_before - sold_qty)
            if remaining_qty <= max(1e-12, qty_before * 0.001):
                p.status = "closed"
                p.closed_at = now
                p.close_price = avg_price if avg_price > 0 else float(p.average_price or p.initial_price or 0.0)
                p.realized_pnl_usdt = float(p.realized_pnl_usdt or 0.0) + realized_piece
                p.close_reason = "MANUAL_ALL_COINS"
                p.total_qty = 0.0
                p.total_invested_usdt = 0.0
            else:
                p.total_qty = remaining_qty
                p.total_invested_usdt = max(0.0, invested_before - cost_closed)
                p.average_price = (p.total_invested_usdt / p.total_qty) if p.total_qty > 0 else float(p.average_price or 0.0)
                add_live_log(
                    db,
                    "LIVE_ALLCOIN_PARTIAL",
                    sym,
                    (
                        f"Campaign={p.campaign.name} | SoldQty={sold_qty:.8f}/{qty_before:.8f} "
                        f"| RealizedPiece={realized_piece:+.2f} | RemainingQty={p.total_qty:.8f}"
                    ),
                )
                try:
                    if p.campaign.tp_pct is not None:
                        _arm_or_rearm_tp_order(db, p, p.campaign, force_rearm=True)
                except Exception as e:
                    add_live_log(db, "LIVE_TP_REARM_FAIL", p.symbol, f"Campaign={p.campaign.name} | error={e}")

        add_live_log(
            db,
            "LIVE_ALLCOIN_CLOSE",
            sym,
            (
                f"Manual close from All Coins | ExecQty={exec_qty:.8f} | Proceeds={proceeds:.2f} "
                f"| Fee={fee_usdt:.4f} | ClosedBotPositions={len(open_positions)}"
            ),
        )
        db.commit()
        return RedirectResponse("/live/all-coins", status_code=303)
    except Exception as e:
        add_live_log(db, "LIVE_ALLCOIN_CLOSE_FAIL", str(symbol or "-").upper(), f"error={e}")
        db.commit()
        return RedirectResponse("/live/all-coins", status_code=303)
    finally:
        _clear_all_coins_cached_payload("live_all_coins_full")
        db.close()


@app.get("/live/all-coins-binance-2", response_class=HTMLResponse)
async def live_all_coins_binance_2_page(
    request: Request,
    symbol: str = "",
    refresh_forecast: int = 0,
) -> HTMLResponse:
    db = SessionLocal()
    try:
        error = None
        symbol_query = str(symbol or "").upper().strip()
        forecast_card = None
        data = _empty_all_coins_data()
        data["summary"] = _get_all_coins_cached_summary("live_all_coins_binance_2_full")
        try:
            if symbol_query:
                qsym = symbol_query if symbol_query.endswith("USDT") else f"{symbol_query}USDT"
                try:
                    forecast_card = get_or_build_forecast(
                        db,
                        qsym,
                        force_refresh=bool(int(refresh_forecast)),
                        interval=settings.forecast_interval,
                        horizon_days=settings.forecast_horizon_days,
                    )
                except Exception as e:
                    forecast_card = {"symbol": qsym, "error": str(e)}
        except Exception as e:
            error = str(e)
        logs = (
            db.query(ActivityLog)
            .filter(ActivityLog.event_type.like("LIVE2_ALLCOIN_%"))
            .order_by(desc(ActivityLog.id))
            .limit(60)
            .all()
        )
        return templates.TemplateResponse(
            "live_all_coins_binance_2.html",
            _context(
                "live_all_coins_binance_2",
                request=request,
                data=data,
                logs=logs,
                all_coins_error=error,
                forecast_symbol=symbol_query,
                forecast_card=forecast_card,
                forecast_build_per_request=max(1, int(settings.forecast_build_per_request)),
                data_api_url="/live/all-coins-binance-2/api/data",
                actions_prefix="/live/all-coins-binance-2",
            ),
        )
    finally:
        db.close()


@app.get("/live/all-coins-binance-2/api/prices")
async def live_all_coins_binance_2_prices_api() -> JSONResponse:
    try:
        data = list_spot_coin_positions_2(cache_ttl_seconds=10)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    tp_map: dict[str, dict] = {}
    try:
        tp_map = _build_tp_map_from_orders(get_open_orders_2())
    except Exception:
        tp_map = {}

    rows = []
    for r in data.get("rows", []):
        sym = str(r.get("symbol", "")).upper()
        tp = tp_map.get(sym)
        rows.append(
            {
                "symbol": sym,
                "price": float(r.get("price", 0.0)),
                "qty_total": float(r.get("qty_total", 0.0)),
                "qty_free": float(r.get("qty_free", 0.0)),
                "market_value": float(r.get("market_value", 0.0)),
                "pnl": float(r.get("pnl", 0.0)),
                "pnl_pct": float(r.get("pnl_pct", 0.0)),
                "status": str(r.get("status", "flat")),
                "tp_price": float(tp["price"]) if tp else None,
                "tp_qty": float(tp["qty"]) if tp else None,
                "tp_count": int(tp["count"]) if tp else 0,
            }
        )

    summary = data.get("summary", {})
    return JSONResponse(
        {
            "rows": rows,
            "summary": {
                "coins_count": int(summary.get("coins_count", 0)),
                "invested_total": float(summary.get("invested_total", 0.0)),
                "market_total": float(summary.get("market_total", 0.0)),
                "pnl_total": float(summary.get("pnl_total", 0.0)),
                "pnl_pct": float(summary.get("pnl_pct", 0.0)),
            },
        }
    )


@app.get("/live/all-coins-binance-2/api/data")
async def live_all_coins_binance_2_data_api() -> JSONResponse:
    cached = _get_all_coins_cached_payload("live_all_coins_binance_2_full", max_age_seconds=30)
    if cached is not None:
        return JSONResponse(cached)

    db = SessionLocal()
    try:
        payload = _build_all_coins_page_payload(
            db,
            list_positions_fn=list_spot_coin_positions_2,
            get_open_orders_fn=get_open_orders_2,
            cache_ttl_seconds=max(60, int(settings.medium_refresh_seconds)),
            include_forecasts=True,
        )
        _set_all_coins_cached_payload("live_all_coins_binance_2_full", payload)
        return JSONResponse(payload)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        db.close()


@app.post("/live/all-coins-binance-2/{symbol}/tp")
async def live_all_coins_binance_2_set_tp(symbol: str, tp_price: str = Form(...)) -> RedirectResponse:
    db = SessionLocal()
    try:
        sym = str(symbol or "").strip().upper()
        target = float(_safe_float(tp_price, 0.0) or 0.0)
        if not sym.endswith("USDT") or len(sym) < 7 or target <= 0:
            add_live_log(db, "LIVE2_ALLCOIN_TP_FAIL", sym or "-", "invalid_symbol_or_tp")
            db.commit()
            return RedirectResponse("/live/all-coins-binance-2", status_code=303)

        open_orders = get_open_orders_2(sym)
        canceled = 0
        for o in open_orders:
            side = str(o.get("side", "")).upper()
            otype = str(o.get("type", "")).upper()
            status = str(o.get("status", "")).upper()
            if side == "SELL" and otype == "LIMIT" and status in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
                oid = int(o.get("orderId", 0) or 0)
                if oid > 0:
                    try:
                        cancel_order_2(sym, oid)
                        canceled += 1
                    except Exception:
                        continue

        tradable_qty, min_qty, _ = normalize_qty_for_sell_2(sym, 1e18, cap_to_free_balance=True)
        if tradable_qty <= 0 or tradable_qty < min_qty:
            add_live_log(
                db,
                "LIVE2_ALLCOIN_TP_SKIP",
                sym,
                (
                    f"skip_set_tp | reason=below_min_lot | tradable={tradable_qty:.8f} | "
                    f"min_qty={min_qty:.8f} | canceled_old={canceled}"
                ),
            )
            db.commit()
            return RedirectResponse("/live/all-coins-binance-2", status_code=303)

        order = place_limit_sell_qty_2(sym, tradable_qty, target)
        add_live_log(
            db,
            "LIVE2_ALLCOIN_TP_SET",
            sym,
            (
                f"Set TP from All Coins Binance 2 | TP={target:.8f} | Qty={tradable_qty:.8f} | "
                f"OrderId={int(order.get('order_id', 0) or 0)} | canceled_old={canceled}"
            ),
        )
        db.commit()
        return RedirectResponse("/live/all-coins-binance-2", status_code=303)
    except Exception as e:
        add_live_log(db, "LIVE2_ALLCOIN_TP_FAIL", str(symbol or "-").upper(), f"error={e}")
        db.commit()
        return RedirectResponse("/live/all-coins-binance-2", status_code=303)
    finally:
        _clear_all_coins_cached_payload("live_all_coins_binance_2_full")
        db.close()


@app.post("/live/all-coins-binance-2/{symbol}/cancel-sell-orders")
async def live_all_coins_binance_2_cancel_symbol_sell_orders(symbol: str) -> RedirectResponse:
    db = SessionLocal()
    try:
        sym = str(symbol or "").strip().upper()
        if not sym.endswith("USDT") or len(sym) < 7:
            add_live_log(db, "LIVE2_ALLCOIN_CANCEL_SELL_FAIL", sym or "-", "invalid_symbol")
            db.commit()
            return RedirectResponse("/live/all-coins-binance-2", status_code=303)

        canceled = 0
        failed = 0
        open_orders = get_open_orders_2(sym)
        for o in open_orders:
            side = str(o.get("side", "")).upper()
            otype = str(o.get("type", "")).upper()
            status = str(o.get("status", "")).upper()
            if side != "SELL" or otype != "LIMIT" or status not in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
                continue
            oid = int(o.get("orderId", 0) or 0)
            if oid <= 0:
                continue
            try:
                cancel_order_2(sym, oid)
                canceled += 1
            except Exception:
                failed += 1

        add_live_log(
            db,
            "LIVE2_ALLCOIN_CANCEL_SELL",
            sym,
            f"Canceled SELL LIMIT orders for symbol={sym} | canceled={canceled} | failed={failed}",
        )
        db.commit()
        return RedirectResponse("/live/all-coins-binance-2", status_code=303)
    except Exception as e:
        add_live_log(db, "LIVE2_ALLCOIN_CANCEL_SELL_FAIL", str(symbol or "-").upper(), f"error={e}")
        db.commit()
        return RedirectResponse("/live/all-coins-binance-2", status_code=303)
    finally:
        _clear_all_coins_cached_payload("live_all_coins_binance_2_full")
        db.close()


@app.post("/live/all-coins-binance-2/cancel-all-sell-orders")
async def live_all_coins_binance_2_cancel_all_sell_orders() -> RedirectResponse:
    db = SessionLocal()
    try:
        canceled = 0
        failed = 0
        try:
            orders = get_open_orders_2()
        except Exception as e:
            add_live_log(db, "LIVE2_ALLCOIN_CANCEL_SELLS_FAIL", "-", f"error={e}")
            db.commit()
            return RedirectResponse("/live/all-coins-binance-2", status_code=303)

        for o in orders:
            side = str(o.get("side", "")).upper()
            otype = str(o.get("type", "")).upper()
            status = str(o.get("status", "")).upper()
            if side != "SELL" or otype != "LIMIT" or status not in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
                continue
            sym = str(o.get("symbol", "")).upper()
            oid = int(o.get("orderId", 0) or 0)
            if oid <= 0 or not sym:
                continue
            try:
                cancel_order_2(sym, oid)
                canceled += 1
            except Exception:
                failed += 1

        add_live_log(
            db,
            "LIVE2_ALLCOIN_CANCEL_SELLS",
            "-",
            f"Canceled SELL LIMIT orders={canceled} | failed={failed}",
        )
        db.commit()
        return RedirectResponse("/live/all-coins-binance-2", status_code=303)
    finally:
        _clear_all_coins_cached_payload("live_all_coins_binance_2_full")
        db.close()


@app.post("/live/all-coins-binance-2/{symbol}/close")
async def live_all_coins_binance_2_close(symbol: str) -> RedirectResponse:
    db = SessionLocal()
    try:
        sym = str(symbol or "").strip().upper()
        if not sym.endswith("USDT") or len(sym) < 7:
            add_live_log(db, "LIVE2_ALLCOIN_CLOSE_FAIL", sym or "-", "invalid_symbol")
            db.commit()
            return RedirectResponse("/live/all-coins-binance-2", status_code=303)

        try:
            open_orders = get_open_orders_2(sym)
            for o in open_orders:
                side = str(o.get("side", "")).upper()
                status = str(o.get("status", "")).upper()
                oid = int(o.get("orderId", 0) or 0)
                if side == "SELL" and status in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"} and oid > 0:
                    try:
                        cancel_order_2(sym, oid)
                    except Exception:
                        pass
        except Exception:
            pass

        tradable_qty, min_qty, _ = normalize_qty_for_sell_2(sym, 1e18, cap_to_free_balance=True)
        if tradable_qty <= 0 or tradable_qty < min_qty:
            add_live_log(
                db,
                "LIVE2_ALLCOIN_CLOSE_SKIP",
                sym,
                (
                    f"skip_manual_close | reason=below_min_lot | tradable={tradable_qty:.8f} | "
                    f"min_qty={min_qty:.8f}"
                ),
            )
            db.commit()
            return RedirectResponse("/live/all-coins-binance-2", status_code=303)

        sell = place_market_sell_qty_2(sym, tradable_qty)
        exec_qty = float(sell.get("executed_qty", 0.0) or 0.0)
        proceeds = float(sell.get("quote_qty", 0.0) or 0.0)
        avg_price = float(sell.get("avg_price", 0.0) or 0.0)
        add_live_log(
            db,
            "LIVE2_ALLCOIN_CLOSE",
            sym,
            (
                f"Manual close from All Coins Binance 2 | ExecQty={exec_qty:.8f} | "
                f"Proceeds={proceeds:.2f} | AvgPrice={avg_price:.8f}"
            ),
        )
        db.commit()
        return RedirectResponse("/live/all-coins-binance-2", status_code=303)
    except Exception as e:
        add_live_log(db, "LIVE2_ALLCOIN_CLOSE_FAIL", str(symbol or "-").upper(), f"error={e}")
        db.commit()
        return RedirectResponse("/live/all-coins-binance-2", status_code=303)
    finally:
        _clear_all_coins_cached_payload("live_all_coins_binance_2_full")
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


@app.get("/live/campaigns/{campaign_id}/api/prices")
async def live_campaign_prices_api(campaign_id: int) -> JSONResponse:
    """Return live mark prices and PnL for open positions in a campaign. Used by in-page polling JS."""
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.id == campaign_id, Campaign.mode == "live").first()
        if not campaign:
            return JSONResponse({"error": "not found"}, status_code=404)

        positions = db.query(Position).filter(Position.campaign_id == campaign_id).order_by(desc(Position.id)).all()
        open_symbols = [p.symbol for p in positions if p.status == "open"]
        prices = get_prices(open_symbols) if open_symbols else {}
        stats = _campaign_stats(db, campaign)

        position_rows = []
        for p in positions:
            if p.status == "open":
                mark = float(prices.get(p.symbol, p.average_price))
                pnl = (mark * float(p.total_qty)) - float(p.total_invested_usdt)
                pnl_pct = (pnl / float(p.total_invested_usdt) * 100.0) if float(p.total_invested_usdt) > 0 else 0.0
            else:
                mark = float(p.close_price or p.average_price)
                pnl = float(p.realized_pnl_usdt or 0.0)
                pnl_pct = (pnl / float(p.total_invested_usdt) * 100.0) if float(p.total_invested_usdt) > 0 else 0.0
            position_rows.append(
                {
                    "id": p.id,
                    "mark": mark,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                }
            )

        return JSONResponse(
            {
                "stats": {
                    "open_count": stats["open_count"],
                    "realized_pnl": float(stats["realized_pnl"]),
                    "unrealized_pnl": float(stats["unrealized_pnl"]),
                },
                "positions": position_rows,
            }
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
            try:
                scan = suggest_top_symbols(
                    max(loop_target, 10),
                    use_v2=loop_v2_mode,
                    max_candidates=max(18, loop_target * 2),
                )
            except Exception:
                # Safe fallback so campaign creation never crashes on scanner errors.
                scan = suggest_top_symbols(max(loop_target, 10), use_v2=False, max_candidates=max(18, loop_target * 2))
            picked = [
                str(item.get("symbol", "")).upper()
                for item in (scan.get("items") or [])
                if item.get("symbol") and str(item.get("symbol", "")).upper() != "BTCUSDT"
            ]
            picked = picked[:loop_target]
        if not picked and not loop_mode:
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

        if loop_mode and not picked:
            opened, errors = 0, []
            db.add(
                ActivityLog(
                    event_type="LOOP_WAIT",
                    symbol="-",
                    message=(
                        f"Campaign '{campaign.name}' created with 0 initial picks. "
                        "Loop engine will keep scanning and open when candidates appear."
                    ),
                )
            )
            db.commit()
        else:
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


@app.post("/paper/smart-campaigns")
async def create_paper_smart_campaign(
    request: Request,
    name: str = Form(...),
    symbol: str = Form(...),
    entry_amount_usdt: str = Form(...),
    tp_pct: str = Form(""),
    sl_pct: str = Form(""),
    strategy_mode: str = Form("auto"),
    trend_filter_enabled: str | None = Form(None),
    strict_support_score_required: str | None = Form(None),
) -> RedirectResponse:
    db = SessionLocal()
    try:
        entry_amount = _safe_float(entry_amount_usdt, 0.0) or 0.0
        if entry_amount <= 0:
            return RedirectResponse("/paper/smart-create", status_code=303)

        symbol_clean = str(symbol or "").strip().upper()
        if not symbol_clean:
            return RedirectResponse("/paper/smart-create", status_code=303)

        tp_value = _safe_float(tp_pct, None)
        sl_value = _safe_float(sl_pct, None)
        trend_mode = str(trend_filter_enabled or "").lower() in {"on", "true", "1", "yes"}
        strict_mode = str(strict_support_score_required or "").lower() in {"on", "true", "1", "yes"}

        plan = build_smart_dca_plan(
            symbol=symbol_clean,
            entry_amount_usdt=entry_amount,
            tp_pct=tp_value,
            sl_pct=sl_value,
            max_levels=5,
            strategy_mode=strategy_mode,
        )
        if not bool(plan.get("ok")):
            db.add(
                ActivityLog(
                    event_type="SMART_DCA_FAIL",
                    symbol=symbol_clean,
                    message=f"Create failed: {plan.get('error', 'unknown error')}",
                )
            )
            db.commit()
            return RedirectResponse("/paper/smart-create", status_code=303)

        campaign = Campaign(
            name=name.strip() or f"SMART DCA {symbol_clean}",
            mode="paper",
            status="active",
            entry_amount_usdt=entry_amount,
            tp_pct=tp_value,
            sl_pct=sl_value,
            ai_dca_enabled=True,
            smart_dca_enabled=True,
            strict_support_score_required=strict_mode,
            trend_filter_enabled=trend_mode,
            auto_reentry_enabled=False,
            loop_enabled=False,
            loop_v2_enabled=False,
            loop_target_count=0,
            ai_dca_profile=f"smart_weighted_{str(strategy_mode or 'auto').strip().lower()}",
            ai_dca_notes=str(plan.get("note", "Smart weighted DCA.")),
            ai_dca_suggested_rules_json=json.dumps(plan.get("rules", [])),
        )
        db.add(campaign)
        db.flush()

        for row in plan.get("rules", []):
            try:
                drop_pct = float(row.get("drop_pct", 0.0))
                alloc_pct = float(row.get("allocation_pct", 0.0))
            except Exception:
                continue
            if drop_pct <= 0 or alloc_pct <= 0:
                continue
            db.add(
                DcaRule(
                    campaign_id=campaign.id,
                    name=str(row.get("name", "SMART-DCA")).strip() or "SMART-DCA",
                    drop_pct=drop_pct,
                    allocation_pct=alloc_pct,
                )
            )

        db.add(
            ActivityLog(
                event_type="SMART_DCA_PLAN",
                symbol=symbol_clean,
                message=(
                    f"Campaign='{campaign.name}' | entry={entry_amount:.2f} | "
                    f"estimate_total={float(plan['estimate']['total_if_all_filled_usdt']):.2f} | "
                    f"levels={len(plan.get('rules', []))}"
                ),
            )
        )
        db.commit()

        opened, errors = create_campaign_positions(db, campaign, [symbol_clean])
        if errors:
            campaign.status = "paused"
            db.add(
                ActivityLog(
                    event_type="CAMPAIGN_ERROR",
                    symbol=symbol_clean,
                    message=f"Campaign '{campaign.name}' failed to open position: {' | '.join(errors)}",
                )
            )
            db.commit()
        elif opened > 0:
            db.add(
                ActivityLog(
                    event_type="SMART_DCA",
                    symbol=symbol_clean,
                    message=f"Campaign '{campaign.name}' started with SMART DCA plan.",
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
async def create_live_campaign_route(
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
            try:
                scan = suggest_top_symbols(
                    max(loop_target, 10),
                    use_v2=loop_v2_mode,
                    max_candidates=max(18, loop_target * 2),
                )
            except Exception:
                scan = suggest_top_symbols(max(loop_target, 10), use_v2=False, max_candidates=max(18, loop_target * 2))
            picked = [
                str(item.get("symbol", "")).upper()
                for item in (scan.get("items") or [])
                if item.get("symbol") and str(item.get("symbol", "")).upper() != "BTCUSDT"
            ]
            picked = picked[:loop_target]
        if not picked and not loop_mode:
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
        if loop_mode and not picked:
            opened, errors = 0, []
            db.add(
                ActivityLog(
                    event_type="LIVE_LOOP_WAIT",
                    symbol="-",
                    message=(
                        f"Campaign '{campaign.name}' created with 0 initial picks. "
                        "Live loop engine will keep scanning and open when candidates appear."
                    ),
                )
            )
            db.commit()
        else:
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


@app.post("/live/smart-campaigns")
async def create_live_smart_campaign(
    request: Request,
    name: str = Form(...),
    symbol: str = Form(...),
    entry_amount_usdt: str = Form(...),
    tp_pct: str = Form(""),
    sl_pct: str = Form(""),
    strategy_mode: str = Form("auto"),
    trend_filter_enabled: str | None = Form(None),
    strict_support_score_required: str | None = Form(None),
) -> RedirectResponse:
    db = SessionLocal()
    try:
        entry_amount = _safe_float(entry_amount_usdt, 0.0) or 0.0
        if entry_amount <= 0:
            return RedirectResponse("/live/smart-create", status_code=303)
        symbol_clean = str(symbol or "").strip().upper()
        if not symbol_clean:
            return RedirectResponse("/live/smart-create", status_code=303)

        tp_value = _safe_float(tp_pct, None)
        sl_value = _safe_float(sl_pct, None)
        trend_mode = str(trend_filter_enabled or "").lower() in {"on", "true", "1", "yes"}
        strict_mode = str(strict_support_score_required or "").lower() in {"on", "true", "1", "yes"}

        plan = build_smart_dca_plan(
            symbol=symbol_clean,
            entry_amount_usdt=entry_amount,
            tp_pct=tp_value,
            sl_pct=sl_value,
            max_levels=5,
            strategy_mode=strategy_mode,
        )
        if not bool(plan.get("ok")):
            db.add(
                ActivityLog(
                    event_type="LIVE_SMART_DCA_FAIL",
                    symbol=symbol_clean,
                    message=f"Create failed: {plan.get('error', 'unknown error')}",
                )
            )
            db.commit()
            return RedirectResponse("/live/smart-create", status_code=303)

        campaign = Campaign(
            name=name.strip() or f"LIVE SMART DCA {symbol_clean}",
            mode="live",
            status="active",
            entry_amount_usdt=entry_amount,
            tp_pct=tp_value,
            sl_pct=sl_value,
            ai_dca_enabled=True,
            smart_dca_enabled=True,
            strict_support_score_required=strict_mode,
            trend_filter_enabled=trend_mode,
            auto_reentry_enabled=False,
            loop_enabled=False,
            loop_v2_enabled=False,
            loop_target_count=0,
            ai_dca_profile=f"smart_weighted_{str(strategy_mode or 'auto').strip().lower()}",
            ai_dca_notes=str(plan.get("note", "Smart weighted DCA.")),
            ai_dca_suggested_rules_json=json.dumps(plan.get("rules", [])),
        )
        db.add(campaign)
        db.flush()

        for row in plan.get("rules", []):
            try:
                drop_pct = float(row.get("drop_pct", 0.0))
                alloc_pct = float(row.get("allocation_pct", 0.0))
            except Exception:
                continue
            if drop_pct <= 0 or alloc_pct <= 0:
                continue
            db.add(
                DcaRule(
                    campaign_id=campaign.id,
                    name=str(row.get("name", "SMART-DCA")).strip() or "SMART-DCA",
                    drop_pct=drop_pct,
                    allocation_pct=alloc_pct,
                )
            )
        db.add(
            ActivityLog(
                event_type="LIVE_SMART_DCA_PLAN",
                symbol=symbol_clean,
                message=(
                    f"Campaign='{campaign.name}' | entry={entry_amount:.2f} | "
                    f"estimate_total={float(plan['estimate']['total_if_all_filled_usdt']):.2f} | "
                    f"levels={len(plan.get('rules', []))}"
                ),
            )
        )
        db.commit()

        try:
            opened, errors = create_live_campaign_positions(db, campaign, [symbol_clean])
        except Exception as e:
            errors = [str(e)]
            opened = 0

        if errors:
            campaign.status = "paused"
            db.add(
                ActivityLog(
                    event_type="LIVE_CAMPAIGN_ERROR",
                    symbol=symbol_clean,
                    message=f"Campaign '{campaign.name}' failed to open position: {' | '.join(errors)}",
                )
            )
            db.commit()
        elif opened > 0:
            db.add(
                ActivityLog(
                    event_type="LIVE_SMART_DCA",
                    symbol=symbol_clean,
                    message=f"Campaign '{campaign.name}' started with SMART DCA plan.",
                )
            )
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
    from app.services.binance_live import cancel_order, get_order_fee_usdt, place_market_sell_qty

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
        qty_before = float(pos.total_qty or 0.0)
        if qty_before <= 0:
            return RedirectResponse(f"/live/campaigns/{campaign_id}", status_code=303)

        sell = place_market_sell_qty(pos.symbol, qty_before)
        proceeds = float(sell["quote_qty"])
        exec_qty = float(sell.get("executed_qty", 0.0) or 0.0)
        close_price = float(sell["avg_price"] or 0.0)
        sell_order_id = int(float(sell.get("order_id", 0.0) or 0.0))
        sell_fee_usdt = 0.0
        if sell_order_id > 0:
            try:
                sell_fee_usdt = float(get_order_fee_usdt(pos.symbol, sell_order_id))
            except Exception:
                sell_fee_usdt = 0.0
        if exec_qty <= 0:
            add_live_log(db, "LIVE_MANUAL_SELL_FAIL", pos.symbol, f"Campaign={pos.campaign.name} | reason=zero_exec_qty")
            db.commit()
            return RedirectResponse(f"/live/campaigns/{campaign_id}", status_code=303)

        invested_before = float(pos.total_invested_usdt or 0.0)
        avg_before = (invested_before / qty_before) if qty_before > 0 else float(pos.average_price or 0.0)
        sold_qty = min(exec_qty, qty_before)
        cost_closed = min(invested_before, avg_before * sold_qty)
        realized_piece = (proceeds - sell_fee_usdt) - cost_closed

        remaining_qty = max(0.0, qty_before - sold_qty)
        if remaining_qty <= max(1e-12, qty_before * 0.001):
            pos.status = "closed"
            pos.closed_at = datetime.utcnow()
            pos.close_price = close_price
            pos.realized_pnl_usdt = float(pos.realized_pnl_usdt or 0.0) + realized_piece
            pos.close_reason = "MANUAL_SELL"
            pos.total_qty = 0.0
            pos.total_invested_usdt = 0.0
            pos.tp_order_id = None
            pos.tp_order_price = None
            pos.tp_order_qty = None
            log_event = "LIVE_MANUAL_SELL"
            log_msg = (
                f"Campaign={pos.campaign.name} | Close={close_price:.6f} | Qty={sold_qty:.8f} "
                f"| Proceeds={proceeds:.2f} | PnL={realized_piece:+.2f}"
            )
        else:
            pos.total_qty = remaining_qty
            pos.total_invested_usdt = max(0.0, invested_before - cost_closed)
            pos.average_price = (pos.total_invested_usdt / pos.total_qty) if pos.total_qty > 0 else float(pos.average_price or 0.0)
            pos.close_reason = "MANUAL_PARTIAL_SELL"
            pos.tp_order_id = None
            pos.tp_order_price = None
            pos.tp_order_qty = None
            log_event = "LIVE_MANUAL_SELL_PARTIAL"
            log_msg = (
                f"Campaign={pos.campaign.name} | SoldQty={sold_qty:.8f}/{qty_before:.8f} | Close={close_price:.6f} "
                f"| Proceeds={proceeds:.2f} | RealizedPiece={realized_piece:+.2f} | RemainingQty={pos.total_qty:.8f}"
            )
            try:
                if pos.campaign.tp_pct is not None:
                    _arm_or_rearm_tp_order(db, pos, pos.campaign, force_rearm=True)
            except Exception as e:
                add_live_log(db, "LIVE_TP_REARM_FAIL", pos.symbol, f"Campaign={pos.campaign.name} | error={e}")

        pos.close_fee_usdt = float(pos.close_fee_usdt or 0.0) + sell_fee_usdt
        db.add(
            ActivityLog(
                event_type=log_event,
                symbol=pos.symbol,
                message=log_msg,
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


@app.get("/api/live/smart-plan")
async def api_live_smart_plan(
    symbol: str,
    entry_amount_usdt: float = 0.0,
    total_budget_usdt: float | None = None,
    tp_pct: float | None = None,
    sl_pct: float | None = None,
    strategy_mode: str = "auto",
) -> JSONResponse:
    entry = float(entry_amount_usdt or 0.0)
    budget = float(total_budget_usdt or 0.0)
    if budget > 0 and entry <= 0:
        seed_entry = 100.0
        seed = build_smart_dca_plan(
            symbol=symbol,
            entry_amount_usdt=seed_entry,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            max_levels=5,
            strategy_mode=strategy_mode,
        )
        if not seed.get("ok"):
            return JSONResponse(seed)
        mult = float(seed.get("capital_planning", {}).get("total_multiplier_sum", 0.0))
        if mult <= 0:
            return JSONResponse({"ok": False, "error": "Could not derive entry from total budget."})
        entry = budget / mult
    plan = build_smart_dca_plan(
        symbol=symbol,
        entry_amount_usdt=entry,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        max_levels=5,
        strategy_mode=strategy_mode,
    )
    if plan.get("ok") and budget > 0:
        plan["capital_planning"]["requested_total_budget_usdt"] = round(budget, 4)
        plan["capital_planning"]["derived_entry_from_budget_usdt"] = round(entry, 4)
        plan["capital_planning"]["input_mode"] = "total_budget"
    elif plan.get("ok"):
        plan["capital_planning"]["input_mode"] = "base_entry"
    return JSONResponse(plan)


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


@app.get("/api/smart-backtest")
async def api_smart_backtest(
    symbol: str,
    strategy_mode: str = "auto",
    entry_amount_usdt: float = 15.0,
    tp_pct: float = 1.5,
    sl_pct: float | None = 5.0,
    interval: str = "1h",
    candles: int = 700,
) -> JSONResponse:
    res = run_smart_backtest(
        symbol=symbol,
        strategy_mode=strategy_mode,
        entry_amount_usdt=entry_amount_usdt,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        interval=interval,
        candles=candles,
    )
    return JSONResponse(res)


@app.get("/api/paper/suggestions")
async def api_paper_suggestions(limit: int = 5, v2: int = 0) -> JSONResponse:
    safe_limit = min(max(int(limit), 1), 10)
    data = suggest_top_symbols(safe_limit, use_v2=bool(v2))
    return JSONResponse(data)


@app.get("/api/paper/smart-plan")
async def api_paper_smart_plan(
    symbol: str,
    entry_amount_usdt: float = 0.0,
    total_budget_usdt: float | None = None,
    tp_pct: float | None = None,
    sl_pct: float | None = None,
    strategy_mode: str = "auto",
) -> JSONResponse:
    entry = float(entry_amount_usdt or 0.0)
    budget = float(total_budget_usdt or 0.0)
    if budget > 0 and entry <= 0:
        seed_entry = 100.0
        seed = build_smart_dca_plan(
            symbol=symbol,
            entry_amount_usdt=seed_entry,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            max_levels=5,
            strategy_mode=strategy_mode,
        )
        if not seed.get("ok"):
            return JSONResponse(seed)
        mult = float(seed.get("capital_planning", {}).get("total_multiplier_sum", 0.0))
        if mult <= 0:
            return JSONResponse({"ok": False, "error": "Could not derive entry from total budget."})
        entry = budget / mult
    plan = build_smart_dca_plan(
        symbol=symbol,
        entry_amount_usdt=entry,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        max_levels=5,
        strategy_mode=strategy_mode,
    )
    if plan.get("ok") and budget > 0:
        plan["capital_planning"]["requested_total_budget_usdt"] = round(budget, 4)
        plan["capital_planning"]["derived_entry_from_budget_usdt"] = round(entry, 4)
        plan["capital_planning"]["input_mode"] = "total_budget"
    elif plan.get("ok"):
        plan["capital_planning"]["input_mode"] = "base_entry"
    return JSONResponse(plan)


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


# ── Advisor routes ────────────────────────────────────────────────────────────

@app.get("/advisor", response_class=HTMLResponse)
async def advisor_page(request: Request):
    """Advisor dashboard — ML predictions + Hyperopt results."""
    state = advisor_runner.get_state()
    return templates.TemplateResponse("advisor.html", {
        "request": request,
        "state":   state,
    })


@app.post("/advisor/run")
async def advisor_run(
    request: Request,
    symbols:         int = Form(50),
    trials:          int = Form(100),
    feature_version: str = Form("v1"),
):
    """Trigger advisor run in background."""
    version = feature_version if feature_version in ("v1", "v2") else "v1"
    started = advisor_runner.start(n_symbols=symbols, n_trials=trials, feature_version=version)
    if not started:
        return JSONResponse({"ok": False, "msg": "Already running"}, status_code=409)
    return JSONResponse({"ok": True, "msg": f"Started: {symbols} symbols, {trials} trials, features={version.upper()}"})


# ── Smart Campaign routes ──────────────────────────────────────────────────────

@app.get("/api/smart-campaign/capital")
async def api_smart_capital(n: int = 5, entry: float = 100.0) -> JSONResponse:
    """Calculate required capital for N symbols with given entry amount."""
    recs = get_advisor_recommendations()
    data = calculate_required_capital(entry, n, recs)
    return JSONResponse(data)


@app.post("/api/smart-campaign/create")
async def api_smart_create(
    max_symbols:     int   = Form(5),
    entry_amount:    float = Form(100.0),
    feature_version: str   = Form("v1"),
) -> JSONResponse:
    db = SessionLocal()
    try:
        version = feature_version if feature_version in ("v1", "v2") else "v1"
        c = create_campaign(db, max_symbols=max_symbols, entry_amount=entry_amount, feature_version=version)
        return JSONResponse({"ok": True, "id": c.id})
    finally:
        db.close()


@app.post("/api/smart-campaign/{campaign_id}/stop")
async def api_smart_stop(campaign_id: int) -> JSONResponse:
    db = SessionLocal()
    try:
        ok = stop_campaign(db, campaign_id)
        return JSONResponse({"ok": ok})
    finally:
        db.close()


@app.post("/api/smart-campaign/{campaign_id}/resume")
async def api_smart_resume(campaign_id: int) -> JSONResponse:
    db = SessionLocal()
    try:
        ok = resume_campaign(db, campaign_id)
        return JSONResponse({"ok": ok})
    finally:
        db.close()


@app.get("/api/smart-campaign/list")
async def api_smart_list() -> JSONResponse:
    db = SessionLocal()
    try:
        campaigns = db.query(SmartCampaign).order_by(SmartCampaign.created_at.desc()).all()
        return JSONResponse([campaign_summary(db, c) for c in campaigns])
    finally:
        db.close()


@app.get("/api/smart-campaign/dashboard")
async def api_smart_dashboard() -> JSONResponse:
    """Aggregate stats across all smart campaigns for the dashboard."""
    db = SessionLocal()
    try:
        from app.models.smart_campaign import SmartPosition as SP, SmartCampaign as SC
        campaigns = db.query(SC).all()
        positions = db.query(SP).all()

        closed   = [p for p in positions if p.status != "active"]
        active   = [p for p in positions if p.status == "active"]
        won      = [p for p in closed if (p.close_pnl_usdt or 0) > 0]
        lost     = [p for p in closed if (p.close_pnl_usdt or 0) <= 0]

        total_invested  = sum(p.total_invested_usdt or 0 for p in active)
        realized_pnl    = sum(p.close_pnl_usdt or 0 for p in closed)
        open_pnl        = sum(p.pnl_usdt or 0 for p in active)
        total_pnl       = realized_pnl + open_pnl

        win_rate = (len(won) / len(closed) * 100) if closed else 0
        avg_win  = (sum(p.close_pnl_usdt or 0 for p in won)  / len(won))  if won  else 0
        avg_loss = (sum(p.close_pnl_usdt or 0 for p in lost) / len(lost)) if lost else 0

        # Trade log — all positions sorted by latest first
        from datetime import datetime as _dt
        log = []
        for p in sorted(positions, key=lambda x: x.created_at or _dt.min, reverse=True)[:50]:
            log.append({
                "id":            p.id,
                "symbol":        p.symbol,
                "status":        p.status,
                "entry_price":   p.entry_price,
                "avg_price":     p.avg_price,
                "current_price": p.current_price,
                "invested":      p.total_invested_usdt,
                "pnl_pct":       p.pnl_pct,
                "pnl_usdt":      p.pnl_usdt if p.status == "active" else p.close_pnl_usdt,
                "close_reason":  p.close_reason,
                "dca1":          p.dca1_triggered,
                "dca2":          p.dca2_triggered,
                "opened_at":     p.created_at.strftime("%Y-%m-%d %H:%M") if p.created_at else "",
                "closed_at":     p.closed_at.strftime("%Y-%m-%d %H:%M") if p.closed_at else None,
                "campaign_id":   p.campaign_id,
            })

        return JSONResponse({
            "total_campaigns":  len(campaigns),
            "running_campaigns": sum(1 for c in campaigns if c.status == "running"),
            "active_positions": len(active),
            "total_invested":   round(total_invested,  2),
            "realized_pnl":     round(realized_pnl,    2),
            "open_pnl":         round(open_pnl,        2),
            "total_pnl":        round(total_pnl,       2),
            "total_trades":     len(closed),
            "winning_trades":   len(won),
            "losing_trades":    len(lost),
            "win_rate":         round(win_rate, 1),
            "avg_win_usdt":     round(avg_win,  2),
            "avg_loss_usdt":    round(avg_loss, 2),
            "trade_log":        log,
        })
    finally:
        db.close()


@app.post("/api/smart-campaign/reset")
async def api_smart_reset() -> JSONResponse:
    """Delete ALL paper smart campaigns, positions. Keeps advisor analysis intact."""
    db = SessionLocal()
    try:
        db.query(SmartPosition).delete()
        db.query(SmartCampaign).delete()
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()


@app.get("/api/smart-campaign/{campaign_id}")
async def api_smart_detail(campaign_id: int) -> JSONResponse:
    db = SessionLocal()
    try:
        c = db.query(SmartCampaign).filter(SmartCampaign.id == campaign_id).first()
        if not c:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse(campaign_summary(db, c))
    finally:
        db.close()


@app.put("/api/smart-campaign/{campaign_id}")
async def api_smart_edit(campaign_id: int, request: Request) -> JSONResponse:
    body = await request.json()
    db = SessionLocal()
    try:
        c = db.query(SmartCampaign).filter(SmartCampaign.id == campaign_id).first()
        if not c:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if "max_symbols" in body:
            c.max_symbols = int(body["max_symbols"])
        if "entry_amount_usdt" in body:
            c.entry_amount_usdt = float(body["entry_amount_usdt"])
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()


@app.delete("/api/smart-campaign/{campaign_id}")
async def api_smart_delete(campaign_id: int) -> JSONResponse:
    db = SessionLocal()
    try:
        c = db.query(SmartCampaign).filter(SmartCampaign.id == campaign_id).first()
        if not c:
            return JSONResponse({"error": "Not found"}, status_code=404)
        db.query(SmartPosition).filter(SmartPosition.campaign_id == campaign_id).delete()
        db.delete(c)
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()


@app.post("/api/smart-campaign/position/{position_id}/sell")
async def api_smart_sell(position_id: int) -> JSONResponse:
    db = SessionLocal()
    try:
        result = smart_manual_sell(db, position_id)
        return JSONResponse(result)
    finally:
        db.close()


@app.post("/advisor/refresh")
async def advisor_refresh():
    """Trigger a quick ML-only refresh (no Hyperopt, ~1-2 min)."""
    started = advisor_runner.start_refresh()
    if not started:
        return JSONResponse({"ok": False, "msg": "Already running"}, status_code=409)
    return JSONResponse({"ok": True, "msg": "Quick ML refresh started"})


@app.post("/advisor/refresh-v2")
async def advisor_refresh_v2():
    """Trigger a quick ML-only V2 refresh."""
    started = advisor_runner.start_refresh_v2()
    if not started:
        return JSONResponse({"ok": False, "msg": "V2 refresh already running"}, status_code=409)
    return JSONResponse({"ok": True, "msg": "V2 Quick ML refresh started"})


@app.get("/advisor/status")
async def advisor_status():
    """Polling endpoint — returns current advisor state as JSON."""
    return JSONResponse(advisor_runner.get_state())


@app.post("/advisor/run-v2")
async def advisor_run_v2(
    symbols: int = Form(50),
    trials:  int = Form(100),
):
    """Trigger V2 advisor run in background (separate from V1)."""
    started = advisor_runner.start_v2(n_symbols=symbols, n_trials=trials)
    if not started:
        return JSONResponse({"ok": False, "msg": "V2 analysis already running"}, status_code=409)
    return JSONResponse({"ok": True, "msg": f"V2 started: {symbols} symbols, {trials} trials"})


@app.get("/advisor/status-v2")
async def advisor_status_v2():
    """Polling endpoint for V2 advisor state."""
    return JSONResponse(advisor_runner.get_state_v2())


# ═══════════════════════════════════════════════════════════════════════════════
# LIVE SMART CAMPAIGN ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/live-advisor", response_class=HTMLResponse)
async def live_advisor_page(request: Request):
    from app.services.binance_live import is_configured, get_usdt_free
    configured = is_configured()
    balance = 0.0
    if configured:
        try:
            balance = get_usdt_free()
        except Exception:
            pass
    return templates.TemplateResponse(
        "live_advisor.html",
        {"request": request, "configured": configured, "usdt_balance": round(balance, 2)},
    )


@app.get("/api/live-smart/balance")
def api_live_balance():
    from app.services.binance_live import is_configured, get_usdt_free
    if not is_configured():
        return JSONResponse({"ok": False, "error": "API keys not configured"})
    try:
        bal = get_usdt_free()
        return JSONResponse({"ok": True, "usdt_free": round(bal, 2)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/live-smart/create")
def api_live_create_campaign(
    max_symbols:     int   = Form(5),
    entry_amount:    float = Form(50.0),
    feature_version: str   = Form("v1"),
):
    db = SessionLocal()
    try:
        version = feature_version if feature_version in ("v1", "v2") else "v1"
        result = create_live_campaign(db, max_symbols, entry_amount, feature_version=version)
        if not result["ok"]:
            return JSONResponse({"ok": False, "error": result["error"]}, status_code=400)
        c = result["campaign"]
        return JSONResponse({"ok": True, "id": c.id})
    finally:
        db.close()


@app.get("/api/live-smart/list")
async def api_live_list_campaigns():
    db = SessionLocal()
    try:
        from app.models.live_smart_campaign import LiveSmartCampaign as LSC
        campaigns = db.query(LSC).order_by(LSC.id.desc()).all()
        return JSONResponse([live_campaign_summary(db, c) for c in campaigns])
    finally:
        db.close()


@app.post("/api/live-smart/{campaign_id}/stop")
def api_live_stop_campaign(campaign_id: int):
    db = SessionLocal()
    try:
        ok = stop_live_campaign(db, campaign_id)
        return JSONResponse({"ok": ok})
    finally:
        db.close()


@app.post("/api/live-smart/{campaign_id}/resume")
def api_live_resume_campaign(campaign_id: int):
    db = SessionLocal()
    try:
        ok = resume_live_campaign(db, campaign_id)
        return JSONResponse({"ok": ok})
    finally:
        db.close()


@app.post("/api/live-smart/position/{position_id}/sell")
def api_live_manual_sell(position_id: int):
    db = SessionLocal()
    try:
        result = manual_sell_live(db, position_id)
        return JSONResponse(result)
    finally:
        db.close()


@app.get("/api/live-smart/logs")
async def api_live_logs(campaign_id: int = None, limit: int = 100):
    db = SessionLocal()
    try:
        logs = get_recent_logs(db, campaign_id=campaign_id, limit=limit)
        return JSONResponse(logs)
    finally:
        db.close()


@app.get("/api/live-smart/dashboard")
async def api_live_dashboard():
    db = SessionLocal()
    try:
        from app.models.live_smart_campaign import LiveSmartCampaign as LSC, LiveSmartPosition as LSP
        from datetime import datetime as _dt
        campaigns = db.query(LSC).all()
        positions = db.query(LSP).all()

        closed  = [p for p in positions if p.status != "active"]
        active  = [p for p in positions if p.status == "active"]
        won     = [p for p in closed if (p.close_pnl_usdt or 0) > 0]
        lost    = [p for p in closed if (p.close_pnl_usdt or 0) <= 0]

        total_invested = sum(p.total_invested_usdt or 0 for p in active)
        realized_pnl   = sum(p.close_pnl_usdt or 0 for p in closed)
        open_pnl       = sum(p.pnl_usdt or 0 for p in active)
        total_pnl      = realized_pnl + open_pnl

        win_rate = (len(won) / len(closed) * 100) if closed else 0
        avg_win  = (sum(p.close_pnl_usdt or 0 for p in won)  / len(won))  if won  else 0
        avg_loss = (sum(p.close_pnl_usdt or 0 for p in lost) / len(lost)) if lost else 0

        log = []
        for p in sorted(positions, key=lambda x: x.created_at or _dt.min, reverse=True)[:50]:
            log.append({
                "id":            p.id,
                "symbol":        p.symbol,
                "status":        p.status,
                "entry_price":   p.entry_price,
                "avg_price":     p.avg_price,
                "current_price": p.current_price,
                "invested":      p.total_invested_usdt,
                "pnl_pct":       p.pnl_pct,
                "pnl_usdt":      p.pnl_usdt if p.status == "active" else p.close_pnl_usdt,
                "close_reason":  p.close_reason,
                "dca1":          p.dca1_triggered,
                "dca1_skipped":  p.dca1_skipped,
                "dca2":          p.dca2_triggered,
                "dca2_skipped":  p.dca2_skipped,
                "opened_at":     p.created_at.strftime("%Y-%m-%d %H:%M") if p.created_at else "",
                "closed_at":     p.closed_at.strftime("%Y-%m-%d %H:%M") if p.closed_at else None,
                "campaign_id":   p.campaign_id,
                "order_id":      p.binance_order_id,
            })

        return JSONResponse({
            "total_campaigns":   len(campaigns),
            "running_campaigns": sum(1 for c in campaigns if c.status == "running"),
            "active_positions":  len(active),
            "total_invested":    round(total_invested, 2),
            "realized_pnl":      round(realized_pnl,   2),
            "open_pnl":          round(open_pnl,        2),
            "total_pnl":         round(total_pnl,       2),
            "total_trades":      len(closed),
            "winning_trades":    len(won),
            "losing_trades":     len(lost),
            "win_rate":          round(win_rate, 1),
            "avg_win_usdt":      round(avg_win,  2),
            "avg_loss_usdt":     round(avg_loss, 2),
            "trade_log":         log,
        })
    finally:
        db.close()


@app.put("/api/live-smart/{campaign_id}")
async def api_live_smart_edit(campaign_id: int, request: Request) -> JSONResponse:
    body = await request.json()
    db = SessionLocal()
    try:
        c = db.query(LiveSmartCampaign).filter(LiveSmartCampaign.id == campaign_id).first()
        if not c:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if "max_symbols" in body:
            c.max_symbols = int(body["max_symbols"])
        if "entry_amount_usdt" in body:
            c.entry_amount_usdt = float(body["entry_amount_usdt"])
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()


@app.delete("/api/live-smart/{campaign_id}")
async def api_live_smart_delete(campaign_id: int) -> JSONResponse:
    db = SessionLocal()
    try:
        from app.models.live_smart_campaign import LiveSmartCampaignLog
        c = db.query(LiveSmartCampaign).filter(LiveSmartCampaign.id == campaign_id).first()
        if not c:
            return JSONResponse({"error": "Not found"}, status_code=404)
        db.query(LiveSmartCampaignLog).filter(LiveSmartCampaignLog.campaign_id == campaign_id).delete()
        db.query(LiveSmartPosition).filter(LiveSmartPosition.campaign_id == campaign_id).delete()
        db.delete(c)
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()


@app.get("/api/live-smart/capital")
async def api_live_capital(n: int = 5, entry: float = 50.0):
    from app.services.smart_campaign_service import calculate_required_capital
    from app.services.live_smart_campaign_service import get_advisor_recommendations
    recs = get_advisor_recommendations()
    return JSONResponse(calculate_required_capital(entry, n, recs))
