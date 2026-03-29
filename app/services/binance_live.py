import hashlib
import hmac
import math
import time
from typing import Any
from urllib.parse import urlencode

import requests

from app.core.config import settings
from app.services.binance_public import get_exchange_info

BASE_URL = "https://api.binance.com"
TIMEOUT = 15
_SYMBOL_FILTER_CACHE: dict[str, dict[str, float]] = {}
_CACHE_EXPIRES_AT = 0.0


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


def _order_summary(raw: dict) -> dict[str, float]:
    executed_qty = float(raw.get("executedQty", 0.0))
    quote_qty = float(raw.get("cummulativeQuoteQty", 0.0))
    avg_price = quote_qty / executed_qty if executed_qty > 0 else 0.0
    return {
        "executed_qty": executed_qty,
        "quote_qty": quote_qty,
        "avg_price": avg_price,
    }


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


def place_market_sell_qty(symbol: str, quantity: float) -> dict[str, float]:
    if quantity <= 0:
        raise RuntimeError("quantity must be > 0")
    _load_symbol_filters()
    filters = _SYMBOL_FILTER_CACHE.get(symbol.upper(), {})
    step = float(filters.get("step_size", 0.0))
    min_qty = float(filters.get("min_qty", 0.0))
    qty = _round_step_down(float(quantity), step)
    if qty <= 0 or qty < min_qty:
        raise RuntimeError(f"quantity below min lot size for {symbol}: {qty}")
    raw = _signed_request(
        "POST",
        "/api/v3/order",
        {
            "symbol": symbol.upper(),
            "side": "SELL",
            "type": "MARKET",
            "quantity": f"{qty:.8f}",
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
    step = float(filters.get("step_size", 0.0))
    min_qty = float(filters.get("min_qty", 0.0))
    min_notional = float(filters.get("min_notional", 0.0))
    tick = float(filters.get("tick_size", 0.0))
    qty = _round_step_down(float(quantity), step)
    px = _round_step_down(float(price), tick)
    if qty <= 0 or qty < min_qty:
        raise RuntimeError(f"quantity below min lot size for {symbol}: {qty}")
    if px <= 0:
        raise RuntimeError(f"invalid limit price for {symbol}: {px}")
    if min_notional > 0 and (qty * px) < min_notional:
        raise RuntimeError(f"order below min notional for {symbol}: {qty * px}")
    raw = _signed_request(
        "POST",
        "/api/v3/order",
        {
            "symbol": symbol.upper(),
            "side": "SELL",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": f"{qty:.8f}",
            "price": f"{px:.8f}",
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
