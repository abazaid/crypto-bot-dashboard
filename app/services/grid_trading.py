from __future__ import annotations

import bisect
import math
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.paper_v2 import ActivityLog, GridBot, GridTrade
from app.services.binance_live import (
    get_order_fee_usdt,
    get_usdt_free,
    place_market_buy_quote,
    place_market_sell_qty,
)
from app.services.binance_public import get_prices

_MIN_ORDER_USDT = 5.0
_MAX_FILLS_PER_CYCLE = 3


def _fee_rate() -> float:
    return max(0.0, float(getattr(settings, "paper_fee_pct", 0.1)) / 100.0)


def _log(db: Session, mode: str, event: str, symbol: str, msg: str) -> None:
    prefix = "LIVE_" if mode == "live" else ""
    db.add(ActivityLog(event_type=f"{prefix}GRID_{event}", symbol=symbol or "-", message=msg))


def _record_trade(
    db: Session,
    bot: GridBot,
    side: str,
    grid_index: int,
    price: float,
    qty: float,
    quote_usdt: float,
    fee_usdt: float,
    reason: str,
    pnl_usdt: Optional[float] = None,
) -> None:
    db.add(
        GridTrade(
            bot_id=bot.id,
            side=side,
            grid_index=grid_index,
            price=float(price),
            qty=float(qty),
            quote_usdt=float(quote_usdt),
            fee_usdt=float(fee_usdt),
            pnl_usdt=(float(pnl_usdt) if pnl_usdt is not None else None),
            reason=reason,
            created_at=datetime.utcnow(),
        )
    )


def get_grid_levels(lower: float, upper: float, count: int, mode: str) -> list[float]:
    count = max(2, int(count))
    if mode == "geometric":
        ratio = (upper / lower) ** (1.0 / (count - 1))
        return [lower * (ratio ** i) for i in range(count)]
    interval = (upper - lower) / (count - 1)
    return [lower + i * interval for i in range(count)]


def profit_per_grid_pct(levels: list[float], fee_rate: float) -> float:
    if len(levels) < 2:
        return 0.0
    avg_interval = (levels[-1] - levels[0]) / (len(levels) - 1)
    avg_price = sum(levels) / len(levels)
    gross = avg_interval / avg_price
    net = gross * (1 - fee_rate) ** 2
    return round(net * 100, 4)


def _grid_index_for_price(levels: list[float], price: float) -> int:
    idx = bisect.bisect_right(levels, price) - 1
    return max(0, min(idx, len(levels) - 1))


def _paper_buy(db: Session, bot: GridBot, grid_index: int, price: float, usdt: float, reason: str) -> bool:
    if price <= 0 or usdt < _MIN_ORDER_USDT:
        return False
    if float(bot.usdt_reserved) + 1e-9 < usdt:
        return False
    fee = usdt * _fee_rate()
    total = usdt + fee
    qty = usdt / price
    bot.coin_qty = float(bot.coin_qty) + qty
    bot.usdt_reserved = max(0.0, float(bot.usdt_reserved) - total)
    bot.realized_fees_usdt = float(bot.realized_fees_usdt) + fee
    bot.buy_count = int(bot.buy_count) + 1
    bot.last_action_at = datetime.utcnow()
    _record_trade(db, bot, "BUY", grid_index, price, qty, usdt, fee, reason)
    _log(db, bot.mode, "BUY", bot.symbol, f"Bot={bot.name} | grid={grid_index} | price={price:.6f} | usdt={usdt:.2f} | qty={qty:.8f}")
    return True


def _paper_sell(db: Session, bot: GridBot, grid_index: int, price: float, qty: float, reason: str) -> bool:
    qty = min(float(qty), float(bot.coin_qty))
    if price <= 0 or qty <= 0:
        return False
    gross = qty * price
    if gross < _MIN_ORDER_USDT:
        _log(db, bot.mode, "SELL_SKIP", bot.symbol, f"Bot={bot.name} | grid={grid_index} | skip=dust | gross={gross:.4f}")
        return False
    fee = gross * _fee_rate()
    net = gross - fee
    # PnL = net received minus what was invested (usdt_per_grid approximation)
    usdt_per_grid = float(bot.total_investment_usdt) / max(1, int(bot.grid_count))
    pnl = net - usdt_per_grid
    bot.coin_qty = max(0.0, float(bot.coin_qty) - qty)
    bot.usdt_reserved = float(bot.usdt_reserved) + net
    bot.realized_pnl_usdt = float(bot.realized_pnl_usdt) + pnl
    bot.realized_fees_usdt = float(bot.realized_fees_usdt) + fee
    bot.sell_count = int(bot.sell_count) + 1
    bot.last_action_at = datetime.utcnow()
    _record_trade(db, bot, "SELL", grid_index, price, qty, gross, fee, reason, pnl_usdt=pnl)
    _log(db, bot.mode, "SELL", bot.symbol, f"Bot={bot.name} | grid={grid_index} | price={price:.6f} | qty={qty:.8f} | pnl={pnl:+.2f}")
    return True


def _live_buy(db: Session, bot: GridBot, grid_index: int, usdt: float, reason: str) -> bool:
    if usdt < _MIN_ORDER_USDT:
        return False
    if float(bot.usdt_reserved) + 1e-9 < usdt:
        return False
    if get_usdt_free() + 1e-9 < usdt:
        _log(db, bot.mode, "SKIP", bot.symbol, f"Bot={bot.name} | reason=insufficient_cash | need={usdt:.2f}")
        return False
    try:
        order = place_market_buy_quote(bot.symbol, usdt)
    except Exception as e:
        _log(db, bot.mode, "BUY_FAIL", bot.symbol, f"Bot={bot.name} | grid={grid_index} | error={e}")
        return False
    qty = float(order.get("executed_qty", 0.0))
    spent = float(order.get("quote_qty", 0.0))
    price = float(order.get("avg_price", 0.0))
    if qty <= 0 or spent <= 0 or price <= 0:
        return False
    order_id = int(float(order.get("order_id", 0) or 0))
    fee_usdt = 0.0
    if order_id > 0:
        try:
            fee_usdt = float(get_order_fee_usdt(bot.symbol, order_id))
        except Exception:
            pass
    bot.coin_qty = float(bot.coin_qty) + qty
    bot.usdt_reserved = max(0.0, float(bot.usdt_reserved) - (spent + fee_usdt))
    bot.realized_fees_usdt = float(bot.realized_fees_usdt) + fee_usdt
    bot.buy_count = int(bot.buy_count) + 1
    bot.last_action_at = datetime.utcnow()
    _record_trade(db, bot, "BUY", grid_index, price, qty, spent, fee_usdt, reason)
    _log(db, bot.mode, "BUY", bot.symbol, f"Bot={bot.name} | grid={grid_index} | price={price:.6f} | spent={spent:.2f} | qty={qty:.8f}")
    return True


def _live_sell(db: Session, bot: GridBot, grid_index: int, qty: float, reason: str) -> bool:
    qty = min(float(qty), float(bot.coin_qty))
    if qty <= 0:
        return False
    try:
        order = place_market_sell_qty(bot.symbol, qty)
    except Exception as e:
        _log(db, bot.mode, "SELL_FAIL", bot.symbol, f"Bot={bot.name} | grid={grid_index} | error={e}")
        return False
    exec_qty = float(order.get("executed_qty", 0.0))
    quote = float(order.get("quote_qty", 0.0))
    price = float(order.get("avg_price", 0.0))
    if exec_qty <= 0 or quote <= 0:
        return False
    order_id = int(float(order.get("order_id", 0) or 0))
    fee_usdt = 0.0
    if order_id > 0:
        try:
            fee_usdt = float(get_order_fee_usdt(bot.symbol, order_id))
        except Exception:
            pass
    net = quote - fee_usdt
    usdt_per_grid = float(bot.total_investment_usdt) / max(1, int(bot.grid_count))
    pnl = net - usdt_per_grid
    bot.coin_qty = max(0.0, float(bot.coin_qty) - exec_qty)
    bot.usdt_reserved = float(bot.usdt_reserved) + net
    bot.realized_pnl_usdt = float(bot.realized_pnl_usdt) + pnl
    bot.realized_fees_usdt = float(bot.realized_fees_usdt) + fee_usdt
    bot.sell_count = int(bot.sell_count) + 1
    bot.last_action_at = datetime.utcnow()
    _record_trade(db, bot, "SELL", grid_index, price if price > 0 else net / max(exec_qty, 1e-12), exec_qty, quote, fee_usdt, reason, pnl_usdt=pnl)
    _log(db, bot.mode, "SELL", bot.symbol, f"Bot={bot.name} | grid={grid_index} | qty={exec_qty:.8f} | quote={quote:.2f} | pnl={pnl:+.2f}")
    return True


def _do_buy(db: Session, bot: GridBot, grid_index: int, price: float, reason: str) -> bool:
    usdt_per_grid = float(bot.total_investment_usdt) / max(1, int(bot.grid_count))
    usdt = min(usdt_per_grid, float(bot.usdt_reserved))
    if bot.mode == "live":
        return _live_buy(db, bot, grid_index, usdt, reason)
    return _paper_buy(db, bot, grid_index, price, usdt, reason)


def _do_sell(db: Session, bot: GridBot, grid_index: int, price: float, reason: str) -> bool:
    usdt_per_grid = float(bot.total_investment_usdt) / max(1, int(bot.grid_count))
    qty = usdt_per_grid / max(price, 1e-12)
    qty = min(qty, float(bot.coin_qty))
    if bot.mode == "live":
        return _live_sell(db, bot, grid_index, qty, reason)
    return _paper_sell(db, bot, grid_index, price, qty, reason)


def _activate_both_mode(db: Session, bot: GridBot, price: float, levels: list[float]) -> None:
    """Initial coin purchase for 'both' investment mode: buy coins for all grids above current price."""
    grids_above = [lv for lv in levels if lv > price]
    if not grids_above:
        return
    usdt_per_grid = float(bot.total_investment_usdt) / max(1, int(bot.grid_count))
    total_coin_usdt = usdt_per_grid * len(grids_above)
    total_coin_usdt = min(total_coin_usdt, float(bot.usdt_reserved))
    if total_coin_usdt < _MIN_ORDER_USDT:
        return
    if bot.mode == "live":
        _live_buy(db, bot, -1, total_coin_usdt, "initial_both_setup")
    else:
        _paper_buy(db, bot, -1, price, total_coin_usdt, "initial_both_setup")


def _run_bot_cycle(db: Session, bot: GridBot, price: float) -> bool:
    bot.last_price = float(price)
    if bot.status not in ("active", "waiting"):
        return False

    # Trigger price check
    if bot.trigger_price and bot.status == "waiting":
        tp = float(bot.trigger_price)
        if price < tp:
            return False
        bot.status = "active"
        bot.triggered_at = datetime.utcnow()
        _log(db, bot.mode, "TRIGGERED", bot.symbol, f"Bot={bot.name} | trigger_price={tp:.6f} | current={price:.6f}")

    levels = get_grid_levels(float(bot.lower_limit), float(bot.upper_limit), int(bot.grid_count), str(bot.grid_mode))

    # First cycle after activation: set anchor, optionally buy coins for "both" mode
    if bot.last_grid_index is None:
        if bot.investment_mode == "both":
            _activate_both_mode(db, bot, price, levels)
        bot.last_grid_index = _grid_index_for_price(levels, price)
        _log(db, bot.mode, "INIT", bot.symbol, f"Bot={bot.name} | price={price:.6f} | anchor_grid={bot.last_grid_index}")
        return True

    # Stop Loss check
    if bot.stop_loss_price and price <= float(bot.stop_loss_price):
        _log(db, bot.mode, "STOP_LOSS", bot.symbol, f"Bot={bot.name} | price={price:.6f} | sl={bot.stop_loss_price:.6f}")
        _close_bot(db, bot, price, "stop_loss")
        return True

    # Take Profit check
    if bot.take_profit_price and price >= float(bot.take_profit_price):
        _log(db, bot.mode, "TAKE_PROFIT", bot.symbol, f"Bot={bot.name} | price={price:.6f} | tp={bot.take_profit_price:.6f}")
        _close_bot(db, bot, price, "take_profit")
        return True

    # Price out of range
    if price < float(bot.lower_limit) or price > float(bot.upper_limit):
        _log(db, bot.mode, "OUT_OF_RANGE", bot.symbol, f"Bot={bot.name} | price={price:.6f} | range=[{bot.lower_limit},{bot.upper_limit}]")
        bot.last_grid_index = _grid_index_for_price(levels, price)
        return False

    cur_idx = _grid_index_for_price(levels, price)
    last_idx = int(bot.last_grid_index)
    fills = 0
    changed = False

    if cur_idx > last_idx:
        # Price moved UP → SELL at each crossed level
        for i in range(last_idx + 1, cur_idx + 1):
            if fills >= _MAX_FILLS_PER_CYCLE:
                break
            ok = _do_sell(db, bot, i, levels[i], "grid_sell")
            if ok:
                fills += 1
                changed = True
    elif cur_idx < last_idx:
        # Price moved DOWN → BUY at each crossed level
        for i in range(last_idx, cur_idx, -1):
            if fills >= _MAX_FILLS_PER_CYCLE:
                break
            ok = _do_buy(db, bot, i - 1, levels[i - 1], "grid_buy")
            if ok:
                fills += 1
                changed = True

    bot.last_grid_index = cur_idx
    return changed


def _close_bot(db: Session, bot: GridBot, price: float, reason: str) -> None:
    if float(bot.coin_qty) > 0:
        if bot.mode == "live":
            _live_sell(db, bot, -1, float(bot.coin_qty), reason)
        else:
            _paper_sell(db, bot, -1, price, float(bot.coin_qty), reason)
    bot.status = "stopped"
    bot.last_action_at = datetime.utcnow()


def create_bot(
    db: Session,
    *,
    mode: str,
    name: str,
    symbol: str,
    lower_limit: float,
    upper_limit: float,
    grid_count: int,
    grid_mode: str,
    investment_mode: str,
    total_investment_usdt: float,
    trigger_price: Optional[float] = None,
    take_profit_price: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
) -> GridBot:
    bot = GridBot(
        name=(name.strip() or "Grid Bot"),
        mode=("live" if mode == "live" else "paper"),
        symbol=str(symbol or "").upper().strip(),
        status=("waiting" if trigger_price else "active"),
        lower_limit=float(lower_limit),
        upper_limit=float(upper_limit),
        grid_count=max(2, min(500, int(grid_count))),
        grid_mode=("geometric" if grid_mode == "geometric" else "arithmetic"),
        investment_mode=("both" if investment_mode == "both" else "usdt_only"),
        total_investment_usdt=max(10.0, float(total_investment_usdt)),
        trigger_price=(float(trigger_price) if trigger_price else None),
        take_profit_price=(float(take_profit_price) if take_profit_price else None),
        stop_loss_price=(float(stop_loss_price) if stop_loss_price else None),
        usdt_reserved=max(10.0, float(total_investment_usdt)),
    )
    db.add(bot)
    db.flush()
    _log(db, bot.mode, "CREATE", bot.symbol, f"Bot={bot.name} | capital={bot.total_investment_usdt:.2f} | grids={bot.grid_count} | range=[{bot.lower_limit},{bot.upper_limit}]")
    return bot


def run_grid_cycle(db: Session, mode: str) -> None:
    bots = (
        db.query(GridBot)
        .filter(GridBot.mode == ("live" if mode == "live" else "paper"))
        .filter(GridBot.status.in_(["active", "waiting"]))
        .all()
    )
    if not bots:
        return
    symbols = sorted({b.symbol for b in bots if b.symbol})
    prices = get_prices(symbols) if symbols else {}
    changed = False
    for bot in bots:
        px = float(prices.get(bot.symbol, 0.0))
        if px <= 0:
            continue
        changed = _run_bot_cycle(db, bot, px) or changed
    if changed:
        db.commit()


def toggle_bot_status(db: Session, bot: GridBot) -> str:
    if bot.status == "stopped":
        return "stopped"
    bot.status = "paused" if bot.status == "active" else "active"
    bot.last_action_at = datetime.utcnow()
    _log(db, bot.mode, "STATUS", bot.symbol, f"Bot={bot.name} | status={bot.status}")
    return str(bot.status)
