"""
Paper exchange — a simulated account for the AI Trader paper pool.

Same interface the pool service uses from binance_live, but no order ever reaches
an exchange and no API key is used. Fills are simulated against the LIVE public
order book (best bid/ask) with Binance lot rules, the configured fee and a small
slippage, so paper results track what the real engine would have done.

The paper account has no balances of its own: the pool ledger is the only source
of truth, so balances are reported as unlimited and the pool's own cash/qty checks
are what constrain every order.
"""
from __future__ import annotations

import itertools
import logging
import math

import requests

from app.core.config import settings
from app.services import binance_live, binance_public

logger = logging.getLogger(__name__)

PAPER_SLIPPAGE_PCT = 0.05  # on top of the bid/ask spread, per fill
_UNLIMITED = 1e15
_ORDER_IDS = itertools.count(1)


class _PaperBalances(dict):
    """Every asset reads as unlimited: the pool ledger alone decides what may be spent or sold."""

    def get(self, key, default=None):  # type: ignore[override]
        return {"free": _UNLIMITED, "locked": 0.0}


def is_configured() -> bool:
    return True


def get_balances() -> dict[str, dict[str, float]]:
    return _PaperBalances(USDT={"free": _UNLIMITED, "locked": 0.0})


def get_symbol_lot_filters(symbol: str) -> dict[str, float]:
    return binance_live.get_symbol_lot_filters(symbol)  # public exchangeInfo, no keys


def get_order_by_client_id(symbol: str, client_order_id: str) -> dict | None:
    return None  # simulated orders either fill synchronously or raise; nothing to recover


def _book(symbol: str) -> tuple[float, float]:
    """(best bid, best ask) from the live public order book; last price for both as a fallback."""
    try:
        r = requests.get(f"{binance_public.BASE_URL}/api/v3/ticker/bookTicker", params={"symbol": symbol.upper()}, timeout=binance_public.TIMEOUT)
        r.raise_for_status()
        row = r.json()
        bid, ask = float(row.get("bidPrice", 0.0)), float(row.get("askPrice", 0.0))
        if bid > 0 and ask > 0:
            return bid, ask
    except (requests.RequestException, ValueError) as exc:
        logger.warning("paper: book ticker for %s failed, using last price: %s", symbol, exc)
    last = float(binance_public.get_prices([symbol.upper()]).get(symbol.upper(), 0.0))
    if last <= 0:
        raise RuntimeError(f"paper: no market price for {symbol}")
    return last, last


def _floor_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step + 1e-9) * step


def _check_lot(symbol: str, qty: float, price: float, filters: dict[str, float]) -> None:
    min_qty = float(filters.get("min_qty", 0.0) or 0.0)
    min_notional = float(filters.get("min_notional", 0.0) or 0.0)
    if qty <= 0 or qty < min_qty:
        raise RuntimeError(f"quantity below min lot size for {symbol}: {qty}")
    if min_notional > 0 and qty * price < min_notional:
        raise RuntimeError(f"order below min notional for {symbol}: {qty * price:.4f}")


def place_market_buy_quote(symbol: str, quote_usdt: float, client_order_id: str | None = None) -> dict[str, float]:
    """Spend ~quote_usdt at the ask (+slippage). Fee is taken in the bought coin, as Binance does without BNB."""
    if quote_usdt <= 0:
        raise RuntimeError("quote_usdt must be > 0")
    filters = get_symbol_lot_filters(symbol)
    _bid, ask = _book(symbol)
    price = ask * (1.0 + PAPER_SLIPPAGE_PCT / 100.0)
    qty = _floor_step(float(quote_usdt) / price, float(filters.get("step_size", 0.0) or 0.0))
    _check_lot(symbol, qty, price, filters)
    fee_base = qty * float(settings.trading_fee_pct) / 100.0
    return {
        "order_id": float(next(_ORDER_IDS)),
        "status": "FILLED",
        "executed_qty": qty,
        "quote_qty": qty * price,
        "avg_price": price,
        "fee_base": fee_base,
        "fee_usdt": fee_base * price,
        "net_qty": qty - fee_base,
    }


def place_market_sell_qty(symbol: str, quantity: float, client_order_id: str | None = None) -> dict[str, float]:
    """Sell quantity at the bid (-slippage). Fee is taken in USDT from the proceeds."""
    if quantity <= 0:
        raise RuntimeError("quantity must be > 0")
    filters = get_symbol_lot_filters(symbol)
    bid, _ask = _book(symbol)
    price = bid * (1.0 - PAPER_SLIPPAGE_PCT / 100.0)
    qty = _floor_step(float(quantity), float(filters.get("step_size", 0.0) or 0.0))
    _check_lot(symbol, qty, price, filters)
    quote = qty * price
    return {
        "order_id": float(next(_ORDER_IDS)),
        "status": "FILLED",
        "executed_qty": qty,
        "quote_qty": quote,
        "avg_price": price,
        "fee_base": 0.0,
        "fee_usdt": quote * float(settings.trading_fee_pct) / 100.0,
        "net_qty": qty,
    }
