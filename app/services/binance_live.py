import hashlib
import hmac
import math
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import requests

from app.core.config import settings
from app.services.binance_public import get_book_tickers, get_exchange_info, get_prices

BASE_URL = "https://api.binance.com"
TIMEOUT = 15
_SYMBOL_FILTER_CACHE: dict[str, dict[str, float]] = {}
_CACHE_EXPIRES_AT = 0.0
_COST_BASIS_CACHE: dict[str, dict[str, Any]] = {}
_ALL_COINS_CACHE: dict[str, Any] = {"expires_at": 0.0, "key": "", "data": None}
_KNOWN_QUOTES = [
    "USDT",
    "USDC",
    "FDUSD",
    "BUSD",
    "TUSD",
    "BTC",
    "ETH",
    "BNB",
    "TRY",
    "EUR",
]


def is_configured() -> bool:
    return bool(settings.binance_api_key and settings.binance_api_secret)


def _ensure_keys() -> None:
    if not is_configured():
        raise RuntimeError("Missing Binance API keys: set BINANCE_API_KEY and BINANCE_API_SECRET")


def _signed_request(method: str, path: str, params: dict[str, Any] | None = None) -> dict:
    _ensure_keys()
    q = dict(params or {})
    q["timestamp"] = int(time.time() * 1000)
    q["recvWindow"] = 5000
    query = urlencode(q, doseq=True)
    signature = hmac.new(
        settings.binance_api_secret.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    query = f"{query}&signature={signature}"
    headers = {"X-MBX-APIKEY": settings.binance_api_key}
    url = f"{BASE_URL}{path}?{query}"
    resp = requests.request(method.upper(), url, headers=headers, timeout=TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"Binance API error {resp.status_code}: {resp.text}")
    return resp.json()


def get_account_info() -> dict:
    return _signed_request("GET", "/api/v3/account")


def get_balances() -> dict[str, dict[str, float]]:
    data = get_account_info()
    out: dict[str, dict[str, float]] = {}
    for b in data.get("balances", []):
        asset = str(b.get("asset", "")).upper()
        out[asset] = {
            "free": float(b.get("free", 0.0)),
            "locked": float(b.get("locked", 0.0)),
        }
    return out


def get_asset_free(asset: str) -> float:
    return float(get_balances().get(asset.upper(), {}).get("free", 0.0))


def get_usdt_free() -> float:
    return get_asset_free("USDT")


def _load_symbol_filters() -> None:
    global _CACHE_EXPIRES_AT
    if _CACHE_EXPIRES_AT > time.time() and _SYMBOL_FILTER_CACHE:
        return
    data = get_exchange_info()
    _SYMBOL_FILTER_CACHE.clear()
    for row in data.get("symbols", []):
        symbol = str(row.get("symbol", "")).upper()
        min_qty = 0.0
        step_size = 0.0
        min_notional = 0.0
        tick_size = 0.0
        for f in row.get("filters", []):
            ftype = str(f.get("filterType", "")).upper()
            if ftype == "LOT_SIZE":
                min_qty = float(f.get("minQty", 0.0))
                step_size = float(f.get("stepSize", 0.0))
            if ftype == "PRICE_FILTER":
                tick_size = float(f.get("tickSize", 0.0))
            if ftype in {"NOTIONAL", "MIN_NOTIONAL"}:
                min_notional = float(f.get("minNotional", 0.0))
        _SYMBOL_FILTER_CACHE[symbol] = {
            "min_qty": min_qty,
            "step_size": step_size,
            "min_notional": min_notional,
            "tick_size": tick_size,
        }
    _CACHE_EXPIRES_AT = time.time() + 900


def _round_step_down(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def _precision_from_step(step: float) -> int:
    if step <= 0:
        return 8
    s = f"{step:.16f}".rstrip("0")
    if "." not in s:
        return 0
    return max(0, len(s.split(".")[1]))


def _fmt_with_step(value: float, step: float) -> str:
    p = _precision_from_step(step)
    if p <= 0:
        return str(int(math.floor(value)))
    return f"{value:.{p}f}"


def _order_summary(raw: dict) -> dict[str, float]:
    executed_qty = float(raw.get("executedQty", 0.0))
    quote_qty = float(raw.get("cummulativeQuoteQty", 0.0))
    avg_price = quote_qty / executed_qty if executed_qty > 0 else 0.0
    return {
        "order_id": float(raw.get("orderId", 0) or 0),
        "status": str(raw.get("status", "")),
        "executed_qty": executed_qty,
        "quote_qty": quote_qty,
        "avg_price": avg_price,
    }


def _base_asset_from_symbol(symbol: str) -> str:
    s = symbol.upper()
    for q in sorted(_KNOWN_QUOTES, key=len, reverse=True):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


def _quote_asset_from_symbol(symbol: str) -> str:
    s = symbol.upper()
    for q in sorted(_KNOWN_QUOTES, key=len, reverse=True):
        if s.endswith(q) and len(s) > len(q):
            return q
    return "USDT"


def get_symbol_lot_filters(symbol: str) -> dict[str, float]:
    _load_symbol_filters()
    return _SYMBOL_FILTER_CACHE.get(symbol.upper(), {}).copy()


def normalize_qty_for_sell(symbol: str, quantity: float, cap_to_free_balance: bool = True) -> tuple[float, float, float]:
    filters = get_symbol_lot_filters(symbol)
    step = float(filters.get("step_size", 0.0))
    min_qty = float(filters.get("min_qty", 0.0))
    qty = float(quantity)
    if cap_to_free_balance:
        base_asset = _base_asset_from_symbol(symbol)
        free_base = get_asset_free(base_asset)
        qty = min(qty, free_base)
        if qty <= 0 and free_base > 0:
            qty = free_base
    qty = _round_step_down(qty, step)
    return qty, min_qty, step


def _asset_to_usdt(asset: str, amount: float, symbol: str, trade_price: float) -> float:
    a = str(asset or "").upper()
    if amount <= 0:
        return 0.0
    if a == "USDT":
        return float(amount)
    base = _base_asset_from_symbol(symbol)
    quote = _quote_asset_from_symbol(symbol)
    if a == quote:
        return float(amount)
    if a == base:
        return float(amount) * max(0.0, float(trade_price))
    # fallback: try direct asset/USDT ticker
    try:
        rows = requests.get(f"{BASE_URL}/api/v3/ticker/price", timeout=TIMEOUT).json()
        pair = f"{a}USDT"
        row = next((x for x in rows if str(x.get("symbol", "")).upper() == pair), None)
        if row:
            return float(amount) * float(row.get("price", 0.0))
    except Exception:
        pass
    return 0.0


def get_order_fee_usdt(symbol: str, order_id: int) -> float:
    if int(order_id or 0) <= 0:
        return 0.0
    trades = _signed_request(
        "GET",
        "/api/v3/myTrades",
        {
            "symbol": symbol.upper(),
            "orderId": int(order_id),
            "limit": 1000,
        },
    )
    total = 0.0
    for t in trades:
        if int(t.get("orderId", 0)) != int(order_id):
            continue
        commission = float(t.get("commission", 0.0))
        commission_asset = str(t.get("commissionAsset", "")).upper()
        trade_price = float(t.get("price", 0.0))
        total += _asset_to_usdt(commission_asset, commission, symbol, trade_price)
    return float(total)


def place_market_buy_quote(symbol: str, quote_usdt: float) -> dict[str, float]:
    if quote_usdt <= 0:
        raise RuntimeError("quote_usdt must be > 0")
    raw = _signed_request(
        "POST",
        "/api/v3/order",
        {
            "symbol": symbol.upper(),
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": f"{quote_usdt:.8f}",
        },
    )
    return _order_summary(raw)


def _best_ask(symbol: str) -> float:
    rows = get_book_tickers()
    sym = symbol.upper()
    row = next((x for x in rows if str(x.get("symbol", "")).upper() == sym), None)
    if not row:
        raise RuntimeError(f"No book ticker for {sym}")
    ask = float(row.get("askPrice", 0.0))
    if ask <= 0:
        raise RuntimeError(f"Invalid ask price for {sym}")
    return ask


def place_limit_buy_quote(
    symbol: str,
    quote_usdt: float,
    price_buffer_pct: float = 0.03,
    time_in_force: str = "IOC",
) -> dict[str, float]:
    if quote_usdt <= 0:
        raise RuntimeError("quote_usdt must be > 0")
    _load_symbol_filters()
    sym = symbol.upper()
    filters = _SYMBOL_FILTER_CACHE.get(sym, {})
    step = float(filters.get("step_size", 0.0))
    min_qty = float(filters.get("min_qty", 0.0))
    min_notional = float(filters.get("min_notional", 0.0))
    tick = float(filters.get("tick_size", 0.0))

    ask = _best_ask(sym)
    px = ask * (1.0 + max(0.0, float(price_buffer_pct)) / 100.0)
    px = _round_step_down(px, tick)
    if px <= 0:
        raise RuntimeError(f"Invalid limit buy price for {sym}: {px}")

    qty = float(quote_usdt) / px
    qty = _round_step_down(qty, step)
    if qty <= 0 or qty < min_qty:
        raise RuntimeError(f"quantity below min lot size for {sym}: {qty}")
    if min_notional > 0 and (qty * px) < min_notional:
        raise RuntimeError(f"order below min notional for {sym}: {qty * px}")

    qty_str = _fmt_with_step(qty, step)
    px_str = _fmt_with_step(px, tick)
    raw = _signed_request(
        "POST",
        "/api/v3/order",
        {
            "symbol": sym,
            "side": "BUY",
            "type": "LIMIT",
            "timeInForce": str(time_in_force or "IOC").upper(),
            "quantity": qty_str,
            "price": px_str,
        },
    )
    out = _order_summary(raw)
    out["order_id"] = int(raw.get("orderId", 0))
    out["limit_price"] = float(raw.get("price", px) or px)
    out["status"] = str(raw.get("status", ""))
    return out


def place_market_sell_qty(symbol: str, quantity: float) -> dict[str, float]:
    if quantity <= 0:
        raise RuntimeError("quantity must be > 0")
    qty, min_qty, step = normalize_qty_for_sell(symbol, quantity, cap_to_free_balance=True)
    if qty <= 0 or qty < min_qty:
        raise RuntimeError(f"quantity below min lot size for {symbol}: {qty}")
    qty_str = _fmt_with_step(qty, step)
    raw = _signed_request(
        "POST",
        "/api/v3/order",
        {
            "symbol": symbol.upper(),
            "side": "SELL",
            "type": "MARKET",
            "quantity": qty_str,
        },
    )
    return _order_summary(raw)


def place_limit_sell_qty(symbol: str, quantity: float, price: float) -> dict[str, float]:
    if quantity <= 0:
        raise RuntimeError("quantity must be > 0")
    if price <= 0:
        raise RuntimeError("price must be > 0")
    _load_symbol_filters()
    filters = _SYMBOL_FILTER_CACHE.get(symbol.upper(), {})
    qty, min_qty, step = normalize_qty_for_sell(symbol, quantity, cap_to_free_balance=True)
    min_notional = float(filters.get("min_notional", 0.0))
    tick = float(filters.get("tick_size", 0.0))
    px = _round_step_down(float(price), tick)
    if qty <= 0 or qty < min_qty:
        raise RuntimeError(f"quantity below min lot size for {symbol}: {qty}")
    if px <= 0:
        raise RuntimeError(f"invalid limit price for {symbol}: {px}")
    if min_notional > 0 and (qty * px) < min_notional:
        raise RuntimeError(f"order below min notional for {symbol}: {qty * px}")
    qty_str = _fmt_with_step(qty, step)
    px_str = _fmt_with_step(px, tick)
    raw = _signed_request(
        "POST",
        "/api/v3/order",
        {
            "symbol": symbol.upper(),
            "side": "SELL",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": qty_str,
            "price": px_str,
        },
    )
    return {
        "order_id": int(raw.get("orderId", 0)),
        "orig_qty": float(raw.get("origQty", 0.0)),
        "price": float(raw.get("price", 0.0)),
        "status": str(raw.get("status", "")),
    }


def cancel_order(symbol: str, order_id: int) -> dict:
    return _signed_request(
        "DELETE",
        "/api/v3/order",
        {
            "symbol": symbol.upper(),
            "orderId": int(order_id),
        },
    )


def get_order(symbol: str, order_id: int) -> dict:
    return _signed_request(
        "GET",
        "/api/v3/order",
        {
            "symbol": symbol.upper(),
            "orderId": int(order_id),
        },
    )


def get_my_trades(symbol: str, limit: int = 1000) -> list[dict]:
    return _signed_request(
        "GET",
        "/api/v3/myTrades",
        {
            "symbol": symbol.upper(),
            "limit": max(1, min(int(limit), 1000)),
        },
    )


def cancel_open_orders(symbol: str) -> list[dict]:
    out = _signed_request(
        "DELETE",
        "/api/v3/openOrders",
        {
            "symbol": symbol.upper(),
        },
    )
    if isinstance(out, list):
        return out
    return []


def get_open_orders(symbol: str | None = None) -> list[dict]:
    params: dict[str, Any] = {}
    if symbol:
        params["symbol"] = symbol.upper()
    out = _signed_request("GET", "/api/v3/openOrders", params)
    if isinstance(out, list):
        return out
    return []


def _cost_basis_from_trades(symbol: str, qty_now: float, max_trades: int = 1000) -> tuple[float, float, int]:
    cached = _COST_BASIS_CACHE.get(symbol.upper())
    if cached:
        cached_qty = float(cached.get("qty_now", -1.0))
        expires_at = float(cached.get("expires_at", 0.0))
        if abs(cached_qty - float(qty_now)) < 1e-12 and expires_at > time.time():
            return (
                float(cached.get("avg_entry", 0.0)),
                float(cached.get("invested", 0.0)),
                int(cached.get("used", 0)),
            )

    base = _base_asset_from_symbol(symbol)
    quote = _quote_asset_from_symbol(symbol)
    try:
        trades = get_my_trades(symbol, limit=max_trades)
    except Exception:
        return 0.0, 0.0, 0
    if not trades:
        return 0.0, 0.0, 0

    trades = sorted(
        trades,
        key=lambda t: (int(t.get("time", 0) or 0), int(t.get("id", 0) or 0)),
    )
    inv_qty = 0.0
    inv_cost = 0.0
    used = 0
    for t in trades:
        qty = float(t.get("qty", 0.0) or 0.0)
        quote_qty = float(t.get("quoteQty", 0.0) or 0.0)
        price = float(t.get("price", 0.0) or 0.0)
        commission = float(t.get("commission", 0.0) or 0.0)
        commission_asset = str(t.get("commissionAsset", "")).upper()
        is_buyer = bool(t.get("isBuyer", False))

        if qty <= 0:
            continue
        used += 1

        # Convert fee to USDT and adjust base inventory when fee is charged in base.
        fee_usdt = _asset_to_usdt(commission_asset, commission, symbol, price)
        base_fee = commission if commission_asset == base else 0.0
        quote_fee = commission if commission_asset == quote else 0.0

        if is_buyer:
            got_base = max(0.0, qty - base_fee)
            buy_cost = quote_qty + quote_fee + fee_usdt
            inv_qty += got_base
            inv_cost += buy_cost
            continue

        sold_base = qty + base_fee
        if inv_qty <= 0:
            continue
        avg_before = inv_cost / max(inv_qty, 1e-12)
        reduce_qty = min(inv_qty, sold_base)
        inv_qty -= reduce_qty
        inv_cost = max(0.0, inv_cost - (avg_before * reduce_qty))

    if inv_qty <= 0:
        _COST_BASIS_CACHE[symbol.upper()] = {
            "qty_now": float(qty_now),
            "avg_entry": 0.0,
            "invested": 0.0,
            "used": used,
            "expires_at": time.time() + 180.0,
        }
        return 0.0, 0.0, used

    avg_entry = inv_cost / max(inv_qty, 1e-12)
    # Reconcile to current wallet qty (may differ slightly due to limited history/fees).
    invested_now = avg_entry * max(qty_now, 0.0)
    _COST_BASIS_CACHE[symbol.upper()] = {
        "qty_now": float(qty_now),
        "avg_entry": float(max(avg_entry, 0.0)),
        "invested": float(max(invested_now, 0.0)),
        "used": int(used),
        "expires_at": time.time() + 180.0,
    }
    return max(avg_entry, 0.0), max(invested_now, 0.0), used


def list_spot_coin_positions(
    min_usdt_value: float = 0.05,
    include_zero: bool = False,
    cache_ttl_seconds: int = 90,
) -> dict[str, Any]:
    cache_key = f"{float(min_usdt_value):.8f}|{int(bool(include_zero))}"
    if (
        int(cache_ttl_seconds) > 0
        and _ALL_COINS_CACHE.get("data") is not None
        and _ALL_COINS_CACHE.get("key") == cache_key
        and float(_ALL_COINS_CACHE.get("expires_at", 0.0)) > time.time()
    ):
        return _ALL_COINS_CACHE["data"]

    balances = get_balances()
    _load_symbol_filters()
    symbol_rows: list[dict[str, Any]] = []
    symbols: list[str] = []

    for asset, b in balances.items():
        free = float(b.get("free", 0.0))
        locked = float(b.get("locked", 0.0))
        total = free + locked
        if asset in {"USDT", "FDUSD", "USDC", "BUSD", "TUSD"}:
            continue
        if not include_zero and total <= 0:
            continue
        sym = f"{asset}USDT"
        if sym not in _SYMBOL_FILTER_CACHE:
            continue
        symbols.append(sym)
        symbol_rows.append(
            {
                "asset": asset,
                "symbol": sym,
                "qty_total": total,
                "qty_free": free,
                "qty_locked": locked,
            }
        )

    prices = get_prices(symbols) if symbols else {}
    rows: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for r in symbol_rows:
        price = float(prices.get(r["symbol"], 0.0))
        market_value = price * float(r["qty_total"])
        if (not include_zero) and market_value < min_usdt_value:
            continue
        avg_entry, invested, trades_used = _cost_basis_from_trades(r["symbol"], float(r["qty_total"]))
        if avg_entry <= 0 and price > 0:
            avg_entry = price
            invested = market_value
        pnl = market_value - invested
        pnl_pct = (pnl / invested * 100.0) if invested > 0 else 0.0
        rows.append(
            {
                **r,
                "price": price,
                "avg_entry": avg_entry,
                "invested": invested,
                "market_value": market_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "status": "profit" if pnl > 0 else ("loss" if pnl < 0 else "flat"),
                "trades_used": trades_used,
                "as_of": now,
            }
        )

    rows.sort(key=lambda x: float(x.get("market_value", 0.0)), reverse=True)
    summary = {
        "coins_count": len(rows),
        "invested_total": sum(float(x.get("invested", 0.0)) for x in rows),
        "market_total": sum(float(x.get("market_value", 0.0)) for x in rows),
    }
    summary["pnl_total"] = float(summary["market_total"] - summary["invested_total"])
    summary["pnl_pct"] = (
        float(summary["pnl_total"]) / float(summary["invested_total"]) * 100.0
        if float(summary["invested_total"]) > 0
        else 0.0
    )
    out = {"rows": rows, "summary": summary}
    if int(cache_ttl_seconds) > 0:
        _ALL_COINS_CACHE["key"] = cache_key
        _ALL_COINS_CACHE["data"] = out
        _ALL_COINS_CACHE["expires_at"] = time.time() + float(cache_ttl_seconds)
    return out
