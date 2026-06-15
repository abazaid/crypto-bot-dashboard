import base64
import hashlib
import hmac
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import requests

from app.core.config import settings

TIMEOUT = 15
TRADE_PAGE_LIMIT = 500
MAX_TRADE_HISTORY_PAGES = 20
ACCOUNT_INFO_TTL_SECONDS = 5.0
COST_BASIS_TTL_SECONDS = 1800.0
_QUOTE_ASSETS = ["USDT", "USDC", "BTC", "ETH", "KCS"]
_STABLE_ASSETS = {"USDT", "USDC"}
_ACCOUNT_INFO_CACHE: dict[str, Any] = {"expires_at": 0.0, "data": None}
_SYMBOL_CACHE: dict[str, dict[str, Any]] = {}
_SYMBOL_CACHE_EXPIRES_AT = 0.0
_PRICE_CACHE: dict[str, Any] = {"expires_at": 0.0, "data": {}}
_COST_BASIS_CACHE: dict[str, dict[str, Any]] = {}
_ALL_COINS_CACHE: dict[str, Any] = {"expires_at": 0.0, "key": "", "data": None}


def is_configured() -> bool:
    return bool(settings.kucoin_api_key_1 and settings.kucoin_api_secret_1 and settings.kucoin_api_passphrase_1)


def _ensure_keys() -> None:
    if not is_configured():
        raise RuntimeError(
            "Missing KuCoin API keys: set KUCOIN_API_KEY_1, KUCOIN_API_SECRET_1, and KUCOIN_API_PASSPHRASE_1"
        )


def _api_base() -> str:
    return settings.kucoin_api_base_1 or "https://api.kucoin.com"


def _to_kucoin_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().replace("-", "").strip()
    for quote in sorted(_QUOTE_ASSETS, key=len, reverse=True):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}-{quote}"
    return str(symbol or "").upper().strip()


def _from_kucoin_symbol(symbol: str) -> str:
    return str(symbol or "").upper().replace("-", "")


def _base_asset_from_symbol(symbol: str) -> str:
    return _to_kucoin_symbol(symbol).split("-")[0]


def _quote_asset_from_symbol(symbol: str) -> str:
    parts = _to_kucoin_symbol(symbol).split("-")
    return parts[1] if len(parts) > 1 else "USDT"


def _json_body(body: dict[str, Any] | None) -> str:
    if not body:
        return ""
    import json

    return json.dumps(body, separators=(",", ":"))


def _signed_request(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    _ensure_keys()
    method_up = method.upper()
    query = urlencode(params or {}, doseq=True)
    request_path = f"{path}?{query}" if query else path
    body_text = _json_body(body)
    ts = str(int(time.time() * 1000))
    prehash = f"{ts}{method_up}{request_path}{body_text}"
    sign = base64.b64encode(
        hmac.new(settings.kucoin_api_secret_1.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    passphrase = base64.b64encode(
        hmac.new(
            settings.kucoin_api_secret_1.encode("utf-8"),
            settings.kucoin_api_passphrase_1.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    headers = {
        "KC-API-KEY": settings.kucoin_api_key_1,
        "KC-API-SIGN": sign,
        "KC-API-TIMESTAMP": ts,
        "KC-API-PASSPHRASE": passphrase,
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json",
    }
    resp = requests.request(
        method_up,
        f"{_api_base()}{request_path}",
        headers=headers,
        data=body_text if body_text else None,
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"KuCoin API error {resp.status_code}: {resp.text}")
    payload = resp.json()
    if str(payload.get("code", "")) != "200000":
        raise RuntimeError(f"KuCoin API error {payload.get('code')}: {payload.get('msg') or payload}")
    return payload.get("data")


def _public_get(path: str, params: dict[str, Any] | None = None) -> Any:
    resp = requests.get(f"{_api_base()}{path}", params=params or {}, timeout=TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"KuCoin public API error {resp.status_code}: {resp.text}")
    payload = resp.json()
    if str(payload.get("code", "")) != "200000":
        raise RuntimeError(f"KuCoin public API error {payload.get('code')}: {payload.get('msg') or payload}")
    return payload.get("data")


def invalidate_account_cache() -> None:
    _ACCOUNT_INFO_CACHE["data"] = None
    _ACCOUNT_INFO_CACHE["expires_at"] = 0.0
    _ALL_COINS_CACHE["data"] = None
    _ALL_COINS_CACHE["expires_at"] = 0.0


def get_balances() -> dict[str, dict[str, float]]:
    now = time.time()
    cached = _ACCOUNT_INFO_CACHE.get("data")
    if cached is not None and float(_ACCOUNT_INFO_CACHE.get("expires_at", 0.0)) > now:
        return cached

    rows = _signed_request("GET", "/api/v1/accounts", {"type": "trade"})
    out: dict[str, dict[str, float]] = {}
    for row in rows or []:
        asset = str(row.get("currency", "")).upper()
        if not asset:
            continue
        available = float(row.get("available", 0.0) or 0.0)
        holds = float(row.get("holds", 0.0) or 0.0)
        out[asset] = {
            "free": available,
            "locked": holds,
        }
    _ACCOUNT_INFO_CACHE["data"] = out
    _ACCOUNT_INFO_CACHE["expires_at"] = now + ACCOUNT_INFO_TTL_SECONDS
    return out


def get_asset_free(asset: str) -> float:
    return float(get_balances().get(asset.upper(), {}).get("free", 0.0))


def _load_symbol_filters() -> None:
    global _SYMBOL_CACHE_EXPIRES_AT
    if _SYMBOL_CACHE and _SYMBOL_CACHE_EXPIRES_AT > time.time():
        return
    rows = _public_get("/api/v2/symbols")
    _SYMBOL_CACHE.clear()
    for row in rows or []:
        symbol = str(row.get("symbol", "")).upper()
        if not symbol.endswith("-USDT"):
            continue
        _SYMBOL_CACHE[_from_kucoin_symbol(symbol)] = {
            "symbol": symbol,
            "base": str(row.get("baseCurrency", "")).upper(),
            "quote": str(row.get("quoteCurrency", "")).upper(),
            "base_increment": float(row.get("baseIncrement", 0.0) or 0.0),
            "price_increment": float(row.get("priceIncrement", 0.0) or 0.0),
            "base_min_size": float(row.get("baseMinSize", 0.0) or 0.0),
            "quote_min_size": float(row.get("quoteMinSize", 0.0) or 0.0),
            "enable_trading": bool(row.get("enableTrading", True)),
        }
    _SYMBOL_CACHE_EXPIRES_AT = time.time() + 900


def _get_prices(symbols: list[str] | None = None) -> dict[str, float]:
    now = time.time()
    if _PRICE_CACHE.get("data") and float(_PRICE_CACHE.get("expires_at", 0.0)) > now:
        prices = _PRICE_CACHE["data"]
    else:
        data = _public_get("/api/v1/market/allTickers")
        prices = {}
        for row in (data or {}).get("ticker", []) or []:
            sym = _from_kucoin_symbol(row.get("symbol", ""))
            if sym.endswith("USDT"):
                prices[sym] = float(row.get("last", 0.0) or 0.0)
        _PRICE_CACHE["data"] = prices
        _PRICE_CACHE["expires_at"] = now + 10
    if symbols is None:
        return dict(prices)
    return {s.upper(): float(prices.get(s.upper(), 0.0) or 0.0) for s in symbols}


def _round_step_down(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    return math.floor(float(value) / step) * step


def _precision_from_step(step: float) -> int:
    if step <= 0:
        return 8
    s = f"{step:.16f}".rstrip("0")
    if "." not in s:
        return 0
    return max(0, len(s.split(".")[1]))


def _fmt_with_step(value: float, step: float) -> str:
    precision = _precision_from_step(step)
    if precision <= 0:
        return str(int(math.floor(float(value))))
    return f"{float(value):.{precision}f}"


def get_symbol_lot_filters(symbol: str) -> dict[str, Any]:
    _load_symbol_filters()
    return dict(_SYMBOL_CACHE.get(_from_kucoin_symbol(symbol), {}))


def normalize_qty_for_sell(symbol: str, quantity: float, cap_to_free_balance: bool = True) -> tuple[float, float, float]:
    filters = get_symbol_lot_filters(symbol)
    step = float(filters.get("base_increment", 0.0) or 0.0)
    min_qty = float(filters.get("base_min_size", 0.0) or 0.0)
    qty = float(quantity)
    if cap_to_free_balance:
        qty = min(qty, get_asset_free(_base_asset_from_symbol(symbol)))
    qty = _round_step_down(qty, step)
    return qty, min_qty, step


def _asset_to_usdt(asset: str, amount: float, symbol: str, trade_price: float) -> float:
    if amount <= 0:
        return 0.0
    a = str(asset or "").upper()
    if a == "USDT":
        return float(amount)
    if a == _quote_asset_from_symbol(symbol):
        return float(amount)
    if a == _base_asset_from_symbol(symbol):
        return float(amount) * max(float(trade_price), 0.0)
    price = float(_get_prices([f"{a}USDT"]).get(f"{a}USDT", 0.0) or 0.0)
    return float(amount) * price if price > 0 else 0.0


def get_open_orders(symbol: str | None = None) -> list[dict]:
    params: dict[str, Any] = {"status": "active"}
    if symbol:
        params["symbol"] = _to_kucoin_symbol(symbol)
    rows = _signed_request("GET", "/api/v1/orders", params)
    items = (rows or {}).get("items", []) if isinstance(rows, dict) else []
    out: list[dict] = []
    for o in items:
        sym = _from_kucoin_symbol(o.get("symbol", ""))
        size = float(o.get("size", 0.0) or 0.0)
        deal_size = float(o.get("dealSize", 0.0) or 0.0)
        out.append(
            {
                "symbol": sym,
                "orderId": str(o.get("id", "")),
                "side": str(o.get("side", "")).upper(),
                "type": str(o.get("type", "")).upper(),
                "status": "NEW" if bool(o.get("isActive", True)) else "FILLED",
                "price": float(o.get("price", 0.0) or 0.0),
                "origQty": size,
                "executedQty": deal_size,
            }
        )
    return out


def cancel_order(symbol: str, order_id: str) -> dict:
    out = _signed_request("DELETE", f"/api/v1/orders/{order_id}")
    invalidate_account_cache()
    return out if isinstance(out, dict) else {"cancelledOrderIds": out}


def cancel_open_orders(symbol: str) -> list[dict]:
    canceled: list[dict] = []
    for order in get_open_orders(symbol):
        oid = str(order.get("orderId", ""))
        if not oid:
            continue
        try:
            canceled.append(cancel_order(symbol, oid))
        except Exception:
            continue
    return canceled


def place_limit_sell_qty(symbol: str, quantity: float, price: float) -> dict[str, Any]:
    if quantity <= 0:
        raise RuntimeError("quantity must be > 0")
    if price <= 0:
        raise RuntimeError("price must be > 0")
    filters = get_symbol_lot_filters(symbol)
    qty, min_qty, step = normalize_qty_for_sell(symbol, quantity, cap_to_free_balance=True)
    price_step = float(filters.get("price_increment", 0.0) or 0.0)
    min_funds = float(filters.get("quote_min_size", 0.0) or 0.0)
    px = _round_step_down(float(price), price_step)
    if qty <= 0 or qty < min_qty:
        raise RuntimeError(f"quantity below min lot size for {symbol}: {qty}")
    if px <= 0:
        raise RuntimeError(f"invalid limit price for {symbol}: {px}")
    if min_funds > 0 and (qty * px) < min_funds:
        raise RuntimeError(f"order below min funds for {symbol}: {qty * px}")
    data = _signed_request(
        "POST",
        "/api/v1/orders",
        body={
            "clientOid": str(uuid4()),
            "side": "sell",
            "symbol": _to_kucoin_symbol(symbol),
            "type": "limit",
            "price": _fmt_with_step(px, price_step),
            "size": _fmt_with_step(qty, step),
        },
    )
    invalidate_account_cache()
    return {"order_id": str((data or {}).get("orderId", "")), "orig_qty": qty, "price": px, "status": "NEW"}


def _fills_for_order(symbol: str, order_id: str) -> list[dict]:
    time.sleep(0.5)
    params = {"symbol": _to_kucoin_symbol(symbol), "orderId": str(order_id), "pageSize": TRADE_PAGE_LIMIT}
    data = _signed_request("GET", "/api/v1/fills", params)
    return (data or {}).get("items", []) if isinstance(data, dict) else []


def _order_fill_summary(symbol: str, order_id: str) -> dict[str, Any]:
    fills = _fills_for_order(symbol, order_id)
    executed_qty = 0.0
    quote_qty = 0.0
    fee_usdt = 0.0
    for f in fills:
        size = float(f.get("size", 0.0) or 0.0)
        funds = float(f.get("funds", 0.0) or 0.0)
        price = float(f.get("price", 0.0) or 0.0)
        fee = float(f.get("fee", 0.0) or 0.0)
        fee_currency = str(f.get("feeCurrency", "")).upper()
        executed_qty += size
        quote_qty += funds
        fee_usdt += _asset_to_usdt(fee_currency, fee, symbol, price)
    return {
        "order_id": str(order_id),
        "status": "FILLED" if executed_qty > 0 else "UNKNOWN",
        "executed_qty": executed_qty,
        "quote_qty": quote_qty,
        "avg_price": (quote_qty / executed_qty) if executed_qty > 0 else 0.0,
        "fee_usdt": fee_usdt,
    }


def place_market_sell_qty(symbol: str, quantity: float) -> dict[str, Any]:
    if quantity <= 0:
        raise RuntimeError("quantity must be > 0")
    qty, min_qty, step = normalize_qty_for_sell(symbol, quantity, cap_to_free_balance=True)
    if qty <= 0 or qty < min_qty:
        raise RuntimeError(f"quantity below min lot size for {symbol}: {qty}")
    data = _signed_request(
        "POST",
        "/api/v1/orders",
        body={
            "clientOid": str(uuid4()),
            "side": "sell",
            "symbol": _to_kucoin_symbol(symbol),
            "type": "market",
            "size": _fmt_with_step(qty, step),
        },
    )
    invalidate_account_cache()
    return _order_fill_summary(symbol, str((data or {}).get("orderId", "")))


def get_order_fee_usdt(symbol: str, order_id: str) -> float:
    return float(_order_fill_summary(symbol, str(order_id)).get("fee_usdt", 0.0) or 0.0)


def get_my_trades(symbol: str, page: int = 1, page_size: int = TRADE_PAGE_LIMIT) -> list[dict]:
    data = _signed_request(
        "GET",
        "/api/v1/fills",
        {
            "symbol": _to_kucoin_symbol(symbol),
            "currentPage": max(1, int(page)),
            "pageSize": max(1, min(int(page_size), TRADE_PAGE_LIMIT)),
        },
    )
    return (data or {}).get("items", []) if isinstance(data, dict) else []


def get_my_trades_full_history(symbol: str, max_pages: int = MAX_TRADE_HISTORY_PAGES) -> list[dict]:
    trades: list[dict] = []
    seen_ids: set[str] = set()
    for page in range(1, max(1, int(max_pages)) + 1):
        batch = get_my_trades(symbol, page=page, page_size=TRADE_PAGE_LIMIT)
        if not batch:
            break
        added = 0
        for t in batch:
            tid = str(t.get("tradeId") or t.get("id") or "")
            if tid and tid in seen_ids:
                continue
            if tid:
                seen_ids.add(tid)
            trades.append(t)
            added += 1
        if len(batch) < TRADE_PAGE_LIMIT or added <= 0:
            break
    return sorted(trades, key=lambda t: int(t.get("createdAt", 0) or 0))


def _cost_basis_from_trades(symbol: str, qty_now: float, max_pages: int = MAX_TRADE_HISTORY_PAGES) -> tuple[float, float, int]:
    sym = _from_kucoin_symbol(symbol)
    cached = _COST_BASIS_CACHE.get(sym)
    if cached:
        cached_qty = float(cached.get("qty_now", -1.0))
        if abs(cached_qty - float(qty_now)) < 1e-12 and float(cached.get("expires_at", 0.0)) > time.time():
            return float(cached.get("avg_entry", 0.0)), float(cached.get("invested", 0.0)), int(cached.get("used", 0))

    try:
        trades = get_my_trades_full_history(sym, max_pages=max_pages)
    except Exception:
        return 0.0, 0.0, 0
    if not trades:
        return 0.0, 0.0, 0

    inv_qty = 0.0
    inv_cost = 0.0
    used = 0
    base = _base_asset_from_symbol(sym)
    quote = _quote_asset_from_symbol(sym)
    for t in trades:
        side = str(t.get("side", "")).lower()
        qty = float(t.get("size", 0.0) or 0.0)
        funds = float(t.get("funds", 0.0) or 0.0)
        price = float(t.get("price", 0.0) or 0.0)
        fee = float(t.get("fee", 0.0) or 0.0)
        fee_currency = str(t.get("feeCurrency", "")).upper()
        if qty <= 0:
            continue
        used += 1
        fee_usdt = _asset_to_usdt(fee_currency, fee, sym, price)
        base_fee = fee if fee_currency == base else 0.0
        quote_fee = fee if fee_currency == quote else 0.0
        if side == "buy":
            got_base = max(0.0, qty - base_fee)
            inv_qty += got_base
            inv_cost += funds + quote_fee + fee_usdt
            continue
        if side == "sell":
            sold_base = qty + base_fee
            if inv_qty <= 0:
                continue
            avg_before = inv_cost / max(inv_qty, 1e-12)
            reduce_qty = min(inv_qty, sold_base)
            inv_qty -= reduce_qty
            inv_cost = max(0.0, inv_cost - (avg_before * reduce_qty))

    if inv_qty <= 0:
        avg_entry = 0.0
        invested = 0.0
    else:
        avg_entry = inv_cost / max(inv_qty, 1e-12)
        invested = avg_entry * max(float(qty_now), 0.0)
    _COST_BASIS_CACHE[sym] = {
        "qty_now": float(qty_now),
        "avg_entry": float(max(avg_entry, 0.0)),
        "invested": float(max(invested, 0.0)),
        "used": used,
        "expires_at": time.time() + COST_BASIS_TTL_SECONDS,
    }
    return max(avg_entry, 0.0), max(invested, 0.0), used


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
    for asset, balance in balances.items():
        free = float(balance.get("free", 0.0) or 0.0)
        locked = float(balance.get("locked", 0.0) or 0.0)
        total = free + locked
        if asset in _STABLE_ASSETS:
            continue
        if not include_zero and total <= 0:
            continue
        sym = f"{asset}USDT"
        if sym not in _SYMBOL_CACHE:
            continue
        symbols.append(sym)
        symbol_rows.append({"asset": asset, "symbol": sym, "qty_total": total, "qty_free": free, "qty_locked": locked})

    prices = _get_prices(symbols) if symbols else {}
    now = datetime.now(timezone.utc)
    candidate_rows = []
    for r in symbol_rows:
        price = float(prices.get(r["symbol"], 0.0) or 0.0)
        market_value = price * float(r["qty_total"])
        if (not include_zero) and market_value < min_usdt_value:
            continue
        candidate_rows.append((r, price, market_value))

    cost_basis: dict[str, tuple[float, float, int]] = {}
    if candidate_rows:
        with ThreadPoolExecutor(max_workers=min(len(candidate_rows), 8)) as ex:
            fut_to_sym = {
                ex.submit(_cost_basis_from_trades, r["symbol"], float(r["qty_total"]), MAX_TRADE_HISTORY_PAGES): r["symbol"]
                for r, _, _ in candidate_rows
            }
            for fut in as_completed(fut_to_sym):
                sym = fut_to_sym[fut]
                try:
                    cost_basis[sym] = fut.result()
                except Exception:
                    cost_basis[sym] = (0.0, 0.0, 0)

    rows: list[dict[str, Any]] = []
    for r, price, market_value in candidate_rows:
        avg_entry, invested, trades_used = cost_basis.get(r["symbol"], (0.0, 0.0, 0))
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
